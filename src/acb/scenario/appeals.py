"""Appeal review (spec 4.7, 9.5, 10.2 S20): check an appeal against the vault events and resolve it.

An appeal (``ACB.fileAppeal(agentId, appealHash, uri)``) points at an ``acb-appeal/1`` payload that names
the orders the appellant says it delivered. The review reads, for each order, the public events of every
registered vault: was an escrow opened for the appellant, did the appellant claim delivery
(``DeliveryClaimed``) before the refund, and was the escrow refunded only after its deadline? Only orders
that are adverse evidence against the appellant count (its latest posted bundle, hash-checked: orders where
it is the payee with outcome FAILED or CLAWBACK). The appeal is upheld only if at least one such order was
claimed before its refund or refunded before its deadline; named orders that carried no adverse weight
(a delivered warm-up order, say) are listed but ignored. Otherwise it is rejected (reason 1103). The review is written as canonical JSON (``acb-appeal-review/1``) and its keccak is the
``evidenceHash`` of ``ACB.resolveAppeal(appealId, upheld, evidenceHash)``, sent with the bureau's poster key.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from acb.canonical import atomic_write_bytes, canonical_bytes, chash

REASON_UPHELD = 1102
REASON_REJECTED = 1103
ADVERSE_OUTCOMES = ("FAILED", "CLAWBACK")
"""Bundle order outcomes that weigh against the payee (refund reasons 2400 and 2403, spec 4.1)."""
ADVERSE_REFUND_CODES = (2400, 2403)


class AppealError(RuntimeError):
    """The appeal or its payload could not be read."""


def _vaults(w3: Any, dep: Any) -> list[str]:
    from acb.chain.contracts import AcbContract

    acb = AcbContract(w3, dep.contracts["AgentCreditBureau"].address)
    found = {str(lg["args"]["vault"]).lower() for lg in _logs(acb, "VaultRegistered", dep.start_block)}
    return sorted(found | {v.address.lower() for v in dep.vaults.values()})


def _logs(contract: Any, event: str, from_block: int, **filters: Any) -> list[dict[str, Any]]:
    from acb.chain.abi import json_safe

    out = []
    for lg in contract.logs(event, from_block, "latest", **filters):
        out.append(
            {
                "args": json_safe(dict(lg["args"])),
                "blockNumber": int(lg["blockNumber"]),
                "transactionHash": "0x" + bytes(lg["transactionHash"]).hex()
                if not isinstance(lg["transactionHash"], str)
                else lg["transactionHash"].lower(),
            }
        )
    return out


def open_appeals(w3: Any, dep: Any, agent_id: int, from_block: int) -> list[int]:
    """Ids of the appeals of ``agent_id`` filed at or after ``from_block`` and not resolved yet, oldest first."""
    from acb.chain.contracts import AcbContract

    acb = AcbContract(w3, dep.contracts["AgentCreditBureau"].address)
    resolved = {int(lg["args"]["appealId"]) for lg in _logs(acb, "AppealResolved", from_block)}
    return [
        int(lg["args"]["appealId"])
        for lg in _logs(acb, "AppealFiled", from_block)
        if int(lg["args"]["agentId"]) == int(agent_id) and int(lg["args"]["appealId"]) not in resolved
    ]


def load_appeal(w3: Any, dep: Any, appeal_id: int, payload_dirs: list[Path]) -> tuple[dict, dict]:
    """The ``AppealFiled`` event of ``appeal_id`` and its payload (hash-checked)."""
    from acb.chain.contracts import AcbContract

    acb = AcbContract(w3, dep.contracts["AgentCreditBureau"].address)
    filed = [lg for lg in _logs(acb, "AppealFiled", dep.start_block) if int(lg["args"]["appealId"]) == int(appeal_id)]
    if not filed:
        raise AppealError(f"appeal {appeal_id} not found on-chain")
    event = filed[0]
    appeal_hash = str(event["args"]["appealHash"]).lower()
    for folder in payload_dirs:
        path = Path(folder) / f"{appeal_hash}.json"
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if chash(payload) != appeal_hash:
                raise AppealError("the appeal payload does not match its on-chain hash")
            return event, payload
    raise AppealError(f"appeal payload {appeal_hash} not found")


def latest_bundle(
    w3: Any, dep: Any, agent_id: int, to_block: int, bundle_dirs: list[Path]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The subject's latest ``GradePosted`` at or before ``to_block`` and its bundle (hash-checked).

    Only files named ``<agentId>-<asOfBlock>.json`` in ``bundle_dirs/bundles`` whose keccak equals the posted
    ``evidenceHash`` are accepted; a file with the right name but other content is ignored.
    """
    from acb.bureau.evidence import bundle_name
    from acb.chain.contracts import AcbContract

    acb = AcbContract(w3, dep.contracts["AgentCreditBureau"].address)
    posts = [
        lg
        for lg in _logs(acb, "GradePosted", dep.start_block)
        if int(lg["args"]["agentId"]) == int(agent_id) and lg["blockNumber"] <= int(to_block)
    ]
    if not posts:
        return None, None
    post = posts[-1]
    name = bundle_name(agent_id, int(post["args"]["asOfBlock"]))
    want = str(post["args"]["evidenceHash"]).lower()
    for folder in bundle_dirs:
        path = Path(folder) / "bundles" / name
        if not path.is_file():
            continue
        try:
            bundle = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(bundle, dict) and chash(bundle) == want:
            return post, bundle
    return post, None


def adverse_orders(
    w3: Any, dep: Any, agent_id: int, to_block: int, bundle_dirs: list[Path], vaults: list[str]
) -> tuple[set[str], str]:
    """Order ids that weigh against ``agent_id`` as a provider, and where the set came from.

    From the subject's latest bundle: counted orders where it is the payee with outcome ``FAILED`` or
    ``CLAWBACK``. Without a bundle: escrows to the subject refunded with 2400 (non-delivery) or 2403
    (clawback) in the deployment's own vaults.
    """
    _post, bundle = latest_bundle(w3, dep, agent_id, to_block, bundle_dirs)
    if bundle is not None:
        ids = {
            str(o.get("orderId") or "").lower()
            for o in bundle.get("orders") or []
            if o.get("role") == "payee" and o.get("outcome") in ADVERSE_OUTCOMES and o.get("counted", True)
        }
        return {i for i in ids if i}, "bundle"
    from acb.chain.contracts import VaultContract

    ids: set[str] = set()
    own = {v.address.lower() for v in dep.vaults.values()}
    for vault_address in vaults:
        if vault_address not in own:
            continue
        vault = VaultContract(w3, vault_address)
        for refund in _logs(vault, "EscrowRefunded", dep.start_block):
            if int(refund["args"]["reasonCode"]) not in ADVERSE_REFUND_CODES or refund["blockNumber"] > to_block:
                continue
            oid = str(refund["args"]["orderId"]).lower()
            opened = _logs(vault, "EscrowOpened", dep.start_block, orderId=oid)
            if opened and int(opened[0]["args"]["payeeAgentId"]) == int(agent_id):
                ids.add(oid)
    return ids, "vault events"


def review_appeal(
    w3: Any,
    dep: Any,
    appeal_id: int,
    *,
    payload_dirs: list[Path],
    out_dir: Path,
    bundle_dirs: list[Path] | None = None,
) -> dict[str, Any]:
    """Build the review of ``appeal_id`` from public events; returns it with ``evidenceHash``.

    Only orders that are adverse evidence against the appellant (see :func:`adverse_orders`) can support
    an appeal: a named order that carried no adverse weight, such as a delivered warm-up order, is listed
    as ``adverseEvidence: false`` and ignored. ``bundle_dirs`` defaults to ``[out_dir]``.
    """
    from acb.chain.contracts import VaultContract

    event, payload = load_appeal(w3, dep, appeal_id, payload_dirs)
    agent_id = int(event["args"]["agentId"])
    order_ids = [str(o).lower() for o in payload.get("orderIds") or []]
    vaults = _vaults(w3, dep)
    adverse, adverse_source = adverse_orders(
        w3, dep, agent_id, int(event["blockNumber"]), list(bundle_dirs or [out_dir]), vaults
    )
    orders = []
    for oid in order_ids:
        entry: dict[str, Any] = {"orderId": oid, "found": False, "adverseEvidence": oid in adverse}
        for vault_address in vaults:
            vault = VaultContract(w3, vault_address)
            opened = _logs(vault, "EscrowOpened", dep.start_block, orderId=oid)
            if not opened:
                continue
            o = opened[0]
            claims = _logs(vault, "DeliveryClaimed", dep.start_block, orderId=oid)
            refunds = _logs(vault, "EscrowRefunded", dep.start_block, orderId=oid)
            refund = refunds[0] if refunds else None
            refund_ts = int(w3.eth.get_block(refund["blockNumber"])["timestamp"]) if refund else None
            deadline = int(o["args"]["deadline"])
            entry = {
                "orderId": oid,
                "found": True,
                "adverseEvidence": oid in adverse,
                "vault": vault_address,
                "payeeAgentId": str(o["args"]["payeeAgentId"]),
                "payeeMatches": int(o["args"]["payeeAgentId"]) == agent_id,
                "amount": str(o["args"]["amount"]),
                "openTx": o["transactionHash"],
                "deadline": deadline,
                "claimed": bool(claims),
                "claimedBeforeRefund": bool(claims)
                and (refund is None or claims[0]["blockNumber"] <= refund["blockNumber"]),
                "claimTx": claims[0]["transactionHash"] if claims else None,
                "refundTx": refund["transactionHash"] if refund else None,
                "refundReason": int(refund["args"]["reasonCode"]) if refund else None,
                "refundedAt": refund_ts,
                "refundAfterDeadline": bool(refund_ts is not None and refund_ts > deadline),
            }
            break
        if not entry["adverseEvidence"]:
            entry["note"] = "not adverse evidence"
        orders.append(entry)
    supports = [
        o
        for o in orders
        if o["found"]
        and o["adverseEvidence"]
        and o.get("payeeMatches")
        and (o["claimedBeforeRefund"] or (o["refundTx"] and not o["refundAfterDeadline"]))
    ]
    upheld = bool(supports)
    review = {
        "schema": "acb-appeal-review/1",
        "appealId": str(int(appeal_id)),
        "agentId": str(agent_id),
        "appealHash": str(event["args"]["appealHash"]).lower(),
        "appealTx": event["transactionHash"],
        "claim": str(payload.get("claim") or "")[:500],
        "orders": orders,
        "adverseEvidence": {"source": adverse_source, "orderIds": sorted(adverse)},
        "rule": (
            "upheld only if a named order that is adverse evidence against the appellant was claimed by the"
            " appellant before its refund, or refunded before its deadline; other named orders are ignored"
        ),
        "decision": "UPHELD" if upheld else "REJECTED",
        "reasonCode": REASON_UPHELD if upheld else REASON_REJECTED,
        "reviewedAtBlock": int(w3.eth.block_number),
    }
    digest = chash(review)
    folder = Path(out_dir) / "appeals" / "reviews"
    folder.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(folder / f"{digest}.json", canonical_bytes(review))
    return {"review": review, "evidenceHash": digest, "upheld": upheld, "file": str(folder / f"{digest}.json")}


def resolve_appeal(sender: Any, w3: Any, dep: Any, appeal_id: int, upheld: bool, evidence_hash: str) -> Any:
    """``ACB.resolveAppeal(appealId, upheld, evidenceHash)`` from the poster key (``sender``)."""
    from acb.chain.contracts import AcbContract

    acb = AcbContract(w3, dep.contracts["AgentCreditBureau"].address)
    return sender.send(acb.resolve_appeal(int(appeal_id), bool(upheld), evidence_hash))


__all__ = [
    "ADVERSE_OUTCOMES",
    "ADVERSE_REFUND_CODES",
    "REASON_REJECTED",
    "REASON_UPHELD",
    "AppealError",
    "adverse_orders",
    "latest_bundle",
    "load_appeal",
    "open_appeals",
    "resolve_appeal",
    "review_appeal",
]
