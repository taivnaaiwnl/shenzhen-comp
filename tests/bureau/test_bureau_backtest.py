"""Backtest (spec 4.12, 12.2): deterministic for a seed, under 60 s for 300 agents and 6 rounds, and
smear / NEW-sybil failure records change honest providers' scores by exactly 0."""

from __future__ import annotations

import time

import pytest

from acb.bureau.backtest import auc_bps, composition, ks_bps, report_bytes, run_backtest
from acb.cli import main


def test_composition_sums_to_the_population():
    for n in (60, 100, 300, 1000):
        counts = composition(n)
        members = sum(v for k, v in counts.items() if not k.endswith(("_ring", "_group")))
        members += counts["dust_ring"] * 6 + counts["wash_ring"] * 3 + counts["smear_group"] * 3
        assert members == n
    with pytest.raises(ValueError):
        composition(59)


def test_auc_and_ks():
    assert auc_bps([10, 20], [1, 2]) == 10_000 and ks_bps([10, 20], [1, 2]) == 10_000
    assert auc_bps([5], [5]) == 5000 and ks_bps([5], [5]) == 0
    assert auc_bps([1], [10]) == 0


def test_small_backtest_is_deterministic():
    a = run_backtest(agents=120, rounds=3, seed=7)
    b = run_backtest(agents=120, rounds=3, seed=7)
    assert report_bytes(a) == report_bytes(b)
    assert report_bytes(run_backtest(agents=120, rounds=3, seed=8)) != report_bytes(a)


@pytest.mark.slow
def test_cli_backtest_300_agents_twice(tmp_path, capsys):
    outputs = []
    for i in range(2):
        target = tmp_path / f"backtest{i}.json"
        started = time.monotonic()
        assert main(["bureau", "backtest", "--agents", "300", "--seed", "2026", "--out", str(target)]) == 0
        assert time.monotonic() - started < 60
        outputs.append(target.read_bytes())
    assert outputs[0] == outputs[1]
    assert b"\r\n" not in outputs[0] and not outputs[0].startswith(b"\xef\xbb\xbf")
    import json

    report = json.loads(outputs[0])
    assert report["params"] == {"agents": 300, "rounds": 6, "seed": 2026, "anchors": 3}
    m = report["metrics"]
    assert m["smearMaxAbsDelta"] == 0 and m["smearGradeChanges"] == 0 and m["smearCases"] > 0
    assert all(report["expectations"].values())
    assert m["aucBps"] >= 9000 and m["falseAdverseBps"] <= 500
    assert m["bestGradeByType"]["dust_subject"] not in ("A", "B") and m["bestGradeByType"]["wash"] not in ("A", "B")
    assert "AUC" in capsys.readouterr().out
