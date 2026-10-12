"""The wallet agent holds no keys (spec 2.1, 7.6): it refuses to start, and refuses every LLM call, while a
``*_PRIVATE_KEY`` or ``*_MNEMONIC`` variable is visible."""

from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest
from agent_helpers import DEFAULT_SCRIPT, RecordingLLM, make_env

from acb.agent.mock_llm import MockLLM
from acb.keys import PrivateKeyInEnvironment

AGENT_SRC = Path(__file__).resolve().parents[2] / "src" / "acb" / "agent"


def serve_args() -> argparse.Namespace:
    return argparse.Namespace(network="anvil", port=None, host="127.0.0.1")


@pytest.mark.parametrize("name", ["OWNER_PRIVATE_KEY", "ACB_MNEMONIC", "sim_atlas_private_key"])
def test_serve_refuses_to_start_with_a_key_in_the_environment(monkeypatch, name):
    from acb.agent.cli import _serve

    monkeypatch.setenv(name, "0x" + "11" * 32)
    monkeypatch.setenv("AGENT_TOKEN", "t" * 40)
    monkeypatch.setenv("EVENTS_TOKEN", "e" * 40)
    with pytest.raises(SystemExit) as exc:
        _serve(serve_args())
    assert exc.value.code == 78


def test_serve_refuses_to_start_without_tokens(monkeypatch):
    from acb.agent.cli import _serve

    for name in list(os.environ):
        if name.upper().endswith(("_PRIVATE_KEY", "_MNEMONIC")):
            monkeypatch.delenv(name)
    monkeypatch.delenv("AGENT_TOKEN", raising=False)
    monkeypatch.setenv("EVENTS_TOKEN", "e" * 40)
    assert _serve(serve_args()) == 2
    monkeypatch.setenv("AGENT_TOKEN", "t" * 40)
    assert _serve(argparse.Namespace(network="anvil", port=None, host="0.0.0.0")) == 2


def test_the_cli_process_exits_78_with_a_key(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith(("_PRIVATE_KEY", "_MNEMONIC"))}
    env.update({"AGENT_SIGNER_PRIVATE_KEY": "0x" + "22" * 32, "AGENT_TOKEN": "t" * 40, "EVENTS_TOKEN": "e" * 40})
    proc = subprocess.run(
        [sys.executable, "-m", "acb", "agent", "serve", "--network", "anvil"],
        check=False,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
    )
    assert proc.returncode == 78
    assert "AGENT_SIGNER_PRIVATE_KEY" in proc.stderr and "22" * 32 not in proc.stderr + proc.stdout


def test_every_turn_refuses_the_llm_call_with_a_key(tmp_path, monkeypatch):
    rec = RecordingLLM(MockLLM(DEFAULT_SCRIPT))
    env = make_env(tmp_path, llm=rec)
    mid = env.verify(env.chain.register_mandate(["atlas"]))
    monkeypatch.setenv("OWNER_PRIVATE_KEY", "0x" + "33" * 32)
    with pytest.raises(PrivateKeyInEnvironment):
        env.chat("what is my status?")
    with pytest.raises(PrivateKeyInEnvironment):
        env.agent.runtime.run_purchase(mid, [{"payee_agent_id": 1, "sku": "fx-live-15m", "quantity": 1}])
    assert env.signer.requests == []  # nothing was decided or sent


def test_the_agent_package_never_loads_keys_or_dotenv():
    """Only the owner's CLI confirmation (owner_actions, run by `acb chat`) may load the owner key, and only
    the `acb chat` command loads .env; nothing the agent process runs does."""
    allowed = {"owner_actions.py": {"load_role_key"}, "cli.py": {"load_env_file"}}
    for path in AGENT_SRC.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
        }
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imports |= {a.name for a in node.names}
                assert node.module != "dotenv", path
            if isinstance(node, ast.Import):
                assert all(a.name != "dotenv" for a in node.names), path
        used = (names | imports) & {"load_role_key", "load_env_file", "KeyRing"}
        permitted = allowed.get(path.name, set()) | ({"KeyRing"} if path.name == "owner_actions.py" else set())
        assert used <= permitted, (path.name, used)


def test_in_the_service_a_key_ends_the_whole_process(monkeypatch):
    """A SystemExit in a worker thread would end only that thread: the guard calls os._exit(78)."""
    import acb.agent.llm as llm_mod

    codes = []
    monkeypatch.setattr(llm_mod.os, "_exit", lambda code: codes.append(code))
    monkeypatch.setenv("BUREAU_PRIVATE_KEY", "0x" + "44" * 32)
    llm_mod.set_exit_on_keys(True)
    try:
        with pytest.raises(PrivateKeyInEnvironment):  # (the patched _exit returns, so the error follows)
            llm_mod.guard_keys()
    finally:
        llm_mod.set_exit_on_keys(False)
    assert codes == [78]
