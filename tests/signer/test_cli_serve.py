"""``acb signer serve`` as its own process on Anvil (marker ``slow``): health, auth, one execution."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import uuid

import httpx
import pytest

pytestmark = [pytest.mark.chain, pytest.mark.slow]

TOKENS = {"SIGNER_TOKEN": "t" * 40, "EVENTS_TOKEN": "v" * 40}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_signer_serve_process(anvil, fresh_deployed, tmp_path):
    url, _ = anvil
    port = _free_port()
    base = {k: v for k, v in os.environ.items() if k.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE"}}
    env = {
        **base,
        **TOKENS,
        "PYTHONUTF8": "1",
        "NETWORK": "anvil",
        "ACB_PROCESS_ROLE": "signer",
        "ANVIL_RPC_URL": url,
        "ACB_DEPLOYMENTS_DIR": os.environ["ACB_DEPLOYMENTS_DIR"],
        "ACB_OUT_DIR": str(tmp_path / "out"),
        "ACB_SIGNER_STATE_DIR": str(tmp_path / "state"),
    }
    flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(
        [sys.executable, "-m", "acb", "signer", "serve", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        **flags,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        health = None
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                health = httpx.get(f"{base_url}/health", timeout=2).json()
                break
            except httpx.HTTPError:
                time.sleep(0.3)
        assert health is not None, proc.stderr.read().decode(errors="replace") if proc.poll() is not None else "timeout"
        assert (
            health["ok"] and health["chainId"] == 31337 and health["vault"] == fresh_deployed.vaults["wallet"].address
        )
        body = {
            "requestId": str(uuid.uuid4()),
            "kind": "REPORT_VIOLATION",
            "violationReport": {
                "toolName": "fetch_url",
                "rawArgs": {},
                "rawText": None,
                "secretMatch": None,
                "turn": "invoice",
            },
        }
        assert httpx.post(f"{base_url}/v1/execute", json=body, timeout=30).status_code == 401
        auth = {"Authorization": "Bearer " + TOKENS["SIGNER_TOKEN"]}
        r = httpx.post(f"{base_url}/v1/execute", json=body, headers=auth, timeout=60)
        assert r.status_code == 200 and r.json()["status"] == "executed", r.text
        assert r.json()["violation"]["kind"] == 9 and r.json()["tx"][0]["function"] == "recordViolation"
        again = httpx.get(f"{base_url}/v1/requests/{body['requestId']}", headers=auth, timeout=10)
        assert again.json() == r.json()
        events = httpx.get(
            f"{base_url}/events", headers={"Authorization": "Bearer " + TOKENS["EVENTS_TOKEN"]}, timeout=10
        )
        assert any(e["type"] == "violation" for e in events.json())
        assert (tmp_path / "state" / "deployment.json").is_file()
        assert (tmp_path / "out" / "signer.jsonl").is_file()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
