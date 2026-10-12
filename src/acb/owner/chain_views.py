"""Chain reads of the owner console and the verification behind every confirm screen (spec 11.1, TB7).

Nothing here trusts the wallet agent (P2). A confirm screen is rebuilt from:

- **mandates**: only the agentIds, the numbers and the purpose of the agent's draft. For each payee the
  console reads the live ``getAgentWallet``, the effective grade, the score and the registration age
  from the chain and takes the display name from the pinned directory (``deployments/<network>.json``),
  never from the agent. Lookalike warnings come from the same directory. :func:`prepare_mandate`
  returns exactly the values ``registerMandate`` will be sent with, a random-salt ``mandateId``
  (spec 3.2) and a short code over those values (:func:`signing_code`);
- **approvals**: the signed invoice of the order (fetched from the agent, or a file). The console
  verifies the EIP-191 signature itself against the issuer's live agent wallet, recomputes ``orderId``
  and the amount, checks the buyer fields, chain, token and expiry, that the order does not exist yet,
  and that our vault recorded ``DecisionRecorded(NEEDS_OWNER_APPROVAL)`` for exactly that order,
  invoice hash, payee and amount. ``ownerApprove`` is built only from those verified values.

A value the owner sees is the value that is signed: the confirm step re-reads the chain
(:func:`recheck_mandate`, :func:`recheck_approval`) and refuses when anything changed since the review.

The panels (spec 11.2) read the vault, mandates, grades and the autonomy timeline from the chain
(:class:`PanelReader`), with incremental log scans so a public RPC is asked only for new blocks.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from web3 import Web3

from acb import reason_codes
from acb.canonical import chash, invoice_hash, order_id, recover_invoice_signer
from acb.types import TIER_FROZEN, Decision, Grade, OrderState, SignedInvoice

APPROVAL_TTL_S = 900
"""Validity of an owner approval (chain seconds)."""
ESCROW_MIN_S = 300
"""Minimum time an owner-approved escrow stays open for the provider's claim."""
MAX_PAYEES = 6
MAX_VALID_HOURS = 168
MAX_PURPOSE_CHARS = 200
ZERO_ADDRESS = "0x" + "00" * 20
ZERO_HASH = "0x" + "00" * 32
HEX32_CHARS = frozenset("0123456789abcdef")


class VerificationError(ValueError):
    """A confirm screen cannot be built or confirmed (the message is safe to show to the owner)."""


def is_hex32(value: Any) -> bool:
    """``0x`` + 64 lowercase or uppercase hex characters."""
    return (
        isinstance(value, str)
        and len(value) == 66
        and value[:2] in ("0x", "0X")
        and all(c in HEX32_CHARS for c in value[2:].lower())
    )


def is_address(value: Any) -> bool:
    """``0x`` + 40 hex characters."""
    return (
        isinstance(value, str)
        and len(value) == 42
        and value[:2] in ("0x", "0X")
        and all(c in HEX32_CHARS for c in value[2:].lower())
    )


def usdc_text(units: int) -> str:
    """Base units as a two-decimal USDC string (exact; never a float)."""
    from acb.logbus import format_usdc

    return format_usdc(int(units))


def reason_entries(codes: Iterable[int]) -> list[dict[str, Any]]:
    """``[{"code", "text"}]`` for display; unknown codes get an explicit "unknown" text."""
    out = []
    for code in codes:
        c = int(code)
        if c == 0:
            continue
        try:
            text = reason_codes.text(c)
        except KeyError:
            text = "unknown reason code"
        out.append({"code": c, "text": text})
    return out


def reason_catalogue() -> dict[str, str]:
    """``{code: text}`` of every catalogue code (P1's own data; the UI renders texts from it)."""
    out = {str(code): info.text for code, info in reason_codes.CATALOGUE.items()}
    out.update({str(code): info.text for code, info in reason_codes.RETIRED.items()})
    return out


# ---------------------------------------------------------------------------
# the chain as the owner console sees it
# ---------------------------------------------------------------------------


class _LogCache:
    """Logs of one event of one contract, scanned incrementally from the deployment's start block."""

    def __init__(self, contract: Any, event: str, start_block: int, **filters: Any):
        self.contract = contract
        self.event = event
        self.filters = filters
        self.next_block = int(start_block)
        self.logs: list[Any] = []
        self.lock = threading.Lock()

    def refresh(self, latest: int) -> list[Any]:
        with self.lock:
            if latest >= self.next_block:
                found = self.contract.logs(self.event, self.next_block, latest, **self.filters)
                self.logs.extend(found)
                self.next_block = latest + 1
            return list(self.logs)


@dataclass
class OwnerChain:
    """The network profile, Web3, deployments, bound contracts and pinned directory of one console."""

    net: Any
    w3: Any
    dep: Any
    c: Any
    directory: Any
    vault_name: str = "wallet"
    _registered: dict[int, int | None] = field(default_factory=dict)
    _block_ts: dict[int, int] = field(default_factory=dict)
    _caches: dict[str, _LogCache] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def load(cls, network: str | None = None, *, dep: Any = None, w3: Any = None) -> OwnerChain:
        """Build from ``config/networks.yaml`` and ``deployments/<network>.json`` (or the given objects)."""
        from acb import config
        from acb.agent.directory import Directory
        from acb.chain.client import make_web3
        from acb.chain.contracts import bind
        from acb.chain.deployments import load_deployments
        from acb.types import NetworkProfile

        name = network or (dep.network if dep is not None else config.network_name())
        net = NetworkProfile.load(name)
        w3 = w3 if w3 is not None else make_web3(net)
        dep = dep if dep is not None else load_deployments(name)
        if "wallet" not in dep.vaults:
            raise VerificationError("the deployment has no wallet vault")
        return cls(net=net, w3=w3, dep=dep, c=bind(w3, dep), directory=Directory.from_deployments(dep))

    # ---- basics ----------------------------------------------------------------------------------

    @property
    def vault(self) -> Any:
        return self.c.vault(self.vault_name)

    @property
    def wallet_agent_id(self) -> int:
        return int(self.dep.vaults[self.vault_name].agent_id)

    @property
    def usdc(self) -> str:
        return self.dep.contracts["USDC"].address.lower()

    def now(self) -> int:
        from acb.chain import clock

        return clock.now(self.w3)

    def latest_block(self) -> int:
        return int(self.w3.eth.block_number)

    def explorer_tx(self, tx_hash: str | None) -> str | None:
        if not tx_hash:
            return None
        return self.net.explorer_tx_url(tx_hash)

    def explorer_template(self) -> str | None:
        for explorer in self.net.explorers:
            if explorer.get("tx"):
                return str(explorer["tx"])
        return None

    def block_time(self, number: int) -> int | None:
        number = int(number)
        if number in self._block_ts:
            return self._block_ts[number]
        try:
            ts = int(self.w3.eth.get_block(number)["timestamp"])
        except Exception:  # noqa: BLE001 - shown as unknown
            return None
        self._block_ts[number] = ts
        return ts

    def cache(self, key: str, contract: Any, event: str, **filters: Any) -> _LogCache:
        with self._lock:
            if key not in self._caches:
                self._caches[key] = _LogCache(contract, event, int(self.dep.start_block), **filters)
            return self._caches[key]

    # ---- per-agent reads -------------------------------------------------------------------------

    def registered_at(self, agent_id: int) -> int | None:
        """Block time of the agent's ERC-8004 registration (``registerTx`` of the deployment, else the
        identity registry's ``Registered`` log since the deployment's start block), or None."""
        aid = int(agent_id)
        if aid in self._registered:
            return self._registered[aid]
        found: int | None = None
        for entry in self.dep.agents.values():
            if int(entry.agent_id) == aid and entry.register_tx:
                try:
                    receipt = self.w3.eth.get_transaction_receipt(entry.register_tx)
                    found = self.block_time(int(receipt["blockNumber"]))
                except Exception:  # noqa: BLE001 - fall back to the log scan
                    found = None
                break
        if found is None:
            try:
                logs = self.c.identity.logs("Registered", int(self.dep.start_block), "latest", agentId=aid)
                if logs:
                    found = self.block_time(int(logs[0]["blockNumber"]))
            except Exception:  # noqa: BLE001 - unknown
                found = None
        if found is not None:
            self._registered[aid] = found
        return found

    def live_wallet(self, agent_id: int) -> str | None:
        """``getAgentWallet`` (lowercase), or None for no wallet or a nonexistent agent."""
        try:
            wallet = self.c.identity.get_agent_wallet(int(agent_id)).lower()
        except Exception:  # noqa: BLE001 - a nonexistent agent reverts
            return None
        return None if wallet == ZERO_ADDRESS else wallet

    def payee_row(self, agent_id: int, *, now: int, max_age: int, roles: Mapping[str, str]) -> dict[str, Any]:
        """Everything the owner must see about one payee, read from the chain and the pinned directory.

        ``problems`` block a confirmation; ``warnings`` are shown prominently but do not block.
        """
        aid = int(agent_id)
        entry = self.directory.get(aid)
        problems: list[str] = []
        warnings: list[str] = []
        if entry is None:
            problems.append(f"agent #{aid} is not in the pinned directory")
        elif entry not in self.directory.providers:
            problems.append(f"agent #{aid} is not a provider of the pinned directory")
        if aid == self.wallet_agent_id:
            problems.append("the payee is our own wallet")
        live = self.live_wallet(aid)
        if live is None:
            problems.append(f"agent #{aid} has no agent wallet on-chain")
        elif live in {r.lower() for r in roles.values() if r}:
            problems.append(f"the wallet of agent #{aid} is one of our own role addresses")
        pinned_dir = entry.wallet.lower() if entry is not None and entry.wallet else None
        if live is not None and pinned_dir is not None and live != pinned_dir:
            problems.append(f"the live wallet of agent #{aid} differs from the pinned directory")
        grade = Grade.NONE
        score: int | None = None
        posted_at: int | None = None
        try:
            grade = Grade(self.c.acb.effective_grade(aid, max_age))
            record = self.c.acb.get_record(aid)
            if record.grade != Grade.NONE:
                score, posted_at = int(record.score), int(record.posted_at)
        except Exception:  # noqa: BLE001 - nonexistent agent: shown as NONE
            problems.append(f"agent #{aid} has no readable grade")
        if grade == Grade.D:
            warnings.append(f"agent #{aid} is graded D: the vault refuses every payment to it")
        elif grade in (Grade.NEW, Grade.NONE):
            warnings.append(f"agent #{aid} is new or unrated: payments go to escrow with a small cap")
        registered = self.registered_at(aid)
        lookalike = self.directory.lookalike_of(aid) if entry is not None else None
        if lookalike:
            warnings.append(f"the name of agent #{aid} looks like {lookalike}")
        return {
            "agentId": str(aid),
            "name": entry.name if entry is not None else f"unknown agent #{aid}",
            "inDirectory": entry is not None,
            "category": entry.category if entry is not None else "",
            "liveWallet": live,
            "grade": grade.name,
            "score": score,
            "gradePostedAt": posted_at,
            "registeredAt": registered,
            "ageS": (int(now) - registered) if registered is not None else None,
            "lookalikeOf": lookalike,
            "problems": problems,
            "warnings": warnings,
        }

    def roles(self) -> dict[str, str]:
        """Addresses no mandate may pay: the vault, its owner, agent and autonomy operator."""
        vault = self.vault
        out = {"vault": vault.address}
        for name, fn in (("owner", vault.owner), ("agent", vault.agent), ("autonomy", vault.autonomy_operator)):
            try:
                out[name] = fn().lower()
            except Exception:  # noqa: BLE001 - fall back to the deployment's record
                entry = self.dep.vaults[self.vault_name]
                out[name] = {"owner": entry.owner, "agent": entry.agent, "autonomy": entry.autonomy}[name].lower()
        return out


# ---------------------------------------------------------------------------
# mandates: rebuilt from chain reads (spec 11.1, 3.2)
# ---------------------------------------------------------------------------


def draft_fields(draft: Mapping[str, Any]) -> dict[str, Any]:
    """The only values P1 takes from an agent draft: agentIds, numbers and purpose (spec 11.1).

    Accepts the agent's stored draft (``payeeIds``) or a ``mandate_draft`` card (``payees[].agentId``).
    Names, grades, wallets and every other field of the draft are ignored. Raises
    :class:`VerificationError` on malformed values.
    """
    if not isinstance(draft, Mapping):
        raise VerificationError("the draft is not an object")
    raw_ids: Any = draft.get("payeeIds")
    if raw_ids is None and isinstance(draft.get("payees"), list):
        raw_ids = [p.get("agentId") if isinstance(p, Mapping) else None for p in draft["payees"]]
    if not isinstance(raw_ids, list):
        raise VerificationError("the draft has no payee agent ids")
    ids: list[int] = []
    for value in raw_ids:
        if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit():
            raise VerificationError("the draft's payee ids are malformed")
        if len(str(value)) > 78:
            raise VerificationError("the draft's payee ids are malformed")
        ids.append(int(value))
    if not 1 <= len(ids) <= MAX_PAYEES or len(set(ids)) != len(ids):
        raise VerificationError(f"a mandate needs 1 to {MAX_PAYEES} distinct payees")

    def amount(key: str) -> int:
        value = draft.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit():
            raise VerificationError(f"the draft's {key} is malformed")
        if len(str(value)) > 30:
            raise VerificationError(f"the draft's {key} is too large")
        return int(value)

    per_tx, total = amount("maxPerTx"), amount("maxTotal")
    hours = draft.get("validHours")
    if isinstance(hours, bool) or not isinstance(hours, int):
        raise VerificationError("the draft's validHours is malformed")
    recurring = draft.get("recurring", False)
    if not isinstance(recurring, bool):
        raise VerificationError("the draft's recurring flag is malformed")
    purpose = draft.get("purpose", "")
    if not isinstance(purpose, str):
        raise VerificationError("the draft's purpose is malformed")
    if not 0 < per_tx <= total:
        raise VerificationError("the draft needs 0 < max per payment <= total")
    if total >= 2**128:
        raise VerificationError("the draft's total does not fit the vault")
    if not 1 <= hours <= MAX_VALID_HOURS:
        raise VerificationError(f"the draft's validity must be 1 to {MAX_VALID_HOURS} hours")
    return {
        "ids": ids,
        "maxPerTx": per_tx,
        "maxTotal": total,
        "validHours": hours,
        "recurring": recurring,
        "purpose": purpose[:MAX_PURPOSE_CHARS],
    }


def purpose_hash(purpose: str) -> str:
    """``purposeHash`` stored on-chain: ``chash({"purpose": purpose})`` (the agent uses the same)."""
    return chash({"purpose": purpose})


def mandate_id_for(
    *,
    vault: str,
    ids: list[int],
    wallets: list[str],
    max_per_tx: int,
    max_total: int,
    valid_until: int,
    recurring: bool,
    purpose_hash_: str,
    salt: str,
) -> str:
    """``chash({vault, payeeAgentIds, payeeWallets, maxPerTx, maxTotal, validUntil, recurring,
    purposeHash, salt})`` with a random 32-byte salt (spec 3.2)."""
    return chash(
        {
            "vault": vault.lower(),
            "payeeAgentIds": [str(int(i)) for i in ids],
            "payeeWallets": [w.lower() for w in wallets],
            "maxPerTx": str(int(max_per_tx)),
            "maxTotal": str(int(max_total)),
            "validUntil": int(valid_until),
            "recurring": bool(recurring),
            "purposeHash": purpose_hash_,
            "salt": salt,
        }
    )


def prepare_mandate(oc: OwnerChain, draft: Mapping[str, Any], *, draft_id: str, salt: str | None = None) -> dict:
    """The confirm card of a mandate draft, rebuilt from chain reads, with the exact values to sign.

    Returns ``{"kind": "mandate", "draftId", "values", "payees", "problems", "warnings",
    "confirmable", "code", "context"}``. ``values`` are the arguments of ``registerMandate``;
    ``confirmable`` is False whenever ``problems`` is not empty.
    """
    fields = draft_fields(draft)
    vault = oc.vault
    now = oc.now()
    max_age = vault.max_grade_age()
    roles = oc.roles()
    rows = [oc.payee_row(aid, now=now, max_age=max_age, roles=roles) for aid in fields["ids"]]
    problems = [p for r in rows for p in r["problems"]]
    warnings = [w for r in rows for w in r["warnings"]]
    state = vault.state()
    if state.frozen or state.tier == TIER_FROZEN:
        warnings.append("the wallet is frozen: the mandate can be registered, but nothing is paid until you unfreeze")
    else:
        if fields["maxPerTx"] > int(state.config.per_tx):
            warnings.append(
                f"max per payment {usdc_text(fields['maxPerTx'])} USDC is above the tier {state.tier} cap of "
                f"{usdc_text(int(state.config.per_tx))} USDC: larger payments need your approval"
            )
        if fields["recurring"] and state.tier < 2:
            warnings.append("recurring mandates are usable only from Tier 2")
    free = vault.free_balance()
    if fields["maxTotal"] > free:
        warnings.append(
            f"the total {usdc_text(fields['maxTotal'])} USDC is above the free balance {usdc_text(free)} USDC"
        )
    wallets = [r["liveWallet"] or ZERO_ADDRESS for r in rows]
    valid_until = now + int(fields["validHours"]) * 3600
    phash = purpose_hash(fields["purpose"])
    salt = salt or ("0x" + secrets.token_hex(32))
    mid = mandate_id_for(
        vault=vault.address,
        ids=fields["ids"],
        wallets=wallets,
        max_per_tx=fields["maxPerTx"],
        max_total=fields["maxTotal"],
        valid_until=valid_until,
        recurring=fields["recurring"],
        purpose_hash_=phash,
        salt=salt,
    )
    values = {
        "mandateId": mid,
        "payeeAgentIds": [str(i) for i in fields["ids"]],
        "payeeWallets": wallets,
        "maxPerTx": str(fields["maxPerTx"]),
        "maxTotal": str(fields["maxTotal"]),
        "validUntil": valid_until,
        "recurring": fields["recurring"],
        "purposeHash": phash,
    }
    prepared = {
        "kind": "mandate",
        "draftId": draft_id,
        "values": values,
        "salt": salt,
        "purpose": fields["purpose"],
        "validHours": fields["validHours"],
        "payees": rows,
        "problems": problems,
        "warnings": warnings,
        "confirmable": not problems,
        "context": {"now": now, "tier": state.tier, "frozen": bool(state.frozen), "vault": vault.address},
    }
    prepared["code"] = signing_code(prepared)
    return prepared


def recheck_mandate(oc: OwnerChain, prepared: Mapping[str, Any]) -> list[str]:
    """Problems that appeared since the review (live wallet changed, payee left the directory, ...)."""
    v = prepared["values"]
    now = oc.now()
    vault = oc.vault
    roles = oc.roles()
    problems: list[str] = []
    if vault.get_mandate(v["mandateId"]).exists:
        problems.append("this mandate id is already registered")
    if not now < int(v["validUntil"]):
        problems.append("the reviewed validity has passed: review the draft again")
    for aid, wallet in zip(v["payeeAgentIds"], v["payeeWallets"], strict=True):
        row = oc.payee_row(int(aid), now=now, max_age=vault.max_grade_age(), roles=roles)
        problems.extend(row["problems"])
        if row["liveWallet"] != wallet:
            problems.append(f"the wallet of agent #{aid} changed since you reviewed it")
    return problems


# ---------------------------------------------------------------------------
# approvals: built only from an invoice P1 verified itself (spec 11.1)
# ---------------------------------------------------------------------------


def parse_signed_invoice(raw: Any) -> SignedInvoice:
    """A signed invoice from its canonical wire form (``acb-invoice/1`` plus ``signature``) or from the
    model form of an ``ApprovalRequest`` card. Raises :class:`VerificationError`."""
    if isinstance(raw, SignedInvoice):
        return raw
    if not isinstance(raw, Mapping):
        raise VerificationError("no signed invoice for this order")
    try:
        if "schema" in raw:
            return SignedInvoice.from_wire(dict(raw))
        si = SignedInvoice.model_validate(dict(raw))
    except (TypeError, ValueError):
        raise VerificationError("the signed invoice is malformed or not canonical") from None
    # the model form must also be the canonical wire form the issuer signed
    try:
        return SignedInvoice.from_wire(si.wire_dict())
    except (TypeError, ValueError):
        raise VerificationError("the signed invoice is not canonical") from None


def _needs_owner_record(oc: OwnerChain, oid: str, ihash: str, issuer: int, amount: int) -> bool:
    """True when our vault emitted ``DecisionRecorded(NEEDS_OWNER_APPROVAL)`` for exactly this invoice."""
    vault = oc.vault
    logs = vault.logs("DecisionRecorded", int(oc.dep.start_block), "latest", orderId=oid)
    for log in logs:
        args = log["args"]
        if (
            Web3.to_hex(args["orderId"]).lower() == oid
            and int(args["decision"]) == int(Decision.NEEDS_OWNER_APPROVAL)
            and Web3.to_hex(args["invoiceHash"]).lower() == ihash
            and int(args["payeeAgentId"]) == int(issuer)
            and int(args["amount"]) == int(amount)
        ):
            return True
    return False


def prepare_approval(oc: OwnerChain, order: str, raw_invoice: Any) -> dict:
    """The verified approval card of ``order`` (spec 11.1); raises :class:`VerificationError`.

    Returns ``{"kind": "approval", "orderId", "values", "invoice", "payee", "warnings", "detector",
    "confirmable", "code", "context"}``; ``values`` are the arguments of ``ownerApprove``.
    """
    from acb.injection import detect, invoice_fields

    if not is_hex32(order):
        raise VerificationError("the order id must be 0x + 64 hex")
    oid = order.lower()
    si = parse_signed_invoice(raw_invoice)
    inv = si.invoice
    wire = si.wire_dict()
    vault = oc.vault
    issuer = int(inv.issuer_agent_id)
    entry = oc.directory.get(issuer)
    if entry is None or entry not in oc.directory.providers:
        raise VerificationError(f"the issuer #{issuer} is not a provider of the pinned directory")
    if int(inv.chain_id) != int(oc.dep.chain_id):
        raise VerificationError("the invoice is for another chain")
    if inv.token.lower() != oc.usdc:
        raise VerificationError("the invoice is not in our USDC")
    if int(inv.buyer_agent_id) != oc.wallet_agent_id or inv.buyer_vault.lower() != vault.address:
        raise VerificationError("the invoice is not addressed to our wallet")
    if not inv.items:
        raise VerificationError("the invoice has no items")
    total = sum(int(i.unit_price) * int(i.quantity) for i in inv.items)
    if total != int(inv.amount) or int(inv.amount) <= 0:
        raise VerificationError("the invoice amount is not the sum of its items")
    catalogue = entry.skus
    for item in inv.items:
        if item.sku not in catalogue or int(catalogue[item.sku]) != int(item.unit_price):
            raise VerificationError("an invoice item is not in the issuer's pinned catalogue")
    live = oc.live_wallet(issuer)
    if live is None:
        raise VerificationError(f"the issuer #{issuer} has no agent wallet on-chain")
    try:
        signer = recover_invoice_signer(wire)
    except (KeyError, TypeError, ValueError):
        raise VerificationError("the invoice signature is malformed") from None
    if signer != live:
        raise VerificationError("the invoice is not signed by the issuer's live agent wallet")
    if entry.wallet and live != entry.wallet.lower():
        raise VerificationError("the issuer's live wallet differs from the pinned directory")
    if live in set(oc.roles().values()):
        raise VerificationError("the issuer's wallet is one of our own role addresses")
    ihash = invoice_hash(wire)
    if ihash != si.invoice_hash:
        raise VerificationError("the invoice hash does not match")
    computed = order_id(int(oc.dep.chain_id), vault.address, issuer, inv.invoice_id)
    if computed != oid:
        raise VerificationError("the order id is not the one derived from the signed invoice")
    now = oc.now()
    if now > int(inv.expires_at):
        raise VerificationError("the invoice has expired: ask the provider for a new quote")
    if vault.get_order(oid).state != OrderState.NONE:
        raise VerificationError("this order already exists on-chain")
    existing = vault.get_approval(oid)
    if existing.exists and not existing.used and now <= existing.expiry:
        raise VerificationError("this order already has an unused owner approval")
    if not _needs_owner_record(oc, oid, ihash, issuer, int(inv.amount)):
        raise VerificationError("our vault has no NEEDS_OWNER_APPROVAL record for this invoice")
    max_age = vault.max_grade_age()
    row = oc.payee_row(issuer, now=now, max_age=max_age, roles=oc.roles())
    grade = Grade[row["grade"]]
    if grade == Grade.D:
        raise VerificationError("the payee is graded D: the vault refuses every payment to it")
    as_escrow = grade in (Grade.C, Grade.NEW, Grade.NONE)
    deadline = max(int(inv.delivery_deadline), now + ESCROW_MIN_S) if as_escrow else 0
    values = {
        "orderId": oid,
        "payeeAgentId": str(issuer),
        "expectedPayee": live,
        "amount": str(int(inv.amount)),
        "expiry": now + APPROVAL_TTL_S,
        "asEscrow": as_escrow,
        "escrowDeadline": deadline,
    }
    detector = detect(invoice_fields(wire))
    warnings = list(row["warnings"])
    if detector.adverse:
        warnings.append("the invoice text contains instructions aimed at the wallet (detector: adverse)")
    elif detector.quarantine:
        warnings.append("the invoice text was flagged by the detector (quarantined)")
    state = vault.state()
    prepared = {
        "kind": "approval",
        "orderId": oid,
        "values": values,
        "invoice": {
            "invoiceId": inv.invoice_id,
            "invoiceHash": ihash,
            "issuedAt": int(inv.issued_at),
            "expiresAt": int(inv.expires_at),
            "deliveryDeadline": int(inv.delivery_deadline),
            "items": [
                {
                    "sku": i.sku,
                    "description": i.description,
                    "unitPrice": str(int(i.unit_price)),
                    "quantity": int(i.quantity),
                }
                for i in inv.items
            ],
            "memo": inv.memo,
            "signer": signer,
            "signatureVerified": True,
        },
        "payee": row,
        "detector": {
            "adverse": detector.adverse,
            "quarantine": detector.quarantine,
            "rules": sorted({h.rule for h in detector.hits}),
        },
        "warnings": warnings,
        "problems": [],
        "confirmable": True,
        "context": {"now": now, "tier": state.tier, "frozen": bool(state.frozen), "vault": vault.address},
    }
    prepared["code"] = signing_code(prepared)
    return prepared


def recheck_approval(oc: OwnerChain, prepared: Mapping[str, Any], raw_invoice: Any) -> list[str]:
    """Problems that appeared since the review: the full verification again, then the same values."""
    try:
        fresh = prepare_approval(oc, prepared["orderId"], raw_invoice)
    except VerificationError as exc:
        return [str(exc)]
    keep = ("orderId", "payeeAgentId", "expectedPayee", "amount", "asEscrow")
    return [f"{k} changed since you reviewed it" for k in keep if fresh["values"][k] != prepared["values"][k]]


# ---------------------------------------------------------------------------
# confirmation code (the CLI and the web card show the same one)
# ---------------------------------------------------------------------------


def signing_code(prepared: Mapping[str, Any]) -> str:
    """6 hex characters of ``chash`` over exactly the values that will be signed."""
    return chash({"kind": prepared["kind"], "values": dict(prepared["values"])})[2:8].upper()


# ---------------------------------------------------------------------------
# panels (spec 11.2)
# ---------------------------------------------------------------------------


def _hex(value: Any) -> str:
    return Web3.to_hex(value).lower()


def _jsonable(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return Web3.to_hex(value).lower()
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value) if abs(value) >= 2**53 else value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


class PanelReader:
    """Chain data of the five panels, read with incremental log scans (spec 11.2)."""

    def __init__(self, oc: OwnerChain):
        self.oc = oc

    def wallet(self) -> dict[str, Any]:
        """Panel 1: tier, limits, window, balances, mandate budgets."""
        oc = self.oc
        vault = oc.vault
        state = vault.state()
        free = vault.free_balance()
        frozen = bool(state.frozen) or state.tier == TIER_FROZEN
        next_needed = None
        if not frozen and state.tier < 3:
            try:
                next_needed = int(vault.tier_config(state.tier).min_clean_to_promote)
            except Exception:  # noqa: BLE001 - unknown
                next_needed = None
        cfg = state.config
        return {
            "vault": vault.address,
            "agentId": str(state.agent_id),
            "owner": state.owner,
            "agent": state.agent,
            "tier": state.tier,
            "frozen": frozen,
            "tierBeforeFreeze": state.tier_before_freeze,
            "maxAutoTier": state.max_auto_tier,
            "limits": {
                "perTx": str(cfg.per_tx),
                "windowCap": str(cfg.window_cap),
                "newPayeeCap": str(cfg.new_payee_cap),
                "maxTx": int(cfg.max_tx),
                "payGrades": [g.name for g in Grade if (int(cfg.pay_mask) >> int(g)) & 1],
                "escrowGrades": [g.name for g in Grade if (int(cfg.escrow_mask) >> int(g)) & 1],
            },
            "windowSpent": str(state.window_spent),
            "windowCount": int(state.window_count),
            "balance": str(state.balance),
            "reserveFloor": str(state.reserve_floor),
            "escrowLocked": str(state.escrow_locked),
            "freeBalance": str(free),
            "cleanSinceChange": int(state.clean_since_change),
            "cleanNeeded": next_needed,
            "violationPoints": int(state.violation_points),
            "lastTierChangeAt": int(state.last_tier_change_at),
            "mandates": self.mandates(),
        }

    def mandates(self) -> list[dict[str, Any]]:
        oc = self.oc
        vault = oc.vault
        latest = oc.latest_block()
        logs = oc.cache("mandates", vault, "MandateRegistered").refresh(latest)
        now = oc.now()
        out = []
        for log in logs:
            args = log["args"]
            mid = _hex(args["mandateId"])
            info = vault.get_mandate(mid)
            ids = [int(x) for x in args["payeeAgentIds"]]
            out.append(
                {
                    "mandateId": mid,
                    "payees": [
                        {"agentId": str(a), "name": oc.directory.name_of(a), "wallet": str(w).lower()}
                        for a, w in zip(ids, args["payeeWallets"], strict=False)
                    ],
                    "maxPerTx": str(info.max_per_tx),
                    "maxTotal": str(info.max_total),
                    "spent": str(info.spent),
                    "validUntil": int(info.valid_until),
                    "recurring": bool(info.recurring),
                    "active": bool(info.active) and now <= int(info.valid_until),
                    "revoked": not bool(info.active),
                    "expired": now > int(info.valid_until),
                    "txHash": _hex(log["transactionHash"]),
                    "explorer": oc.explorer_tx(_hex(log["transactionHash"])),
                }
            )
        out.reverse()
        return out

    def grades(self) -> list[dict[str, Any]]:
        """Panel 2: every agent of the pinned directory with its grade, score and reasons."""
        oc = self.oc
        latest = oc.latest_block()
        posted = oc.cache("grades", oc.c.acb, "GradePosted").refresh(latest)
        last_post: dict[int, Any] = {}
        for log in posted:
            last_post[int(log["args"]["agentId"])] = log
        max_age = oc.vault.max_grade_age()
        rows = []
        for key, agent in sorted(oc.dep.agents.items(), key=lambda kv: int(kv[1].agent_id)):
            aid = int(agent.agent_id)
            row: dict[str, Any] = {
                "agentId": str(aid),
                "key": key,
                "name": agent.name,
                "category": agent.category,
                "anchor": aid in {int(a) for a in oc.dep.anchors},
                "self": aid == oc.wallet_agent_id,
                "lookalikeOf": oc.directory.lookalike_of(aid),
            }
            try:
                record = oc.c.acb.get_record(aid)
                effective = oc.c.acb.effective_grade(aid, max_age)
            except Exception:  # noqa: BLE001 - not readable: shown as unrated
                record, effective = None, Grade.NONE
            if record is None or record.grade == Grade.NONE:
                row.update(grade="NONE", effectiveGrade=effective.name, score=None, reasons=[], postedAt=None)
            else:
                row.update(
                    grade=record.grade.name,
                    previousGrade=record.prev_grade.name,
                    effectiveGrade=Grade(effective).name,
                    score=int(record.score),
                    reasons=reason_entries(record.reason_list),
                    postedAt=int(record.posted_at),
                    modelId=record.model_id,
                    evidenceHash=record.evidence_hash,
                    asOfBlock=int(record.as_of_block),
                )
            log = last_post.get(aid)
            if log is not None:
                tx = _hex(log["transactionHash"])
                row.update(
                    txHash=tx,
                    explorer=oc.explorer_tx(tx),
                    evidenceUri=str(log["args"]["evidenceURI"])[:300],
                    recompute=f"acb bureau recompute --network {oc.net.name} --tx {tx}",
                )
            rows.append(row)
        return rows

    def autonomy(self) -> dict[str, Any]:
        """Panel 4: tier changes and violations of the wallet vault (newest first)."""
        oc = self.oc
        vault = oc.vault
        latest = oc.latest_block()
        changes = oc.cache("tiers", vault, "TierChanged").refresh(latest)
        violations = oc.cache("violations", vault, "ViolationRecorded").refresh(latest)
        agents = oc.cache("agents", vault, "AgentChanged").refresh(latest)
        out_changes = []
        for log in changes:
            a = log["args"]
            tx = _hex(log["transactionHash"])
            out_changes.append(
                {
                    "fromTier": int(a["fromTier"]),
                    "toTier": int(a["toTier"]),
                    "reasons": reason_entries([int(a["reasonCode"])]),
                    "evidenceHash": _hex(a["evidenceHash"]),
                    "by": str(a["by"]).lower(),
                    "txHash": tx,
                    "explorer": oc.explorer_tx(tx),
                    "block": int(log["blockNumber"]),
                    "timestamp": oc.block_time(int(log["blockNumber"])),
                }
            )
        out_violations = []
        for log in violations:
            a = log["args"]
            tx = _hex(log["transactionHash"])
            kind = int(a["kind"])
            try:
                from acb.types import ViolationKind

                kind_name = ViolationKind(kind).name
            except ValueError:
                kind_name = str(kind)
            out_violations.append(
                {
                    "kind": kind_name,
                    "severity": int(a["severity"]),
                    "tierAfter": int(a["tierAfter"]),
                    "evidenceHash": _hex(a["evidenceHash"]),
                    "txHash": tx,
                    "explorer": oc.explorer_tx(tx),
                    "timestamp": oc.block_time(int(log["blockNumber"])),
                }
            )
        rotations = [
            {
                "oldAgent": str(log["args"]["oldAgent"]).lower(),
                "newAgent": str(log["args"]["newAgent"]).lower(),
                "txHash": _hex(log["transactionHash"]),
                "explorer": oc.explorer_tx(_hex(log["transactionHash"])),
            }
            for log in agents
        ]
        state = vault.state()
        frozen = bool(state.frozen) or state.tier == TIER_FROZEN
        needed = None
        if not frozen and state.tier < 3:
            needed = int(vault.tier_config(state.tier).min_clean_to_promote)
        out_changes.reverse()
        out_violations.reverse()
        return {
            "tier": state.tier,
            "frozen": frozen,
            "maxAutoTier": state.max_auto_tier,
            "cleanSinceChange": int(state.clean_since_change),
            "cleanNeeded": needed,
            "violationPoints": int(state.violation_points),
            "lastTierChangeAt": int(state.last_tier_change_at),
            "changes": out_changes,
            "violations": out_violations,
            "rotations": rotations,
        }


__all__ = [
    "APPROVAL_TTL_S",
    "ESCROW_MIN_S",
    "OwnerChain",
    "PanelReader",
    "VerificationError",
    "draft_fields",
    "is_address",
    "is_hex32",
    "mandate_id_for",
    "parse_signed_invoice",
    "prepare_approval",
    "prepare_mandate",
    "purpose_hash",
    "reason_catalogue",
    "reason_entries",
    "recheck_approval",
    "recheck_mandate",
    "signing_code",
    "usdc_text",
]
