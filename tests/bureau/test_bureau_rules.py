"""Scorecard rules (spec 12.2 "Scorecard"): every bin boundary, reason ordering, cold start, circular
cap, standing gate, age gate, clusters, counted-order filters and the fail cap per payer cluster."""

from __future__ import annotations

import random

import pytest
from bureau_builders import USDC, Market

from acb.bureau.clusters import build_clusters
from acb.bureau.config import load_config
from acb.bureau.facts import ChainFacts, order_key
from acb.bureau.models.sc_1_0_0 import evaluate_features, grade_of, reasons_of, score
from acb.types import Grade

ANCHOR = 7


@pytest.fixture(scope="module")
def cfg():
    return load_config(anchors=[ANCHOR])


# ---- points table (spec 4.4), written out from the spec, not from the config ------------------

SPEC_BINS = {
    "F1": (
        "rBps",
        [(9500, 80), (9499, 40), (8500, 40), (8499, 20), (7000, 20), (6999, -60), (5000, -60), (4999, -120), (0, -120)],
    ),
    "F2": ("neg", [(0, 20), (1, 0), (499, 0), (500, -40), (1499, -40), (1500, -90), (2499, -90), (2500, -150)]),
    "F3": ("flag", [(0, 0), (1, -60), (999, -60), (1000, -200)]),
    "F4a": ("n", [(20, 60), (19, 40), (10, 40), (9, 20), (5, 20), (4, 0), (0, 0)]),
    "F4b": ("age", [(604800, 20), (604799, 10), (86400, 10), (86399, 0), (0, 0)]),
    "F5a": ("div", [(2000, 40), (1999, 20), (1000, 20), (999, 0), (500, 0), (499, -30), (0, -30)]),
    "F5b": ("cqBps", [(4000, 0), (3999, -40), (10000, 0)]),
    "F5c": ("cycBps", [(5000, 0), (5001, -80), (0, 0), (10000, -80)]),
    "F6": ("spikeX100", [(500, -50), (499, -20), (300, -20), (299, 0), (0, 0)]),
}


@pytest.mark.parametrize("fid", sorted(SPEC_BINS))
def test_every_bin_boundary(cfg, fid):
    feature, cases = SPEC_BINS[fid]
    table = cfg.tables[fid]
    assert table.feature == feature
    for value, points in cases:
        assert table.points(value) == points, (fid, value)


def test_max_points_and_codes(cfg):
    expected = {
        "F1": (80, 1001),
        "F2": (20, 1002),
        "F3": (0, 1004),
        "F4a": (60, 1006),
        "F4b": (20, 1007),
        "F5a": (40, 1008),
        "F5b": (0, 1009),
        "F5c": (0, 1010),
        "F6": (0, 1011),
    }
    for fid, (mx, code) in expected.items():
        assert (cfg.tables[fid].max, cfg.tables[fid].code) == (mx, code)


# ---- grade rules and reasons (spec 4.5, 4.6) ---------------------------------------------------


def raw(**kw):
    base = {
        "onW": 5000,
        "lateW": 0,
        "failW": 0,
        "clawW": 0,
        "badW": 0,
        "injW": 0,
        "spamW": 0,
        "n": 25,
        "age": 700_000,
        "div": 3000,
        "good": 2,
        "cqBps": 10_000,
        "cycBps": 0,
        "spikeX100": 0,
    }
    base.update(kw)
    return base


def decide(cfg, **kw):
    features, points, total = evaluate_features(raw(**kw), cfg)
    grade, rules = grade_of(features, total, cfg)
    return grade, total, reasons_of(features, points, rules, cfg), rules


def test_strong_record_reason(cfg):
    grade, total, reasons, _ = decide(cfg)
    assert grade == Grade.A and total == 820 and reasons == [1100, 0, 0, 0]


def test_cold_start_variants(cfg):
    assert decide(cfg, n=2, failW=2000)[0] == Grade.D
    assert decide(cfg, n=2, failW=1000)[0] == Grade.C
    assert decide(cfg, n=2, spamW=1000)[0] == Grade.C  # flag 500
    assert decide(cfg, n=2, clawW=1000)[0] == Grade.C  # neg 1000
    grade, _, reasons, rules = decide(cfg, n=2, spamW=998)  # flag 499
    assert grade == Grade.NEW and rules["coldStart"] and reasons[0] == 1101


def test_hard_flag_beats_everything(cfg):
    grade, _, reasons, rules = decide(cfg, injW=1000)
    assert grade == Grade.D and rules["hardFlag"] and reasons[0] == 1004 and reasons.count(1004) == 1
    assert decide(cfg, injW=999)[0] != Grade.D


def test_circular_cap(cfg):
    grade, total, reasons, rules = decide(cfg, cycBps=5001)
    assert total == 740 and grade == Grade.C and rules["circularCap"] and reasons[0] == 1010
    assert decide(cfg, cycBps=5000)[0] == Grade.A


def test_standing_gate_and_age_gate(cfg):
    grade, _, reasons, rules = decide(cfg, good=0)
    assert grade == Grade.C and rules["standingGate"] and reasons[0] == 1012
    grade, _, reasons, rules = decide(cfg, age=599, div=3000)
    assert grade == Grade.B and rules["ageGate"] and reasons[0] == 1007
    assert decide(cfg, age=600)[0] == Grade.A


def test_bands(cfg):
    # 600 + F1 80 + F2 20 + F4a 60 + F4b 20 + F5a 40 = 820; subtract to reach each band edge
    assert decide(cfg, div=1000, n=10)[1] == 780
    assert decide(cfg, spikeX100=500, div=499, n=0, age=0)[0] == Grade.NEW  # n=0 -> cold start
    _, total, _, _ = decide(cfg, cqBps=0, div=0, spikeX100=500)  # 820 - 40 - 70 - 50 = 660
    assert total == 660 and decide(cfg, cqBps=0, div=0, spikeX100=500)[0] == Grade.B


def test_reason_order_loss_then_code(cfg):
    # F4a loss 40 (1006), F4b loss 20 (1007), F5a loss 20 (1008), F5b loss 40 (1009): ties by code
    _, _, reasons, _ = decide(cfg, n=5, age=700, div=1000, cqBps=3999)
    assert reasons == [1006, 1009, 1007, 1008]


def test_reason_alternates(cfg):
    _, _, reasons, _ = decide(cfg, clawW=600)  # neg 600 without failures -> 1003
    assert 1003 in reasons and 1002 not in reasons
    _, _, reasons, _ = decide(cfg, spamW=500)  # flag 250 without injection -> 1005
    assert 1005 in reasons and 1004 not in reasons


def test_score_clamp(cfg):
    assert (
        decide(cfg, onW=0, failW=100_000, clawW=10_000, injW=999, n=3, div=0, cqBps=0, cycBps=9000, spikeX100=900)[1]
        == 300
    )


# ---- monotonicity (spec 4.4, 12.2): 5,000 seeded cases ------------------------------------------

UP = ("onW", "n", "age", "div", "cqBps")
DOWN = ("failW", "clawW", "badW", "injW", "spamW", "cycBps", "spikeX100")


def test_monotonicity_property(cfg):
    rng = random.Random(2026)
    for _ in range(5000):
        r = raw(
            onW=rng.randint(0, 30_000),
            lateW=rng.randint(0, 10_000),
            failW=rng.randint(0, 6000),
            clawW=rng.randint(0, 3000),
            badW=rng.randint(0, 3000),
            injW=rng.randint(0, 1500),
            spamW=rng.randint(0, 2000),
            n=rng.randint(0, 40),
            age=rng.randint(0, 2_000_000),
            div=rng.randint(0, 5000),
            good=rng.randint(0, 5),
            cqBps=rng.randint(0, 10_000),
            cycBps=rng.randint(0, 10_000),
            spikeX100=rng.randint(0, 1000),
        )
        base = evaluate_features(r, cfg)[2]
        key = rng.choice(UP + DOWN + ("late",))
        delta = rng.randint(1, 3000)
        r2 = dict(r)
        if key == "late":  # holding onW + lateW fixed, moving weight to late lowers the score
            moved = min(delta, r2["onW"])
            r2["onW"] -= moved
            r2["lateW"] += moved
            assert evaluate_features(r2, cfg)[2] <= base
            continue
        r2[key] += delta
        if key == "cqBps":
            r2[key] = min(r2[key], 10_000)
        changed = evaluate_features(r2, cfg)[2]
        if key in UP:
            assert changed >= base, (key, r, r2)
        else:
            assert changed <= base, (key, r, r2)


# ---- clusters (spec 4.1) -------------------------------------------------------------------------


def test_clusters_merge_rules():
    a, b, c, d = "0x" + "0a" * 20, "0x" + "0b" * 20, "0x" + "0c" * 20, "0x" + "0d" * 20
    zero = "0x" + "00" * 20
    clusters = build_clusters({1: (a,), 2: (b, a), 3: (c,), 4: (d, c), 5: (zero,), 6: (zero,), 7: ("",)})
    assert clusters[1] == clusters[2] == 1  # shared identity/vault owner
    assert clusters[3] == clusters[4] == 3  # one's identity owner is the other's vault owner
    assert clusters[5] == 5 and clusters[6] == 6 and clusters[7] == 7  # zero addresses never merge


# ---- counted-order filters and the fail cap (spec 4.1, 4.3) --------------------------------------


def test_counted_order_filters(cfg):
    m = Market()
    m.agent(ANCHOR)
    m.agent(1, has_vault=False)
    m.agent(2, vault_owner="0x" + "01" + f"{1:038x}")  # agent 2's vault owner is agent 1's identity owner
    m.order(ANCHOR, 1, 99_999)
    m.order(ANCHOR, 1, 100_000)
    missing = m.order(ANCHOR, 1, 200_000, invoice_ok=False)
    bad = m.order(ANCHOR, 1, 200_000, invoice_ok=False)
    m.order(2, 1, 200_000)
    m.order(ANCHOR, 1, 200_000, vault="0x" + "99" * 20)
    facts = m.facts()
    notes = {
        order_key(missing.vault, missing.order_id): "no_invoice",
        order_key(bad.vault, bad.order_id): "bad_invoice",
    }
    facts = ChainFacts(**{**facts.__dict__, "invoice_notes": notes})
    b = score(m.inputs(1), cfg, facts)
    notes = [o["note"] for o in b.orders]
    assert notes == ["dust", "", "no_invoice", "bad_invoice", "self_dealing", "unregistered_vault"]
    assert b.features["n"] == 1 and b.features["onW"] == 1000


def test_fail_cap_per_payer_cluster(cfg):
    m = Market()
    shared = "0x" + "5e" * 20
    m.agent(1, has_vault=False)
    for payer in (20, 21):  # one cluster (shared vault owner), both graded A
        m.agent(payer, Grade.A, vault_owner=shared)
        m.orders_n(payer, 1, 3, 200_000, "FAILED")
    m.agent(22, Grade.A)  # a distinct cluster
    m.orders_n(22, 1, 2, 100_000, "FAILED")  # min(a, ref) // ref: 500 each
    b = score(m.inputs(1), cfg, m.facts())
    assert b.features["failW"] == 3000 + 1000


def test_new_and_c_payers_have_no_adverse_weight(cfg):
    m = Market()
    m.agent(1, has_vault=False)
    for payer, grade in ((30, Grade.NEW), (31, Grade.C), (32, Grade.NONE), (33, Grade.D)):
        m.agent(payer, grade)
        m.order(payer, 1, 200_000, "FAILED")
    assert score(m.inputs(1), cfg, m.facts()).features["failW"] == 0


def test_spike_needs_min_prior_orders(cfg):
    m = Market()
    m.agent(ANCHOR)
    m.agent(1, has_vault=False)
    m.orders_n(ANCHOR, 1, 4, 200_000)
    m.orders_n(ANCHOR, 1, 2, 800_000)
    assert score(m.inputs(1), cfg, m.facts()).features["spikeX100"] == 0  # 6 orders: not yet
    m.order(ANCHOR, 1, 1_000_000, "PENDING")
    b = score(m.inputs(1), cfg, m.facts())
    assert b.features["spikeX100"] == 500 and b.points["F6"] == -50  # prior 4 x 0.20, max of last 3 = 1.00


def test_bundle_is_deterministic_and_order_independent(cfg):
    m = Market()
    m.agent(ANCHOR)
    m.agent(1, has_vault=False)
    m.orders_n(ANCHOR, 1, 6, USDC // 5)
    m.order(ANCHOR, 1, USDC // 5, "LATE")
    first = score(m.inputs(1), cfg, m.facts())
    m.orders.reverse()
    second = score(m.inputs(1), cfg, m.facts())
    assert first.as_canonical() == second.as_canonical()
    assert first.evidence_hash == second.evidence_hash
    assert list(first.as_canonical()) == [
        "schema",
        "model",
        "chain",
        "contracts",
        "subject",
        "orders",
        "reports",
        "features",
        "points",
        "score",
        "grade",
        "reasons",
        "rules",
        "override",
        "previous",
    ]
    assert first.as_canonical()["schema"] == "acb-evidence/1"
    assert first.model == {
        "id": "acb-sc-1.0.0",
        "configHash": cfg.config_hash,
        "detectorId": "acb-inj-1.0.0",
        "profile": "demo",
        "anchors": ["7"],
    }
