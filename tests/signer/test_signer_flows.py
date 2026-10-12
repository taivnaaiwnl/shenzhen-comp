"""The signer on Anvil (spec 7.3): normal flows S6-S10, S14, S18 and the per-kind checks."""

from __future__ import annotations

import json

import pytest
from signer_env import U, make_executor

from acb.canonical import chash, order_id
from acb.types import Decision, Grade, OrderState, Outcome

pytestmark = pytest.mark.chain


def _order(env, si):
    return order_id(env.dep.chain_id, env.vault.address, int(si.invoice.issuer_agent_id), si.invoice.invoice_id)


def test_s6_pay_executes_and_logs_the_decision_before_sending(env):
    si, qr = env.invoice("atlas", 4)  # 4 x 0.20 = 0.80, grade A, tier 1
    sent_with_log: list[bool] = []
    original = env.executor._send

    def spy(run, fn):
        if fn.fn_name == "pay":
            sent_with_log.append(run.decision.decision_hash in env.executor.decisions)
        return original(run, fn)

    env.executor._send = spy
    resp = env.executor.execute(env.pay_request(si, qr))
    assert resp.status == "executed", resp
    assert resp.reason_codes == (2000,) and resp.violation is None
    assert sent_with_log == [True]  # 7.3 step 6: decision log entry before the outflow
    tx = resp.tx[-1]
    assert tx.function == "pay" and tx.status == 1 and tx.from_address == env.keys.address("agent_signer")
    paid = next(log for log in tx.logs if log["event"] == "Paid")
    assert paid["args"]["decisionHash"] == resp.decision.decision_hash
    assert paid["args"]["invoiceHash"] == si.invoice_hash
    order = env.vault.get_order(_order(env, si))
    assert order.state == OrderState.PAID and order.amount == 800_000
    found = env.executor.decisions.lookup(resp.decision.decision_hash)
    assert (
        found["found"]
        and found["txHash"] == tx.hash
        and found["kind"] == "PAY"
        and found["orderId"]
        == order_id(env.dep.chain_id, env.vault.address, env.agent_id("atlas"), si.invoice.invoice_id)
    )
    # the log line of 7.5 is an event of signer.jsonl and never holds counterparty text
    lines = (env.executor.out_dir / "signer.jsonl").read_text(encoding="utf-8").splitlines()
    last = json.loads(lines[-1])
    assert last["type"] == "decision" and last["seq"] == resp.log_seq and last["reason_codes"] == [2000]
    request = last["data"]["request"]
    assert request["invoiceHash"] == si.invoice_hash and request["signerDecision"]["outcome"] == "PAY"
    assert request["tx"][0]["function"] == "pay" and "Thanks for your order" not in lines[-1]


def test_idempotent_request_id_never_resends(env):
    si, qr = env.invoice("atlas", 2)
    req = env.pay_request(si, qr)
    first = env.executor.execute(req)
    nonce = env.w3.eth.get_transaction_count(env.keys.checksum_address("agent_signer"))
    again = env.executor.execute(req)
    assert again == first
    assert env.w3.eth.get_transaction_count(env.keys.checksum_address("agent_signer")) == nonce
    # a new executor on the same state folder (restart) returns the stored result too
    from acb.signer.executor import Executor

    restarted = Executor(
        w3=env.w3,
        dep=env.dep,
        account=env.keys.account("agent_signer"),
        state_dir=env.executor.state.folder,
        out_dir=env.executor.out_dir,
        cfg=env.cfg,
        bus=env.executor.bus,
    )
    assert restarted.execute(req) == first
    assert restarted.stored(req.request_id) == first
    assert restarted.decisions.lookup(first.decision.decision_hash)["txHash"] == first.tx[-1].hash


def test_interrupted_request_is_never_executed_again(env):
    si, qr = env.invoice("atlas", 2)
    req = env.pay_request(si, qr)
    env.executor.state.begin(req.request_id)  # as if the process died mid-request
    resp = env.executor.execute(req)
    assert resp.status == "failed" and resp.reason_codes == (4104,) and resp.tx == ()
    assert env.vault.get_order(_order(env, si)).state == OrderState.NONE


def test_s7_capped_preview_is_never_executed_then_requote_pays_capped(env):
    sku = "fx-history-day"
    si, qr = env.invoice("slowpoke", 5, sku=sku)  # 1.00 > B cap 0.50
    resp = env.executor.execute(env.pay_request(si, qr))
    assert resp.status == "rejected" and resp.reason_codes == (2002, 2301, 4104) and resp.tx == ()
    assert resp.decision.requote_quantity == 2
    si2, qr2 = env.invoice("slowpoke", 2, sku=sku, cap_of=si.invoice.invoice_id)
    resp = env.executor.execute(env.pay_request(si2, qr2, "PAY_CAPPED"))
    assert resp.status == "executed" and resp.reason_codes == (2002,)
    paid = next(log for log in resp.tx[-1].logs if log["event"] == "Paid")
    assert paid["args"]["decision"] == int(Decision.PAY_CAPPED) and paid["args"]["amount"] == 400_000


def test_s8_escrow_claim_and_release_with_payload_check(env):
    si, qr = env.invoice("nova", 1)  # NEW: escrow 0.40
    resp = env.executor.execute(env.pay_request(si, qr, "ESCROW"))
    assert resp.status == "executed" and resp.reason_codes == (2004,)
    assert resp.tx[-1].function == "escrow"
    order = _order(env, si)
    payload = chash({"data": [1, 2, 3]})
    # release before any claim: rejected
    d = env.delivery("nova", order, si.invoice.invoice_id, payload)
    resp = env.executor.execute(env.request("RELEASE", order_id=order, delivery=d))
    assert resp.status == "rejected" and resp.reason_codes == (4104,)
    env.send("nova", env.vault.claim_delivered(order, payload))
    # a receipt signed by someone else, or with another payload hash, is refused
    bad = env.delivery("nova", order, si.invoice.invoice_id, payload, signer="attacker")
    assert env.executor.execute(env.request("RELEASE", order_id=order, delivery=bad)).status == "rejected"
    other = env.delivery("nova", order, si.invoice.invoice_id, chash({"data": "x"}))
    assert env.executor.execute(env.request("RELEASE", order_id=order, delivery=other)).status == "rejected"
    resp = env.executor.execute(env.request("RELEASE", order_id=order, delivery=d))
    assert resp.status == "executed" and resp.tx[-1].function == "release"
    info = env.vault.get_order(order)
    assert info.state == OrderState.RELEASED and info.outcome == Outcome.ON_TIME


def test_record_delivery_on_time_late_and_failed(env):
    si, qr = env.invoice("atlas", 2, delivery=60)
    assert env.executor.execute(env.pay_request(si, qr)).status == "executed"
    order = _order(env, si)
    d = env.delivery("atlas", order, si.invoice.invoice_id, chash({"p": 1}))
    # received_at must be a plausible chain time
    resp = env.executor.execute(env.request("RECORD_DELIVERY", order_id=order, delivery=d, received_at=env.now() + 99))
    assert resp.status == "rejected"
    resp = env.executor.execute(env.request("RECORD_DELIVERY", order_id=order, delivery=d, received_at=env.now()))
    assert resp.status == "executed" and env.vault.get_order(order).outcome == Outcome.ON_TIME

    si, qr = env.invoice("atlas", 1, delivery=30)
    env.executor.execute(env.pay_request(si, qr))
    order = _order(env, si)
    env.warp(40)
    d = env.delivery("atlas", order, si.invoice.invoice_id, chash({"p": 2}))
    resp = env.executor.execute(env.request("RECORD_DELIVERY", order_id=order, delivery=d, received_at=env.now()))
    assert resp.status == "executed" and env.vault.get_order(order).outcome == Outcome.LATE

    si, qr = env.invoice("orion", 1, delivery=30)
    env.executor.execute(env.pay_request(si, qr))
    order = _order(env, si)
    early = env.executor.execute(env.request("RECORD_DELIVERY", order_id=order))
    assert early.status == "rejected"  # FAILED needs deadline + grace
    env.warp(30 + env.cfg.delivery.grace_s + 1)
    resp = env.executor.execute(env.request("RECORD_DELIVERY", order_id=order))
    assert resp.status == "executed" and env.vault.get_order(order).outcome == Outcome.FAILED

    # C14 exposure: ON_TIME and LATE settle an order; a FAILED order is a realised loss and stays exposed
    view = env.executor.view
    block = view.block()
    assert view.payee_logs(env.agent_id("atlas"), block).unsecured_exposure == 0
    assert view.payee_logs(env.agent_id("orion"), block).unsecured_exposure == int(si.invoice.amount)


def test_s14_refund_and_non_delivery_report(env):
    si, qr = env.invoice("nova", 1, delivery=60)
    assert env.executor.execute(env.pay_request(si, qr, "ESCROW")).status == "executed"
    order = _order(env, si)
    early = env.executor.execute(env.request("REFUND", order_id=order, reason_code=2400))
    assert early.status == "rejected" and early.reason_codes == (4104,)  # deadline not passed
    assert env.executor.execute(env.request("REFUND", order_id=order, reason_code=2403)).status == "rejected"
    env.warp(61)
    resp = env.executor.execute(env.request("REFUND", order_id=order, reason_code=2400))
    assert resp.status == "executed" and resp.reason_codes == (2400,)
    assert env.vault.get_order(order).outcome == Outcome.FAILED
    from acb.types import ReportRequest

    report = ReportRequest(kind="NON_DELIVERY", subject_agent_id=env.agent_id("nova"), order_id=order, invoice=None)
    resp = env.executor.execute(env.request("REPORT", report=report))
    assert resp.status == "executed" and resp.tx[-1].function == "giveFeedback"
    fb = next(log for log in resp.tx[-1].logs if log["event"] == "NewFeedback")
    assert fb["args"]["tag2"] == "NON_DELIVERY" and fb["args"]["value"] == -1
    assert (
        fb["args"]["endpoint"]
        == f"acb:v1;order={order};reporter={env.dep.vaults['wallet'].agent_id};vault={env.vault.address}"
    )
    feedback_hash = fb["args"]["feedbackHash"]
    path = env.executor.out_dir / "reports" / f"{feedback_hash}.json"
    assert chash(json.loads(path.read_bytes())) == feedback_hash
    assert fb["args"]["feedbackURI"] == f"evidence/anvil/reports/{feedback_hash}.json"
    # one report per (subject, kind, order)
    again = env.executor.execute(env.request("REPORT", report=report))
    assert again.status == "rejected"


def test_grade_drop_to_c_between_passes_executes_the_stricter_escrow(env):
    si, qr = env.invoice("atlas", 2)
    p = env.proposal(si, "PAY")
    pass1 = env.pass1(si, qr, p)
    assert pass1.outcome == Decision.PAY
    env.post_grade("atlas", Grade.C)
    resp = env.executor.execute(env.pay_request(si, qr, agent_decision=pass1))
    assert resp.status == "executed" and resp.decision.outcome == Decision.ESCROW
    assert resp.reason_codes == (2003, 2115) and resp.tx[-1].function == "escrow"


def test_s18_owner_approved_execution_at_tier0(env):
    env.send("autonomy", env.vault.set_tier(0, 3105, "0x" + "22" * 32))
    si, qr = env.invoice("atlas", 2)
    resp = env.executor.execute(env.pay_request(si, qr, "REQUEST_OWNER_APPROVAL"))
    assert resp.status == "refused" and resp.reason_codes == (2200,)
    assert resp.tx[-1].function == "recordDecision"
    order = _order(env, si)
    assert env.vault.get_order(order).state == OrderState.NONE  # NEEDS_OWNER_APPROVAL leaves the order open
    # PAY_APPROVED before the owner approved: rejected (A04: approvals exist only as owner txs)
    req = env.pay_request(si, qr, kind="PAY_APPROVED", source="owner")
    req = req.model_copy(update={"order_id": order})
    resp = env.executor.execute(req)
    assert resp.status == "rejected" and resp.reason_codes == (4104,)
    wallet = env.c.identity.get_agent_wallet(env.agent_id("atlas"))
    env.send("owner", env.vault.owner_approve(order, env.agent_id("atlas"), wallet, 400_000, env.now() + 900, False, 0))
    req = env.pay_request(si, qr, kind="PAY_APPROVED", source="owner").model_copy(update={"order_id": order})
    resp = env.executor.execute(req)
    assert resp.status == "executed" and resp.reason_codes == (2007,), resp
    assert resp.tx[-1].function == "executeApproved"
    assert env.vault.get_order(order).state == OrderState.PAID
    assert env.executor.decisions.lookup(resp.decision.decision_hash)["txHash"] == resp.tx[-1].hash


def test_simulation_revert_is_rejected_4102_not_sent_and_not_a_violation(env):
    si, qr = env.invoice("atlas", 2, delivery=1)
    env.warp(5)  # the delivery deadline is in the past: the vault reverts BadDeadline
    nonce = env.w3.eth.get_transaction_count(env.keys.checksum_address("agent_signer"))
    resp = env.executor.execute(env.pay_request(si, qr))
    assert resp.status == "rejected" and resp.reason_codes == (2000, 4102) and resp.violation is None
    assert env.w3.eth.get_transaction_count(env.keys.checksum_address("agent_signer")) == nonce
    assert env.executor.state.violation_times == []


def test_rate_limit_rejects_4103(env):
    clock = [1000.0]
    ex = make_executor(
        env,
        cfg=env.cfg.model_copy(update={"signer": env.cfg.signer.model_copy(update={"max_outflows_per_minute": 1})}),
        monotonic=lambda: clock[0],
    )
    si, qr = env.invoice("atlas", 1)
    assert ex.execute(env.pay_request(si, qr)).status == "executed"
    si2, qr2 = env.invoice("atlas", 1)
    resp = ex.execute(env.pay_request(si2, qr2))
    assert resp.status == "rejected" and resp.reason_codes[-1] == 4103 and resp.violation is None
    assert ex.state.violation_times == []  # a rate-limit rejection is not a violation
    clock[0] += 61
    resp = ex.execute(env.pay_request(si2, qr2))
    assert resp.status == "executed"


def test_recording_budget_then_2112_off_chain(env):
    cfg = env.cfg.model_copy(
        update={"signer": env.cfg.signer.model_copy(update={"max_recorded_decisions_per_hour": 1})}
    )
    ex = make_executor(env, cfg=cfg)
    env.post_grade("ghost", Grade.D)
    si, qr = env.invoice("ghost", 1)
    first = ex.execute(env.pay_request(si, qr, "REFUSE"))
    assert first.status == "refused" and first.reason_codes == (2100,) and first.tx[-1].function == "recordDecision"
    si2, qr2 = env.invoice("ghost", 1)
    second = ex.execute(env.pay_request(si2, qr2, "REFUSE"))
    assert second.status == "refused" and second.reason_codes == (2100, 2112) and second.tx == ()


def test_unknown_mandate_or_invoice_rules_are_rejected(env):
    si, qr = env.invoice("atlas", 1)
    resp = env.executor.execute(env.pay_request(si, qr).model_copy(update={"mandate_id": "0x" + "99" * 32}))
    assert resp.status == "rejected" and resp.reason_codes == (4104,)
    # a quote request that answers another sku: V08 at the signer -> 2119, dropped off-chain
    bad_qr = qr.model_copy(update={"quantity": 3})
    req = env.pay_request(si, bad_qr)
    resp = env.executor.execute(req)
    assert resp.status == "rejected" and resp.reason_codes == (2119,) and resp.tx == ()
    # an ESCROW request whose decision is PAY is not silently turned into something else
    resp = env.executor.execute(env.pay_request(si, qr, kind="ESCROW"))
    assert resp.status == "rejected"


def test_health_shows_only_public_data(env):
    h = env.executor.health()
    assert h["ok"] and h["address"] == env.keys.address("agent_signer") and h["chainId"] == 31337
    assert h["vault"] == env.vault.address and isinstance(h["nonce"], int)
    assert set(h) == {"ok", "address", "chainId", "vault", "nonce"}


def test_tier_and_balance_unchanged_by_refusals(env):
    before = env.c.usdc.balance_of(env.vault.address)
    env.post_grade("ghost", Grade.D)
    si, qr = env.invoice("ghost", 1)
    env.executor.execute(env.pay_request(si, qr, "REFUSE"))
    assert env.c.usdc.balance_of(env.vault.address) == before and env.tier() == 1
    assert U == 1_000_000


def test_a59_exposure_from_vault_logs_turns_pay_into_escrow(env):
    si, qr = env.invoice("atlas", 4)  # 0.80 paid, no delivery recorded: unsecured exposure 0.80
    assert env.executor.execute(env.pay_request(si, qr)).status == "executed"
    si2, qr2 = env.invoice("atlas", 2)  # 0.80 + 0.40 > A limit 1.00
    resp = env.executor.execute(env.pay_request(si2, qr2))
    assert resp.status == "executed" and resp.decision.outcome == Decision.ESCROW
    assert resp.reason_codes == (2009, 2301) and resp.tx[-1].function == "escrow"
    # once the first delivery is recorded the exposure is gone again
    order = _order(env, si)
    d = env.delivery("atlas", order, si.invoice.invoice_id, chash({"p": 9}))
    env.executor.execute(env.request("RECORD_DELIVERY", order_id=order, delivery=d, received_at=env.now()))
    si3, qr3 = env.invoice("atlas", 2)
    assert env.executor.execute(env.pay_request(si3, qr3)).reason_codes == (2000,)
