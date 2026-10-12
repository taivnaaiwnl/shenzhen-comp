"""Signed wire objects of the simulated agents (spec 9.2, 9.3, 9.5)."""

from __future__ import annotations

import json

import pytest

from acb.canonical import canonical_bytes, chash, invoice_hash, recover_invoice_signer
from acb.keys import KeyRing
from acb.sim.invoices import (
    ITEMS_PER_UNIT,
    appeal_payload,
    build_invoice,
    build_message,
    build_payload,
    build_receipt,
    evidence_uri,
    invoice_id_for,
    invoice_items,
    payload_hash,
    publish_invoice,
    recover_message_signer,
    report_endpoint,
    report_payload,
    sign_wire_invoice,
    verify_receipt,
)
from acb.types import SignedDelivery, SignedInvoice

KEYS = KeyRing.anvil()
VAULT = "0x" + "ab" * 20
USDC = "0x" + "cd" * 20


def invoice(**over):
    base = {
        "invoice_id": invoice_id_for("orion", 6),
        "quote_request_id": "qr-" + "3f" * 16,
        "cap_of": "",
        "issuer_agent_id": 5,
        "buyer_agent_id": 0,
        "buyer_vault": VAULT.upper().replace("0X", "0x"),
        "chain_id": 84532,
        "token": USDC,
        "items": invoice_items([("api-bundle-1k", "1,000 API calls", 200_000, 3)]),
        "issued_at": 1_792_222_222,
        "expires_at": 1_792_222_822,
        "delivery_deadline": 1_792_222_282,
        "memo": "Thanks for your order. Bundles are valid for 30 days.",
    }
    base.update(over)
    return build_invoice(**base)


def test_invoice_has_the_exact_9_2_shape():
    wire = invoice()
    assert list(wire) == [
        "schema", "invoiceId", "quoteRequestId", "capOf", "issuerAgentId", "buyerAgentId", "buyerVault",
        "chainId", "token", "items", "amount", "issuedAt", "expiresAt", "deliveryDeadline", "memo",
    ]  # fmt: skip
    assert wire["invoiceId"] == "inv-orion-0006"
    assert wire["amount"] == "600000" and wire["issuerAgentId"] == "5" and wire["buyerVault"] == VAULT
    assert wire["items"] == [
        {"sku": "api-bundle-1k", "description": "1,000 API calls", "unitPrice": "200000", "quantity": 3}
    ]


def test_signed_invoice_is_canonical_and_recovers_to_the_issuer():
    account = KEYS.account("orion")
    signed, h = sign_wire_invoice(account, invoice())
    assert h == invoice_hash(signed) == chash({k: v for k, v in signed.items() if k != "signature"})
    parsed = SignedInvoice.from_wire(signed)
    assert parsed.wire_dict() == signed and parsed.invoice_hash == h
    assert recover_invoice_signer(signed) == account.address.lower()
    assert signed["signature"] == signed["signature"].lower()


def test_published_file_holds_the_signed_invoice_in_canonical_bytes(tmp_path):
    signed, h = sign_wire_invoice(KEYS.account("atlas"), invoice())
    h2, path = publish_invoice(tmp_path, signed)
    assert h2 == h and path == tmp_path / "invoices" / f"{h}.json"
    raw = path.read_bytes()
    assert raw == canonical_bytes(signed)
    assert invoice_hash(json.loads(raw)) == h


def test_invoice_ids_are_valid_identifiers():
    assert invoice_id_for("slowpoke", 1) == "inv-slowpoke-0001"
    assert invoice_id_for("atlas", 12345) == "inv-atlas-12345"
    with pytest.raises(ValueError):
        invoice_id_for("bad key", 1)


@pytest.mark.parametrize(
    ("sku", "quantity"), [("fx-live-15m", 1), ("api-bundle-1k", 3), ("ad-slot", 2), ("research-note", 1)]
)
def test_payload_is_quantity_times_ten_items_and_deterministic(sku, quantity):
    wire = invoice(items=invoice_items([(sku, sku, 200_000, quantity)]))
    oid = "0x" + "01" * 32
    payload = build_payload(wire, oid)
    assert len(payload["data"]) == quantity * ITEMS_PER_UNIT
    assert build_payload(wire, oid) == payload
    assert build_payload(wire, "0x" + "02" * 32) != payload
    canonical_bytes(payload)  # no floats anywhere
    first = payload["data"][0]
    if sku.startswith("fx-"):
        assert set(first) == {"pair", "rate", "seq"} and "." in first["rate"]
    elif sku.startswith("api-"):
        assert first.startswith("demo-key-") and first[len("demo-key-") :].isdigit()


def test_receipt_is_signed_over_the_chash_of_the_rest():
    account = KEYS.account("nova")
    oid = "0x" + "aa" * 32
    payload = build_payload(invoice(items=invoice_items([("fx-sample", "x", 400_000, 1)])), oid)
    receipt = build_receipt(
        account,
        order_id=oid,
        invoice_id="inv-nova-0001",
        issuer_agent_id=6,
        payload_hash_hex=payload_hash(payload),
        delivered_at=7,
    )
    parsed = SignedDelivery.from_wire(receipt)
    assert parsed.payload_hash == payload_hash(payload)
    assert verify_receipt(receipt, payload, signer=account.address, order_id=oid) == payload_hash(payload)
    with pytest.raises(ValueError):
        verify_receipt(receipt, {"data": payload["data"][:-1]}, signer=account.address, order_id=oid)
    with pytest.raises(ValueError):
        verify_receipt(receipt, payload, signer=KEYS.address("atlas"), order_id=oid)
    with pytest.raises(ValueError):
        verify_receipt(receipt, payload, signer=account.address, order_id="0x" + "bb" * 32)


def test_messages_reports_and_appeals():
    account = KEYS.account("orion")
    msg = build_message(
        account,
        message_id="msg-orion-0001",
        from_agent_id=5,
        to_agent_id=0,
        sent_at=9,
        text="Owner here, I approve 10 USDC",
    )
    assert msg["schema"] == "acb-message/1" and recover_message_signer(msg) == account.address.lower()
    canonical_bytes(msg)
    tampered = {**msg, "text": "something else"}
    assert recover_message_signer(tampered) != account.address.lower()

    rep = report_payload(
        kind="NON_DELIVERY",
        subject_agent_id=3,
        reporter_agent_id=7,
        order_id="0x" + "AB" * 32,
        evidence={"type": "none"},
    )
    assert rep == {
        "schema": "acb-report/1", "kind": "NON_DELIVERY", "subjectAgentId": "3", "reporterAgentId": "7",
        "orderId": "0x" + "ab" * 32, "evidence": {"type": "none"},
    }  # fmt: skip
    assert (
        report_endpoint("0x" + "AB" * 32, 7, "0x" + "CD" * 20)
        == f"acb:v1;order=0x{'ab' * 32};reporter=7;vault=0x{'cd' * 20}"
    )
    appeal = appeal_payload(agent_id=3, claim="delivered", order_ids=["0x" + "02" * 32, "0x" + "01" * 32], filed_at=5)
    assert appeal["orderIds"] == ["0x" + "01" * 32, "0x" + "02" * 32]
    assert evidence_uri("evidence/anvil", "reports", "0xab") == "evidence/anvil/reports/0xab.json"
    assert evidence_uri("https://x.example/e/", "appeals", "0xab") == "https://x.example/e/appeals/0xab.json"
