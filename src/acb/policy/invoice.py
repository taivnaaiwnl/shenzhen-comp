"""Stage 1 of the policy: invoice validation V01-V09 (spec 6.2), run before any LLM sees an invoice.

:func:`validate_invoice` is pure (no I/O, no clock, no randomness). The first failing rule rejects the
invoice with code 2119 (INVOICE_REJECTED) and a sub-reason (the rule id and a short detail); an
expired invoice is rejected with 2117. A rejected invoice is dropped off-chain. A signed invoice that
fails V08 because it answers no open quote request, but carries a valid signature of the issuer's live
wallet, is flagged ``keep_as_spam_evidence`` (the agent stores it under
``$ACB_OUT_DIR/agent/spam_evidence/<issuer>/<invoiceHash>.json``; the signer files SPAM from three).

=====  ===========  ===========================================================================
Rule   Name         Rejects when
=====  ===========  ===========================================================================
V01    SHAPE        body > 16 KiB; schema != acb-invoice/1; unknown or missing fields; wrong JSON
                    types; strings other than ``memo`` and ``items[].description`` not printable
                    ASCII; agent ids, addresses or the signature not in canonical form
V02    IDS          ``invoiceId`` not ``^[A-Za-z0-9-]{1,40}$``; ``quoteRequestId`` not
                    ``^qr-[0-9a-f]{32}$`` or ""; ``capOf`` not "" or an invoice id
V03    AMOUNTS      amounts not ``^[0-9]{1,12}$`` (canonical, no leading zero); not a multiple of
                    0.01 USDC; quantity not 1..1000; items not 1..6; amount != sum; amount < 0.10
V04    CATALOGUE    sku not in the issuer's pinned catalogue; unit price differs; description > 80
                    characters; memo > 500 characters
V05    CHAIN_TOKEN  chainId or token differ from the network's
V06    TIMES        issued in the future (> 30 s); validity > 900 s; expired (2117); delivery
                    deadline before issue or more than ``max_delivery_s`` after it
V07    BUYER        buyerAgentId / buyerVault are not our vault's
V08    SOLICITED    no outstanding, unused quote request with the same issuer, sku and quantity;
                    for a cap re-quote: no superseded invoice or quantity != q'
V09    SIGNATURE    the EIP-191 signature over invoiceHash does not recover to the wallet pinned in
                    the quote request's mandate, or that wallet differs from the live agent wallet
=====  ===========  ===========================================================================

The signature check in V09 uses :func:`acb.canonical.recover_invoice_signer`, which rejects malleable
signatures. After V04 the invoice is parsed with :meth:`acb.types.SignedInvoice.from_wire`, which
accepts only the canonical wire form (changes.md D-12); a failure there is a V01 rejection.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from acb.canonical import atomic_write_bytes, canonical_bytes, invoice_hash, recover_invoice_signer
from acb.injection import detect, invoice_fields
from acb.policy.config import PolicyConfig
from acb.types import (
    INVOICE_SCHEMA,
    InvoiceRejection,
    QuoteRequest,
    SignedInvoice,
    ValidatedInvoice,
    ValidationContext,
)

CODE_INVOICE_REJECTED = 2119
CODE_INVOICE_EXPIRED = 2117

INVOICE_KEYS = frozenset(
    {
        "schema",
        "invoiceId",
        "quoteRequestId",
        "capOf",
        "issuerAgentId",
        "buyerAgentId",
        "buyerVault",
        "chainId",
        "token",
        "items",
        "amount",
        "issuedAt",
        "expiresAt",
        "deliveryDeadline",
        "memo",
        "signature",
    }
)
ITEM_KEYS = frozenset({"sku", "description", "unitPrice", "quantity"})
_STR_FIELDS = (
    "schema",
    "invoiceId",
    "quoteRequestId",
    "capOf",
    "issuerAgentId",
    "buyerAgentId",
    "buyerVault",
    "token",
    "amount",
    "memo",
    "signature",
)
_INT_FIELDS = ("chainId", "issuedAt", "expiresAt", "deliveryDeadline")
_FREE_TEXT = frozenset({"memo"})  # plus items[].description

INVOICE_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,40}$")
QUOTE_REQUEST_ID_RE = re.compile(r"^qr-[0-9a-f]{32}$")
AMOUNT_RE = re.compile(r"^[0-9]{1,12}$")
AGENT_ID_RE = re.compile(r"^(0|[1-9][0-9]{0,76})$")
ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
SIGNATURE_RE = re.compile(r"^0x[0-9a-f]{130}$")
MAX_QUANTITY = 1000
MAX_ITEMS = 6
MAX_JSON_INT = 2**53


class _Reject(Exception):
    def __init__(self, rule: str, detail: str, code: int = CODE_INVOICE_REJECTED):
        super().__init__(detail)
        self.rule = rule
        self.detail = detail
        self.code = code


def _printable_ascii(text: str) -> bool:
    return all(0x20 <= ord(ch) <= 0x7E for ch in text)


def superseded_key(issuer_agent_id: int, invoice_id: str) -> str:
    """Key of ``ValidationContext.superseded``: ``"<issuerAgentId>:<invoiceId>"`` -> q' (6.5)."""
    return f"{int(issuer_agent_id)}:{invoice_id}"


def _safe_hash(raw: Any) -> str | None:
    try:
        return invoice_hash(raw) if isinstance(raw, Mapping) else None
    except (TypeError, ValueError):
        return None


def _parse_body(raw: Any, max_bytes: int) -> dict[str, Any]:
    """Accept a dict, or JSON text/bytes (then the size is the size of the received body)."""
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > max_bytes:
            raise _Reject("V01", f"body larger than {max_bytes} bytes")
        try:
            raw = json.loads(bytes(raw).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise _Reject("V01", "body is not UTF-8 JSON") from exc
    elif isinstance(raw, str):
        if len(raw.encode("utf-8")) > max_bytes:
            raise _Reject("V01", f"body larger than {max_bytes} bytes")
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise _Reject("V01", "body is not JSON") from exc
    if not isinstance(raw, dict):
        raise _Reject("V01", "invoice must be a JSON object")
    try:
        size = len(canonical_bytes(raw))
    except TypeError as exc:
        raise _Reject("V01", f"not canonical JSON: {exc}") from None
    if size > max_bytes:
        raise _Reject("V01", f"body larger than {max_bytes} bytes")
    return raw


def _v01_shape(raw: dict[str, Any]) -> None:
    if raw.get("schema") != INVOICE_SCHEMA:
        raise _Reject("V01", f"schema must be {INVOICE_SCHEMA}")
    keys = set(raw)
    if keys != INVOICE_KEYS:
        missing = sorted(INVOICE_KEYS - keys)
        unknown = sorted(keys - INVOICE_KEYS)
        raise _Reject("V01", f"fields: missing {missing}, unknown {unknown}")
    for name in _STR_FIELDS:
        if not isinstance(raw[name], str):
            raise _Reject("V01", f"{name} must be a string")
        if name not in _FREE_TEXT and not _printable_ascii(raw[name]):
            raise _Reject("V01", f"{name} is not printable ASCII")
    for name in _INT_FIELDS:
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_JSON_INT:
            raise _Reject("V01", f"{name} must be a non-negative JSON integer")
    items = raw["items"]
    if not isinstance(items, list):
        raise _Reject("V01", "items must be a list")
    for i, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != ITEM_KEYS:
            raise _Reject("V01", f"items[{i}] must have exactly {sorted(ITEM_KEYS)}")
        for key in ("sku", "description", "unitPrice"):
            if not isinstance(item[key], str):
                raise _Reject("V01", f"items[{i}].{key} must be a string")
        if not _printable_ascii(item["sku"]) or not _printable_ascii(item["unitPrice"]):
            raise _Reject("V01", f"items[{i}] sku/unitPrice not printable ASCII")
        quantity = item["quantity"]
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise _Reject("V01", f"items[{i}].quantity must be a JSON integer")
    for name in ("issuerAgentId", "buyerAgentId"):
        if not AGENT_ID_RE.fullmatch(raw[name]):
            raise _Reject("V01", f"{name} must be a canonical decimal string")
    for name in ("buyerVault", "token"):
        if not ADDRESS_RE.fullmatch(raw[name]):
            raise _Reject("V01", f"{name} must be a lowercase 0x address")
    if not SIGNATURE_RE.fullmatch(raw["signature"]):
        raise _Reject("V01", "signature must be 65 bytes of lowercase 0x hex")


def _v02_ids(raw: dict[str, Any]) -> None:
    if not INVOICE_ID_RE.fullmatch(raw["invoiceId"]):
        raise _Reject("V02", "invoiceId must match ^[A-Za-z0-9-]{1,40}$")
    qr = raw["quoteRequestId"]
    if qr != "" and not QUOTE_REQUEST_ID_RE.fullmatch(qr):
        raise _Reject("V02", "quoteRequestId must be qr-<32 hex> or empty")
    cap_of = raw["capOf"]
    if cap_of != "" and not INVOICE_ID_RE.fullmatch(cap_of):
        raise _Reject("V02", "capOf must be empty or an invoice id")


def _amount(text: str, what: str, step: int) -> int:
    if not AMOUNT_RE.fullmatch(text) or (len(text) > 1 and text[0] == "0"):
        raise _Reject("V03", f"{what} must be a canonical decimal string of at most 12 digits")
    value = int(text)
    if value % step:
        raise _Reject("V03", f"{what} is not a multiple of {step} base units")
    return value


def _v03_amounts(raw: dict[str, Any], cfg: PolicyConfig) -> None:
    step = cfg.invoice.amount_step
    items = raw["items"]
    if not 1 <= len(items) <= MAX_ITEMS:
        raise _Reject("V03", f"items must have 1..{MAX_ITEMS} entries")
    total = 0
    for i, item in enumerate(items):
        unit = _amount(item["unitPrice"], f"items[{i}].unitPrice", step)
        quantity = item["quantity"]
        if not 1 <= quantity <= MAX_QUANTITY:
            raise _Reject("V03", f"items[{i}].quantity must be 1..{MAX_QUANTITY}")
        total += unit * quantity
    amount = _amount(raw["amount"], "amount", step)
    if amount != total:
        raise _Reject("V03", "amount is not the sum of unitPrice * quantity")
    if amount < cfg.invoice.min_amount:
        raise _Reject("V03", f"amount below the minimum {cfg.invoice.min_amount}")


def _v04_catalogue(raw: dict[str, Any], vctx: ValidationContext, cfg: PolicyConfig) -> None:
    issuer = int(raw["issuerAgentId"])
    catalogue = vctx.catalogue.get(issuer)
    if not catalogue:
        raise _Reject("V04", "issuer has no pinned catalogue")
    for i, item in enumerate(raw["items"]):
        sku = item["sku"]
        if sku not in catalogue:
            raise _Reject("V04", f"items[{i}].sku is not in the issuer's catalogue")
        if int(item["unitPrice"]) != int(catalogue[sku]):
            raise _Reject("V04", f"items[{i}].unitPrice differs from the catalogue price")
        if len(item["description"]) > cfg.invoice.max_description:
            raise _Reject("V04", f"items[{i}].description longer than {cfg.invoice.max_description} characters")
    if len(raw["memo"]) > cfg.invoice.max_memo:
        raise _Reject("V04", f"memo longer than {cfg.invoice.max_memo} characters")


def _parse_canonical(raw: dict[str, Any]) -> SignedInvoice:
    try:
        return SignedInvoice.from_wire(raw)
    except ValueError as exc:
        raise _Reject("V01", f"not the canonical wire form: {str(exc).splitlines()[0][:120]}") from None


def _v05_chain_token(raw: dict[str, Any], vctx: ValidationContext) -> None:
    if raw["chainId"] != vctx.chain_id:
        raise _Reject("V05", "chainId is not this network's")
    if raw["token"] != vctx.usdc.lower():
        raise _Reject("V05", "token is not this network's USDC")


def _v06_times(raw: dict[str, Any], vctx: ValidationContext, cfg: PolicyConfig, check_expiry: bool) -> None:
    now = vctx.now
    issued, expires, deadline = raw["issuedAt"], raw["expiresAt"], raw["deliveryDeadline"]
    if issued > now + cfg.invoice.max_future_s:
        raise _Reject("V06", "issuedAt is in the future")
    if expires <= issued or expires - issued > cfg.invoice.max_validity_s:
        raise _Reject("V06", f"validity must be 1..{cfg.invoice.max_validity_s} s")
    if check_expiry and now > expires:
        raise _Reject("V06", "invoice expired", CODE_INVOICE_EXPIRED)
    if deadline < issued or deadline - issued > cfg.invoice.max_delivery_s:
        raise _Reject("V06", f"deliveryDeadline must be within {cfg.invoice.max_delivery_s} s of issuedAt")


def _v07_buyer(raw: dict[str, Any], vctx: ValidationContext) -> None:
    if int(raw["buyerAgentId"]) != int(vctx.vault_agent_id):
        raise _Reject("V07", "buyerAgentId is not our agent")
    if raw["buyerVault"] != vctx.vault_address.lower():
        raise _Reject("V07", "buyerVault is not our vault")


def _v08_solicited(raw: dict[str, Any], vctx: ValidationContext, ihash: str, check_open: bool) -> QuoteRequest:
    qr_id = raw["quoteRequestId"]
    issuer = int(raw["issuerAgentId"])
    if qr_id == "":
        raise _Reject("V08", "unsolicited: no quoteRequestId")
    matches = [q for q in vctx.quote_requests if q.quote_request_id == qr_id]
    if not matches:
        raise _Reject("V08", "answers no quote request of this agent")
    qr = matches[0]
    if int(qr.issuer_agent_id) != issuer:
        raise _Reject("V08", "quote request was sent to another issuer")
    if check_open:
        if qr.used_by is not None and qr.used_by != ihash:
            raise _Reject("V08", "quote request already used by another invoice")
        if vctx.now > qr.closes_at:
            raise _Reject("V08", "quote request closed")
    if qr.cap_of != raw["capOf"]:
        raise _Reject("V08", "capOf differs from the quote request")
    items = raw["items"]
    if len(items) != 1 or items[0]["sku"] != qr.sku or items[0]["quantity"] != qr.quantity:
        raise _Reject("V08", "sku or quantity differ from the quote request")
    if raw["capOf"] != "":
        key = superseded_key(issuer, raw["capOf"])
        if key not in vctx.superseded:
            raise _Reject("V08", "cap re-quote of an invoice that was not superseded")
        if items[0]["quantity"] != int(vctx.superseded[key]):
            raise _Reject("V08", "cap re-quote quantity differs from q'")
    return qr


def signature_valid(raw: Mapping[str, Any], vctx: ValidationContext) -> bool:
    """V09: signer == the wallet pinned for the issuer (quote request's mandate) == the live wallet."""
    try:
        issuer = int(raw["issuerAgentId"])
        signer = recover_invoice_signer(raw)
    except (KeyError, TypeError, ValueError):
        return False
    pinned = vctx.pinned_wallets.get(issuer)
    live = vctx.live_wallets.get(issuer)
    return pinned is not None and live is not None and signer == pinned.lower() == live.lower()


def signed_by_live_wallet(raw: Mapping[str, Any], vctx: ValidationContext) -> bool:
    """The invoice is signed by the issuer's live wallet (used for spam evidence, which has no mandate)."""
    try:
        issuer = int(raw["issuerAgentId"])
        signer = recover_invoice_signer(raw)
    except (KeyError, TypeError, ValueError):
        return False
    live = vctx.live_wallets.get(issuer)
    return live is not None and signer == live.lower()


def validate_invoice(
    raw: Any,
    vctx: ValidationContext,
    cfg: PolicyConfig,
    *,
    check_signature: bool = True,
    check_expiry: bool = True,
    check_quote_open: bool = True,
) -> ValidatedInvoice | InvoiceRejection:
    """Run V01-V09 in order; return the :class:`~acb.types.ValidatedInvoice` or the first rejection.

    ``raw`` is the wire invoice (a dict, or its JSON text/bytes). The keyword switches exist for the
    signer's second pass: it re-checks the signature and expiry itself as gate C04 (so a failure is a
    recorded REFUSE 2102/2117 instead of a silent drop), and for an owner-approved order it does not
    require the quote request to be still open.
    """
    ihash = _safe_hash(raw)
    try:
        body = _parse_body(raw, cfg.invoice.max_bytes)
        ihash = _safe_hash(body)
        _v01_shape(body)
        _v02_ids(body)
        _v03_amounts(body, cfg)
        _v04_catalogue(body, vctx, cfg)
        signed = _parse_canonical(body)
        ihash = signed.invoice_hash
        _v05_chain_token(body, vctx)
        _v06_times(body, vctx, cfg, check_expiry)
        _v07_buyer(body, vctx)
        try:
            qr = _v08_solicited(body, vctx, ihash, check_quote_open)
        except _Reject as rej:
            rej.spam = signed_by_live_wallet(body, vctx)  # type: ignore[attr-defined]
            raise
        if check_signature and not signature_valid(body, vctx):
            raise _Reject("V09", "signature does not recover to the pinned and live wallet")
    except _Reject as rej:
        return InvoiceRejection(
            code=rej.code,
            rule=rej.rule,
            detail=rej.detail,
            invoice_hash=ihash,
            keep_as_spam_evidence=bool(getattr(rej, "spam", False)),
        )
    detector = detect(invoice_fields(body))
    return ValidatedInvoice(signed=signed, quote_request=qr, detector=detector)


# ---------------------------------------------------------------------------
# helpers for the agent (not pure: randomness and files)
# ---------------------------------------------------------------------------


def new_quote_request_id() -> str:
    """``"qr-" + 32 lowercase hex`` from ``secrets.token_hex(16)`` (6.6)."""
    return "qr-" + secrets.token_hex(16)


def new_quote_request(
    *,
    mandate_id: str,
    issuer_agent_id: int,
    sku: str,
    quantity: int,
    now: int,
    cfg: PolicyConfig,
    cap_of: str = "",
    quote_request_id: str | None = None,
) -> QuoteRequest:
    """A fresh single-use quote request. It closes ``max_validity_s + close_after_s`` after ``now``
    (an invoice cannot outlive ``max_validity_s``; 6.2 closes the request 60 s after its expiry)."""
    return QuoteRequest(
        quote_request_id=quote_request_id or new_quote_request_id(),
        mandate_id=mandate_id,
        issuer_agent_id=int(issuer_agent_id),
        sku=sku,
        quantity=int(quantity),
        cap_of=cap_of,
        created_at=int(now),
        closes_at=int(now) + cfg.invoice.max_validity_s + cfg.quote.close_after_s,
        used_by=None,
    )


def spam_evidence_path(out_dir: str | os.PathLike[str], issuer_agent_id: int, ihash: str) -> Path:
    """``<out_dir>/agent/spam_evidence/<issuer>/<invoiceHash>.json`` (6.2)."""
    if not re.fullmatch(r"0x[0-9a-f]{64}", ihash):
        raise ValueError("invoice hash must be 0x + 64 lowercase hex")
    return Path(out_dir) / "agent" / "spam_evidence" / str(int(issuer_agent_id)) / f"{ihash}.json"


def write_spam_evidence(out_dir: str | os.PathLike[str], raw: Mapping[str, Any]) -> Path:
    """Store a signed unsolicited invoice (canonical bytes, atomically) and return its path."""
    ihash = invoice_hash(raw)
    path = spam_evidence_path(out_dir, int(raw["issuerAgentId"]), ihash)
    atomic_write_bytes(path, canonical_bytes(dict(raw)))
    return path


__all__ = [
    "CODE_INVOICE_EXPIRED",
    "CODE_INVOICE_REJECTED",
    "INVOICE_KEYS",
    "ITEM_KEYS",
    "new_quote_request",
    "new_quote_request_id",
    "signature_valid",
    "signed_by_live_wallet",
    "spam_evidence_path",
    "superseded_key",
    "validate_invoice",
    "write_spam_evidence",
]
