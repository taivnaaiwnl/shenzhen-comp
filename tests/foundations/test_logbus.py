"""Event bus and /events router (spec 10.3)."""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from acb.logbus import EventBus, events_router, format_log_line, format_usdc, read_jsonl


def test_emit_writes_jsonl_with_the_envelope(out_dir):
    bus = EventBus(
        "signer", run_id="r-20261017-0900", chain_id=84532, explorer_tx="https://sepolia.basescan.org/tx/{hash}"
    )
    tx = "0x" + "ab" * 32
    ev = bus.emit(
        "decision",
        {
            "orderId": "0x01",
            "payee": {"agentId": "53", "name": "Ghost Feeds", "grade": "D"},
            "amount": "400000",
            "outcome": "REFUSE",
            "tier": 2,
        },
        step="S12",
        reason_codes=[2100],
        tx_hash=tx,
    )
    assert ev.seq == 1 and ev.explorer == f"https://sepolia.basescan.org/tx/{tx}"
    lines = (out_dir / "signer.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert list(row) == [
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
    assert row["run_id"] == "r-20261017-0900" and row["chain_id"] == 84532 and row["reason_codes"] == [2100]
    assert row["ts"].endswith("Z") and len(row["ts"]) == 20
    line = format_log_line(ev)
    assert line.endswith(f"S12 REFUSE Ghost Feeds #53 0.40 USDC grade=D tier=2 reasons=2100 tx={tx[:10]}...")


def test_sequence_continues_after_restart_and_since(out_dir):
    bus = EventBus("agent", run_id="r")
    for i in range(3):
        bus.emit("info", {"i": i})
    again = EventBus("agent", run_id="r")
    assert again.emit("info", {"i": 3}).seq == 4
    assert [e.seq for e in again.since(0)] == [4]
    assert [e.seq for e in bus.since(1)] == [2, 3]
    assert [e.seq for e in bus.since(0, limit=1)] == [1]
    assert [e.data["i"] for e in read_jsonl(out_dir / "agent.jsonl")] == [0, 1, 2, 3]


def test_unknown_type_and_memory_only(tmp_path):
    bus = EventBus("bureau", run_id="r", jsonl=False)
    with pytest.raises(ValueError):
        bus.emit("hack", {})
    bus.emit("grade", {"agentId": "7"})
    assert bus.path is None and bus.last_seq == 1
    assert list(tmp_path.iterdir()) == []


def test_wait_returns_new_events(tmp_path):
    bus = EventBus("sim", run_id="r", out_dir=tmp_path)
    threading.Timer(0.2, lambda: bus.emit("message", {"m": 1})).start()
    t0 = time.monotonic()
    events = bus.wait(0, timeout=5)
    assert [e.seq for e in events] == [1] and time.monotonic() - t0 < 4
    assert bus.wait(1, timeout=0.1) == []


def test_format_usdc():
    assert format_usdc("400000") == "0.40"
    assert format_usdc(15_000_000) == "15.00"
    assert format_usdc(1) == "0.000001"
    assert format_usdc(-250000) == "-0.25"


def test_events_router_requires_the_token(tmp_path, monkeypatch):
    bus = EventBus("autonomy", run_id="r", out_dir=tmp_path)
    app = FastAPI()
    app.include_router(events_router(bus))
    client = TestClient(app)
    monkeypatch.delenv("EVENTS_TOKEN", raising=False)
    assert client.get("/events").status_code == 503
    monkeypatch.setenv("EVENTS_TOKEN", "t" * 40)
    assert client.get("/events").status_code == 401
    assert client.get("/events", headers={"Authorization": "Bearer wrong"}).status_code == 401
    bus.emit("tier", {"from": 1, "to": 2}, reason_codes=[3000])
    bus.emit("info", {"x": 1})
    ok = client.get("/events?after=1", headers={"Authorization": "Bearer " + "t" * 40})
    assert ok.status_code == 200
    body = ok.json()
    assert [e["seq"] for e in body] == [2] and body[0]["run_id"] == "r"
    waited = client.get("/events?after=2&wait=0.3", headers={"Authorization": "Bearer " + "t" * 40})
    assert waited.status_code == 200 and waited.json() == []
