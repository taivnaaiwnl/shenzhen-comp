"""Re-evaluate one step's grade-change check of a Base Sepolia run under the real-time grade window.

Before the runner learned that a public network's real-time clock can bring a later step's expected
grade change forward (``Runner._real_time_grade_window``), a step failed when such a change landed in it.
This script re-applies exactly that rule to a finished step, without sending anything: the step's only
failures must be grade-change mismatches, every extra change must be one a later step expects with the
same grade and the same posted reasons (read from the bureau's own log of the run), and nothing expected
in the step may be missing. If so, the step is marked PASSED with a note, and the early change is recorded
so the later step does not expect it again. Otherwise nothing is changed.

    uv run python scripts/recheck_grade_window.py <run_id> <step>
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

from acb.scenario.runner import STEP_IDS, load_scenario_config

ROOT = Path(__file__).resolve().parents[1]
MISMATCH = re.compile(r"^grade changes (\[.*\]) != expected (\[.*\])$")


def main() -> None:
    run_id, step = sys.argv[1], sys.argv[2]
    path = ROOT / "runs" / run_id / "state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    entry = state["steps"][step]
    failures = list(entry.get("failures") or [])
    if entry.get("status") != "FAILED" or len(failures) != 1 or not MISMATCH.match(failures[0]):
        raise SystemExit(f"{step}: not a single grade-change mismatch ({failures}); nothing changed")
    got_raw, want_raw = MISMATCH.match(failures[0]).groups()
    got = {tuple(x) for x in ast.literal_eval(got_raw)}
    want = {tuple(x) for x in ast.literal_eval(want_raw)}
    if want - got:
        raise SystemExit(f"{step}: expected changes missing {sorted(want - got)}; nothing changed")

    cfg = load_scenario_config()
    deployments = json.loads((ROOT / "deployments" / "base_sepolia.json").read_text(encoding="utf-8"))
    ids = {str(a["agentId"]): k for k, a in deployments["agents"].items()}
    bureau_log = ROOT / "runs" / run_id / "out" / "base_sepolia" / "bureau.jsonl"
    started, finished = entry["startedAt"], entry["finishedAt"]
    posted: dict[str, list[int]] = {}
    for line in bureau_log.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        data = event.get("data") or {}
        if event.get("type") == "grade" and started <= event.get("ts", "") <= finished:
            posted[ids.get(str(data.get("agentId")), "?")] = [int(c) for c in data.get("reasons") or []]

    early = state.setdefault("earlyGradeChanges", {})
    idx = STEP_IDS.index(step)
    notes = []
    for agent, grade in sorted(got - want):
        for later in STEP_IDS[idx + 1 :]:
            lexp = (cfg["steps_by_id"].get(later) or {}).get("expect") or {}
            if (lexp.get("grade_changes") or {}).get(agent) != grade:
                continue
            reasons = (lexp.get("reasons") or {}).get(agent)
            if reasons is not None and posted.get(agent) != list(reasons):
                raise SystemExit(f"{agent} -> {grade}: posted reasons {posted.get(agent)} != {later}'s {reasons}")
            early[f"{agent}:{grade}"] = step
            notes.append(
                f"{agent} -> {grade}: the change expected in {later} landed in {step} (real-time clock); "
                f"posted reasons {posted.get(agent)} match {later}'s expectation"
            )
            break
        else:
            raise SystemExit(f"{agent} -> {grade} is expected in no later step; nothing changed")
    entry["status"] = "PASSED"
    entry["failures"] = []
    entry.setdefault("notes", []).extend(
        notes + [f"re-evaluated after the run under the real-time grade window (was: {failures[0]})"]
    )
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for n in notes:
        print(n)
    print(f"{step} marked PASSED in {path}")


if __name__ == "__main__":
    main()
