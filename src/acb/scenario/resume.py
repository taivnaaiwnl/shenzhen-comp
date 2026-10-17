"""Resuming a run (``acb scenario run --from S6 --run-id <id>``) and stopping kept runs (spec 10.1).

A run's progress lives in ``runs/<run_id>/state.json``: step results and block windows, facts (mandate
id, order ids, ...), the chain's RPC URL and the Anvil process id with its start token, and the expected
reverts of the stolen-key steps. Nothing secret is stored there; the services are always restarted with
fresh tokens. On resume the recorder rebuilds its transaction list by re-scanning the blocks of the
finished steps, so the evidence of a resumed run is complete.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from acb import config
from acb.scenario.env import EnvError
from acb.scenario.runner import STEP_IDS, Runner, step_index


def _alive(pid: int | None, token: str | None) -> bool:
    from acb.launcher import _pid_alive, process_start_token

    if not pid:
        return False
    if not _pid_alive(int(pid)):
        return False
    return token is None or process_start_token(int(pid)) == token


def resume_run(runner: Runner, from_step: str) -> None:
    """Load ``state.json``, re-attach to the chain, rebuild the recorder; the services restart lazily."""
    state = json.loads(runner.state_path.read_text(encoding="utf-8"))
    if state.get("network") != runner.env.network:
        raise EnvError(f"run {state.get('runId')} is on {state.get('network')}, not {runner.env.network}")
    first = step_index(from_step)
    for sid in STEP_IDS[:first]:
        entry = (state.get("steps") or {}).get(sid) or {}
        if entry.get("status") != "PASSED":
            raise EnvError(f"cannot resume at {from_step}: {sid} is {entry.get('status') or 'not run'}")
    chain = state.get("chain") or {}
    rpc = chain.get("rpcUrl")
    if runner.env.network == "anvil" and not _alive(chain.get("anvilPid"), chain.get("anvilStartToken")):
        raise EnvError("the Anvil node of this run is gone (run with --keep to resume); start again from S0")
    if not rpc:
        raise EnvError("state.json has no RPC URL")
    _stop_services(state)
    runner.state.update(
        {
            k: v
            for k, v in state.items()
            if k in ("startedAt", "steps", "facts", "scanStart", "recorder", "narration", "earlyGradeChanges")
        }
    )
    runner.facts = runner.state.setdefault("facts", {})
    for sid in STEP_IDS[first:]:
        runner.state["steps"].pop(sid, None)
    runner.narration = list(state.get("narration") or [])
    runner.env.attach_chain(rpc)
    if runner.env.network == "anvil":
        runner.env.anvil = _AttachedAnvil(int(chain["anvilPid"]), rpc)
    runner.env.load_deployments()
    run_txt = runner.env.out_dir / "run.txt"
    if run_txt.is_file():
        runner.run_lines = run_txt.read_text(encoding="utf-8").splitlines()
    rebuild_records(runner, first)


class _AttachedAnvil:
    """An Anvil node started by an earlier runner process (stopped by pid at the end)."""

    def __init__(self, pid: int, url: str):
        self.pid = pid
        self.url = url

    def stop(self) -> None:
        from acb.launcher import stop_pid

        stop_pid(self.pid)


def rebuild_records(runner: Runner, first: int) -> None:
    """Re-scan the blocks of the finished steps in their windows and re-apply claims."""
    rec = runner.ensure_recorder()
    rec.scanned_to = int(runner.state.get("scanStart") or 0)
    rec.bind_deployment(runner.env.dep, runner.env.keys)
    saved = runner.state.get("recorder") or {}
    for h, err in (saved.get("expectRevert") or {}).items():
        rec.mark_expected_revert(h, err)
    for h, notes in (saved.get("notes") or {}).items():
        for n in notes:
            rec.note(h, n)
    for sid in STEP_IDS[:first]:
        blocks = (runner.state["steps"].get(sid) or {}).get("blocks")
        if blocks:
            rec.scan(sid, int(blocks[1]))
    runner.apply_claims(None)  # type: ignore[arg-type]


def _stop_services(state: dict[str, Any]) -> list[str]:
    from acb.launcher import stop_pid

    out = []
    for role, info in (state.get("services") or {}).items():
        if _alive(info.get("pid"), info.get("startToken")):
            out.append(f"{role}: {stop_pid(int(info['pid']))}")
    return out


def reset_run(run_id: str | None, *, network: str = "anvil") -> list[str]:
    """Stop the services and the Anvil node of kept runs (by pid, only when the start token matches)."""
    from acb.launcher import stop_pid

    paths = (
        [config.REPO_ROOT / "runs" / run_id / "state.json"]
        if run_id
        else sorted((config.REPO_ROOT / "runs").glob("*/state.json"))
    )
    lines = []
    for path in paths:
        if not Path(path).is_file():
            lines.append(f"{path}: no state file")
            continue
        state = json.loads(Path(path).read_text(encoding="utf-8"))
        if state.get("network") != network:
            continue
        for line in _stop_services(state):
            lines.append(f"{state.get('runId')} {line}")
        chain = state.get("chain") or {}
        if network == "anvil" and _alive(chain.get("anvilPid"), chain.get("anvilStartToken")):
            lines.append(f"{state.get('runId')} anvil: {stop_pid(int(chain['anvilPid']))}")
    return lines or ["nothing to stop"]


__all__ = ["rebuild_records", "reset_run", "resume_run"]
