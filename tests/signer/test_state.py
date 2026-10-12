"""Signer state, decision log and rate limiter (no chain)."""

from __future__ import annotations

import json
import uuid

import pytest

from acb.canonical import chash
from acb.signer.decision_log import DecisionLog
from acb.signer.reports import bad_data_payload, endpoint_for, report_payload
from acb.signer.state import IN_PROGRESS, RateLimiter, SignerState, valid_request_id
from acb.types import ExecuteResponse


def test_request_ids_are_uuid4():
    assert valid_request_id(str(uuid.uuid4()))
    assert not valid_request_id(str(uuid.uuid1()))
    assert not valid_request_id(str(uuid.uuid4()).upper())
    assert not valid_request_id("../../etc/passwd")


def test_requests_are_stored_and_survive_a_restart(tmp_path):
    state = SignerState(tmp_path)
    rid = str(uuid.uuid4())
    assert state.stored(rid) is None
    state.begin(rid)
    assert state.stored(rid) == IN_PROGRESS
    resp = ExecuteResponse(
        request_id=rid, status="executed", decision=None, tx=(), violation=None, reason_codes=(2000,), log_seq=7
    )
    state.finish(resp)
    assert SignerState(tmp_path).stored(rid) == resp
    with pytest.raises(ValueError):
        state.stored("not-a-uuid")


def test_invoices_quote_requests_violations_budget_reports(tmp_path):
    state = SignerState(tmp_path)
    h1, h2 = "0x" + "11" * 32, "0x" + "22" * 32
    assert not state.seen_conflict(5, "inv-1", h1)
    state.mark_invoice("qr-" + "a" * 32, 5, "inv-1", h1)
    assert state.quote_request_used_by("qr-" + "a" * 32) == h1
    assert not state.seen_conflict(5, "inv-1", h1) and state.seen_conflict(5, "inv-1", h2)
    assert not state.seen_conflict(6, "inv-1", h2)
    state.add_violation(1000)
    state.add_violation(1500)
    assert state.violations_in_window(1700, 600) == [1500]
    for t in range(3):
        state.add_recorded(10_000 + t)
    assert state.recording_allowed(10_010, 4) and not state.recording_allowed(10_010, 3)
    assert state.recording_allowed(10_000 + 3600, 3)
    key = SignerState.report_key(5, "SPAM", "0x" + "00" * 32)
    state.add_report(key, h1)
    reloaded = SignerState(tmp_path)
    assert reloaded.report_filed(key) == h1 and reloaded.violation_times == [1000, 1500]
    assert json.loads((tmp_path / "state.json").read_bytes())["schema"] == "acb-signer-state/1"


def test_decision_log_is_append_only_and_indexed(tmp_path):
    path = tmp_path / "decisions.jsonl"
    log = DecisionLog(path)
    h = "0x" + "ab" * 32
    assert log.lookup(h) is None and h not in log
    log.append(decision_hash=h, request_id="r1", kind="PAY", order_id="0x" + "01" * 32, outcome="PAY", reasons=(2000,))
    assert log.lookup(h)["txHash"] is None
    log.add_tx(h, "0x" + "cd" * 32)
    log.append(decision_hash=h, request_id="r2", kind="PAY", order_id="0x" + "01" * 32, outcome="PAY", reasons=(2000,))
    again = DecisionLog(path)  # rebuilt from the file
    found = again.lookup(h.upper().replace("0X", "0x"))
    assert found["requestId"] == "r1" and found["txHash"] == "0x" + "cd" * 32
    lines = path.read_bytes().split(b"\n")
    assert len([x for x in lines if x]) == 3 and b"\r" not in path.read_bytes()


def test_rate_limiter_uses_the_injected_monotonic_clock():
    now = [0.0]
    rl = RateLimiter(2, 60.0, clock=lambda: now[0])
    assert rl.allowed()
    rl.record()
    rl.record()
    assert not rl.allowed()
    now[0] = 59.9
    assert not rl.allowed()
    now[0] = 60.0
    assert rl.allowed()


def test_report_payload_shapes():
    order = "0x" + "12" * 32
    vault = "0x" + "AB" * 20
    assert endpoint_for(order, 0, vault) == f"acb:v1;order={order};reporter=0;vault={vault.lower()}"
    assert endpoint_for(None, 3, vault).startswith("acb:v1;order=0x" + "00" * 32)
    p = report_payload("NON_DELIVERY", 7, 0, order, {"type": "order_outcome"})
    assert p["schema"] == "acb-report/1" and p["subjectAgentId"] == "7" and "detector" not in p
    bd = bad_data_payload(7, 0, order, vault)
    assert bd["evidence"] == {"type": "owner_confirmation", "vault": vault.lower(), "orderId": order}
    assert chash(bd) == chash(bad_data_payload(7, 0, order, vault.lower()))


def test_state_dir_is_bound_to_one_deployment(tmp_path):
    from acb.signer.state import prepare_state_dir

    folder = tmp_path / "signer"
    prepare_state_dir(folder, "0x" + "aa" * 32)
    state = SignerState(folder)
    state.add_violation(5)
    (folder / "decisions.jsonl").write_text("{}\n", encoding="utf-8")
    prepare_state_dir(folder, "0x" + "aa" * 32)  # same deployment: kept
    assert SignerState(folder).violation_times == [5]
    prepare_state_dir(folder, "0x" + "bb" * 32)  # a new chain: old state archived
    assert SignerState(folder).violation_times == []
    assert not (folder / "decisions.jsonl").exists()
    archived = folder / "archive" / ("0x" + "aa" * 8)
    assert (archived / "decisions.jsonl").is_file() and (archived / "state.json").is_file()
