"""The recorded run S0-S21 on Anvil with MockLLM, twice from scratch (spec 10.1, 12.2, 15.4 WP9).

Each run asserts, step by step, every expected decision, reason code, violation, tier, grade-change set
and transaction status of 10.2 (``config/scenario.yaml``); a step that does not match is FAILED and the
command exits 1. The two runs must produce the same fingerprint (decisions, reasons, violations, grade
changes and transaction functions and statuses per step), the staged evidence must be a valid
``acb-run/1`` file whose every transaction verifies offline, and the export must copy only curated files.

About 9 minutes per run: marker ``slow``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid

import pytest

from acb import config
from acb.canonical import chash
from acb.scenario import evidence as ev

pytestmark = [pytest.mark.slow, pytest.mark.chain]


def _run(run_id: str) -> tuple[int, dict, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith(("_PRIVATE_KEY", "_MNEMONIC"))}
    env.pop("ACB_DEPLOYMENTS_DIR", None)
    env.pop("ACB_OUT_DIR", None)
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "acb", "scenario", "run", "--network", "anvil", "--llm", "mock", "--run-id", run_id],
        cwd=config.REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
    )
    state_path = config.REPO_ROOT / "runs" / run_id / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    return proc.returncode, state, proc.stdout[-4000:] + proc.stderr[-2000:]


@pytest.fixture(scope="module")
def two_runs():
    from acb.config import foundry_tool

    if foundry_tool("anvil") is None:
        pytest.skip("anvil not found")
    ids = [f"r-test-{uuid.uuid4().hex[:8]}", f"r-test-{uuid.uuid4().hex[:8]}"]
    results = [_run(run_id) for run_id in ids]
    yield ids, results
    if all(code == 0 for code, _state, _out in results):
        for run_id in ids:
            shutil.rmtree(config.REPO_ROOT / "runs" / run_id, ignore_errors=True)


def test_every_step_passes_twice(two_runs):
    _ids, results = two_runs
    for code, state, out in results:
        failed = {
            sid: e.get("failures") for sid, e in (state.get("steps") or {}).items() if e.get("status") != "PASSED"
        }
        assert code == 0 and not failed, (failed, out)
        assert sorted(state["steps"], key=lambda s: int(s[1:])) == [f"S{i}" for i in range(22)]


def test_two_runs_have_the_same_fingerprint(two_runs):
    _ids, results = two_runs
    a, b = (r[1]["fingerprint"] for r in results)
    diff = {sid: (a.get(sid), b.get(sid)) for sid in set(a) | set(b) if a.get(sid) != b.get(sid)}
    assert not diff, diff
    assert chash(a) == chash(b)


def test_key_decisions_and_grade_changes(two_runs):
    _ids, results = two_runs
    fp = results[0][1]["fingerprint"]
    assert fp["S6"]["decisions"] == [["PAY", [2000]]]
    assert fp["S7"]["decisions"] == [["PAY_CAPPED", [2002]]]
    assert fp["S12"]["decisions"] == [["REFUSE", [2100]]]
    assert fp["S15"]["decisions"] == [["REFUSE", [2105, 2108, 2301]]]
    assert fp["S15"]["violations"] == [["PROVENANCE_AMOUNT", 2]]
    assert fp["S16"]["violations"] == [["INJECTION_FOLLOWED", 3]]
    assert fp["S18"]["decisions"] == [["NEEDS_OWNER_APPROVAL", [2200]], ["PAY", [2007]]]
    assert [tuple(x) for x in fp["S11"]["gradeChanges"]] == [("ghost", "D")]
    assert sorted(tuple(x) for x in fp["S16"]["gradeChanges"]) == [("nova", "C"), ("orion", "D")]
    assert fp["S13"]["txs"] == ["pay:0", "pay:0"]


def test_staged_evidence_is_valid_and_verifies_offline(two_runs, tmp_path):
    ids, _results = two_runs
    folder = ev.staging_folder(ids[0], "anvil")
    evidence = json.loads((folder / "evidence.json").read_text(encoding="utf-8"))
    assert ev.validate_evidence(evidence, folder) == []
    assert len(evidence["transactions"]) >= 150
    assert sum(1 for t in evidence["transactions"] if t["signedByAgentKey"]) >= 20
    assert all(g["recompute"] == "MATCH" for g in evidence["gradeChanges"])
    assert sum(1 for t in evidence["transactions"] if t["headline"]) >= 20
    report = ev.verify_folder(folder)
    assert report["offlineOk"] and report["offlineVerified"] == report["total"]
    result = ev.export(ids[0], "anvil", dest=tmp_path / "anvil")
    assert {p.name for p in (tmp_path / "anvil").iterdir()} <= ev.CURATED
    assert result["transactions"] == len(evidence["transactions"])
