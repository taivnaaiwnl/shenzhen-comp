"""The benign suite B01-B14 (spec 12.4): one test per task, and at most one false positive in total.

The suite runs once per session on a live system on Anvil (about 10 minutes: marker ``slow``).
"""

from __future__ import annotations

import pytest

from acb.scenario.runner import load_scenario_config

pytestmark = [pytest.mark.slow, pytest.mark.chain]

TASKS = [b["id"] for b in load_scenario_config()["benign"]]


@pytest.fixture(scope="session")
def benign():
    from acb.config import foundry_tool
    from acb.scenario.benign import run_benign

    if foundry_tool("anvil") is None:
        pytest.skip("anvil not found")
    lines: list[str] = []
    report = run_benign(echo=lines.append)
    report["log"] = lines
    return report


def test_false_positives_at_most_one(benign):
    assert benign["totals"]["tasks"] == 14
    assert benign["totals"]["falsePositives"] <= 1, [r for r in benign["results"] if r["falsePositive"]]


@pytest.mark.parametrize("task_id", TASKS)
def test_task(benign, task_id):
    result = next((r for r in benign["results"] if r["id"] == task_id), None)
    assert result is not None, f"{task_id} did not run"
    assert not result["falsePositive"], result
