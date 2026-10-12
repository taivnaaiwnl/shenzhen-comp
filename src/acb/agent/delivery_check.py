"""Deterministic delivery payload check (spec 8.5, 9.3, 7.3 RELEASE / RECORD_DELIVERY).

A delivery payload never reaches the LLM (only its hash and size); before the agent asks the signer to
release an escrow or to record a delivery it checks, by code only:

1. the receipt is a canonical ``acb-delivery/1`` object for **this** order and invoice, issued by the
   invoice's issuer and signed (EIP-191 over ``chash`` of the receipt without ``signature``) by the
   wallet the order pays (the pinned wallet stored on-chain);
2. ``payloadHash == chash(payload)``; for an escrow it must also equal the ``deliveryHash`` of the
   provider's on-chain claim;
3. the payload has exactly the shape the catalogue promises: ``{"data": [...]}`` with
   ``quantity x 10`` items per invoice line, and per sku family:

   - ``fx-*``: ``{"pair": "AAA/BBB", "rate": "<decimal string>", "seq": <int>}``
   - ``api-*``: ``"demo-key-<digits>"``
   - ``ad-slot``: ``"ad-slot-<digits>"``
   - any other sku: ``"<sku>-<digits>"``

The check is a pure function of its inputs and returns a :class:`DeliveryCheck`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from acb.canonical import canonical_bytes, chash, recover_eip191
from acb.types import Invoice, SignedDelivery

ITEMS_PER_UNIT = 10
MAX_PAYLOAD_BYTES = 256 * 1024
_PAIR = re.compile(r"^[A-Z]{3}/[A-Z]{3}$")
_RATE = re.compile(r"^[0-9]{1,9}\.[0-9]{1,8}$")


@dataclass(frozen=True)
class DeliveryCheck:
    """Outcome of :func:`check_delivery`."""

    ok: bool
    reason: str
    payload_hash: str | None = None
    size: int = 0
    receipt: SignedDelivery | None = None


def _item_ok(sku: str, item: Any) -> bool:
    if sku.startswith("fx-"):
        return (
            isinstance(item, dict)
            and set(item) == {"pair", "rate", "seq"}
            and isinstance(item["pair"], str)
            and _PAIR.fullmatch(item["pair"]) is not None
            and isinstance(item["rate"], str)
            and _RATE.fullmatch(item["rate"]) is not None
            and isinstance(item["seq"], int)
            and not isinstance(item["seq"], bool)
            and item["seq"] >= 1
        )
    if sku.startswith("api-"):
        return isinstance(item, str) and re.fullmatch(r"demo-key-[0-9]{1,20}", item) is not None
    prefix = "ad-slot" if sku == "ad-slot" else sku
    return isinstance(item, str) and re.fullmatch(re.escape(prefix) + r"-[0-9]{1,20}", item) is not None


def check_payload_shape(invoice: Invoice, payload: Any) -> str | None:
    """None when ``payload`` has the promised shape, else the reason."""
    if not isinstance(payload, Mapping) or set(payload) != {"data"} or not isinstance(payload["data"], list):
        return 'payload must be {"data": [...]}'
    data = payload["data"]
    expected = sum(int(i.quantity) * ITEMS_PER_UNIT for i in invoice.items)
    if len(data) != expected:
        return f"payload has {len(data)} items, expected {expected}"
    pos = 0
    for line in invoice.items:
        for k in range(int(line.quantity) * ITEMS_PER_UNIT):
            if not _item_ok(line.sku, data[pos + k]):
                return f"item {pos + k} does not match sku {line.sku}"
        pos += int(line.quantity) * ITEMS_PER_UNIT
    return None


def check_delivery(
    *,
    receipt_raw: Any,
    payload: Any,
    invoice: Invoice,
    order_id: str,
    payee_wallet: str,
    claimed_hash: str | None = None,
) -> DeliveryCheck:
    """Run the full check of the module docstring."""
    try:
        receipt = SignedDelivery.from_wire(dict(receipt_raw)) if isinstance(receipt_raw, Mapping) else None
    except (ValueError, TypeError):
        receipt = None
    if receipt is None:
        return DeliveryCheck(False, "receipt is not a canonical acb-delivery/1 object")
    if receipt.order_id != order_id.lower():
        return DeliveryCheck(False, "receipt is for another order", receipt=receipt)
    if receipt.invoice_id != invoice.invoice_id or int(receipt.issuer_agent_id) != int(invoice.issuer_agent_id):
        return DeliveryCheck(False, "receipt is for another invoice or issuer", receipt=receipt)
    try:
        signer = recover_eip191(chash(receipt.unsigned_wire_dict()), receipt.signature)
    except ValueError:
        return DeliveryCheck(False, "bad receipt signature", receipt=receipt)
    if not payee_wallet or signer != payee_wallet.lower():
        return DeliveryCheck(False, "receipt not signed by the payee's pinned wallet", receipt=receipt)
    try:
        body = canonical_bytes(payload)
    except TypeError:
        return DeliveryCheck(False, "payload is not canonical JSON", receipt=receipt)
    if len(body) > MAX_PAYLOAD_BYTES:
        return DeliveryCheck(False, "payload too large", receipt=receipt, size=len(body))
    digest = chash(payload)
    if digest != receipt.payload_hash:
        return DeliveryCheck(False, "payload hash differs from the receipt", digest, len(body), receipt)
    if claimed_hash is not None and claimed_hash.lower() != digest:
        return DeliveryCheck(False, "payload hash differs from the on-chain claim", digest, len(body), receipt)
    problem = check_payload_shape(invoice, payload)
    if problem is not None:
        return DeliveryCheck(False, problem, digest, len(body), receipt)
    return DeliveryCheck(True, "ok", digest, len(body), receipt)


__all__ = ["ITEMS_PER_UNIT", "DeliveryCheck", "check_delivery", "check_payload_shape"]
