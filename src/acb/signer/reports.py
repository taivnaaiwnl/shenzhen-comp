"""Reports filed by the wallet (spec 7.3 REPORT, 9.5, 4.1-4.2): the signer decides whether the evidence
suffices; the agent cannot choose a report freely.

==================  ==========================================================================
Kind                Evidence the signer requires
==================  ==========================================================================
INJECTION_ATTEMPT   our vault has ``DecisionRecorded(REFUSE)`` for the order with this invoice's hash
                    and the subject as payee, the invoice is signed by the subject's live wallet, and
                    the signer's own detector run on the signed invoice is ``adverse``
NON_DELIVERY        our order with the subject as payee has outcome FAILED
LATE_DELIVERY       our order with the subject as payee has outcome LATE (informational)
SPAM                at least 3 distinct invoices signed by the subject's live wallet, each with an empty
                    ``quoteRequestId`` and addressed to our vault and agent
BAD_DATA            an EIP-191 signature of the vault owner over the report payload hash
==================  ==========================================================================

The payload is canonical JSON ``acb-report/1``; ``feedbackHash = chash(payload)``; the file is written to
``$ACB_OUT_DIR/reports/<feedbackHash>.json``. The owner's signature is checked but never stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from acb.canonical import chash, invoice_hash, order_id, recover_eip191, recover_invoice_signer
from acb.injection import DETECTOR_ID, detect, invoice_fields
from acb.types import Decision, Outcome, ReportRequest

REPORT_SCHEMA = "acb-report/1"
REPORT_TAG = "acb-report"
ZERO_HASH = "0x" + "00" * 32
SPAM_MIN_INVOICES = 3


class ReportRejected(ValueError):
    """The evidence does not support the report; nothing is sent."""


@dataclass(frozen=True)
class ReportPlan:
    """A verified report, ready for ``giveFeedback``."""

    kind: str
    subject_agent_id: int
    order_id: str
    payload: dict[str, Any]
    endpoint: str


def endpoint_for(order: str | None, reporter_agent_id: int, vault: str) -> str:
    """``acb:v1;order=<0x orderId>;reporter=<agentId>;vault=<0x vault>`` (4.1)."""
    return f"acb:v1;order={order or ZERO_HASH};reporter={int(reporter_agent_id)};vault={vault.lower()}"


def report_payload(
    kind: str, subject_agent_id: int, reporter_agent_id: int, order: str | None, evidence: dict[str, Any], detector=None
) -> dict[str, Any]:
    """The canonical ``acb-report/1`` payload (9.5). The owner console uses the same function to compute
    the hash it signs for BAD_DATA."""
    payload: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "kind": kind,
        "subjectAgentId": str(int(subject_agent_id)),
        "reporterAgentId": str(int(reporter_agent_id)),
        "orderId": order or ZERO_HASH,
        "evidence": evidence,
    }
    if detector is not None:
        payload["detector"] = detector
    return payload


def bad_data_payload(subject_agent_id: int, reporter_agent_id: int, order: str, vault: str) -> dict[str, Any]:
    return report_payload(
        "BAD_DATA",
        subject_agent_id,
        reporter_agent_id,
        order,
        {"type": "owner_confirmation", "vault": vault.lower(), "orderId": order},
    )


def plan_report(
    req: ReportRequest, *, view: Any, block: int, chain_id: int, vault: str, reporter_agent_id: int
) -> ReportPlan:
    """Verify the evidence for ``req`` on fresh chain state; raise :class:`ReportRejected` if it fails."""
    subject = int(req.subject_agent_id)
    kind = req.kind
    vault = vault.lower()
    live = view.live_wallet(subject, block)

    if kind == "INJECTION_ATTEMPT":
        if req.invoice is None or req.order_id is None:
            raise ReportRejected("INJECTION_ATTEMPT needs the signed invoice and the order id")
        inv = req.invoice.invoice
        wire = req.invoice.wire_dict()
        if int(inv.issuer_agent_id) != subject:
            raise ReportRejected("the invoice was not issued by the subject")
        if order_id(chain_id, vault, subject, inv.invoice_id) != req.order_id:
            raise ReportRejected("the order id is not derived from the invoice")
        try:
            signer = recover_invoice_signer(wire)
        except ValueError as exc:
            raise ReportRejected("bad invoice signature") from exc
        if live is None or signer != live:
            raise ReportRejected("the invoice is not signed by the subject's agent wallet")
        ihash = invoice_hash(wire)
        records = view.decision_records(req.order_id, block)
        if not any(
            r["decision"] == int(Decision.REFUSE) and r["invoiceHash"] == ihash and r["payeeAgentId"] == subject
            for r in records
        ):
            raise ReportRejected("no DecisionRecorded(REFUSE) with this invoice hash in our vault")
        result = detect(invoice_fields(wire))
        if not result.adverse:
            raise ReportRejected("the signer's own detector does not find the invoice adverse")
        detector = {"id": DETECTOR_ID, "hits": [h.to_wire() for h in result.hits]}
        evidence = {"type": "signed_invoice", "invoice": wire}
        payload = report_payload(kind, subject, reporter_agent_id, req.order_id, evidence, detector)
        return ReportPlan(kind, subject, req.order_id, payload, endpoint_for(req.order_id, reporter_agent_id, vault))

    if kind in ("NON_DELIVERY", "LATE_DELIVERY"):
        if req.order_id is None:
            raise ReportRejected(f"{kind} needs an order id")
        order = view.order(req.order_id, block)
        if int(order.payee_agent_id) != subject:
            raise ReportRejected("the subject is not the payee of this order")
        wanted = Outcome.FAILED if kind == "NON_DELIVERY" else Outcome.LATE
        if order.outcome != wanted:
            raise ReportRejected(f"the order outcome is {Outcome(order.outcome).name}, not {wanted.name}")
        evidence = {"type": "order_outcome", "vault": vault, "orderId": req.order_id, "outcome": wanted.name}
        payload = report_payload(kind, subject, reporter_agent_id, req.order_id, evidence)
        return ReportPlan(kind, subject, req.order_id, payload, endpoint_for(req.order_id, reporter_agent_id, vault))

    if kind == "SPAM":
        hashes: dict[str, dict[str, Any]] = {}
        for si in req.spam_invoices:
            inv = si.invoice
            wire = si.wire_dict()
            if int(inv.issuer_agent_id) != subject or inv.quote_request_id != "":
                continue
            if inv.buyer_vault != vault or int(inv.buyer_agent_id) != int(reporter_agent_id):
                continue
            try:
                if live is None or recover_invoice_signer(wire) != live:
                    continue
            except ValueError:
                continue
            hashes[invoice_hash(wire)] = wire
        if len(hashes) < SPAM_MIN_INVOICES:
            raise ReportRejected(f"SPAM needs {SPAM_MIN_INVOICES} distinct signed unsolicited invoices")
        evidence = {"type": "signed_invoices", "invoices": [hashes[h] for h in sorted(hashes)]}
        payload = report_payload(kind, subject, reporter_agent_id, None, evidence)
        return ReportPlan(kind, subject, ZERO_HASH, payload, endpoint_for(None, reporter_agent_id, vault))

    if kind == "BAD_DATA":
        if req.order_id is None or req.owner_signature is None:
            raise ReportRejected("BAD_DATA needs the order id and the owner's signature")
        order = view.order(req.order_id, block)
        if int(order.payee_agent_id) != subject:
            raise ReportRejected("the subject is not the payee of this order")
        payload = bad_data_payload(subject, reporter_agent_id, req.order_id, vault)
        try:
            signer = recover_eip191(chash(payload), req.owner_signature)
        except ValueError as exc:
            raise ReportRejected("bad owner signature") from exc
        if signer != view.vault.owner(block=block):
            raise ReportRejected("the report is not signed by the vault owner")
        return ReportPlan(kind, subject, req.order_id, payload, endpoint_for(req.order_id, reporter_agent_id, vault))

    raise ReportRejected(f"unknown report kind {kind}")


__all__ = [
    "REPORT_SCHEMA",
    "REPORT_TAG",
    "SPAM_MIN_INVOICES",
    "ReportPlan",
    "ReportRejected",
    "bad_data_payload",
    "endpoint_for",
    "plan_report",
    "report_payload",
]
