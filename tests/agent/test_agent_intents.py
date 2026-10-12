"""The dialogue-evaluation fixture in CI (spec 8.9): the subset the rule-based parser supports, without an LLM,
and the fixture's own requirements (>= 30 intents, >= 5 ambiguous, >= 5 corrections, >= 3 Chinese, 5 hostile)."""

from __future__ import annotations

import pytest
from agent_eval import CountingLLM, load_cases, run_case, summarise

from acb.agent.fallback_parser import FallbackParser

CASES = load_cases()


def test_the_fixture_covers_the_required_kinds():
    kinds = [c["kind"] for c in CASES]
    assert len(CASES) >= 30
    assert sum(1 for c in CASES if c.get("clarify")) >= 5
    assert sum(1 for c in CASES if c["kind"] == "correction" or c.get("correction")) >= 5
    assert kinds.count("chinese") >= 3
    assert kinds.count("hostile") == 5
    assert len({c["id"] for c in CASES}) == len(CASES)
    for c in CASES:
        if not c.get("hostile"):
            assert set(c["expect"]) >= {"payees", "per_tx", "total", "valid_hours"}, c["id"]


@pytest.mark.parametrize("case", [c for c in CASES if c.get("fallback")], ids=lambda c: c["id"])
def test_fallback_parser_subset(case, tmp_path):
    llm = CountingLLM(FallbackParser())
    result = run_case(case, llm, tmp_path)
    if case.get("hostile"):
        assert result.safe, result.problems
        return
    assert result.correct, result.problems
    assert result.registered and result.turns_to_mandate is not None and result.turns_to_mandate <= 4
    assert result.clarified == bool(case.get("clarify")), result.replies
    assert all(rec["valid"] for rec in llm.records)


def test_summary_of_the_fallback_subset(tmp_path):
    llm = CountingLLM(FallbackParser())
    results = [run_case(c, llm, tmp_path / c["id"]) for c in CASES if c.get("fallback")]
    summary = summarise(results, llm.records)
    assert summary["correctMandateRate"] == "1.0000"
    assert summary["clarificationPrecision"] == "1.0000" and summary["clarificationRecall"] == "1.0000"
    assert summary["hostileSafeRate"] == "1.0000"
