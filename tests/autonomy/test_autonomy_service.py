"""Autonomy service P5 (spec 15.3) with a stub evaluator: tokens, endpoints, loopback-only binding,
the signer decision-log client and the ``acb autonomy evaluate`` command on Anvil."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from acb.autonomy.evaluator import Plan, SignerDecisionLog
from acb.autonomy.service import create_app, serve
from acb.chain.contracts import VaultStatus
from acb.cli import main
from acb.logbus import EventBus
from acb.types import AutonomyResult

RESULT = AutonomyResult(
    rule="none", from_tier=1, to_tier=None, reason_code=None, evidence_hash=None, tx=None, simulated_revert=None
)


class StubEvaluator:
    def __init__(self):
        self.net = SimpleNamespace(name="anvil")
        self.vault_address = "0x" + "11" * 20
        self.sender = SimpleNamespace(address="0x" + "22" * 20)
        self.cfg = SimpleNamespace(poll_s=0)
        self.vault = SimpleNamespace(status=lambda: VaultStatus(1, False, 0, 2, 15, 0, 3, 0, 0, 2, 0, 100))
        self.last_plan = Plan("none", detail="cooldown")
        self.last_result = None
        self.unreachable_streak = 0
        self.calls = 0

    def evaluate(self):
        self.calls += 1
        self.last_result = RESULT
        return RESULT

    def run_forever(self, stop: threading.Event, interval: float) -> None:
        while not stop.wait(interval):
            self.evaluate()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EVENTS_TOKEN", "e" * 40)
    monkeypatch.setenv("AUTONOMY_TOKEN", "a" * 40)
    stub = StubEvaluator()
    return TestClient(create_app(stub, EventBus("autonomy", jsonl=False), poll_s=0)), stub


EV = {"Authorization": "Bearer " + "e" * 40}
AU = {"Authorization": "Bearer " + "a" * 40}


def test_evaluate_needs_the_autonomy_token(client):
    c, stub = client
    assert c.post("/evaluate").status_code == 401
    assert c.post("/evaluate", headers=EV).status_code == 401
    r = c.post("/evaluate", headers=AU)
    assert r.status_code == 200 and r.json()["rule"] == "none" and stub.calls == 1


def test_status_needs_the_events_token(client):
    c, _ = client
    assert c.get("/status", headers=AU).status_code == 401
    body = c.get("/status", headers=EV).json()
    assert body["status"]["tier"] == 1 and body["lastPlan"]["detail"] == "cooldown"
    assert c.get("/health").json()["operator"] == "0x" + "22" * 20
    assert c.get("/events", headers=EV).status_code == 200


def test_background_loop_runs_and_stops(monkeypatch):
    monkeypatch.setenv("EVENTS_TOKEN", "e" * 40)
    stub = StubEvaluator()
    with TestClient(create_app(stub, EventBus("autonomy", jsonl=False), poll_s=0.05)):
        deadline = threading.Event()
        deadline.wait(0.4)
    assert stub.calls >= 2


def test_serve_refuses_other_interfaces():
    with pytest.raises(SystemExit):
        serve("anvil", host="0.0.0.0")


def test_signer_decision_log_client(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        h = request.url.path.rsplit("/", 1)[-1]
        if h == "0xfound":
            return httpx.Response(200, json={"found": True, "requestId": "r1"})
        if h == "0xmissing":
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(401, json={"detail": "bad token"})

    transport = httpx.MockTransport(handler)
    real_get = httpx.get
    monkeypatch.setattr(
        httpx, "get", lambda url, **kw: httpx.Client(transport=transport).get(url, headers=kw.get("headers"))
    )
    log = SignerDecisionLog("http://127.0.0.1:9", token="t" * 40)
    assert log("0xfound") == {"found": True, "requestId": "r1"}  # the whole entry, matched by the operator
    assert log("0xmissing") is False
    assert log("0xother") is None  # 401: unknown, never "not found"
    assert seen["auth"] == "Bearer " + "t" * 40
    monkeypatch.setattr(httpx, "get", real_get)
    assert SignerDecisionLog("http://127.0.0.1:9", token="x", timeout=0.5)("0xabc") is None  # nothing listens


@pytest.mark.chain
def test_cli_evaluate_dry_run_on_anvil(anvil, fresh_deployed, out_dir, capsys, monkeypatch):
    import socket

    with socket.socket() as sock:  # a port where no signer listens
        sock.bind(("127.0.0.1", 0))
        monkeypatch.setenv("SIGNER_PORT", str(sock.getsockname()[1]))
    assert main(["autonomy", "evaluate", "--network", "anvil", "--dry-run", "--env-file", str(out_dir / "none")]) == 0
    out = capsys.readouterr().out
    assert '"rule"' in out
