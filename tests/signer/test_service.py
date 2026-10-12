"""Signer transport (spec 7.1, 7.2): loopback only, constant-time tokens, 64 KiB bodies, 422 for unknown
kinds, idempotent request ids, decision lookups for the autonomy operator, event feed."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from acb.logbus import EventBus
from acb.signer.decision_log import DecisionLog
from acb.signer.service import MAX_BODY_BYTES, create_app, serve, token_ok
from acb.types import ExecuteRequest, ExecuteResponse

TOKENS = {"SIGNER_TOKEN": "s" * 40, "EVENTS_TOKEN": "e" * 40}
SIGNER = {"Authorization": "Bearer " + TOKENS["SIGNER_TOKEN"]}
EVENTS = {"Authorization": "Bearer " + TOKENS["EVENTS_TOKEN"]}


class FakeExecutor:
    """Duck-typed executor: records requests and answers like the real one (no chain)."""

    def __init__(self, tmp: Path):
        self.bus = EventBus("signer", out_dir=tmp)
        self.decisions = DecisionLog(tmp / "decisions.jsonl")
        self.requests: list[ExecuteRequest] = []
        self._stored: dict[str, ExecuteResponse] = {}

    def health(self):
        return {"ok": True, "address": "0x" + "ab" * 20, "chainId": 31337, "vault": "0x" + "cd" * 20, "nonce": 3}

    def execute(self, req: ExecuteRequest) -> ExecuteResponse:
        if req.request_id in self._stored:
            return self._stored[req.request_id]
        self.requests.append(req)
        resp = ExecuteResponse(
            request_id=req.request_id,
            status="refused",
            decision=None,
            tx=(),
            violation=None,
            reason_codes=(2100,),
            log_seq=1,
        )
        self._stored[req.request_id] = resp
        return resp

    def stored(self, request_id: str):
        return self._stored.get(request_id)


@pytest.fixture
def fake(tmp_path):
    ex = FakeExecutor(tmp_path)
    return ex, TestClient(create_app(ex, environ=TOKENS))


def _body(kind="RECORD_DECISION", **extra):
    return {"requestId": str(uuid.uuid4()), "kind": kind, **extra}


def test_health_needs_no_token_and_shows_no_secret(fake):
    _, client = fake
    r = client.get("/health")
    assert r.status_code == 200 and set(r.json()) == {"ok", "address", "chainId", "vault", "nonce"}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Basic " + "s" * 40},
        EVENTS,
        {"Authorization": "Bearer "},
    ],
)
def test_execute_needs_the_signer_token(fake, headers):
    ex, client = fake
    r = client.post("/v1/execute", json=_body(), headers=headers)
    assert r.status_code == 401 and r.json()["reasonCodes"] == [4100]
    assert ex.requests == []
    fails = [e for e in ex.bus.since(0) if e.type == "error" and e.data.get("event") == "signer.auth_fail"]
    assert fails and TOKENS["SIGNER_TOKEN"] not in json.dumps([e.model_dump() for e in fails])


def test_read_endpoints_use_the_right_tokens(fake):
    _, client = fake
    assert client.get("/events", headers=SIGNER).status_code == 401
    assert client.get("/v1/decisions/0x" + "00" * 32, headers=SIGNER).status_code == 401
    assert client.get(f"/v1/requests/{uuid.uuid4()}", headers=EVENTS).status_code == 401
    assert client.get("/events", headers=EVENTS).status_code == 200
    assert client.get(f"/v1/requests/{uuid.uuid4()}", headers=SIGNER).status_code == 404


def test_unconfigured_tokens_never_match(tmp_path):
    ex = FakeExecutor(tmp_path)
    client = TestClient(create_app(ex, environ={}))
    assert client.post("/v1/execute", json=_body(), headers={"Authorization": "Bearer "}).status_code == 401
    assert not token_ok("Bearer ", "") and not token_ok(None, "x") and token_ok("bearer x", "x")


def test_execute_round_trip_and_idempotency(fake):
    ex, client = fake
    body = _body()
    r1 = client.post("/v1/execute", json=body, headers=SIGNER)
    r2 = client.post("/v1/execute", json=body, headers=SIGNER)
    assert r1.status_code == 200 and r1.json() == r2.json()
    assert r1.json()["requestId"] == body["requestId"] and r1.json()["reasonCodes"] == [2100]
    assert len(ex.requests) == 1
    r3 = client.get(f"/v1/requests/{body['requestId']}", headers=SIGNER)
    assert r3.status_code == 200 and r3.json() == r1.json()


def test_unknown_kind_and_malformed_bodies_are_422_4104(fake):
    ex, client = fake
    r = client.post("/v1/execute", json=_body("SET_TIER"), headers=SIGNER)
    assert r.status_code == 422 and r.json() == {"detail": "unknown kind", "reasonCodes": [4104]}
    r = client.post("/v1/execute", json=_body(extra="x"), headers=SIGNER)
    assert r.status_code == 422 and r.json()["reasonCodes"] == [4104]
    r = client.post("/v1/execute", json={**_body(), "requestId": "not-a-uuid"}, headers=SIGNER)
    assert r.status_code == 422
    r = client.post("/v1/execute", json={**_body(), "requestId": str(uuid.uuid1())}, headers=SIGNER)
    assert r.status_code == 422  # UUID4 only
    r = client.post("/v1/execute", content=b"{not json", headers=SIGNER)
    assert r.status_code == 422
    assert ex.requests == []


def test_body_cap_64_kib(fake):
    ex, client = fake
    big = _body(reasonCode=1)
    big["padding"] = "x" * MAX_BODY_BYTES
    r = client.post("/v1/execute", content=json.dumps(big).encode(), headers=SIGNER)
    assert r.status_code == 413 and ex.requests == []

    def chunks():  # no Content-Length: the stream is counted
        for _ in range(70):
            yield b"x" * 1024

    r = client.post("/v1/execute", content=chunks(), headers=SIGNER)
    assert r.status_code == 413


def test_decision_lookup_for_the_autonomy_operator(fake):
    ex, client = fake
    h = "0x" + "12" * 32
    assert client.get(f"/v1/decisions/{h}", headers=EVENTS).status_code == 404
    ex.decisions.append(
        decision_hash=h,
        request_id=str(uuid.uuid4()),
        kind="PAY",
        order_id="0x" + "34" * 32,
        outcome="PAY",
        reasons=(2000,),
    )
    ex.decisions.add_tx(h, "0x" + "56" * 32)
    r = client.get(f"/v1/decisions/{h}", headers=EVENTS)
    body = r.json()
    assert r.status_code == 200 and body["found"] and body["txHash"] == "0x" + "56" * 32 and body["kind"] == "PAY"
    assert set(body) == {"found", "requestId", "kind", "orderId", "outcome", "txHash", "loggedAt"}
    assert body["outcome"] == "PAY"
    # a logged refusal is found too, but its outcome tells a consumer it authorises no outflow
    h2 = "0x" + "13" * 32
    ex.decisions.append(
        decision_hash=h2,
        request_id=str(uuid.uuid4()),
        kind="PAY",
        order_id="0x" + "35" * 32,
        outcome="NEEDS_OWNER_APPROVAL",
        reasons=(2201, 2301),
    )
    body = client.get(f"/v1/decisions/{h2}", headers=EVENTS).json()
    assert body["found"] and body["outcome"] == "NEEDS_OWNER_APPROVAL" and body["txHash"] is None


def test_events_feed(fake):
    ex, client = fake
    ex.bus.emit("info", {"message": "hello"})
    r = client.get("/events?after=0", headers=EVENTS)
    assert r.status_code == 200 and any(e["data"].get("message") == "hello" for e in r.json())
    last = r.json()[-1]["seq"]
    assert client.get(f"/events?after={last}", headers=EVENTS).json() == []


def test_serve_refuses_other_interfaces(fake):
    ex, _ = fake
    for host in ("0.0.0.0", "localhost", "::1", "192.168.1.5"):
        with pytest.raises(SystemExit):
            serve(ex, host=host, port=1)


def test_cli_refuses_without_tokens_or_on_another_host(monkeypatch, capsys):
    from acb.cli import main

    monkeypatch.delenv("SIGNER_TOKEN", raising=False)
    monkeypatch.delenv("EVENTS_TOKEN", raising=False)
    assert main(["signer", "serve", "--network", "anvil"]) == 2
    assert "refusing" in capsys.readouterr().err
    assert main(["signer", "serve", "--host", "0.0.0.0"]) == 2


@pytest.mark.chain
def test_execute_over_http_on_anvil(env):
    client = TestClient(create_app(env.executor, environ=TOKENS))
    si, qr = env.invoice("atlas", 2)
    req = env.pay_request(si, qr)
    r = client.post("/v1/execute", content=req.model_dump_json(by_alias=True), headers=SIGNER)
    assert r.status_code == 200, r.text
    resp = ExecuteResponse.model_validate(r.json())
    assert resp.status == "executed" and resp.reason_codes == (2000,)
    look = client.get(f"/v1/decisions/{resp.decision.decision_hash}", headers=EVENTS).json()
    assert look["found"] and look["txHash"] == resp.tx[-1].hash
    assert client.get("/health").json()["address"] == env.keys.address("agent_signer")
