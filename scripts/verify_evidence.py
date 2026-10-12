#!/usr/bin/env python3
"""Verify the execution evidence of an Agent Credit Bureau run (spec 10.5). Standard library only.

    python scripts/verify_evidence.py evidence/base_sepolia/evidence.json [--rpc URL] [--receipts-only]

For every transaction listed in ``evidence.json`` (schema ``acb-run/1``) the script fetches
``eth_getTransactionReceipt`` from the RPC (default https://sepolia.base.org; at most 4 requests per
second, retries with backoff) and checks the chain id (``eth_chainId``), ``status == expectedStatus``, the
block number, ``from`` and ``to`` (or the created contract), and that the topic0 of every expected event
appears in the logs of a successful transaction. It compares the answer with the saved receipt file. For
every grade change it recomputes keccak256 of the evidence bundle file and checks it equals the
``evidenceHash`` in the data of the ``GradePosted`` log.

``--receipts-only`` checks the same things offline against the saved receipt files (for readers who
cannot reach an RPC). The script prints one line per check and ``verified X / Y``; the exit code is 0 only
if every check passes.

keccak256 is implemented below in pure Python (the Ethereum variant of Keccak with 0x01 padding, not
NIST SHA3-256); event topics are embedded in :data:`EVENT_TOPICS`, generated from ``abi/``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_RPC = "https://sepolia.base.org"
DEFAULT_RPS = 4.0

# ---------------------------------------------------------------------------
# keccak256 (Keccak-f[1600], rate 1088 bits, Ethereum padding 0x01 ... 0x80)
# ---------------------------------------------------------------------------

_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)  # fmt: skip
_ROT = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)
_MASK = (1 << 64) - 1


def _rol(x: int, n: int) -> int:
    n %= 64
    return ((x << n) | (x >> (64 - n))) & _MASK if n else x


def _keccak_f(a: list[list[int]]) -> None:
    for rc in _RC:
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                a[x][y] ^= d[x]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _ROT[x][y])
        for x in range(5):
            for y in range(5):
                a[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y])
        a[0][0] ^= rc


def keccak256(data: bytes) -> bytes:
    """Ethereum keccak256 of ``data``."""
    rate = 136
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate:
        padded.append(0)
    padded[-1] |= 0x80
    state = [[0] * 5 for _ in range(5)]
    for offset in range(0, len(padded), rate):
        block = padded[offset : offset + rate]
        for i in range(rate // 8):
            lane = int.from_bytes(block[8 * i : 8 * i + 8], "little")
            state[i % 5][i // 5] ^= lane
        _keccak_f(state)
    out = bytearray()
    for i in range(4):
        out += state[i % 5][i // 5].to_bytes(8, "little")
    return bytes(out)


def keccak_hex(data: bytes) -> str:
    return "0x" + keccak256(data).hex()


# ---------------------------------------------------------------------------
# event topics (generated from abi/ by `python -m acb.scenario.evidence topics`; checked by a test)
# ---------------------------------------------------------------------------

EVENT_TOPICS: dict[str, tuple[tuple[str, str], ...]] = {
    "AdminTransferStarted": (("AdminTransferStarted(address,address)", "0xe5cd1c804f1c9cc6d7009e4c0fb532f0e2d8863524c3323a6b3790c3f80bf25c"),),
    "AdminTransferred": (("AdminTransferred(address,address)", "0xf8ccb027dfcd135e000e9d45e6cc2d662578a8825d4c45b5e32e0adf67e79ec6"),),
    "AgentChanged": (("AgentChanged(address,address)", "0xb21f1bf0d206b43eb02be4f423c2788a95affbda226125eaecabe3def8ba5c42"),),
    "AppealFiled": (("AppealFiled(uint256,uint256,address,bytes32,string)", "0xc989c0068514c1b5ed05f5f83a2b2f67bce149c71beb6e5f041bc4fa25a0246d"),),
    "AppealResolved": (("AppealResolved(uint256,bool,bytes32)", "0xfd3b887dbea35f0e5625f669a1386a74d7bacf8cc2548bb55a62f2cfa97bc10f"),),
    "Approval": (("Approval(address,address,uint256)", "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"),),
    "ApprovalForAll": (("ApprovalForAll(address,address,bool)", "0x17307eab39ab6107e8899845ad3d59bd9653f200f220920489ca2b5937696c31"),),
    "ApprovalRevoked": (("ApprovalRevoked(bytes32)", "0x8e6f5512abf17ac535879008fc5c63697815b29415502ece4e762cd7b12c9dd0"),),
    "AutonomyOperatorChanged": (("AutonomyOperatorChanged(address,address)", "0x0d001688c0e6628c8346d798125bbeedaa5a3cd406090646d4f33a726201fa28"),),
    "ConfigChanged": (("ConfigChanged(bytes32,uint256)", "0x867716e28221fc5767f7ac9e2623573aa2b682e2b925334db66a0753751708d6"),),
    "DecisionRecorded": (("DecisionRecorded(bytes32,uint256,uint8,uint128,uint16,bytes32,bytes32,uint8)", "0x8462110b5711568eac40d2b3b62ac31dec262e1ab04368525d3f4c52a828cd4b"),),
    "DeliveryClaimed": (("DeliveryClaimed(bytes32,uint256,bytes32,uint64,bool)", "0x79dcd7b5d67364a33e12e3e74a83b2cfe68a8dc9338ff92ae0988ea76c290274"),),
    "DeliveryRecorded": (("DeliveryRecorded(bytes32,uint256,address,uint8,bytes32)", "0xef1e3213cdcb809e38825182a05f012ef710416dee78b1a83a54275507cfea25"),),
    "EscrowOpened": (("EscrowOpened(bytes32,uint256,address,uint128,uint64,bytes32,bytes32,bytes32,uint16,uint8)", "0xea683ad20ee04834d2c1f7cf1a03932d13be69453c337c41c519fc3c8cccf13b"),),
    "EscrowRefunded": (("EscrowRefunded(bytes32,uint256,address,uint128,uint16,uint8)", "0xd2469272b9f1d5a920aa5b3118711c4eb92e3d32fef5feac56ddbbb15279b15e"),),
    "EscrowReleased": (("EscrowReleased(bytes32,uint256,address,uint128,uint8,bytes32)", "0x1503142a11d68df925f8caac318850f25fb527a5f2631f5da4514a17c304f0dd"),),
    "FeedbackRevoked": (("FeedbackRevoked(uint256,address,uint64)", "0x25156fd3288212246d8b008d5921fde376c71ed14ac2e072a506eb06fde6d09d"),),
    "GradePosted": (("GradePosted(uint256,uint8,uint8,uint16,uint16[4],bytes32,bytes32,bytes32,uint64,address,string)", "0x9930d87e432d5b546ff43fc8b779e49afb12b3449c7c3ae9c871a08db476a641"),),
    "MandateRegistered": (("MandateRegistered(bytes32,uint256[],address[],uint128,uint128,uint64,bool,bytes32)", "0xe49f2d4052d9ef444f662768f50ad57645afbf996f1cf72e17cb090f860ef648"),),
    "MandateRevoked": (("MandateRevoked(bytes32)", "0x31c289dd93001fcacb63a0874240a9d06870135458e9be63e0463f46edf663fa"),),
    "MetadataSet": (("MetadataSet(uint256,string,string,bytes)", "0x2c149ed548c6d2993cd73efe187df6eccabe4538091b33adbd25fafdb8a1468b"),),
    "MetadataUpdate": (("MetadataUpdate(uint256)", "0xf8e1a15aba9398e019f0b49df1a4fde98ee17ae345cb5f6b5e2c27f5033e8ce7"),),
    "NewFeedback": (("NewFeedback(uint256,address,uint64,int128,uint8,string,string,string,string,string,bytes32)", "0x6a4a61743519c9d648a14e6493f47dbe3ff1aa29e7785c96c8326a205e58febc"),),
    "OwnerApproved": (("OwnerApproved(bytes32,uint256,address,uint128,uint64,bool,uint64)", "0x113c4ff387749be7eb64db8c135767108e675b909ce19e934cf5558e632f3761"),),
    "OwnershipTransferStarted": (("OwnershipTransferStarted(address,address)", "0x38d16b8cac22d99fc7c124b9cd0de2d3fa1faef420bfe791d8c362d765e22700"),),
    "OwnershipTransferred": (("OwnershipTransferred(address,address)", "0x8be0079c531659141344cd1fd0a4f28419497f9722a3daafe3b4186f6b6457e0"),),
    "Paid": (("Paid(bytes32,uint256,address,uint128,uint8,uint16,bytes32,bytes32,bytes32,uint64,uint8)", "0xd4e5b3a74c2b76306dea7c36453dafd139b9a52255390aec2ede739846dd9cc0"),),
    "PosterSet": (("PosterSet(address,bool)", "0x3da19a2cb558b1fcec9947c13144c03185aa0074e2dbeeab3ab9c54f090616cc"),),
    "Registered": (("Registered(uint256,string,address)", "0xca52e62c367d81bb2e328eb795f7c7ba24afb478408a26c0e201d155c449bc4a"),),
    "ResponseAppended": (("ResponseAppended(uint256,address,uint64,address,string,bytes32)", "0xb1c6be0b5b8aef6539e2fac0fd131a2faa7b49edf8e505b5eb0ad487d56051d4"),),
    "TierChanged": (("TierChanged(uint8,uint8,uint16,bytes32,address)", "0x5759f888cf13b411c46f7d0dda3dc09ed0b7037c58ecd96847b63a65a93e57e3"),),
    "TierConfigSet": (("TierConfigSet(uint8,uint128,uint128,uint128,uint8,uint8,uint8,uint8)", "0xc366d66976f797a190a84df3ba98173954bb794c7a7b6b41e00bea73dec86e05"),),
    "Transfer": (("Transfer(address,address,uint256)", "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"),),
    "URIUpdated": (("URIUpdated(uint256,string,address)", "0x3a2c7fffc2cba7582c690e3b82c453ea02a308326a98a3ad7576c606336409fb"),),
    "UpgradeDelaySet": (("UpgradeDelaySet(uint64)", "0xdcd823d823cad769c8259e23b1d7d589a10e921465814baaa77c127e1445b21d"),),
    "VaultCodeHashSet": (("VaultCodeHashSet(bytes32)", "0x10162c0b8febab49faab16f95d769ef317912812850dfe565fa437634cc242af"),),
    "VaultInitialized": (("VaultInitialized(address,address,address,uint256,uint8)", "0x7808b5dbe51d59eab48197922953da5340efcbed7891dc1bed5efb0f38de5d2f"),),
    "VaultRegistered": (("VaultRegistered(address,uint256,address)", "0x68247146602edfa0f5e6476527cd84f11ec94afe896aea4fd993e8f203dfcb2e"),),
    "ViolationRecorded": (("ViolationRecorded(uint8,uint8,bytes32,address,uint8)", "0x16a148b018c1a0d3dbcb2901fb5f3e0950d5c8e1e579b6c11c8c00e2c95043bb"),),
    "Withdrawn": (("Withdrawn(address,uint128)", "0xab48b3d59a240196dc5bdd7f7a638fca310f8194c7d350c3dd7765861311ddf8"),),
}  # fmt: skip

GRADE_POSTED = "GradePosted"
GRADE_POSTED_EVIDENCE_WORD = 8
"""Word index of ``evidenceHash`` in the non-indexed data of ``GradePosted`` (grade, previousGrade,
score, reasons[4], configHash, evidenceHash, asOfBlock, evidenceURI offset)."""


# ---------------------------------------------------------------------------
# JSON-RPC client: rate limited, retries with backoff
# ---------------------------------------------------------------------------


class RpcError(RuntimeError):
    pass


class Rpc:
    def __init__(self, url: str, rps: float = DEFAULT_RPS, retries: int = 4, timeout: float = 20.0):
        self.url = url
        self.min_interval = 1.0 / rps if rps > 0 else 0.0
        self.retries = retries
        self.timeout = timeout
        self._last = 0.0
        self._id = 0

    def call(self, method: str, params: list):
        delay = 0.5
        last_error = "no answer"
        for attempt in range(self.retries + 1):
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self._id += 1
            body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}).encode()
            request = urllib.request.Request(
                self.url, data=body, headers={"Content-Type": "application/json", "User-Agent": "acb-verify/1"}
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    answer = json.loads(response.read().decode("utf-8"))
                if "error" in answer:
                    last_error = str(answer["error"])
                    code = answer["error"].get("code") if isinstance(answer["error"], dict) else None
                    if code in (-32005, 429, -32603) and attempt < self.retries:
                        time.sleep(delay)
                        delay *= 2
                        continue
                    raise RpcError(f"{method}: {last_error}")
                return answer.get("result")
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
        raise RpcError(f"{method}: {last_error}")


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def _int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value)
    return int(text, 16) if text.startswith("0x") else int(text)


def _low(value) -> str | None:
    return str(value).lower() if value is not None else None


def topic_set(name: str) -> set[str]:
    return {t for _sig, t in EVENT_TOPICS.get(name, ())}


def check_transaction(tx: dict, receipt: dict, transaction: dict | None, chain_id: int) -> list[str]:
    """Problems of one transaction against its receipt (empty list = verified)."""
    problems = []
    if _low(receipt.get("transactionHash")) != _low(tx.get("txHash")):
        problems.append("receipt is for another transaction")
    status = _int(receipt.get("status"))
    if status != int(tx.get("expectedStatus", 1)):
        problems.append(f"status {status} != expected {tx.get('expectedStatus')}")
    if status != int(tx.get("status", status)):
        problems.append(f"status {status} != recorded {tx.get('status')}")
    if _int(receipt.get("blockNumber")) != int(tx.get("block", -1)):
        problems.append(f"block {_int(receipt.get('blockNumber'))} != {tx.get('block')}")
    if _low(receipt.get("from")) != _low(tx.get("actorAddress")):
        problems.append(f"from {receipt.get('from')} != {tx.get('actorAddress')}")
    to = _low(receipt.get("to")) or _low(receipt.get("contractAddress"))
    if to != _low(tx.get("to")):
        problems.append(f"to {to} != {tx.get('to')}")
    tx_chain = _int(transaction.get("chainId")) if transaction is not None else None
    if tx_chain is not None and tx_chain != chain_id:
        problems.append(f"transaction chainId {tx_chain} != {chain_id}")
    if status == 1:
        topics = {_low((log.get("topics") or [None])[0]) for log in receipt.get("logs") or []}
        for name in tx.get("expectedEvents") or []:
            if not topic_set(name) & topics:
                problems.append(f"event {name} missing")
    return problems


def same_receipt(a: dict, b: dict) -> list[str]:
    problems = []
    for key in ("transactionHash", "blockHash", "blockNumber", "status", "gasUsed", "from", "to", "contractAddress"):
        if _low(a.get(key)) != _low(b.get(key)):
            problems.append(f"{key} differs from the saved receipt")
    la, lb = a.get("logs") or [], b.get("logs") or []
    if len(la) != len(lb):
        problems.append("log count differs from the saved receipt")
    else:
        for x, y in zip(la, lb, strict=True):
            if [_low(t) for t in x.get("topics") or []] != [_low(t) for t in y.get("topics") or []] or _low(
                x.get("data")
            ) != _low(y.get("data")):
                problems.append("logs differ from the saved receipt")
                break
    return problems


def _topic_int(topic) -> int | None:
    try:
        return int(str(topic), 16)
    except (TypeError, ValueError):
        return None


def evidence_hash_of(receipt: dict, agent_id=None, acb: str | None = None) -> list[str]:
    """``evidenceHash`` of the ``GradePosted`` logs of a receipt.

    With ``agent_id`` only logs whose indexed ``agentId`` (topic 1) equals it count, and with ``acb`` only
    logs emitted by that contract, so a bundle cannot be matched against another agent's posting.
    """
    out = []
    want = topic_set(GRADE_POSTED)
    for log in receipt.get("logs") or []:
        topics = log.get("topics") or []
        if not topics or _low(topics[0]) not in want:
            continue
        if acb is not None and _low(log.get("address")) != _low(acb):
            continue
        if agent_id is not None and (len(topics) < 2 or _topic_int(topics[1]) != _int(agent_id)):
            continue
        data = str(log.get("data") or "0x")[2:]
        start = GRADE_POSTED_EVIDENCE_WORD * 64
        out.append("0x" + data[start : start + 64].lower())
    return out


def _topic_address(topic) -> str | None:
    text = str(topic or "")
    return ("0x" + text[-40:]).lower() if len(text) >= 42 else None


def agent_keys_from(receipt: dict, wallet_vault: str | None) -> set[str]:
    """Agent signer addresses the wallet vault announced in this receipt.

    ``VaultInitialized(owner, agent, autonomyOperator, ...)`` (agent = topic 2) and
    ``AgentChanged(oldAgent, newAgent)`` (both topics 1 and 2: each was the vault's agent key), emitted by
    the wallet vault only.
    """
    keys: set[str] = set()
    if not wallet_vault:
        return keys
    init, changed = topic_set("VaultInitialized"), topic_set("AgentChanged")
    for log in receipt.get("logs") or []:
        topics = log.get("topics") or []
        if len(topics) < 3 or _low(log.get("address")) != _low(wallet_vault):
            continue
        t0 = _low(topics[0])
        found = [topics[2]] if t0 in init else [topics[1], topics[2]] if t0 in changed else []
        for topic in found:
            address = _topic_address(topic)
            if address and address != "0x" + "0" * 40:
                keys.add(address)
    return keys


def check_agent_key(tx: dict, agent_keys: set[str]) -> list[str]:
    """``signedByAgentKey`` must say whether the sender is an agent key the wallet vault announced."""
    signed = _low(tx.get("actorAddress")) in agent_keys
    if bool(tx.get("signedByAgentKey")) != signed:
        return [
            f"signedByAgentKey {bool(tx.get('signedByAgentKey'))} but the sender is{'' if signed else ' not'} an agent key"
        ]
    return []


def verify(
    evidence_path: Path,
    *,
    rpc_url: str | None = DEFAULT_RPC,
    receipts_only: bool = False,
    rps: float = DEFAULT_RPS,
    out=print,
) -> dict:
    """Verify one ``evidence.json``. Returns ``{"ok", "verified", "total", "failures"}``."""
    evidence_path = Path(evidence_path)
    folder = evidence_path.parent
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if evidence.get("schema") != "acb-run/1":
        raise SystemExit(f"{evidence_path}: not an acb-run/1 evidence file")
    chain_id = int(evidence["chainId"])
    rpc = None if receipts_only else Rpc(rpc_url or DEFAULT_RPC, rps=rps)
    failures: list[str] = []
    total = verified = 0
    if rpc is not None:
        got = _int(rpc.call("eth_chainId", []))
        if got != chain_id:
            raise SystemExit(f"the RPC serves chain {got}, the evidence is for chain {chain_id}")
    receipts: dict[str, dict] = {}
    contracts = evidence.get("contracts") or {}
    wallet_vault = (contracts.get("vaults") or {}).get("wallet")
    acb_address = contracts.get("AgentCreditBureau")
    # pass 1: every transaction against its receipt (saved, and the RPC's unless --receipts-only)
    checked: list[tuple[dict, list[str], dict | None]] = []
    for tx in evidence.get("transactions") or []:
        h = _low(tx["txHash"])
        problems: list[str] = []
        saved_path = folder / str(tx.get("receiptFile") or f"receipts/{h}.json")
        saved = None
        if saved_path.is_file():
            saved = json.loads(saved_path.read_text(encoding="utf-8"))
        else:
            problems.append(f"saved receipt {saved_path.name} missing")
        receipt = None
        transaction = saved.get("transaction") if saved else None
        if rpc is not None:
            try:
                receipt = rpc.call("eth_getTransactionReceipt", [h])
            except RpcError as exc:
                problems.append(str(exc))
            if receipt is None and not problems:
                problems.append("no receipt on the RPC")
            if receipt is not None and saved is not None:
                problems += same_receipt(receipt, saved.get("receipt") or {})
        elif saved is not None:
            receipt = saved.get("receipt")
        if receipt is not None:
            receipts[h] = receipt
            problems += check_transaction(tx, receipt, transaction, chain_id)
        checked.append((tx, problems, receipt))
    # the agent keys come from the wallet vault's own events (VaultInitialized, AgentChanged) in receipts
    # that verified, never from a list in evidence.json, so the judge-facing signedByAgentKey flag is checked
    agent_keys: set[str] = set()
    for _tx, problems, receipt in checked:
        if receipt is not None and not problems and _int(receipt.get("status")) == 1:
            agent_keys |= agent_keys_from(receipt, wallet_vault)
    # pass 2: the agent-key flag, then the report lines
    out(f"{'seq':>4}  {'step':<4}  {'function':<16} {'status':>6}  result")
    for tx, problems, receipt in checked:
        total += 1
        h = _low(tx["txHash"])
        problems = problems + check_agent_key(tx, agent_keys)
        result = "OK" if not problems else "FAIL " + "; ".join(problems)
        if not problems:
            verified += 1
        else:
            failures.append(f"{h}: {'; '.join(problems)}")
        out(
            f"{tx.get('seq', ''):>4}  {tx.get('step', ''):<4}  {str(tx.get('function', ''))[:16]:<16} {receipt and _int(receipt.get('status'))!s:>6}  {result}"
        )
    for change in evidence.get("gradeChanges") or []:
        total += 1
        h = _low(change.get("txHash"))
        problems = []
        receipt = receipts.get(h)
        if receipt is None:
            problems.append("posting transaction not verified")
        bundle = folder / str(change.get("bundle") or "")
        if not change.get("bundle") or not bundle.is_file():
            problems.append(f"bundle {change.get('bundle')} missing")
        elif receipt is not None:
            digest = keccak_hex(bundle.read_bytes())
            posted = evidence_hash_of(receipt, change.get("agentId"), acb_address)
            if not posted:
                problems.append(f"no GradePosted for agent {change.get('agentId')} in the posting transaction")
            elif digest not in posted:
                problems.append(f"bundle keccak {digest} != GradePosted evidenceHash of agent {change.get('agentId')}")
        result = "OK" if not problems else "FAIL " + "; ".join(problems)
        if not problems:
            verified += 1
        else:
            failures.append(f"grade {change.get('agentId')} {h}: {'; '.join(problems)}")
        out(f"{'':>4}  {'':<4}  {'bundle #' + str(change.get('agentId')):<16} {'':>6}  {result}")
    mode = "receipts only (offline)" if receipts_only else f"RPC {rpc_url}"
    out(f"verified {verified} / {total} ({mode})")
    return {"ok": verified == total and total > 0, "verified": verified, "total": total, "failures": failures}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify an Agent Credit Bureau evidence.json (spec 10.5).")
    parser.add_argument("evidence", help="path to evidence.json")
    parser.add_argument("--rpc", default=DEFAULT_RPC, help=f"JSON-RPC URL (default {DEFAULT_RPC})")
    parser.add_argument("--receipts-only", action="store_true", help="verify offline against the saved receipts")
    parser.add_argument("--rps", type=float, default=DEFAULT_RPS, help="RPC requests per second (default 4)")
    args = parser.parse_args(argv)
    report = verify(Path(args.evidence), rpc_url=args.rpc, receipts_only=args.receipts_only, rps=args.rps)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
