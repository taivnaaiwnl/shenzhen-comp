"""The counted attack suite A01-A63 (spec 12.3): one test per attack id.

The suite runs once per session against a live system on Anvil (about 20 minutes: marker ``slow``).
Each attack is classified by the harness from what it observed; a test fails when its attack is
**Failed** (or could not run), and when the observed category differs from the one 12.3 predicts.
"""

from __future__ import annotations

import pytest

from acb.scenario.runner import load_scenario_config

pytestmark = [pytest.mark.slow, pytest.mark.chain]

ATTACKS = [a["id"] for a in load_scenario_config()["attacks"]]


@pytest.fixture(scope="session")
def suite(tmp_path_factory):
    from acb.config import foundry_tool
    from acb.scenario.attacks import run_suite

    if foundry_tool("anvil") is None:
        pytest.skip("anvil not found")
    lines: list[str] = []
    report = run_suite(echo=lines.append, out=tmp_path_factory.mktemp("attacks") / "attack_suite.json")
    report["log"] = lines
    return report


def test_suite_totals(suite):
    totals = suite["totals"]
    assert totals["failed"] == 0, [r["id"] for r in suite["results"] if r["actual"] == "Failed"]
    assert totals["errors"] == 0, [(r["id"], r["error"]) for r in suite["results"] if r["actual"] == "Error"]
    assert totals["fundsLostOutsideIntent"] == "0"
    assert len(suite["results"]) == 63
    assert suite["schema"] == "acb-attacks/1"


@pytest.mark.parametrize("attack_id", ATTACKS)
def test_attack(suite, attack_id):
    result = next((r for r in suite["results"] if r["id"] == attack_id), None)
    assert result is not None, f"{attack_id} did not run"
    assert result["actual"] not in ("Failed", "Error"), result
    failed_checks = [c for c in result["checks"] if not c["ok"]]
    assert result["actual"] == result["expected"], (
        result["actual"],
        result["expected"],
        failed_checks,
        result["notes"],
    )
    assert not failed_checks, failed_checks
