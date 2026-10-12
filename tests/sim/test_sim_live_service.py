"""``acb sim serve`` and ``acb sim run`` against the session Anvil deployment (marker ``chain``)."""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import time

import httpx
import pytest

pytestmark = pytest.mark.chain


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_sim_run_status_from_the_cli(anvil, deployed, out_dir, capsys, monkeypatch):
    from acb.cli import main

    monkeypatch.setenv("NETWORK", "anvil")
    assert main(["sim", "run", "status", "--step", "S0"]) == 0
    assert '"status": "done"' in capsys.readouterr().out
    assert main(["sim", "run", "no_such_action"]) == 1


@pytest.mark.slow
def test_sim_serve_answers_quotes_on_anvil(anvil, deployed, fresh_deployed, tmp_path):
    url, _ = anvil
    port = _free_port()
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA"}
    }
    env.update(
        {
            "NETWORK": "anvil",
            "ANVIL_RPC_URL": url,
            "ACB_DEPLOYMENTS_DIR": os.environ["ACB_DEPLOYMENTS_DIR"],
            "ACB_OUT_DIR": str(tmp_path),
            "SIM_PORT": str(port),
            "SIM_TOKEN": "t" * 40,
            "EVENTS_TOKEN": "e" * 40,
            "PYTHONUTF8": "1",
            "ACB_PROCESS_ROLE": "sim",
        }
    )
    flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
    proc = subprocess.Popen(
        [sys.executable, "-m", "acb", "sim", "serve"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        **flags,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                health = httpx.get(f"{base}/health", timeout=2).json()
                break
            except httpx.HTTPError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError(proc.stderr.read().decode(errors="replace")[-2000:]) from None
                time.sleep(0.3)
        assert health["chainId"] == 31337 and health["ok"]
        wallet = deployed.vaults["wallet"]
        body = {
            "quoteRequestId": "qr-" + secrets.token_hex(16),
            "buyerAgentId": str(wallet.agent_id),
            "buyerVault": wallet.address,
            "sku": "fx-live-15m",
            "quantity": 4,
            "capOf": "",
        }
        invoice = httpx.post(f"{base}/p/atlas/quote", json=body, timeout=30).json()
        assert invoice["amount"] == "800000" and invoice["buyerVault"] == wallet.address
        assert (tmp_path / "invoices").is_dir()
        r = httpx.post(f"{base}/admin/run", json={"action": "status"}, timeout=30)
        assert r.status_code == 401
        r = httpx.post(
            f"{base}/admin/run", json={"action": "status"}, headers={"Authorization": "Bearer " + "t" * 40}, timeout=30
        )
        assert r.status_code == 200 and r.json()["result"]["counters"]["atlas"]["quotes"] == 1
        events = httpx.get(f"{base}/events?after=0", headers={"Authorization": "Bearer " + "e" * 40}, timeout=30).json()
        assert any(e["data"].get("message") == "invoice issued" for e in events)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
