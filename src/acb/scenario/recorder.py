"""Every transaction of a run, attributed to a step, with its saved receipt (spec 10.4).

The recorder does not trust any one service to report its transactions: after each step it scans the
chain blocks mined since the previous scan and records every transaction sent from a key of the run
(roles, simulated agents, attack identities). Each record is decoded with our ABIs (function, events,
revert error) and enriched with what the services logged about it (the signer's decision outcome and
reason codes, violations). A step may claim a transaction that was mined while another step ran (for
example the agent's timer-driven escrow refund); otherwise a transaction belongs to the step during which
it was mined.

For every recorded transaction the raw ``eth_getTransactionReceipt`` and ``eth_getTransactionByHash``
answers and the block timestamp are saved to ``<out>/receipts/<txHash>.json`` exactly as the RPC returned
them, so ``scripts/verify_evidence.py --receipts-only`` can check the run offline.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from web3 import Web3

from acb.chain import abi as abi_mod
from acb.logbus import format_usdc

EXPECTED_EVENTS: dict[str, tuple[str, ...]] = {
    "pay": ("Paid",),
    "escrow": ("EscrowOpened",),
    "release": ("EscrowReleased",),
    "refund": ("EscrowRefunded",),
    "claimDelivered": ("DeliveryClaimed",),
    "recordDelivery": ("DeliveryRecorded",),
    "recordDecision": ("DecisionRecorded",),
    "recordViolation": ("ViolationRecorded",),
    "ownerApprove": ("OwnerApproved",),
    "registerMandate": ("MandateRegistered",),
    "revokeMandate": ("MandateRevoked",),
    "setTier": ("TierChanged",),
    "freeze": ("TierChanged",),
    "unfreeze": ("TierChanged",),
    "postGrade": ("GradePosted",),
    "giveFeedback": ("NewFeedback",),
    "revokeFeedback": ("FeedbackRevoked",),
    "register": ("Registered",),
    "registerVault": ("VaultRegistered",),
    "setPoster": ("PosterSet",),
    "fileAppeal": ("AppealFiled",),
    "resolveAppeal": ("AppealResolved",),
    "mint": ("Transfer",),
    "transfer": ("Transfer",),
    "setAgentWallet": ("MetadataSet",),
    "transferFrom": ("Transfer",),
    "setMaxAutoTier": ("ConfigChanged",),
}
"""Primary events a successful call of each function must emit (verified by ``verify_evidence.py``)."""

ROLE_LABELS = {
    "deployer": "deployer",
    "owner": "owner",
    "admin": "acb-admin",
    "agentSigner": "agent-signer",
    "agentSignerNext": "agent-signer-next",
    "bureau": "bureau",
    "autonomy": "autonomy-operator",
}
AGENT_KEY_LABELS = frozenset({"agent-signer", "agent-signer-next"})
TIER_FROZEN = 255


def iso(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts)))


def _hex(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.lower()
    return Web3.to_hex(value).lower()


RPC_RETRIES = 4
_last_call = [0.0]


def _rpc(w3: Any, method: str, params: list, *, min_interval: float = 0.0) -> Any:
    """One raw JSON-RPC call (results exactly as the node returns them), paced on public networks and
    retried with backoff (0.5, 1, 2, 4 s) on transport errors and rate limits."""
    delay = 0.5
    for attempt in range(RPC_RETRIES + 1):
        wait = _last_call[0] + min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()
        try:
            response = w3.provider.make_request(method, params)
        except Exception as exc:  # noqa: BLE001 - transport errors are retried
            if attempt == RPC_RETRIES:
                raise RuntimeError(f"{method}: {type(exc).__name__}: {exc}") from None
            time.sleep(delay)
            delay *= 2
            continue
        if "error" in response:
            if attempt < RPC_RETRIES and str(response["error"]).lower().find("rate") >= 0:
                time.sleep(delay)
                delay *= 2
                continue
            raise RuntimeError(f"{method}: {response['error']}")
        return response.get("result")
    raise RuntimeError(f"{method}: no answer")  # pragma: no cover


def function_table() -> dict[str, str]:
    """4-byte selector -> function name over every ABI in ``abi/``."""
    table: dict[str, str] = {}
    for name in abi_mod.artefact_names():
        for entry in abi_mod.abi_of(name):
            if entry.get("type") == "function":
                table[abi_mod.selector(entry).lower()] = entry["name"]
    return table


@dataclass
class StepWindow:
    step: str
    start_block: int
    end_block: int | None = None


@dataclass
class Recorder:
    """See the module docstring. ``scan()`` after every step; ``records`` in mining order."""

    w3: Any
    dep: Any
    out_dir: Path
    chain_id: int
    explorers: tuple[dict, ...] = ()
    network: str = "anvil"
    records: list[dict[str, Any]] = field(default_factory=list)
    by_hash: dict[str, dict[str, Any]] = field(default_factory=dict)
    scanned_to: int = 0
    actors: dict[str, str] = field(default_factory=dict)
    contracts: dict[str, str] = field(default_factory=dict)
    expect_revert: set[str] = field(default_factory=set)
    revert_errors: dict[str, str] = field(default_factory=dict)
    notes: dict[str, list[str]] = field(default_factory=dict)
    wallet_tier: int = 1
    min_interval: float = 0.0
    """Seconds between RPC calls (0 on Anvil; the runner sets 0.125 on a public RPC)."""
    _functions: dict[str, str] = field(default_factory=dict)
    _signer_info: dict[str, dict[str, Any]] = field(default_factory=dict)
    _signer_offset: int = 0

    # ------------------------------------------------------------------ setup

    def __post_init__(self) -> None:
        self.out_dir = Path(self.out_dir)
        self._functions = function_table()

    def bind_deployment(self, dep: Any, keys: Any = None, extra: Mapping[str, str] | None = None) -> None:
        """Learn the run's addresses: roles, agents, vault owners, contracts (call after the deploy)."""
        self.dep = dep
        if keys is not None:
            roles = keys.roles() if callable(keys.roles) else keys.roles
            for role in roles:
                self.actors.setdefault(keys.address(role).lower(), role.replace("_", "-"))
        for role, address in dep.roles.items():
            self.actors[address.lower()] = ROLE_LABELS.get(role, role)
        for key, entry in dep.agents.items():
            if key == "wallet":
                continue
            if entry.identity_owner:
                self.actors[entry.identity_owner.lower()] = key
            if entry.wallet:
                self.actors[entry.wallet.lower()] = key
        for name, vault in dep.vaults.items():
            if name != "wallet":
                self.actors.setdefault(vault.owner.lower(), f"{name}-vault-owner")
            self.contracts[vault.address.lower()] = f"TierVault({name})"
        for name, entry in dep.contracts.items():
            self.contracts[entry.address.lower()] = name
        for address, label in (extra or {}).items():
            self.actors.setdefault(address.lower(), label)
        if "wallet" in dep.vaults:
            from acb.chain.contracts import VaultContract

            try:
                self.wallet_tier = int(VaultContract(self.w3, dep.vaults["wallet"].address).tier())
            except Exception:  # noqa: BLE001 - informational
                self.wallet_tier = 1

    def add_actor(self, address: str, label: str) -> None:
        self.actors.setdefault(address.lower(), label)

    def mark_expected_revert(self, tx_hash: str, revert_error: str | None = None) -> None:
        self.expect_revert.add(tx_hash.lower())
        if revert_error:
            self.revert_errors[tx_hash.lower()] = revert_error

    def note(self, tx_hash: str, text: str) -> None:
        self.notes.setdefault(tx_hash.lower(), []).append(text)
        rec = self.by_hash.get(tx_hash.lower())
        if rec is not None:
            rec["notes"] = "; ".join(self.notes[tx_hash.lower()])

    # ------------------------------------------------------------------ service logs

    def _read_signer_log(self) -> None:
        """Map transaction hashes to the signer's request records (kind, outcome, reasons, violation)."""
        path = self.out_dir / "signer.jsonl"
        if not path.is_file():
            return
        with open(path, "rb") as fh:
            fh.seek(self._signer_offset)
            chunk = fh.read()
        if not chunk:
            return
        complete = chunk.rfind(b"\n") + 1
        self._signer_offset += complete
        for line in chunk[:complete].decode("utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            data = event.get("data") or {}
            request = data.get("request") or {}
            for tx in request.get("tx") or []:
                h = str(tx.get("hash") or "").lower()
                if not h:
                    continue
                self._signer_info[h] = {
                    "kind": request.get("kind") or data.get("kind"),
                    "outcome": data.get("outcome"),
                    "status": data.get("status"),
                    "reasons": [int(c) for c in event.get("reason_codes") or []],
                    "violation": request.get("violation"),
                    "orderId": data.get("orderId"),
                    "requestId": request.get("requestId"),
                    "payee": data.get("payee"),
                    "amount": data.get("amount"),
                }

    def signer_info(self, tx_hash: str) -> dict[str, Any] | None:
        self._read_signer_log()
        return self._signer_info.get(tx_hash.lower())

    # ------------------------------------------------------------------ scanning

    def latest_block(self) -> int:
        return int(self.w3.eth.block_number)

    def scan(self, step: str, to_block: int | None = None) -> list[dict[str, Any]]:
        """Record every transaction of the run mined in ``(scanned_to, to_block]`` under ``step``."""
        to_block = self.latest_block() if to_block is None else int(to_block)
        self._read_signer_log()
        new: list[dict[str, Any]] = []
        for number in range(self.scanned_to + 1, to_block + 1):
            block = _rpc(self.w3, "eth_getBlockByNumber", [hex(number), True], min_interval=self.min_interval)
            if not block:
                continue
            ts = int(block["timestamp"], 16)
            for tx in block.get("transactions") or []:
                sender = str(tx.get("from") or "").lower()
                if self.network != "anvil" and sender not in self.actors:
                    continue
                new.append(self._record(tx, step, ts))
            self.scanned_to = number
        return new

    def _record(self, tx: Mapping[str, Any], step: str, block_ts: int) -> dict[str, Any]:
        h = str(tx["hash"]).lower()
        receipt = _rpc(self.w3, "eth_getTransactionReceipt", [h], min_interval=self.min_interval)
        status = int(receipt["status"], 16)
        to = _hex(tx.get("to"))
        created = _hex(receipt.get("contractAddress"))
        data = str(tx.get("input") or "0x")
        if to is None:
            function = "deploy"
            target = created
        elif data in ("0x", ""):
            function = "ethTransfer"
            target = to
        else:
            function = self._functions.get(data[:10].lower(), data[:10].lower())
            target = to
        sender = str(tx["from"]).lower()
        actor = self.actors.get(sender, "other")
        logs = [abi_mod.decode_log(_formatted_log(lg)) for lg in receipt.get("logs") or []]
        decoded = [lg for lg in logs if lg is not None]
        events = [lg["event"] for lg in decoded]
        contract = self.contracts.get(target or "", "contract" if function == "deploy" else "unknown")
        if function == "deploy" and created:
            contract = self._created_label(created, decoded)
        tier_before = self.wallet_tier
        wallet_vault = self.dep.vaults["wallet"].address.lower() if self.dep and "wallet" in self.dep.vaults else ""
        for lg in decoded:
            if lg["event"] == "TierChanged" and lg["address"] == wallet_vault:
                self.wallet_tier = int(lg["args"]["toTier"])
        expected_status = 0 if h in self.expect_revert else 1
        revert = self.revert_errors.get(h)
        if status == 0 and revert is None:
            revert = self._replay_revert(tx, int(receipt["blockNumber"], 16))
        signer = self._signer_info.get(h)
        decision, reasons = self._decision(function, decoded, signer, status)
        # an expectation, not an observation: a successful call must emit its primary events, so a missing
        # one fails the step (Runner.check) and verify_evidence.py
        expected_events = list(EXPECTED_EVENTS.get(function, ())) if status == 1 else []
        if function == "executeApproved" and status == 1:
            # the approved card is either a payment or an escrow: whichever it emitted, else Paid
            expected_events = [e for e in ("Paid", "EscrowOpened") if e in events][:1] or ["Paid"]
        if function == "deploy" and status == 1 and "VaultInitialized" in events:
            expected_events = ["VaultInitialized"]
        record = {
            "seq": len(self.records) + 1,
            "step": step,
            "timestampUtc": iso(block_ts),
            "blockTimestamp": block_ts,
            "actor": actor,
            "actorAddress": sender,
            "signedByAgentKey": actor in AGENT_KEY_LABELS,
            "action": "",
            "contract": contract,
            "to": target,
            "function": function,
            "decision": decision,
            "reasonCodes": reasons,
            "tierBefore": tier_before,
            "tierAfter": self.wallet_tier,
            "chainId": self.chain_id,
            "block": int(receipt["blockNumber"], 16),
            "txHash": h,
            "status": status,
            "expectedStatus": expected_status,
            "expectedEvents": expected_events,
            "gasUsed": int(receipt["gasUsed"], 16),
            "explorer": self._explorer(h),
            "receiptFile": f"receipts/{h}.json",
            "logLine": "",
            "headline": False,
            "artefacts": [],
            "notes": "; ".join(self.notes.get(h, [])),
            # internal fields (dropped from evidence.json)
            "_events": decoded,
            "_revert": revert,
            "_signer": signer,
            "_nonce": int(tx.get("nonce", "0x0"), 16)
            if isinstance(tx.get("nonce"), str)
            else int(tx.get("nonce") or 0),
        }
        record["action"] = self._action(record)
        record["logLine"] = self.log_line(record)
        self._save_receipt(h, receipt, tx, block_ts)
        self.records.append(record)
        self.by_hash[h] = record
        return record

    def _created_label(self, created: str, decoded: list[dict[str, Any]]) -> str:
        label = self.contracts.get(created)
        if label:
            return label
        if any(lg["event"] == "VaultInitialized" for lg in decoded):
            return "TierVault"
        return "contract"

    def _replay_revert(self, tx: Mapping[str, Any], block: int) -> str | None:
        """Decode the revert of a mined failure by replaying the call one block earlier (spec 7.4)."""
        params = {"from": Web3.to_checksum_address(tx["from"]), "data": tx.get("input") or "0x"}
        if tx.get("to"):
            params["to"] = Web3.to_checksum_address(tx["to"])
        value = tx.get("value")
        if value:
            params["value"] = int(value, 16) if isinstance(value, str) else int(value)
        try:
            self.w3.eth.call(params, max(0, block - 1))
        except Exception as exc:  # noqa: BLE001 - every revert form is decoded below
            return abi_mod.decode_exception(exc).text
        return None

    def _explorer(self, h: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for entry in self.explorers or ():
            name = str(entry.get("name") or "")
            tpl = entry.get("tx")
            if name and tpl:
                out[name] = str(tpl).format(hash=h)
        return out

    @staticmethod
    def _decision(
        function: str, decoded: list[dict[str, Any]], signer: dict[str, Any] | None, status: int
    ) -> tuple[str | None, list[int]]:
        from acb.types import Decision, Outcome

        if status == 0:
            return None, []
        by_event = {lg["event"]: lg for lg in decoded}
        if signer is not None and function in ("pay", "escrow", "executeApproved", "recordDecision"):
            return signer.get("outcome"), list(signer.get("reasons") or [])
        if "Paid" in by_event:
            a = by_event["Paid"]["args"]
            return Decision(int(a["decision"])).name, [int(a["reasonCode"])]
        if "EscrowOpened" in by_event:
            return "ESCROW", [int(by_event["EscrowOpened"]["args"]["reasonCode"])]
        if "DecisionRecorded" in by_event:
            a = by_event["DecisionRecorded"]["args"]
            return Decision(int(a["decision"])).name, [int(a["reasonCode"])]
        if "EscrowRefunded" in by_event:
            return "REFUND", [int(by_event["EscrowRefunded"]["args"]["reasonCode"])]
        if "EscrowReleased" in by_event:
            return "RELEASE", [2008]
        if "DeliveryRecorded" in by_event:
            return f"DELIVERY_{Outcome(int(by_event['DeliveryRecorded']['args']['outcome'])).name}", []
        if "ViolationRecorded" in by_event:
            from acb.types import ViolationKind

            a = by_event["ViolationRecorded"]["args"]
            reasons = [int(lg["args"]["reasonCode"]) for lg in decoded if lg["event"] == "TierChanged"]
            return f"VIOLATION_{ViolationKind(int(a['kind'])).name}", reasons
        if "TierChanged" in by_event:
            a = by_event["TierChanged"]["args"]
            return f"TIER_{int(a['fromTier'])}_TO_{int(a['toTier'])}", [int(a["reasonCode"])]
        if "GradePosted" in by_event:
            from acb.types import Grade

            a = by_event["GradePosted"]["args"]
            return f"GRADE_{Grade(int(a['grade'])).name}", [int(r) for r in a["reasons"] if int(r)]
        if "AppealResolved" in by_event:
            a = by_event["AppealResolved"]["args"]
            return ("APPEAL_UPHELD", [1102]) if a["upheld"] else ("APPEAL_REJECTED", [1103])
        if "OwnerApproved" in by_event:
            return "OWNER_APPROVED", []
        if "NewFeedback" in by_event:
            a = by_event["NewFeedback"]["args"]
            tag2 = str(a.get("tag2") or "")
            tag1 = str(a.get("tag1") or "")
            return (f"REPORT_{tag2}" if tag1 == "acb-report" else f"FEEDBACK_{tag1}"), []
        return None, []

    def name_of(self, agent_id: Any) -> str:
        if self.dep is None:
            return f"#{agent_id}"
        for key, entry in self.dep.agents.items():
            if str(entry.agent_id) == str(agent_id):
                return entry.name if key != "wallet" else "the wallet"
        return f"agent #{agent_id}"

    def _action(self, r: dict[str, Any]) -> str:
        fn = r["function"]
        ev = {lg["event"]: lg["args"] for lg in r["_events"]}

        def amt(args: Mapping[str, Any], key: str = "amount") -> str:
            try:
                return f"{format_usdc(int(args[key]))} USDC"
            except (KeyError, TypeError, ValueError):
                return ""

        if r["status"] == 0:
            return f"{fn} reverted ({r['_revert'] or 'revert'})"
        if fn == "pay" and "Paid" in ev:
            return f"pay {self.name_of(ev['Paid']['payeeAgentId'])} {amt(ev['Paid'])}"
        if fn == "executeApproved":
            a = ev.get("Paid") or ev.get("EscrowOpened") or {}
            return f"execute owner-approved payment to {self.name_of(a.get('payeeAgentId'))} {amt(a)}".strip()
        if fn == "escrow" and "EscrowOpened" in ev:
            return f"escrow {amt(ev['EscrowOpened'])} for {self.name_of(ev['EscrowOpened']['payeeAgentId'])}"
        if fn == "claimDelivered" and "DeliveryClaimed" in ev:
            a = ev["DeliveryClaimed"]
            return f"{self.name_of(a['payeeAgentId'])} claims delivery{' (late)' if a.get('late') else ''}"
        if fn == "release" and "EscrowReleased" in ev:
            return f"release escrow to {self.name_of(ev['EscrowReleased']['payeeAgentId'])} {amt(ev['EscrowReleased'])}"
        if fn == "refund" and "EscrowRefunded" in ev:
            a = ev["EscrowRefunded"]
            return f"refund escrow of {self.name_of(a['payeeAgentId'])} {amt(a)} ({a['reasonCode']})"
        if fn == "recordDelivery" and "DeliveryRecorded" in ev:
            from acb.types import Outcome

            a = ev["DeliveryRecorded"]
            return f"record delivery {Outcome(int(a['outcome'])).name} for {self.name_of(a['payeeAgentId'])}"
        if fn == "recordDecision" and "DecisionRecorded" in ev:
            from acb.types import Decision

            a = ev["DecisionRecorded"]
            verb = "refuse" if int(a["decision"]) == int(Decision.REFUSE) else "ask the owner about"
            return f"{verb} {self.name_of(a['payeeAgentId'])} invoice {amt(a)}"
        if fn == "recordViolation" and "ViolationRecorded" in ev:
            from acb.types import ViolationKind

            a = ev["ViolationRecorded"]
            return f"record violation {ViolationKind(int(a['kind'])).name} severity {a['severity']}"
        if fn in ("setTier", "freeze", "unfreeze") and "TierChanged" in ev:
            a = ev["TierChanged"]
            return f"{fn}: tier {_tier(a['fromTier'])} -> {_tier(a['toTier'])} ({a['reasonCode']})"
        if fn == "postGrade" and "GradePosted" in ev:
            from acb.types import Grade

            a = ev["GradePosted"]
            return f"post grade {Grade(int(a['grade'])).name} for {self.name_of(a['agentId'])} (score {a['score']})"
        if fn == "giveFeedback" and "NewFeedback" in ev:
            a = ev["NewFeedback"]
            what = a.get("tag2") if a.get("tag1") == "acb-report" else a.get("tag1")
            return f"feedback {what} on {self.name_of(a['agentId'])}"
        if fn == "revokeFeedback":
            return "revoke previous grade feedback"
        if fn == "registerMandate":
            return "register mandate"
        if fn == "ownerApprove" and "OwnerApproved" in ev:
            a = ev["OwnerApproved"]
            return f"owner approves {self.name_of(a['payeeAgentId'])} {amt(a)}"
        if fn == "register" and "Registered" in ev:
            return f"register identity #{ev['Registered']['agentId']} ({r['actor']})"
        if fn == "registerVault":
            return "register vault"
        if fn == "fileAppeal":
            return "file appeal"
        if fn == "resolveAppeal" and "AppealResolved" in ev:
            return "appeal " + ("upheld" if ev["AppealResolved"]["upheld"] else "rejected")
        if fn == "deploy":
            return f"deploy {r['contract']}"
        if fn in ("mint", "transfer") and "Transfer" in ev:
            a = ev["Transfer"]
            to = self.contracts.get(str(a.get("to", "")).lower(), str(a.get("to", ""))[:10])
            return f"USDC {amt(a, 'value')} to {to}"
        if fn == "ethTransfer":
            return "ETH top-up"
        return fn

    def log_line(self, r: dict[str, Any]) -> str:
        """The human log line of spec 10.3 for one transaction."""
        parts = [r["timestampUtc"], r["step"], str(r["decision"] or r["function"])]
        parts.append(r["action"])
        if r["tierAfter"] is not None:
            parts.append(f"tier={r['tierAfter']}")
        if r["reasonCodes"]:
            parts.append("reasons=" + ",".join(str(c) for c in r["reasonCodes"]))
        parts.append(f"status={r['status']}")
        parts.append(f"tx={r['txHash'][:10]}...")
        return " ".join(p for p in parts if p)

    def _save_receipt(self, h: str, receipt: Any, tx: Any, block_ts: int) -> None:
        folder = self.out_dir / "receipts"
        folder.mkdir(parents=True, exist_ok=True)
        body = {"txHash": h, "receipt": receipt, "transaction": tx, "blockTimestamp": block_ts}
        text = json.dumps(body, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
        (folder / f"{h}.json").write_bytes(text.encode("utf-8"))

    # ------------------------------------------------------------------ queries and claims

    def claim(self, tx_hash: str, step: str) -> None:
        """Assign a recorded transaction to ``step`` (semantic ownership beats the mining window)."""
        rec = self.by_hash.get(tx_hash.lower())
        if rec is not None and rec["step"] != step:
            self.note(tx_hash, f"mined during {rec['step']}, belongs to {step}")
            rec["step"] = step
            rec["logLine"] = self.log_line(rec)

    def of_step(self, step: str) -> list[dict[str, Any]]:
        return [r for r in self.records if r["step"] == step]

    def find(self, step: str | None = None, function: str | None = None, **event_filters: Any) -> list[dict[str, Any]]:
        out = []
        for r in self.records:
            if step is not None and r["step"] != step:
                continue
            if function is not None and r["function"] != function:
                continue
            if event_filters and not _match_events(r, event_filters):
                continue
            out.append(r)
        return out

    def renumber(self) -> None:
        """Sort by (block, tx position as mined) and renumber ``seq`` (claims keep the mining order)."""
        for i, r in enumerate(self.records, start=1):
            r["seq"] = i

    def public(self, rec: Mapping[str, Any]) -> dict[str, Any]:
        """A record without its internal fields (the evidence.json form)."""
        return {k: v for k, v in rec.items() if not k.startswith("_")}

    def events_of(self, rec: Mapping[str, Any], name: str) -> list[dict[str, Any]]:
        return [lg["args"] for lg in rec["_events"] if lg["event"] == name]

    def iter_events(self, step: str | None = None, name: str | None = None) -> Iterable[tuple[dict, dict]]:
        for r in self.records:
            if step is not None and r["step"] != step:
                continue
            for lg in r["_events"]:
                if name is None or lg["event"] == name:
                    yield r, lg


def _formatted_log(raw: Mapping[str, Any]) -> dict[str, Any]:
    """A raw JSON-RPC log (hex strings) in the form web3's event decoder expects."""
    from hexbytes import HexBytes

    def num(value: Any) -> int:
        if isinstance(value, str):
            return int(value, 16) if value.startswith("0x") else int(value)
        return int(value or 0)

    return {
        "address": Web3.to_checksum_address(raw["address"]),
        "topics": [HexBytes(t) for t in raw.get("topics") or []],
        "data": HexBytes(raw.get("data") or "0x"),
        "logIndex": num(raw.get("logIndex")),
        "transactionIndex": num(raw.get("transactionIndex")),
        "transactionHash": HexBytes(raw.get("transactionHash") or b""),
        "blockHash": HexBytes(raw.get("blockHash") or b""),
        "blockNumber": num(raw.get("blockNumber")),
    }


def _tier(value: Any) -> str:
    return "FROZEN" if int(value) == TIER_FROZEN else str(value)


def _match_events(rec: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    for key, want in filters.items():
        event, _, arg = key.partition("__")
        found = False
        for lg in rec["_events"]:
            if lg["event"] != event:
                continue
            if not arg or str(lg["args"].get(arg)).lower() == str(want).lower():
                found = True
                break
        if not found:
            return False
    return True


__all__ = ["EXPECTED_EVENTS", "Recorder", "function_table", "iso"]
