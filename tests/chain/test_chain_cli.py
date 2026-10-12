"""``acb deploy all`` twice (the second run skips every phase) and ``acb chain up|status|down``."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from acb.chain.anvil import free_port
from acb.chain.deployments import load_deployments

pytestmark = [pytest.mark.chain, pytest.mark.slow]


def _acb(args: list[str], env: dict[str, str], timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "acb", *args], env=env, capture_output=True, text=True, timeout=timeout, check=False
    )


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith(("_PRIVATE_KEY", "_MNEMONIC"))}
    env.update(extra)
    env["PYTHONUTF8"] = "1"
    return env


def test_deploy_all_twice(anvil, deployed, tmp_path, monkeypatch):
    url, w3 = anvil
    snap = w3.provider.make_request("evm_snapshot", [])["result"]
    try:
        env = _env(ANVIL_RPC_URL=url, ACB_DEPLOYMENTS_DIR=str(tmp_path))
        no_env = str(tmp_path / "none.env")
        first = _acb(["deploy", "all", "--network", "anvil", "--fresh", "--env-file", no_env], env)
        assert first.returncode == 0, first.stderr
        assert "transactions sent: 29" in first.stdout
        monkeypatch.setenv("ACB_DEPLOYMENTS_DIR", str(tmp_path))
        dep = load_deployments("anvil")
        assert dep.chain_id == 31337 and len(dep.agents) == 12 and len(dep.vaults) == 5
        assert dep.contracts["AgentCreditBureau"].address != deployed.contracts["AgentCreditBureau"].address
        second = _acb(["deploy", "all", "--network", "anvil", "--env-file", no_env], env)
        assert second.returncode == 0, second.stderr
        assert "transactions sent: 0 (every phase skipped)" in second.stdout
        for phase in ("contracts", "agents", "vaults", "funding"):
            assert f"skip {phase}" in second.stdout
        assert load_deployments("anvil") == dep
        shown = _acb(["deploy", "show", "--network", "anvil"], env)
        assert shown.returncode == 0 and '"schema": "acb-deployments/1"' in shown.stdout
        assert "PRIVATE" not in first.stdout + second.stdout + shown.stdout
    finally:
        w3.provider.make_request("evm_revert", [snap])


def test_deploy_public_network_needs_yes(tmp_path):
    env = _env(BASE_SEPOLIA_RPC_URL="http://127.0.0.1:9", ACB_DEPLOYMENTS_DIR=str(tmp_path))
    for role in ("DEPLOYER", "OWNER", "ADMIN"):
        env.pop(f"{role}_PRIVATE_KEY", None)
    result = _acb(["deploy", "all", "--network", "base_sepolia", "--env-file", str(tmp_path / "none.env")], env)
    assert result.returncode == 2
    assert "acb keys init" in result.stderr
    assert not list(tmp_path.iterdir())


def test_chain_up_status_down(tmp_path):
    port = free_port()
    env = _env(ACB_CHAIN_STATE_FILE=str(tmp_path / "chain.json"), ANVIL_RPC_URL=f"http://127.0.0.1:{port}")
    up = _acb(["chain", "up"], env, timeout=90)
    try:
        assert up.returncode == 0, up.stderr
        assert f"http://127.0.0.1:{port}" in up.stdout
        assert _acb(["chain", "status"], env).returncode == 0
        again = _acb(["chain", "up"], env, timeout=90)
        assert "already running" in again.stdout
    finally:
        down = _acb(["chain", "down"], env, timeout=90)
    assert down.returncode == 0 and ("stopped" in down.stdout or "terminated" in down.stdout)
    assert _acb(["chain", "status"], env).returncode == 1
    assert not (tmp_path / "chain.json").exists()
