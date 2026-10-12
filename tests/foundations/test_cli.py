"""The `acb` command: every subcommand listed, missing packages shown as "not built" (spec 15.3)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from acb import cli

REPO = Path(__file__).resolve().parents[2]
ALL_COMMANDS = [
    "up",
    "down",
    "keys",
    "chain",
    "deploy",
    "bureau",
    "autonomy",
    "signer",
    "agent",
    "chat",
    "owner",
    "sim",
    "scenario",
    "attacks",
    "evidence",
]


def test_build_parser_lists_every_subcommand():
    parser, status = cli.build_parser()
    choices = parser._subparsers._group_actions[0].choices
    assert set(ALL_COMMANDS) <= set(choices)
    assert set(status) == {m for m, _ in cli.PACKAGE_CLIS}
    for module, state in status.items():
        assert state == "ok" or state.startswith(("not built", "failed to load")), (module, state)


def test_help_via_python_m_acb():
    proc = subprocess.run(
        [sys.executable, "-m", "acb", "--help"], capture_output=True, text=True, check=False, cwd=REPO
    )
    assert proc.returncode == 0
    for command in ALL_COMMANDS:
        assert f"    {command}" in proc.stdout, command


def test_placeholder_reports_not_built(capsys, monkeypatch):
    import importlib

    real = importlib.import_module

    def fake(name, *a, **k):
        if name == "acb.sim.cli":
            raise ModuleNotFoundError("No module named 'acb.sim'", name="acb.sim")
        if name == "acb.owner.cli":
            raise RuntimeError("boom")
        return real(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", fake)
    parser, status = cli.build_parser()
    assert status["acb.sim.cli"] == "not built"
    assert status["acb.owner.cli"].startswith("failed to load")
    args = parser.parse_args(["sim"])
    assert args.func(args) == 2
    assert "not built" in capsys.readouterr().err


def test_main_without_command_prints_help(capsys):
    assert cli.main([]) == 0
    assert "usage: acb" in capsys.readouterr().out


def test_keys_addresses_anvil_in_process(capsys):
    assert cli.main(["keys", "addresses", "--network", "anvil"]) == 0
    out = capsys.readouterr().out
    assert "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266" in out and "sybil6" in out
