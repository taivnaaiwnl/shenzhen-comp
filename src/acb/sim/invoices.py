"""Wire objects the simulated agents sign (spec 9.2, 9.3, 9.5): invoices, delivery receipts and
payloads, inbox messages, report and appeal payloads.

Every object is built in its canonical form (camelCase keys, lowercase hex, amounts and agent ids as
decimal strings, plain JSON integers) and signed with EIP-191 over a 32-byte hash, exactly as the
wallet agent, the signer and the bureau verify it:

- invoice: ``signature`` = ``personal_sign(invoiceHash)``, ``invoiceHash = chash(invoice without
  signature)``; the issuer's registered ERC-8004 wallet signs.
- delivery receipt (``acb-delivery/1``): ``signature`` = ``personal_sign(chash(receipt without
  signature))``.
- inbox message (``acb-message/1``, defined here because the spec names no format): same scheme.

Payloads are synthetic (spec 9.3): ``{"data": [...]}`` with ``quantity x 10`` items per invoice line.
FX skus (``fx-*``) give ``{"pair", "rate", "seq"}`` items with the rate as a decimal string; API skus
(``api-*``) give ``"demo-key-<n>"``; ``ad-slot`` gives ``"ad-slot-<n>"``; any other sku
``"<sku>-<n>"``. ``n`` is derived from the orderId, so a payload is deterministic per order.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from eth_account.signers.local import LocalAccount
from web3 import Web3

from acb.canonical import chash, recover_eip191, sign_digest_eip191, sign_invoice, write_canonical
from acb.types import DELIVERY_SCHEMA, INVOICE_SCHEMA, SignedDelivery, SignedInvoice

MESSAGE_SCHEMA = "acb-message/1"
REPORT_SCHEMA = "acb-report/1"
APPEAL_SCHEMA = "acb-appeal/1"
ITEMS_PER_UNIT = 10

INVOICE_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,40}$")
QUOTE_REQUEST_ID_RE = re.compile(r"^qr-[0-9a-f]{32}$")

FX_PAIRS: tuple[tuple[str, int], ...] = (
    ("USD/CNY", 71_250),
    ("EUR/USD", 10_850),
    ("USD/JPY", 1_495_000),
    ("GBP/USD", 12_950),
    ("USD/MNT", 33_950_000),
)
"""FX pairs of the synthetic data with a base rate in units of 1e-4."""


def invoice_id_for(key: str, number: int) -> str:
    """``inv-<persona>-<NNNN>`` (spec 9.2 example ``inv-orion-0006``)."""
    invoice_id = f"inv-{key}-{number:04d}"
    if not INVOICE_ID_RE.fullmatch(invoice_id):
        raise ValueError(f"invoice id {invoice_id!r} does not match {INVOICE_ID_RE.pattern}")
    return invoice_id


def invoice_items(lines: Iterable[tuple[str, str, int, int]]) -> list[dict[str, Any]]:
    """Wire items from ``(sku, description, unit_price, quantity)`` tuples."""
    return [
        {"sku": sku, "description": description, "unitPrice": str(int(unit)), "quantity": int(quantity)}
        for sku, description, unit, quantity in lines
    ]


def build_invoice(
    *,
    invoice_id: str,
    quote_request_id: str,
    cap_of: str,
    issuer_agent_id: int,
    buyer_agent_id: int,
    buyer_vault: str,
    chain_id: int,
    token: str,
    items: list[dict[str, Any]],
    issued_at: int,
    expires_at: int,
    delivery_deadline: int,
    memo: str,
) -> dict[str, Any]:
    """The unsigned ``acb-invoice/1`` wire object of spec 9.2 (``amount`` = sum of the lines)."""
    amount = sum(int(i["unitPrice"]) * int(i["quantity"]) for i in items)
    return {
        "schema": INVOICE_SCHEMA,
        "invoiceId": invoice_id,
        "quoteRequestId": quote_request_id,
        "capOf": cap_of,
        "issuerAgentId": str(int(issuer_agent_id)),
        "buyerAgentId": str(int(buyer_agent_id)),
        "buyerVault": buyer_vault.lower(),
        "chainId": int(chain_id),
        "token": token.lower(),
        "items": items,
        "amount": str(amount),
        "issuedAt": int(issued_at),
        "expiresAt": int(expires_at),
        "deliveryDeadline": int(delivery_deadline),
        "memo": memo,
    }


def sign_wire_invoice(account: LocalAccount, invoice: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """Sign an invoice with its issuer's wallet; returns ``(signed wire dict, invoiceHash)``.

    The result is parsed back with ``SignedInvoice.from_wire`` (canonical form only) and its
    signature recovered, so a malformed invoice never leaves the simulator.
    """
    signed = sign_invoice(account, invoice)
    parsed = SignedInvoice.from_wire(signed)
    if recover_eip191(parsed.invoice_hash, parsed.signature) != account.address.lower():
        raise ValueError("invoice signature does not recover to the issuer")  # pragma: no cover
    return signed, parsed.invoice_hash


def publish_invoice(out_dir: str | os.PathLike[str], signed: Mapping[str, Any]) -> tuple[str, Path]:
    """Write a signed invoice to ``<out_dir>/invoices/<invoiceHash>.json`` (canonical bytes, atomic).

    The file holds the signed wire object; its ``chash`` without ``signature`` is the invoiceHash the
    bureau looks for (spec 4.1 rule 3).
    """
    invoice_hash = SignedInvoice.from_wire(dict(signed)).invoice_hash
    path = Path(out_dir) / "invoices" / f"{invoice_hash}.json"
    write_canonical(path, dict(signed))
    return invoice_hash, path


# ---------------------------------------------------------------------------
# delivery
# ---------------------------------------------------------------------------


def _seed(order_id: str, index: int) -> int:
    return int.from_bytes(Web3.keccak(text=f"{order_id.lower()}:{index}")[:6], "big")


def _rate(base: int, seed: int) -> str:
    """Base rate (1e-4 units) moved by at most +-0.5 %, as a decimal string with 4 decimals."""
    spread = base // 200
    value = base - spread + seed % (2 * spread + 1) if spread else base
    whole, frac = divmod(value, 10_000)
    return f"{whole}.{frac:04d}"


def payload_items(sku: str, quantity: int, order_id: str, start: int = 0) -> list[Any]:
    """``quantity x 10`` synthetic items of one sku (see the module docstring)."""
    out: list[Any] = []
    for i in range(start, start + int(quantity) * ITEMS_PER_UNIT):
        seed = _seed(order_id, i)
        if sku.startswith("fx-"):
            pair, base = FX_PAIRS[i % len(FX_PAIRS)]
            out.append({"pair": pair, "rate": _rate(base, seed), "seq": i + 1})
        elif sku.startswith("api-"):
            out.append(f"demo-key-{seed}")
        elif sku == "ad-slot":
            out.append(f"ad-slot-{seed}")
        else:
            out.append(f"{sku}-{seed}")
    return out


def build_payload(invoice: Mapping[str, Any], order_id: str) -> dict[str, Any]:
    """The delivery payload ``{"data": [...]}`` for every line of a wire invoice."""
    data: list[Any] = []
    for item in invoice["items"]:
        data.extend(payload_items(item["sku"], int(item["quantity"]), order_id, start=len(data)))
    return {"data": data}


def payload_hash(payload: Mapping[str, Any]) -> str:
    """``chash(payload)``: the ``deliveryHash`` of the on-chain claim and the receipt's ``payloadHash``."""
    return chash(dict(payload))


def build_receipt(
    account: LocalAccount,
    *,
    order_id: str,
    invoice_id: str,
    issuer_agent_id: int,
    payload_hash_hex: str,
    delivered_at: int,
) -> dict[str, Any]:
    """A signed ``acb-delivery/1`` receipt (spec 9.3), checked with ``SignedDelivery.from_wire``."""
    body = {
        "schema": DELIVERY_SCHEMA,
        "orderId": order_id.lower(),
        "invoiceId": invoice_id,
        "issuerAgentId": str(int(issuer_agent_id)),
        "payloadHash": payload_hash_hex.lower(),
        "deliveredAt": int(delivered_at),
    }
    receipt = {**body, "signature": sign_digest_eip191(account, chash(body))}
    SignedDelivery.from_wire(receipt)
    return receipt


def verify_receipt(receipt: Mapping[str, Any], payload: Mapping[str, Any], *, signer: str, order_id: str) -> str:
    """Check a receipt and its payload; returns the payload hash. Raises ``ValueError`` if anything differs."""
    parsed = SignedDelivery.from_wire(dict(receipt))
    if parsed.order_id != order_id.lower():
        raise ValueError("receipt is for another order")
    digest = payload_hash(payload)
    if parsed.payload_hash != digest:
        raise ValueError("payload does not match the receipt's payloadHash")
    if recover_eip191(chash(parsed.unsigned_wire_dict()), parsed.signature) != signer.lower():
        raise ValueError("receipt not signed by the provider's wallet")
    return digest


# ---------------------------------------------------------------------------
# inbox messages, reports, appeals
# ---------------------------------------------------------------------------


def build_message(
    account: LocalAccount,
    *,
    message_id: str,
    from_agent_id: int,
    to_agent_id: int,
    sent_at: int,
    text: str,
) -> dict[str, Any]:
    """A signed ``acb-message/1`` inbox message: EIP-191 over ``chash`` of the message without signature."""
    if not INVOICE_ID_RE.fullmatch(message_id):
        raise ValueError("message id must match ^[A-Za-z0-9-]{1,40}$")
    body = {
        "schema": MESSAGE_SCHEMA,
        "messageId": message_id,
        "fromAgentId": str(int(from_agent_id)),
        "toAgentId": str(int(to_agent_id)),
        "sentAt": int(sent_at),
        "text": text,
    }
    return {**body, "signature": sign_digest_eip191(account, chash(body))}


def recover_message_signer(message: Mapping[str, Any]) -> str:
    """Address (lowercase) that signed an ``acb-message/1`` message."""
    body = {k: v for k, v in message.items() if k != "signature"}
    return recover_eip191(chash(body), message["signature"])


def report_payload(
    *,
    kind: str,
    subject_agent_id: int,
    reporter_agent_id: int,
    order_id: str,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """An ``acb-report/1`` payload (spec 9.5); its ``chash`` is the feedbackHash."""
    return {
        "schema": REPORT_SCHEMA,
        "kind": kind,
        "subjectAgentId": str(int(subject_agent_id)),
        "reporterAgentId": str(int(reporter_agent_id)),
        "orderId": order_id.lower(),
        "evidence": dict(evidence),
    }


def report_endpoint(order_id: str, reporter_agent_id: int, vault: str) -> str:
    """``acb:v1;order=<0x orderId>;reporter=<agentId>;vault=<0x vault>`` (spec 4.1)."""
    return f"acb:v1;order={order_id.lower()};reporter={int(reporter_agent_id)};vault={vault.lower()}"


def appeal_payload(*, agent_id: int, claim: str, order_ids: Iterable[str], filed_at: int) -> dict[str, Any]:
    """An ``acb-appeal/1`` payload (spec 9.5, S20); its ``chash`` is the appealHash."""
    return {
        "schema": APPEAL_SCHEMA,
        "agentId": str(int(agent_id)),
        "claim": claim,
        "orderIds": sorted(o.lower() for o in order_ids),
        "filedAt": int(filed_at),
    }


def evidence_uri(base: str, kind: str, digest: str) -> str:
    """``<EVIDENCE_BASE_URI><kind>/<hash>.json`` with exactly one slash between the parts."""
    base = base or ""
    if base and not base.endswith("/"):
        base += "/"
    return f"{base}{kind}/{digest}.json"


__all__ = [
    "APPEAL_SCHEMA",
    "ITEMS_PER_UNIT",
    "MESSAGE_SCHEMA",
    "QUOTE_REQUEST_ID_RE",
    "REPORT_SCHEMA",
    "appeal_payload",
    "build_invoice",
    "build_message",
    "build_payload",
    "build_receipt",
    "evidence_uri",
    "invoice_id_for",
    "invoice_items",
    "payload_hash",
    "payload_items",
    "publish_invoice",
    "recover_message_signer",
    "report_endpoint",
    "report_payload",
    "sign_wire_invoice",
    "verify_receipt",
]
