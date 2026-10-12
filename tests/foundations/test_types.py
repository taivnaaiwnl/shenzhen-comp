"""Shared types (spec Appendix B): enums, alias policy, amounts, cards, wire helpers."""

from __future__ import annotations

import json

import pytest
from pydantic import TypeAdapter, ValidationError

from acb.canonical import invoice_hash, sign_invoice
from acb.keys import KeyRing
from acb.types import (
    DECISION_TO_TOOL_ACTION,
    EVENT_TYPES,
    GRADE_RANK,
    STRICTNESS,
    TIER_FROZEN,
    TOOL_ACTION_TO_DECISION,
    AgentEntry,
    Bundle,
    Card,
    ChatResponse,
    Decision,
    DecisionCard,
    Deployments,
    Event,
    Grade,
    GradeView,
    Invoice,
    InvoiceItem,
    LLMResult,
    MandateDraft,
    NetworkProfile,
    OrderState,
    Outcome,
    PayeeCandidate,
    SignedDelivery,
    SignedInvoice,
    TierConfig,
    ToolAction,
    ToolCall,
    TxResult,
    ViolationKind,
    grade_mask,
    mask_has,
    stricter,
)

H32 = "0x" + "ab" * 32
ADDR = "0x" + "12" * 20


def test_enum_values_match_spec_0_2():
    assert [g.value for g in (Grade.NONE, Grade.NEW, Grade.D, Grade.C, Grade.B, Grade.A)] == [0, 1, 2, 3, 4, 5]
    assert [d.value for d in Decision] == [0, 1, 2, 3, 4, 5]
    assert [s.value for s in OrderState] == [0, 1, 2, 3, 4, 5]
    assert [o.value for o in Outcome] == [0, 1, 2, 3]
    assert ViolationKind.LIMIT_PROPOSAL == 1 and ViolationKind.REPEATED_VIOLATIONS == 11
    assert len(ViolationKind) == 11
    assert TIER_FROZEN == 255
    assert grade_mask(Grade.NONE) == 0x01 and grade_mask(Grade.A) == 0x20
    assert grade_mask(Grade.A, Grade.B) == 0x30
    assert mask_has(0x30, Grade.B) and not mask_has(0x30, Grade.C)


def test_tool_action_mapping_and_strictness():
    assert TOOL_ACTION_TO_DECISION[ToolAction.REQUEST_OWNER_APPROVAL] is Decision.NEEDS_OWNER_APPROVAL
    assert len(TOOL_ACTION_TO_DECISION) == 5
    for action, decision in TOOL_ACTION_TO_DECISION.items():
        assert DECISION_TO_TOOL_ACTION[decision] is action
    order = sorted(STRICTNESS, key=STRICTNESS.get)
    assert order == [Decision.PAY, Decision.PAY_CAPPED, Decision.ESCROW, Decision.NEEDS_OWNER_APPROVAL, Decision.REFUSE]
    assert stricter(Decision.PAY, Decision.REFUSE) is Decision.REFUSE
    assert stricter(Decision.ESCROW, Decision.PAY_CAPPED) is Decision.ESCROW


def test_grade_rank():
    assert GRADE_RANK == {Grade.D: 0, Grade.C: 1, Grade.NONE: 2, Grade.NEW: 2, Grade.B: 3, Grade.A: 4}


def tier_config(**kw):
    base = {
        "per_tx": 1_000_000,
        "window_cap": 5_000_000,
        "new_payee_cap": 500_000,
        "max_tx": 10,
        "pay_mask": 0x30,
        "escrow_mask": 0x3B,
        "min_clean_to_promote": 3,
    }
    base.update(kw)
    return TierConfig(**base)


def test_alias_policy_round_trip():
    cfg = tier_config()
    wire = cfg.to_wire()
    assert wire == {
        "perTx": "1000000",
        "windowCap": "5000000",
        "newPayeeCap": "500000",
        "maxTx": 10,
        "payMask": 48,
        "escrowMask": 59,
        "minCleanToPromote": 3,
    }
    assert cfg.model_dump()["per_tx"] == 1_000_000, "snake_case and int in Python"
    assert TierConfig.model_validate(wire) == cfg, "camelCase input"
    assert TierConfig.model_validate(cfg.model_dump()) == cfg, "snake_case input"
    assert TierConfig.model_validate_json(json.dumps(wire)) == cfg


def test_models_are_frozen_and_forbid_extra():
    cfg = tier_config()
    with pytest.raises(ValidationError):
        cfg.per_tx = 5
    with pytest.raises(ValidationError):
        TierConfig.model_validate({**cfg.to_wire(), "unexpected": 1})


@pytest.mark.parametrize("bad", [1.5, 1.0, True, -1, "1.00", "0x10", " 5", None])
def test_amount_rejects_floats_bools_negatives_and_non_decimal_strings(bad):
    with pytest.raises(ValidationError):
        tier_config(per_tx=bad)


def test_amount_accepts_int_and_decimal_string_and_big_values():
    assert tier_config(per_tx="800000").per_tx == 800_000
    big = 10**30
    assert tier_config(per_tx=big).to_wire()["perTx"] == str(big)


def test_hex_and_address_normalisation():
    gv = GradeView(
        agent_id=7,
        grade=Grade.A,
        effective_grade=Grade.A,
        score=740,
        reasons=(1006,),
        model_id="acb-sc-1.0.0",
        config_hash=H32.upper().replace("0X", "0x"),
        evidence_hash=H32,
        as_of_block=1,
        posted_at=2,
        tx_hash=None,
        evidence_uri="evidence/x.json",
    )
    assert gv.config_hash == H32
    with pytest.raises(ValidationError):
        GradeView.model_validate({**gv.model_dump(), "config_hash": "0x1234"})
    with pytest.raises(ValidationError):
        GradeView.model_validate({**gv.model_dump(), "evidence_hash": "ab" * 32})
    tx = TxResult(
        hash=H32,
        status=1,
        block=3,
        gas_used=21000,
        function="pay",
        from_address="0xF39FD6E51AAD88F6F4CE6AB8827279CFFFB92266",
        to=ADDR,
        nonce=0,
        chain_id=31337,
        expected_status=1,
        revert_error=None,
        logs=(),
    )
    assert tx.from_address == "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"
    assert tx.to_wire()["fromAddress"] == tx.from_address and tx.to_wire()["gasUsed"] == 21000
    with pytest.raises(ValidationError):
        TxResult.model_validate({**tx.model_dump(), "to": "0x1234"})


def test_agent_ids_are_strings_on_the_wire():
    gv = GradeView(
        agent_id=53,
        grade=Grade.D,
        effective_grade=Grade.D,
        score=410,
        reasons=(1002, 1001),
        model_id="acb-sc-1.0.0",
        config_hash=H32,
        evidence_hash=H32,
        as_of_block=5,
        posted_at=6,
        tx_hash=H32,
        evidence_uri="u",
    )
    wire = gv.to_wire()
    assert wire["agentId"] == "53" and wire["grade"] == 2
    assert GradeView.model_validate(wire).agent_id == 53


def sample_invoice_wire() -> dict:
    return {
        "schema": "acb-invoice/1",
        "invoiceId": "inv-orion-0006",
        "quoteRequestId": "qr-" + "0f" * 16,
        "capOf": "",
        "issuerAgentId": "57",
        "buyerAgentId": "50",
        "buyerVault": "0x5e1fa0c3b2d4e6f8a1b3c5d7e9f0a2b4c6d8e0f2",
        "chainId": 84532,
        "token": "0x036cbd53842c5426634e7929541ec2318f3dcf7e",
        "items": [{"sku": "api-bundle-1k", "description": "1,000 API calls", "unitPrice": "200000", "quantity": 3}],
        "amount": "600000",
        "issuedAt": 1792222222,
        "expiresAt": 1792222822,
        "deliveryDeadline": 1792222282,
        "memo": "Thanks for your order.",
    }


def test_invoice_wire_round_trip_preserves_the_hash():
    raw = sample_invoice_wire()
    inv = Invoice.from_wire(raw)
    assert inv.issuer_agent_id == 57 and inv.amount == 600_000
    assert inv.items == (
        InvoiceItem(sku="api-bundle-1k", description="1,000 API calls", unit_price=200_000, quantity=3),
    )
    assert inv.wire_dict() == raw, "model -> wire gives exactly the 9.2 layout"
    assert invoice_hash(inv) == invoice_hash(raw)
    signed_raw = sign_invoice(KeyRing.anvil().account("orion"), raw)
    signed = SignedInvoice.from_wire(signed_raw)
    assert signed.invoice_hash == invoice_hash(raw)
    assert signed.wire_dict() == signed_raw
    with pytest.raises(ValueError):
        Invoice.from_wire({**raw, "schema": "acb-invoice/2"})
    with pytest.raises(ValidationError):
        Invoice.from_wire({**raw, "extra": "x"})


def test_signed_delivery_wire():
    d = SignedDelivery(
        order_id=H32,
        invoice_id="inv-1",
        issuer_agent_id=5,
        payload_hash=H32,
        delivered_at=10,
        signature="0x" + "11" * 65,
    )
    unsigned = d.unsigned_wire_dict()
    assert unsigned["schema"] == "acb-delivery/1" and "signature" not in unsigned and unsigned["issuerAgentId"] == "5"
    assert SignedDelivery.from_wire(d.wire_dict()) == d


def candidate(**kw):
    base = {
        "agent_id": 7,
        "name": "Atlas Data",
        "grade": Grade.A,
        "score": 740,
        "live_wallet": ADDR,
        "registered_at": 100,
        "lookalike_of": None,
    }
    base.update(kw)
    return PayeeCandidate(**base)


def test_card_discriminated_union():
    adapter = TypeAdapter(Card)
    draft = MandateDraft(
        draft_id="d1",
        purpose="FX data",
        payees=(candidate(),),
        max_per_tx=1_000_000,
        max_total=4_000_000,
        valid_hours=24,
        recurring=False,
        diff=None,
    )
    parsed = adapter.validate_python(draft.to_wire())
    assert isinstance(parsed, MandateDraft) and parsed == draft
    card = DecisionCard(
        order_id=H32,
        payee=candidate(),
        amount=800_000,
        outcome=Decision.PAY,
        reasons=(2000,),
        reason_texts=("Paid: grade A counterparty within tier limits",),
        tx_hash=None,
        offchain=False,
    )
    resp = ChatResponse(reply="ok", cards=(draft, card), llm={"mode": "mock", "model": "mock", "fallback": False})
    again = ChatResponse.model_validate_json(json.dumps(resp.to_wire()))
    assert again == resp and isinstance(again.cards[1], DecisionCard)
    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "unknown"})


def test_event_keeps_snake_case():
    ev = Event(
        seq=41,
        ts="2026-10-17T09:12:03Z",
        run_id="r-1",
        service="signer",
        type="decision",
        step="S12",
        chain_id=84532,
        data={"amount": "400000"},
        reason_codes=(2100,),
        tx_hash="0xabc",
        explorer=None,
    )
    dumped = ev.model_dump(mode="json")
    assert list(dumped) == [
        "seq",
        "ts",
        "run_id",
        "service",
        "type",
        "step",
        "chain_id",
        "data",
        "reason_codes",
        "tx_hash",
        "explorer",
    ]
    assert "decision" in EVENT_TYPES and len(EVENT_TYPES) == 14


def test_llm_types():
    res = LLMResult(
        text=None,
        tool_calls=[ToolCall(name="reply_to_owner", arguments={"text": "hi"}, raw="{}")],
        model="mock",
        latency_ms=5,
        error=None,
    )
    assert res.to_wire()["toolCalls"][0]["name"] == "reply_to_owner"


def test_bundle_canonical_and_hash():
    from acb.canonical import chash

    b = Bundle(
        model={"id": "acb-sc-1.0.0", "configHash": H32, "detectorId": "acb-inj-1.0.0", "profile": "demo"},
        chain={"chainId": 31337, "asOfBlock": 5, "asOfBlockHash": H32, "asOfTimestamp": 1},
        contracts={"acb": ADDR, "identity": ADDR, "reputation": ADDR, "vaults": [ADDR]},
        subject={"agentId": "42", "cluster": ["42"], "registeredBlock": 1, "registeredTimestamp": 1},
        orders=(),
        reports=(),
        features={"n": 0},
        points={"F1": 0},
        score=600,
        grade="NEW",
        reasons=(1101, 0, 0, 0),
        rules={"coldStart": True},
    )
    canon = b.as_canonical()
    assert canon["schema"] == "acb-evidence/1" and canon["orders"] == [] and canon["override"] is None
    assert b.evidence_hash == chash(canon)
    assert Bundle.model_validate(canon) == b


def test_deployments_file_layout_round_trip():
    raw = {
        "schema": "acb-deployments/1",
        "network": "anvil",
        "chainId": 31337,
        "deployedAt": "2026-10-17T08:00:00Z",
        "gitCommit": "abc",
        "startBlock": 0,
        "roles": {"deployer": ADDR, "agentSigner": ADDR},
        "contracts": {
            "AgentCreditBureau": {"address": ADDR, "txHash": H32, "block": 1, "upgradeDelay": 60},
            "USDC": {"address": ADDR, "decimals": 6, "mock": True},
        },
        "vaultCodeHash": H32,
        "anchors": ["11"],
        "agents": {
            "atlas": {
                "agentId": "5",
                "name": "Atlas Data",
                "identityOwner": ADDR,
                "wallet": ADDR,
                "registerTx": H32,
                "category": "fx-data",
                "skus": {"fx-live-15m": "200000"},
            }
        },
        "vaults": {
            "wallet": {
                "address": ADDR,
                "agentId": "1",
                "owner": ADDR,
                "agent": ADDR,
                "autonomy": ADDR,
                "profile": "wallet",
                "deployTx": H32,
                "registerVaultTx": H32,
            }
        },
        "abi": {"sha": H32},
    }
    dep = Deployments.from_json_dict(raw)
    assert dep.contracts["AgentCreditBureau"].extra == {"upgradeDelay": 60}
    assert dep.agents["atlas"].skus["fx-live-15m"] == 200_000 and dep.anchors == (11,)
    assert dep.to_json_dict() == raw


def test_network_profile_load_uses_environment():
    net = NetworkProfile.load("anvil", {"ANVIL_RPC_URL": "http://127.0.0.1:9999"})
    assert net.chain_id == 31337 and net.rpc_url == net.log_rpc_url == "http://127.0.0.1:9999"
    assert net.explorer_tx_url(H32) is None
    base = NetworkProfile.load("base_sepolia", {})
    assert base.rpc_url == "https://sepolia.base.org" and base.log_rpc_url == base.rpc_url
    assert base.logs_chunk_blocks == 200 and base.erc8004["expected_version"] == "2.0.0"
    assert base.explorer_tx_url(H32) == f"https://sepolia.basescan.org/tx/{H32}"
    two = NetworkProfile.load("base_sepolia", {"BASE_SEPOLIA_RPC_URL_2": "https://logs.example"})
    assert two.log_rpc_url == "https://logs.example"
    with pytest.raises(KeyError):
        NetworkProfile.load("mainnet", {})


@pytest.mark.parametrize(
    "change",
    [
        {"buyerVault": "0x5E1Fa0C3b2d4e6f8a1b3c5d7e9f0a2b4c6d8e0f2"},  # mixed-case address
        {"token": "0x036CbD53842c5426634e7929541eC2318f3dCF7e"},
        {"issuerAgentId": 57},  # agent id as a number
        {"amount": 600000},  # amount as a number
        {"chainId": "84532"},  # int as a string
        {"chainId": True},
        {"issuedAt": 1792222222.0},
        {
            "items": [
                {"sku": "api-bundle-1k", "description": "1,000 API calls", "unitPrice": "200000", "quantity": True}
            ]
        },
    ],
)
def test_invoice_from_wire_rejects_non_canonical_forms(change):
    raw = {**sample_invoice_wire(), **change}
    with pytest.raises(ValueError):
        Invoice.from_wire(raw)
    signed = {**raw, "signature": "0x" + "11" * 65}  # parsing does not verify the signature
    with pytest.raises(ValueError):
        SignedInvoice.from_wire(signed)


def test_invoice_from_wire_rejects_snake_case_keys_and_uppercase_signature():
    raw = sample_invoice_wire()
    snake = {k: v for k, v in raw.items() if k != "invoiceId"} | {"invoice_id": raw["invoiceId"]}
    with pytest.raises(ValueError):
        Invoice.from_wire(snake)
    signed = sign_invoice(KeyRing.anvil().account("orion"), raw)
    with pytest.raises(ValueError):
        SignedInvoice.from_wire({**signed, "signature": "0x" + signed["signature"][2:].upper()})


def test_signed_invoice_hash_must_match_its_invoice():
    raw = sample_invoice_wire()
    inv = Invoice.from_wire(raw)
    with pytest.raises(ValidationError):
        SignedInvoice(invoice=inv, signature="0x" + "11" * 65, invoice_hash="0x" + "00" * 32)
    si = SignedInvoice.from_wire(sign_invoice(KeyRing.anvil().account("orion"), raw))
    assert si.invoice_hash == invoice_hash(si.invoice) == invoice_hash(si.wire_dict())


def test_signed_delivery_from_wire_rejects_non_canonical_forms():
    d = SignedDelivery(
        order_id=H32,
        invoice_id="inv-1",
        issuer_agent_id=5,
        payload_hash=H32,
        delivered_at=10,
        signature="0x" + "11" * 65,
    )
    wire = d.wire_dict()
    for change in (
        {"issuerAgentId": 5},
        {"deliveredAt": 10.0},
        {"deliveredAt": "10"},
        {"orderId": "0x" + H32[2:].upper()},
    ):
        with pytest.raises(ValueError):
            SignedDelivery.from_wire({**wire, **change})


def test_deployments_agent_wallet_is_optional():
    entry = {
        "agentId": "1234",
        "name": "Research Wallet",
        "identityOwner": "0x" + "ab" * 20,
        "registerTx": "0x" + "cd" * 32,
        "category": "research",
        "skus": {},
    }
    assert AgentEntry.model_validate(entry).wallet is None
