"""Environment and process rules (spec 2.1, 13.4): dotenv only in config.py; launcher allow-lists."""

from __future__ import annotations

import ast
import json
import os
import sys
import time
from pathlib import Path

import pytest

from acb import launcher
from acb.launcher import build_child_env, is_allowed, spawn, stop_pid

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "acb"


def imports_dotenv(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "dotenv" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "dotenv":
            return True
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", getattr(node.func, "attr", ""))
            in (
                "__import__",
                "import_module",
            )
            and any(isinstance(a, ast.Constant) and str(a.value).split(".")[0] == "dotenv" for a in node.args)
        ):
            return True
    return False


def test_dotenv_is_imported_only_in_config_py():
    users = sorted(str(p.relative_to(REPO)).replace("\\", "/") for p in SRC.rglob("*.py") if imports_dotenv(p))
    assert users == ["src/acb/config.py"]


def test_ast_scan_detects_every_import_form(tmp_path):
    for code in (
        "import dotenv",
        "from dotenv import load_dotenv",
        "from dotenv.main import x",
        "import importlib\nimportlib.import_module('dotenv')",
        "__import__('dotenv')",
    ):
        p = tmp_path / "m.py"
        p.write_text(code, encoding="utf-8")
        assert imports_dotenv(p), code


SOURCE_ENV = {
    "PATH": "C:/bin",
    "SystemRoot": "C:/Windows",
    "TEMP": "C:/tmp",
    "NETWORK": "anvil",
    "ANVIL_RPC_URL": "http://127.0.0.1:1",
    "base_sepolia_rpc_url_2": "https://logs",
    "AGENT_SIGNER_PRIVATE_KEY": "0xsigner",
    "OWNER_PRIVATE_KEY": "0xowner",
    "BUREAU_PRIVATE_KEY": "0xbureau",
    "SIM_ATLAS_PRIVATE_KEY": "0xatlas",
    "ACB_MNEMONIC": "words",
    "SIGNER_TOKEN": "st",
    "AGENT_TOKEN": "at",
    "events_token": "et",
    "LLM_MODEL": "m",
    "llm_api_key": "k",
    "MOCK_LLM_PERSONA": "careful",
    "OPENAI_API_KEY": "sk-openai",
    "UNRELATED": "x",
    "SIGNER_PORT": "18702",
}


def test_patterns_match_case_insensitively():
    pats = launcher.allowed_patterns("agent")
    assert is_allowed("llm_model", pats) and is_allowed("LLM_MODEL", pats) and is_allowed("Path", pats)
    assert is_allowed("BASE_SEPOLIA_RPC_URL_2", pats)
    assert not is_allowed("OPENAI_API_KEY", pats) and not is_allowed("AGENT_SIGNER_PRIVATE_KEY", pats)


@pytest.mark.parametrize("role", ["owner", "agent", "signer", "bureau", "autonomy", "sim"])
def test_child_env_contains_only_allow_listed_names(role):
    env = build_child_env(role, SOURCE_ENV, run_id="r-test", out_dir="C:/out", network="anvil")
    pats = launcher.allowed_patterns(role)
    for name in env:
        assert is_allowed(name, pats) or name in (
            "PYTHONUTF8",
            "ACB_PROCESS_ROLE",
            "ACB_RUN_ID",
            "ACB_OUT_DIR",
            "NETWORK",
        )
    assert env["PYTHONUTF8"] == "1" and env["ACB_PROCESS_ROLE"] == role
    assert env["ACB_RUN_ID"] == "r-test" and env["ACB_OUT_DIR"] == "C:/out"
    assert "UNRELATED" not in env and "ACB_MNEMONIC" not in env and "OPENAI_API_KEY" not in env
    assert env["PATH"] == "C:/bin" and env["SystemRoot"] == "C:/Windows"
    keys = {k for k in env if k.upper().endswith("_PRIVATE_KEY")}
    expected = {
        "owner": {"OWNER_PRIVATE_KEY"},
        "agent": set(),
        "signer": {"AGENT_SIGNER_PRIVATE_KEY"},
        "bureau": {"BUREAU_PRIVATE_KEY"},
        "autonomy": set(),
        "sim": {"SIM_ATLAS_PRIVATE_KEY"},
    }[role]
    assert keys == expected


def test_agent_env_gets_llm_settings_and_the_openai_fallback():
    env = build_child_env("agent", SOURCE_ENV)
    assert env["llm_api_key"] == "k" and env["LLM_MODEL"] == "m" and env["events_token"] == "et"
    fallback = build_child_env("agent", {"OPENAI_API_KEY": "sk-openai", "PATH": "p"})
    assert fallback["LLM_API_KEY"] == "sk-openai" and "OPENAI_API_KEY" not in fallback
    assert fallback["LLM_MODEL"] == "gpt-5.4-mini"


def test_overrides_must_be_allowed():
    env = build_child_env("signer", SOURCE_ENV, overrides={"AGENT_SIGNER_PRIVATE_KEY": "0xnext"})
    assert env["AGENT_SIGNER_PRIVATE_KEY"] == "0xnext"
    with pytest.raises(ValueError):
        build_child_env("agent", SOURCE_ENV, overrides={"OWNER_PRIVATE_KEY": "0x1"})
    with pytest.raises(KeyError):
        build_child_env("base", SOURCE_ENV)


def test_agent_never_receives_key_variables_even_if_allow_listed():
    procs = {"base": ["PATH"], "agent": ["LLM_*"]}
    with pytest.raises(ValueError):
        build_child_env("agent", {"LLM_PRIVATE_KEY": "x", "PATH": "p"}, processes=procs)


def test_spawned_child_sees_exactly_the_filtered_environment(tmp_path):
    source = {k: v for k, v in os.environ.items()}
    source.update({"OWNER_PRIVATE_KEY": "0xowner", "AGENT_TOKEN": "at", "NOT_ALLOWED_X": "1", "ACB_MNEMONIC": "m"})
    env = build_child_env(
        "agent",
        {k: v for k, v in source.items() if k != "OWNER_PRIVATE_KEY" and k != "ACB_MNEMONIC"},
        out_dir=str(tmp_path),
        run_id="r-t",
    )
    dump = tmp_path / "env.json"
    code = f"import json, os; open({str(dump)!r}, 'w').write(json.dumps(dict(os.environ)))"
    proc = spawn([sys.executable, "-c", code], env, log_path=tmp_path / "child.out.txt")
    assert proc.wait(timeout=60) == 0
    seen = json.loads(dump.read_text())
    assert {k.upper() for k in seen} <= {k.upper() for k in env}
    assert seen.get("ACB_PROCESS_ROLE") == "agent" and seen.get("PYTHONUTF8") == "1"
    assert "NOT_ALLOWED_X" not in seen and "OWNER_PRIVATE_KEY" not in seen


def test_stop_pid_stops_a_child_gracefully_or_terminates(tmp_path):
    env = build_child_env("sim", dict(os.environ), out_dir=str(tmp_path))
    proc = spawn(
        [sys.executable, "-c", "import time\nwhile True: time.sleep(0.2)"], env, log_path=tmp_path / "c.out.txt"
    )
    time.sleep(1.0)
    result = stop_pid(proc.pid, proc, grace_s=5)
    assert result in ("stopped", "terminated")
    assert proc.poll() is not None
    assert stop_pid(proc.pid, proc) == "not running"


def test_launcher_start_restart_stop(tmp_path):
    script = "import os, time\nprint(os.environ.get('AGENT_SIGNER_PRIVATE_KEY', 'none'), flush=True)\ntime.sleep(60)"
    src = {k: v for k, v in os.environ.items() if not k.upper().endswith(("_PRIVATE_KEY", "_MNEMONIC"))}
    src["AGENT_SIGNER_PRIVATE_KEY"] = "0xfirst"
    with launcher.Launcher(
        "anvil", run_id="r-l", out_dir=tmp_path, source_env=src, argv_for=lambda role: [sys.executable, "-c", script]
    ) as la:
        first = la.start("signer")
        with pytest.raises(RuntimeError):
            la.start("signer")
        second = la.restart("signer", overrides={"AGENT_SIGNER_PRIVATE_KEY": "0xnext"})
        assert second.pid != first.pid and first.proc.poll() is not None
        state = la.state()
        assert state["children"][0]["role"] == "signer" and state["runId"] == "r-l"
        deadline = time.time() + 30
        log = Path(second.log)
        while time.time() < deadline and "0xnext" not in log.read_text(errors="replace"):
            time.sleep(0.2)
        assert "0xnext" in log.read_text(errors="replace")
    assert second.proc.poll() is not None, "context exit stops every child"


def test_acb_down_never_signals_a_reused_pid(tmp_path, monkeypatch, capsys):
    import argparse

    env = build_child_env("sim", dict(os.environ), out_dir=str(tmp_path))
    proc = spawn(
        [sys.executable, "-c", "import time\nwhile True: time.sleep(0.2)"], env, log_path=tmp_path / "c.out.txt"
    )
    try:
        token = launcher.process_start_token(proc.pid)
        assert token and launcher.process_start_token(proc.pid) == token, "the token is stable"
        state = tmp_path / "launcher.json"
        monkeypatch.setattr(launcher, "state_file", lambda network: state)
        args = argparse.Namespace(network="anvil", grace=5.0)
        # a recorded token that does not match: the PID belongs to another process now
        for recorded in ("1", None):
            child = {"role": "sim", "pid": proc.pid, "startToken": recorded}
            state.write_text(json.dumps({"children": [child]}), encoding="utf-8")
            assert launcher._cmd_down(args) == 0
            assert "skipped" in capsys.readouterr().out
            assert proc.poll() is None, "the unrelated process was not touched"
        # the matching token: stopped
        state.write_text(json.dumps({"children": [{"role": "sim", "pid": proc.pid, "startToken": token}]}))
        assert launcher._cmd_down(args) == 0
        assert proc.wait(timeout=30) is not None
        assert not state.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
