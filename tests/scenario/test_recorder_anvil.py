"""Recorder and verify script on Anvil: record real transactions (success, expected revert, grade posting
with a bundle), write evidence, verify it offline and against the RPC, and catch tampering."""

from __future__ import annotations

import json

import pytest

from acb.canonical import canonical_bytes, chash
from acb.scenario.evidence import load_verifier, validate_evidence
from acb.scenario.recorder import Recorder

pytestmark = pytest.mark.chain


def _evidence(rec: Recorder, grade_changes: list[dict], contracts: dict) -> dict:
    rec.renumber()
    return {
        "schema": "acb-run/1",
        "runId": "r-recorder-test",
        "network": "anvil",
        "chainId": 31337,
        "startedAt": "2026-10-17T09:00:00Z",
        "finishedAt": "2026-10-17T09:10:00Z",
        "gitCommit": "test",
        "contracts": contracts,
        "erc8004": {},
        "llm": {},
        "steps": [],
        "transactions": [rec.public(r) for r in rec.records],
        "offchainDecisions": [],
        "gradeChanges": grade_changes,
    }


@pytest.fixture
def recorded(anvil, fresh_deployed, tmp_path):
    from acb.bureau.config import load_config
    from acb.chain.contracts import bind
    from acb.chain.tx import TxSender
    from acb.keys import KeyRing
    from acb.types import Grade

    url, w3 = anvil
    dep = fresh_deployed
    keys = KeyRing.anvil()
    c = bind(w3, dep)
    rec = Recorder(w3=w3, dep=dep, out_dir=tmp_path, chain_id=31337)
    rec.bind_deployment(dep, keys)
    rec.scanned_to = int(w3.eth.block_number)
    deployer = TxSender(w3, keys.account("deployer"))
    agent = TxSender(w3, keys.account("agent_signer"))
    bureau = TxSender(w3, keys.account("bureau"))
    owner = TxSender(w3, keys.account("owner"))
    vault = c.vaults["wallet"]
    ok = deployer.send(c.usdc.mint(dep.vaults["wallet"].address, 10_000))
    assert ok.status == 1
    bad = agent.send(vault.withdraw(keys.address("attacker"), 1), expect_revert=True)
    assert bad.status == 0
    rec.mark_expected_revert(bad.hash, bad.revert_error)
    frozen = owner.send(vault.freeze(3202, chash({"t": "freeze"})))
    assert frozen.status == 1
    # the owner rotates the agent key: AgentChanged(old, new) names both agent keys for the verifier
    rotated = owner.send(vault.unfreeze(1, 3300, keys.address("agent_signer_next")))
    assert rotated.status == 1
    # a grade posting whose bundle file hashes to the evidenceHash
    cfg = load_config(dep.anchors)
    bundle = {"schema": "acb-evidence/1", "note": "test bundle", "subject": {"agentId": "1"}}
    raw = canonical_bytes(bundle)
    (tmp_path / "bundles").mkdir()
    (tmp_path / "bundles" / "1-1.json").write_bytes(raw)
    aid = int(dep.agents["atlas"].agent_id)
    posted = bureau.send(
        c.acb.post_grade(
            aid,
            int(Grade.D),
            400,
            [1002],
            cfg.model_id,
            cfg.config_hash,
            chash(bundle),
            w3.eth.block_number - 1,
            "evidence/anvil/bundles/1-1.json",
        )
    )
    assert posted.status == 1
    rec.scan("S1")
    changes = [
        {
            "agentId": str(aid),
            "from": "NONE",
            "to": "D",
            "score": 400,
            "reasons": [1002],
            "txHash": posted.hash,
            "bundle": "bundles/1-1.json",
            "recompute": "NOT_RUN",
        }
    ]
    contracts = {
        "AgentCreditBureau": dep.contracts["AgentCreditBureau"].address.lower(),
        "vaults": {"wallet": dep.vaults["wallet"].address.lower()},
    }
    evidence = _evidence(rec, changes, contracts)
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return (
        url,
        path,
        evidence,
        {"ok": ok, "bad": bad, "frozen": frozen, "rotated": rotated, "posted": posted, "aid": aid},
    )


def test_recorder_decodes_functions_events_and_reverts(recorded):
    _url, _path, evidence, txs = recorded
    by_hash = {t["txHash"]: t for t in evidence["transactions"]}
    assert by_hash[txs["ok"].hash]["function"] == "mint"
    assert by_hash[txs["ok"].hash]["expectedEvents"] == ["Transfer"]
    bad = by_hash[txs["bad"].hash]
    assert bad["status"] == 0 and bad["expectedStatus"] == 0 and bad["signedByAgentKey"] is True
    assert bad["action"].startswith("withdraw reverted (NotOwner")
    frozen = by_hash[txs["frozen"].hash]
    assert frozen["tierBefore"] == 1 and frozen["tierAfter"] == 255 and frozen["reasonCodes"] == [3202]
    posted = by_hash[txs["posted"].hash]
    assert posted["decision"] == "GRADE_D" and posted["reasonCodes"] == [1002] and posted["actor"] == "bureau"
    assert validate_evidence(evidence) == []


def test_verify_offline_and_against_rpc(recorded):
    url, path, _evidence, _txs = recorded
    verifier = load_verifier()
    lines: list[str] = []
    offline = verifier.verify(path, receipts_only=True, out=lines.append)
    assert offline["ok"], offline["failures"]
    assert offline["verified"] == offline["total"] == 6
    online = verifier.verify(path, rpc_url=url, rps=100, out=lines.append)
    assert online["ok"], online["failures"]
    assert lines[-1].startswith("verified 6 / 6")


def test_verify_detects_a_tampered_receipt(recorded):
    _url, path, _evidence, txs = recorded
    receipt_file = path.parent / f"receipts/{txs['ok'].hash}.json"
    saved = json.loads(receipt_file.read_text(encoding="utf-8"))
    saved["receipt"]["status"] = "0x0"
    receipt_file.write_text(json.dumps(saved), encoding="utf-8")
    report = load_verifier().verify(path, receipts_only=True, out=lambda _l: None)
    assert not report["ok"] and any(txs["ok"].hash in f for f in report["failures"])


def test_verify_detects_a_changed_status_claim(recorded):
    url, path, evidence, txs = recorded
    for t in evidence["transactions"]:
        if t["txHash"] == txs["bad"].hash:
            t["expectedStatus"] = 1
    path.write_text(json.dumps(evidence), encoding="utf-8")
    report = load_verifier().verify(path, rpc_url=url, rps=100, out=lambda _l: None)
    assert not report["ok"] and report["verified"] == report["total"] - 1


def test_verify_detects_a_tampered_bundle(recorded):
    _url, path, _evidence, _txs = recorded
    (path.parent / "bundles" / "1-1.json").write_bytes(b'{"tampered":true}')
    report = load_verifier().verify(path, receipts_only=True, out=lambda _l: None)
    assert not report["ok"] and any("bundle keccak" in f for f in report["failures"])


def test_verify_detects_a_flipped_agent_key_flag(recorded):
    _url, path, evidence, txs = recorded
    for t in evidence["transactions"]:
        if t["txHash"] in (txs["bad"].hash, txs["ok"].hash):
            t["signedByAgentKey"] = not t["signedByAgentKey"]
    path.write_text(json.dumps(evidence), encoding="utf-8")
    report = load_verifier().verify(path, receipts_only=True, out=lambda _l: None)
    assert report["verified"] == report["total"] - 2
    assert sum("signedByAgentKey" in f for f in report["failures"]) == 2


def test_verify_matches_the_bundle_to_the_posting_agent(recorded):
    _url, path, evidence, txs = recorded
    evidence["gradeChanges"][0]["agentId"] = str(txs["aid"] + 1)
    path.write_text(json.dumps(evidence), encoding="utf-8")
    report = load_verifier().verify(path, receipts_only=True, out=lambda _l: None)
    assert not report["ok"] and any("no GradePosted for agent" in f for f in report["failures"])


def test_recorder_expects_primary_events(recorded):
    _url, _path, evidence, txs = recorded
    by_hash = {t["txHash"]: t for t in evidence["transactions"]}
    assert by_hash[txs["rotated"].hash]["expectedEvents"] == ["TierChanged"]
    assert by_hash[txs["bad"].hash]["expectedEvents"] == []
