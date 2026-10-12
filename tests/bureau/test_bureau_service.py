"""Bureau service P4 (spec 15.3): token checks, endpoints and loopback-only binding (with a stub
bureau, no chain) plus the CLI on Anvil (``acb bureau rescore`` then ``acb bureau recompute``)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from acb.bureau.service import create_app, serve
from acb.cli import build_parser, main
from acb.logbus import EventBus
from acb.types import Grade, GradeView, RescoreResult

VIEW = GradeView(
    agent_id=5,
    grade=Grade.A,
    effective_grade=Grade.A,
    score=740,
    reasons=(1006, 1007, 1008),
    model_id="acb-sc-1.0.0",
    config_hash="0x" + "11" * 32,
    evidence_hash="0x" + "22" * 32,
    as_of_block=40,
    posted_at=1_800_000_000,
    tx_hash="0x" + "33" * 32,
    evidence_uri="evidence/anvil/bundles/5-40.json",
)


class StubBureau:
    def __init__(self, out_dir: Path):
        self.net = SimpleNamespace(name="anvil", chain_id=31337)
        self.cfg = SimpleNamespace(model_id="acb-sc-1.0.0", config_hash="0x" + "11" * 32)
        self.address = "0x" + "44" * 20
        self.sender = object()
        self.out_dir = out_dir
        self.rescored: list[int] = []

    def grade_views(self):
        return [VIEW]

    def grade_view(self, agent_id):
        return VIEW if agent_id == 5 else None

    def rescore(self, rounds):
        self.rescored.append(rounds)
        return RescoreResult(rounds=rounds, postings=(VIEW,), unchanged=(1, 2), tx=())


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTS_TOKEN", "e" * 40)
    monkeypatch.setenv("BUREAU_TOKEN", "b" * 40)
    stub = StubBureau(tmp_path)
    bundle_dir = tmp_path / "bundles"
    bundle_dir.mkdir()
    (bundle_dir / "5-40.json").write_text(json.dumps({"schema": "acb-evidence/1"}))
    bus = EventBus("bureau", jsonl=False)
    return TestClient(create_app(stub, bus)), stub


EV = {"Authorization": "Bearer " + "e" * 40}
BU = {"Authorization": "Bearer " + "b" * 40}


def test_health_needs_no_token(client):
    c, _ = client
    r = c.get("/health")
    assert r.status_code == 200 and r.json()["modelId"] == "acb-sc-1.0.0"


def test_reads_need_the_events_token(client):
    c, _ = client
    assert c.get("/grades").status_code == 401
    assert c.get("/grades", headers=BU).status_code == 401
    r = c.get("/grades", headers=EV)
    assert r.status_code == 200 and r.json()[0]["agentId"] == "5" and r.json()[0]["grade"] == int(Grade.A)
    one = c.get("/grades/5", headers=EV).json()
    assert one["bundleUri"].endswith("5-40.json") and one["bundle"] == {"schema": "acb-evidence/1"}
    assert c.get("/grades/6", headers=EV).status_code == 404
    assert c.get("/events", headers=EV).status_code == 200


def test_rescore_needs_the_bureau_token_and_valid_rounds(client):
    c, stub = client
    assert c.post("/rescore", json={"rounds": 1}, headers=EV).status_code == 401
    assert c.post("/rescore", json={"rounds": 0}, headers=BU).status_code == 422
    assert c.post("/rescore", json={"rounds": 9}, headers=BU).status_code == 422
    assert c.post("/rescore", json={"rounds": 1, "extra": 1}, headers=BU).status_code == 422
    r = c.post("/rescore", json={"rounds": 3}, headers=BU)
    assert r.status_code == 200 and r.json()["rounds"] == 3 and stub.rescored == [3]
    assert c.post("/rescore", headers=BU).status_code == 200 and stub.rescored == [3, 1]


def test_missing_token_configuration_is_503(client, monkeypatch):
    c, _ = client
    monkeypatch.delenv("EVENTS_TOKEN")
    assert c.get("/grades", headers=EV).status_code == 503


def test_serve_refuses_other_interfaces():
    with pytest.raises(SystemExit):
        serve("anvil", host="0.0.0.0")


def test_cli_mounts_bureau_and_autonomy():
    _, status = build_parser()
    assert status["acb.bureau.cli"] == "ok"
    assert status["acb.autonomy.cli"] == "ok"


@pytest.mark.chain
def test_cli_rescore_then_recompute_on_anvil(anvil, fresh_deployed, out_dir, monkeypatch, warp, capsys):
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from bureau_chain import ChainMarket

    _, w3 = anvil
    monkeypatch.setenv("EVIDENCE_BASE_URI", str(out_dir) + "/")
    market = ChainMarket(w3, fresh_deployed, out_dir)
    mid = market.mandate("beacon", ["atlas"])
    for _ in range(4):
        market.escrow_release("beacon", "atlas", 200_000, mid)
    warp(700)
    none_env = str(out_dir / "none.env")
    assert main(["bureau", "rescore", "--network", "anvil", "--rounds", "2", "--env-file", none_env]) == 0
    out = capsys.readouterr().out
    assert "posted agent" in out
    atlas = fresh_deployed.agents["atlas"].agent_id
    code = main(["bureau", "recompute", "--network", "anvil", "--agent-id", str(atlas), "--env-file", none_env])
    out = capsys.readouterr().out
    assert code == 0 and out.splitlines()[0] == "MATCH"
    record = market.c.acb.get_record(atlas)
    tx = json.loads(out.split("\n", 1)[1])["tx"]
    assert record.grade == Grade.A
    assert main(["bureau", "recompute", "--network", "anvil", "--tx", tx, "--env-file", none_env]) == 0
    assert capsys.readouterr().out.startswith("MATCH")
    assert (out_dir / "bureau.jsonl").is_file()
    before = int(w3.eth.block_number)  # the read-only score command never mines or waits
    assert main(["bureau", "score", "--network", "anvil", "--agent-id", str(atlas), "--env-file", none_env]) == 0
    assert int(w3.eth.block_number) == before and f"asOfBlock {before}" in capsys.readouterr().out
