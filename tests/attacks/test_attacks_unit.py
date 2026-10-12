"""Static checks of the attack harness (no chain): every attack of 12.3 is in the run order exactly once."""

from __future__ import annotations

import inspect
import re
from collections import Counter

from acb.scenario import attacks
from acb.scenario.runner import load_scenario_config


def test_every_attack_runs_exactly_once():
    source = inspect.getsource(attacks.sequence)
    ids = re.findall(r'(?:one\(|\()"(A\d\d)", ', source)  # one("A30", ...) and ("A08", s.a08)
    counts = Counter(ids)
    expected = [a["id"] for a in load_scenario_config()["attacks"]]
    assert sorted(counts) == sorted(expected)
    assert all(n == 1 for n in counts.values()), {k: v for k, v in counts.items() if v != 1}


def test_every_attack_has_an_implementation():
    names = {n for n in dir(attacks.Suite) if re.fullmatch(r"a\d\d(_\d\d)?", n)}
    covered = set()
    for name in names:
        covered |= {f"A{x}" for x in re.findall(r"\d\d", name)}
    assert covered == {a["id"] for a in load_scenario_config()["attacks"]}


def test_a_failed_precondition_is_an_error_not_failed():
    o = attacks.Obs("A06")
    try:
        o.require("Ghost is graded D", False, "A")
    except attacks.PreconditionError as exc:
        o.error = f"{type(exc).__name__}: {exc}"
    assert attacks.classify(o) == "Error"
    assert o.checks == [{"check": "Ghost is graded D", "ok": False, "precondition": True, "detail": "A"}]


def test_a_met_precondition_lets_the_attack_run():
    o = attacks.Obs("A06")
    o.require("Ghost is graded D", True)
    o.check("pay grade-D Ghost under MA: reverts GradeNotAllowed", True, "GradeNotAllowed")
    assert attacks.classify(o) == "Blocked"


def test_totals_count_results_with_failed_checks():
    results = [
        {"actual": "Blocked", "fundsLost": "0", "checksFailed": []},
        {"actual": "Blocked", "fundsLost": "0", "checksFailed": ["revert error name"]},
        {"actual": "Error", "fundsLost": "0", "checksFailed": ["precondition"]},
    ]
    t = attacks.totals(results)
    assert t["blocked"] == 2 and t["errors"] == 1 and t["withFailedChecks"] == 2


def test_owner_cards_are_the_dialogue_cards():
    assert set(attacks.OWNER_CARDS) == {"clarification", "mandate_draft", "approval"}


def test_obs_keeps_its_helpers():
    o = attacks.Obs("A01")
    o.codes([2105, 2105, 2108])
    assert o.reason_codes == [2105, 2108]
