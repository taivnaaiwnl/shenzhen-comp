"""Resumable runs (spec 10.1): stop after S5 keeping the Anvil node, resume at S6 with ``--from``, then
``acb scenario reset`` stops the kept node. Marker ``slow`` (about 6 minutes)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid

import pytest

from acb import config

pytestmark = [pytest.mark.slow, pytest.mark.chain]


def _acb(*args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith(("_PRIVATE_KEY", "_MNEMONIC"))}
    env.pop("ACB_DEPLOYMENTS_DIR", None)
    env.pop("ACB_OUT_DIR", None)
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "acb", *args],
        cwd=config.REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=2400,
        check=False,
    )


def test_resume_from_a_kept_chain():
    from acb.config import foundry_tool

    if foundry_tool("anvil") is None:
        pytest.skip("anvil not found")
    run_id = f"r-test-{uuid.uuid4().hex[:8]}"
    state_path = config.REPO_ROOT / "runs" / run_id / "state.json"
    try:
        first = _acb("scenario", "run", "--network", "anvil", "--run-id", run_id, "--to", "S5", "--keep")
        assert first.returncode == 0, first.stdout[-3000:] + first.stderr[-2000:]
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["facts"]["mandateId"] and state["chain"]["anvilPid"]
        second = _acb(
            "scenario", "run", "--network", "anvil", "--run-id", run_id, "--from", "S6", "--to", "S6", "--keep"
        )
        assert second.returncode == 0, second.stdout[-3000:] + second.stderr[-2000:]
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["steps"]["S6"]["status"] == "PASSED"
        assert state["steps"]["S6"]["decisions"][0]["outcome"] == "PAY"
        assert [s for s in state["steps"] if state["steps"][s]["status"] == "PASSED"] == [f"S{i}" for i in range(7)]
        refused = _acb("scenario", "run", "--network", "anvil", "--run-id", run_id, "--from", "S9")
        assert refused.returncode != 0  # S7 and S8 have not run
    finally:
        reset = _acb("scenario", "reset", "--run-id", run_id)
        assert reset.returncode == 0
        shutil.rmtree(config.REPO_ROOT / "runs" / run_id, ignore_errors=True)
