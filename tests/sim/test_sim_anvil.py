"""The simulator on a local Anvil deployment (marker ``chain``): claim-before-delivery, the warm-up,
the S11 Ghost default round, reports and the appeal (spec 9.3, 9.5, 10.2 S4, S11, S20)."""

from __future__ import annotations

import json
import secrets

import pytest
from sim_helpers import QuoteReq, check_invoice, policy_validate
from web3 import Web3

from acb.canonical import invoice_hash, order_id
from acb.chain.contracts import OutflowParams, bind
from acb.sim.core import SimError
from acb.types import Decision, OrderState, Outcome

pytestmark = pytest.mark.chain


def _h(value) -> str:
    return value.lower() if isinstance(value, str) else Web3.to_hex(value)


def _events(sim, vault_addr, name, from_block=0):
    vault = bind(sim.w3, sim.dep).vault_at(vault_addr)
    return vault.logs(name, from_block)


def _beacon_buys(asim, key, sku, qty=1, *, escrow=True):
    """Beacon requests a quote over the provider endpoint and pays it under a mandate (helper)."""
    from acb.sim.market import beacon_mandate

    sim = asim.sim
    b, mandate_id, _ = beacon_mandate(sim)
    qr = "qr-" + secrets.token_hex(16)
    body = {
        "quoteRequestId": qr,
        "buyerAgentId": str(b.agent_id),
        "buyerVault": b.address,
        "sku": sku,
        "quantity": qty,
        "capOf": "",
    }
    signed = sim.quote(key, body)
    party = sim.party(key)
    oid = order_id(sim.chain_id, b.address, party.agent_id, signed["invoiceId"])
    params = OutflowParams(
        order_id=oid,
        payee_agent_id=party.agent_id,
        amount=int(signed["amount"]),
        deadline=signed["deliveryDeadline"],
        mandate_id=mandate_id,
        invoice_hash=invoice_hash(signed),
        decision_hash="0x" + "dd" * 32,
        decision=int(Decision.ESCROW if escrow else Decision.PAY),
        reason_code=2004 if escrow else 2005,
    )
    vault = bind(sim.w3, sim.dep).vault_at(b.address)
    fn = vault.escrow(params) if escrow else vault.pay(params)
    tx = sim.sender(b.agent).send(fn)
    assert tx.status == 1
    return b, signed, oid, body


def test_beacon_quotes_pass_invoice_validation(anvil_sim):
    sim = anvil_sim.sim
    b, signed, _, body = _beacon_buys(anvil_sim, "orion", "api-bundle-1k", 2)
    issuer = int(signed["issuerAgentId"])
    ctx = {
        "now": sim.now(),
        "chain_id": 31337,
        "usdc": sim.usdc,
        "vault": b.address,
        "vault_agent_id": b.agent_id,
        "catalogue": {e.agent_id: dict(e.skus) for e in sim.dep.agents.values()},
        "quote_requests": [QuoteReq(body["quoteRequestId"], issuer, "api-bundle-1k", 2)],
        "pinned_wallets": {issuer: bind(sim.w3, sim.dep).identity.get_agent_wallet(issuer)},
        "live_wallets": {issuer: bind(sim.w3, sim.dep).identity.get_agent_wallet(issuer)},
    }
    assert check_invoice(signed, **ctx) is None
    from acb.types import ValidatedInvoice

    assert isinstance(policy_validate(signed, **ctx), ValidatedInvoice)


def test_claim_before_delivery_on_anvil(anvil_sim):
    sim, w3 = anvil_sim.sim, anvil_sim.w3
    b, signed, oid, _ = _beacon_buys(anvil_sim, "atlas", "fx-live-15m", 1)
    vault = bind(w3, sim.dep).vault_at(b.address)
    assert vault.get_order(oid).claimed_at == 0
    answer = sim.deliver("atlas", {"orderId": oid, "invoiceId": signed["invoiceId"]})
    assert answer["status"] == "delivered"
    order = vault.get_order(oid)
    # the claim is on-chain, mined, with the payload's hash, before the data was handed over
    assert order.claimed_at != 0 and order.delivery_hash == answer["receipt"]["payloadHash"]
    receipt = w3.eth.get_transaction_receipt(answer["claimTx"])
    assert receipt["status"] == 1
    assert w3.eth.get_block(receipt["blockNumber"])["timestamp"] <= answer["receipt"]["deliveredAt"]
    assert order.claimed_at <= signed["deliveryDeadline"]
    # the buyer can no longer refund
    from acb.chain.tx import TxSimulationError

    with pytest.raises(TxSimulationError) as err:
        sim.sender(b.agent).send(vault.refund(oid, 2401))
    assert "DeliveryClaimed" in err.value.revert_error
    claims = _events(sim, b.address, "DeliveryClaimed")
    assert [_h(c["args"]["orderId"]) for c in claims][-1] == oid


def test_no_payload_when_the_claim_reverts_on_anvil(anvil_sim, monkeypatch):
    sim = anvil_sim.sim
    _b, signed, oid, _ = _beacon_buys(anvil_sim, "atlas", "fx-live-15m", 1)
    # another key tries to claim (NotPayee): simulate it by sending from the wrong account
    real = sim.chain.claim

    def wrong_claim(s, party, vault, order, digest):
        return real(s, s.party("ghost"), vault, order, digest)

    monkeypatch.setattr(sim.chain, "claim", wrong_claim)
    with pytest.raises(SimError) as err:
        sim.deliver("atlas", {"orderId": oid, "invoiceId": signed["invoiceId"]})
    assert err.value.status == 502 and "NotPayee" in err.value.body["error"]
    assert not sim.delivery_path(oid).exists()


def test_direct_payment_slowpoke_late_on_anvil(anvil_sim):
    from acb.chain.clock import advance_to

    sim = anvil_sim.sim
    _b, signed, oid, _ = _beacon_buys(anvil_sim, "slowpoke", "fx-history-day-promo", 1, escrow=False)
    body = {"orderId": oid, "invoiceId": signed["invoiceId"]}
    assert sim.deliver("slowpoke", body) == {"status": "pending"}  # its first order: late
    ready = sim.ready_at(oid)
    assert ready == signed["deliveryDeadline"] + 5
    advance_to(sim.w3, ready)
    answer = sim.deliver("slowpoke", body)
    assert answer["status"] == "delivered" and answer["claimTx"] is None
    assert answer["receipt"]["deliveredAt"] > signed["deliveryDeadline"]


@pytest.mark.slow
def test_warmup_produces_19_orders_under_beacons_mandate(anvil_sim):
    sim = anvil_sim.sim
    start = sim.w3.eth.block_number
    out = sim.run_action("warmup", {"step": "S4"})
    result = out["result"]
    beacon_vault = sim.dep.vaults["beacon"].address
    mandate_id = result["mandateId"]
    contracts = bind(sim.w3, sim.dep)
    vault = contracts.vault_at(beacon_vault)

    # the mandate was registered by Beacon's owner key: Atlas, Slowpoke, Ghost, Orion; 1.00; 6.00
    registered = _events(sim, beacon_vault, "MandateRegistered", start)
    assert len(registered) == 1
    mandate_tx = sim.w3.eth.get_transaction(registered[0]["transactionHash"])
    assert mandate_tx["from"].lower() == sim.keys.address("beacon_owner")
    args = registered[0]["args"]
    ids = {sim.dep.agents[k].agent_id for k in ("atlas", "slowpoke", "ghost", "orion")}
    assert set(args["payeeAgentIds"]) == ids and args["maxPerTx"] == 1_000_000 and args["maxTotal"] == 6_000_000

    opened = _events(sim, beacon_vault, "EscrowOpened", start)
    claimed = _events(sim, beacon_vault, "DeliveryClaimed", start)
    released = _events(sim, beacon_vault, "EscrowReleased", start)
    assert len(opened) == len(claimed) == len(released) == 19
    assert {_h(o["args"]["mandateId"]) for o in opened} == {mandate_id}
    assert {_h(o["args"]["orderId"]) for o in opened} == {_h(c["args"]["orderId"]) for c in released}

    per_provider: dict[str, list[str]] = {}
    for order in result["orders"]:
        assert order["state"] == "RELEASED"
        per_provider.setdefault(order["provider"], []).append(order["outcome"])
    assert per_provider == {
        "atlas": ["ON_TIME"] * 5,
        "slowpoke": ["LATE", "ON_TIME", "LATE", "ON_TIME", "LATE", "ON_TIME"],
        "ghost": ["ON_TIME"] * 4,
        "orion": ["ON_TIME"] * 4,
    }
    # the claims came from each provider's own key, and the escrows were all released by Beacon
    for log in claimed:
        tx = sim.w3.eth.get_transaction(log["transactionHash"])
        payee = vault.get_order(_h(log["args"]["orderId"])).payee
        assert tx["from"].lower() == payee
    assert vault.get_mandate(mandate_id).spent == 5 * 200_000 + 6 * 100_000 + 4 * 200_000 + 4 * 200_000
    assert vault.escrow_locked() == 0

    # every invoice is published and signed by the payee's registered wallet
    for order in result["orders"]:
        path = sim.out_dir / "invoices" / f"{order['invoiceHash']}.json"
        raw = json.loads(path.read_bytes())
        assert invoice_hash(raw) == order["invoiceHash"]
        assert raw["buyerVault"] == beacon_vault and raw["quoteRequestId"].startswith("qr-")
    # the run's transactions are all reported, provider claims included
    functions = [t["function"] for t in out["txs"]]
    assert functions.count("escrow") == functions.count("claimDelivered") == functions.count("release") == 19
    assert functions.count("registerMandate") == 1

    # resumable: a second run sends nothing new
    again = sim.run_action("warmup", {})
    assert again["txs"] == [] and again["result"]["settled"] == 19


@pytest.mark.slow
def test_ghost_default_round_and_reports(anvil_sim):
    sim = anvil_sim.sim
    sim.run_action("warmup", {})
    start = sim.w3.eth.block_number
    out = sim.run_action("ghost_default_round", {"step": "S11"})
    result = out["result"]
    beacon_vault = sim.dep.vaults["beacon"].address
    vault = bind(sim.w3, sim.dep).vault_at(beacon_vault)
    assert [o["orderNumber"] for o in result["opened"]] == [5, 6, 7]
    assert all(o["script"] == "never" for o in result["opened"])
    assert len(result["refunds"]) == 3
    for oid in result["refunds"]:
        order = vault.get_order(oid)
        assert order.state == OrderState.REFUNDED and order.outcome == Outcome.FAILED and order.claimed_at == 0
    refunds = _events(sim, beacon_vault, "EscrowRefunded", start)
    assert [r["args"]["reasonCode"] for r in refunds] == [2400] * 3
    assert len(result["reports"]) == 1
    rep = bind(sim.w3, sim.dep).reputation
    ghost = sim.dep.agents["ghost"].agent_id
    feedback = rep.read_feedback(ghost, sim.keys.address("beacon"), 1)
    assert feedback is not None
    payload = json.loads((sim.out_dir / "reports" / f"{result['reports'][0]}.json").read_bytes())
    assert payload["kind"] == "NON_DELIVERY" and payload["evidence"]["reasonCode"] == 2400

    boost = sim.run_action("boost_ghost", {})["result"]["reports"]
    smear = sim.run_action("smear_atlas", {})["result"]["reports"]
    assert len(boost) == len(smear) == 3
    functions = [t["function"] for t in out["txs"]]
    assert functions.count("escrow") == 3 and functions.count("refund") == 3 and functions.count("giveFeedback") == 1

    appeal = sim.run_action("ghost_appeal", {"step": "S20"})["result"]
    assert appeal["appealId"] == 1 and sorted(appeal["orderIds"]) == sorted(result["refunds"])
    assert bind(sim.w3, sim.dep).acb.appeal_agent(1) == ghost


def test_ghost_default_round_in_two_phases(anvil_sim):
    sim = anvil_sim.sim
    opened = sim.run_action("ghost_default_round", {"phase": "open"})["result"]
    assert len(opened["opened"]) == 3 and "refunds" not in opened
    # the first four Ghost orders are delivered (no warm-up here): orders 1-3 are claimed
    assert [o["state"] for o in opened["opened"]] == ["RELEASED"] * 3
    closed = sim.run_action("ghost_default_round", {"phase": "close"})["result"]
    assert closed["refunds"] == {} and closed["reports"] == []


@pytest.mark.slow
def test_orion_becomes_hijacked_after_the_warmup_and_s10(anvil_sim):
    from acb.injection import detect, invoice_fields

    sim = anvil_sim.sim
    sim.run_action("warmup", {})
    assert sim.state.counter("orion", "orders") == 4
    # S10: the wallet's honest Orion order (simulated here by Beacon buying directly)
    b, signed, oid, _ = _beacon_buys(anvil_sim, "orion", "api-bundle-1k", 3, escrow=False)
    assert not detect(invoice_fields(signed)).quarantine
    assert sim.deliver("orion", {"orderId": oid, "invoiceId": signed["invoiceId"]})["status"] == "delivered"
    # S15: the 6th and 7th quotes carry the templates
    body = {
        "quoteRequestId": "qr-" + secrets.token_hex(16),
        "buyerAgentId": str(b.agent_id),
        "buyerVault": b.address,
        "sku": "api-bundle-1k",
        "quantity": 3,
        "capOf": "",
    }
    a = sim.quote("orion", body)
    assert a["memo"] == sim.cfg.template("injection_amount") and detect(invoice_fields(a)).adverse
