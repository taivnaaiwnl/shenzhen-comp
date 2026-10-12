"""``acb agent serve`` over real HTTP with ``acb signer serve`` and ``acb sim serve`` (markers ``chain``, ``slow``).

Each service runs as its own process with an allow-listed environment like the launcher gives it; the agent
process has ``ACB_PROCESS_ROLE=agent`` and no key. The owner dialogue (S5) runs through ``POST /chat``, the
owner registers the draft on-chain, ``/mandates/activated`` verifies it and ``/purchase`` pays Atlas (S6).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from typing import Any

import httpx
import pytest

pytestmark = [pytest.mark.chain, pytest.mark.slow]

TOKENS = {
    "AGENT_TOKEN": "agent-" + "a" * 40,
    "AGENT_ADMIN_TOKEN": "admin-" + "b" * 40,
    "SIGNER_TOKEN": "signer-" + "c" * 40,
    "EVENTS_TOKEN": "events-" + "d" * 40,
    "SIM_TOKEN": "sim-" + "e" * 40,
}
BASE = {"PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA"}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(role: str, args: list[str], extra: dict[str, str], tmp_path) -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items() if k.upper() in BASE}
    env.update({"PYTHONUTF8": "1", "NETWORK": "anvil", "ACB_PROCESS_ROLE": role, **extra})
    flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
    log = open(tmp_path / f"{role}.err.txt", "wb")  # noqa: SIM115 - closed with the process
    return subprocess.Popen(
        [sys.executable, "-m", "acb", *args], env=env, stdout=subprocess.DEVNULL, stderr=log, cwd=tmp_path, **flags
    )


def _wait(url: str, proc: subprocess.Popen, tmp_path, role: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"{role} exited: {(tmp_path / f'{role}.err.txt').read_text(errors='replace')[-2000:]}")
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.3)
    raise AssertionError(f"{role} did not start")


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def test_agent_service_end_to_end_over_http(anvil, deployed, fresh_deployed, tmp_path):
    from acb.agent.owner_actions import mandate_from_draft
    from acb.canonical import chash
    from acb.chain import clock
    from acb.chain.contracts import bind
    from acb.chain.tx import TxSender
    from acb.keys import KeyRing
    from acb.types import Grade

    url, w3 = anvil
    dep = deployed
    keys = KeyRing.anvil()
    c = bind(w3, dep)
    bureau = TxSender(w3, keys.account("bureau"))
    for key, grade, score in (
        ("atlas", Grade.A, 760),
        ("slowpoke", Grade.B, 680),
        ("orion", Grade.A, 750),
        ("ghost", Grade.A, 740),
        ("nova", Grade.NEW, 600),
    ):
        aid = int(dep.agents[key].agent_id)
        as_of = max(w3.eth.block_number - 1, c.acb.get_record(aid).as_of_block)
        fn = c.acb.post_grade(
            aid,
            int(grade),
            score,
            [1100],
            "acb-sc-1.0.0",
            "0x" + "11" * 32,
            chash({"k": key, "b": as_of}),
            as_of,
            "evidence/t.json",
        )
        assert bureau.send(fn).status == 1
    clock.warp(w3, c.acb.upgrade_delay() + 1)

    ports = {"SIGNER_PORT": _free_port(), "SIM_PORT": _free_port(), "AGENT_PORT": _free_port()}
    common = {
        "ANVIL_RPC_URL": url,
        "ACB_DEPLOYMENTS_DIR": os.environ["ACB_DEPLOYMENTS_DIR"],
        "ACB_OUT_DIR": str(tmp_path),
    }
    procs: list[subprocess.Popen] = []
    try:
        signer = _start(
            "signer",
            ["signer", "serve", "--network", "anvil"],
            {
                **common,
                "SIGNER_TOKEN": TOKENS["SIGNER_TOKEN"],
                "EVENTS_TOKEN": TOKENS["EVENTS_TOKEN"],
                "SIGNER_PORT": str(ports["SIGNER_PORT"]),
                "ACB_SIGNER_STATE_DIR": str(tmp_path / "signer-state"),
            },
            tmp_path,
        )
        procs.append(signer)
        sim = _start(
            "sim",
            ["sim", "serve", "--network", "anvil"],
            {
                **common,
                "SIM_TOKEN": TOKENS["SIM_TOKEN"],
                "EVENTS_TOKEN": TOKENS["EVENTS_TOKEN"],
                "SIM_PORT": str(ports["SIM_PORT"]),
                "AGENT_PORT": str(ports["AGENT_PORT"]),
            },
            tmp_path,
        )
        procs.append(sim)
        agent = _start(
            "agent",
            ["agent", "serve", "--network", "anvil"],
            {
                **common,
                **{k: TOKENS[k] for k in ("AGENT_TOKEN", "AGENT_ADMIN_TOKEN", "SIGNER_TOKEN", "EVENTS_TOKEN")},
                **{k: str(v) for k, v in ports.items()},
                "LLM_MODE": "mock",
                "MOCK_LLM_PERSONA": "careful",
                "ACB_AGENT_STATE_DIR": str(tmp_path / "agent-state"),
            },
            tmp_path,
        )
        procs.append(agent)
        _wait(f"http://127.0.0.1:{ports['SIGNER_PORT']}/health", signer, tmp_path, "signer")
        _wait(f"http://127.0.0.1:{ports['SIM_PORT']}/health", sim, tmp_path, "sim")
        _wait(f"http://127.0.0.1:{ports['AGENT_PORT']}/health", agent, tmp_path, "agent")

        base = f"http://127.0.0.1:{ports['AGENT_PORT']}"
        owner_h = {"Authorization": f"Bearer {TOKENS['AGENT_TOKEN']}"}
        events_h = {"Authorization": f"Bearer {TOKENS['EVENTS_TOKEN']}"}

        def chat(text: str) -> dict[str, Any]:
            r = httpx.post(f"{base}/chat", json={"message": text}, headers=owner_h, timeout=60)
            assert r.status_code == 200, r.text
            return r.json()

        assert (
            chat("I need FX market data and API calls for my research today, budget 5 USDC")["cards"][0]["type"]
            == "clarification"
        )
        assert chat("Use all five: Atlas, Slowpoke, Nova, Orion and Ghost")["cards"][0]["type"] == "mandate_draft"
        draft = chat("Actually make the total 4 USDC, not 5")["cards"][0]
        assert draft["maxTotal"] == "4000000"

        m = mandate_from_draft(draft, w3=w3, dep=dep, c=c)
        tx = TxSender(w3, keys.account("owner")).send(
            c.vault("wallet").register_mandate(
                m["mandateId"],
                m["ids"],
                m["wallets"],
                m["maxPerTx"],
                m["maxTotal"],
                m["validUntil"],
                m["recurring"],
                m["purposeHash"],
            )
        )
        assert tx.status == 1
        r = httpx.post(
            f"{base}/mandates/activated",
            json={"mandateId": m["mandateId"], "txHash": tx.hash, "draftId": draft["draftId"]},
            headers=owner_h,
            timeout=60,
        )
        assert r.json()["status"] == "active", r.text

        atlas = int(dep.agents["atlas"].agent_id)
        r = httpx.post(
            f"{base}/purchase/{m['mandateId']}",
            json={"items": [{"payee_agent_id": atlas, "sku": "fx-live-15m", "quantity": 4}]},
            headers=owner_h,
            timeout=180,
        )
        assert r.status_code == 200, r.text
        [result] = r.json()["results"]
        assert result["status"] == "executed" and result["outcome"] == "PAY" and result["reasons"] == [2000]

        state = httpx.get(f"{base}/state", headers=events_h, timeout=30).json()
        assert state["orders"][result["orderId"]]["status"] == "delivered"
        order = c.vault("wallet").get_order(result["orderId"])
        assert int(order.outcome) == 1  # ON_TIME

        assert httpx.post(f"{base}/inbox/invoice", content=b"garbage", timeout=10).json() == {"status": "accepted"}
        signer_health = httpx.get(f"http://127.0.0.1:{ports['SIGNER_PORT']}/health", timeout=10).json()
        assert signer_health["vault"] == dep.vaults["wallet"].address
    finally:
        for proc in procs:
            _stop(proc)
        from acb.chain.tx import resync_all

        resync_all()
