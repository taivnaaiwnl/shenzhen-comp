"""Autonomy rules without a chain: the pure model of spec 5.3 and the evaluator's rules 1-7 of 5.4."""

from __future__ import annotations

import pytest

from acb.autonomy.config import load_autonomy_config
from acb.autonomy.evaluator import CleanOrder, Observation, Outflow, classify_outflow, decide, effective_grade_at
from acb.autonomy.model import TIER_FROZEN, OnchainState, apply, simulate_onchain
from acb.chain.contracts import VaultStatus
from acb.types import Grade

START = OnchainState(
    tier=1,
    tier_before_freeze=0,
    max_auto_tier=2,
    clean=0,
    points=0,
    last_tier_change_at=1000,
    promotion_cooldown=60,
    min_clean_to_promote=(3, 3, 6, 0),
)


# ---- model (5.3) ---------------------------------------------------------------------------------


def test_severe_violation_freezes_and_serious_demotes():
    s, err = apply(START, {"op": "violation", "severity": 3, "at": 1100})
    assert err is None and s.frozen and s.tier_before_freeze == 1 and s.last_tier_change_at == 1100
    s, _ = apply(START, {"op": "violation", "severity": 2, "at": 1100})
    assert s.tier == 0 and s.points == 0
    s, _ = apply(s, {"op": "violation", "severity": 2, "at": 1200})  # tier 0: points only
    assert s.tier == 0 and s.points == 2


def test_two_minor_violations_demote():
    s = simulate_onchain(START, [{"op": "violation", "severity": 1, "at": 1001}])
    assert (s.tier, s.points) == (1, 1)
    s = simulate_onchain(s, [{"op": "violation", "severity": 1, "at": 1002}])
    assert (s.tier, s.points, s.last_tier_change_at) == (0, 0, 1002)


def test_promotion_conditions():
    s = START
    assert apply(s, {"op": "set_tier", "tier": 2, "at": 2000})[1] == "PromotionNotEarned"
    for _ in range(3):
        s, _ = apply(s, {"op": "clean"})
    assert apply(s, {"op": "set_tier", "tier": 2, "at": 1059})[1] == "PromotionCooldown"
    assert apply(s, {"op": "set_tier", "tier": 3, "at": 2000})[1] == "PromotionTooLarge"
    assert apply(s, {"op": "set_tier", "tier": 1, "at": 2000})[1] == "SameTier"
    assert apply(s, {"op": "set_tier", "tier": 4, "at": 2000})[1] == "BadTier"
    pointy, _ = apply(s, {"op": "violation", "severity": 1, "at": 1001})
    assert apply(pointy, {"op": "set_tier", "tier": 2, "at": 2000})[1] == "PromotionNotEarned"
    s2, err = apply(s, {"op": "set_tier", "tier": 2, "at": 1060})
    assert err is None and s2.tier == 2 and s2.clean == 0
    assert apply(s2, {"op": "set_tier", "tier": 3, "at": 9999})[1] == "AboveMaxAutoTier"
    assert apply(s2, {"op": "set_tier", "tier": 0, "at": 1061}) == (
        OnchainState(0, 0, 2, 0, 0, 1061, 60, (3, 3, 6, 0)),
        None,
    )


def test_freeze_unfreeze_and_max_auto_tier():
    frozen, _ = apply(START, {"op": "freeze", "at": 1500})
    assert frozen.tier == TIER_FROZEN
    assert apply(frozen, {"op": "freeze", "at": 1501})[1] == "Frozen"
    assert apply(frozen, {"op": "set_tier", "tier": 0, "at": 1501})[1] == "Frozen"
    assert apply(frozen, {"op": "violation", "severity": 3, "at": 1501}) == (frozen, None)
    clamped, _ = apply(frozen, {"op": "set_max_auto_tier", "value": 0, "at": 1502})
    assert clamped.frozen and clamped.tier_before_freeze == 0 and clamped.max_auto_tier == 0
    assert apply(frozen, {"op": "unfreeze", "tier": 2, "at": 1503})[1] == "BadTier"
    back, err = apply(frozen, {"op": "unfreeze", "tier": 1, "at": 1503})
    assert err is None and back.tier == 1
    assert apply(back, {"op": "unfreeze", "tier": 0, "at": 1504})[1] == "NotFrozen"
    lowered, _ = apply(back, {"op": "set_max_auto_tier", "value": 0, "at": 1505})
    assert lowered.tier == 0 and lowered.last_tier_change_at == 1505


# ---- evaluator rules (5.4) -----------------------------------------------------------------------

CFG = load_autonomy_config()


def status(tier=1, clean=3, points=0, last=1000, max_auto=2) -> VaultStatus:
    return VaultStatus(
        tier=tier,
        frozen=tier == TIER_FROZEN,
        tier_before_freeze=0,
        max_auto_tier=max_auto,
        balance=15_000_000,
        escrow_locked=0,
        reserve_floor=3_000_000,
        spent=0,
        count=0,
        clean=clean,
        points=points,
        last_tier_change_at=last,
    )


def clean_orders(*payees: int, matched: bool | None = True) -> tuple[CleanOrder, ...]:
    return tuple(
        CleanOrder(f"0x{i:064x}", p, 200_000, f"0x{i + 100:064x}", matched, "0x" + "ab" * 32, i, "DeliveryRecorded")
        for i, p in enumerate(payees)
    )


def obs(**kw) -> Observation:
    base = {
        "vault": "0x" + "11" * 20,
        "as_of_block": 50,
        "now": 2000,
        "status": status(),
        "min_clean_to_promote": 3,
        "promotion_cooldown": 60,
        "own_grade": Grade.A,
        "loss": 0,
        "clean_orders": clean_orders(5, 6, 5),
    }
    base.update(kw)
    return Observation(**base)


OUTFLOW = Outflow("Paid", "0x" + "01" * 32, 5, 200_000, "0x" + "02" * 32, 40, 0, "0x" + "03" * 32)


def test_rule_order():
    p = decide(obs(status=status(tier=TIER_FROZEN), unmatched=(OUTFLOW,)), CFG)
    assert p.rule == "frozen" and p.action == "none" and OUTFLOW.order_id in p.alerts[0]  # reported, not sent
    assert not decide(obs(status=status(tier=TIER_FROZEN)), CFG).alerts
    p = decide(obs(unmatched=(OUTFLOW,), own_grade=Grade.D), CFG)
    assert (p.rule, p.action, p.reason_code) == ("unmatched_outflow", "freeze", 3205)
    p = decide(obs(own_grade=Grade.D), CFG)
    assert (p.rule, p.action, p.to_tier, p.reason_code) == ("own_grade_d", "set_tier", 0, 3105) and p.alerts
    p = decide(obs(own_grade=Grade.D, status=status(tier=0)), CFG)
    assert p.rule == "own_grade_d" and p.action == "none" and p.alerts  # never a freeze, never a promotion
    p = decide(obs(own_grade=Grade.C, status=status(tier=2)), CFG)
    assert (p.rule, p.to_tier, p.reason_code) == ("own_grade_c", 1, 3102)
    assert decide(obs(own_grade=Grade.C, status=status(tier=1)), CFG).rule == "none"
    p = decide(obs(loss=500_001), CFG)
    assert (p.rule, p.to_tier, p.reason_code) == ("unsecured_loss", 0, 3103)
    assert decide(obs(loss=500_000), CFG).rule == "promote"
    p = decide(obs(), CFG)
    assert (p.rule, p.action, p.to_tier, p.reason_code) == ("promote", "set_tier", 2, 3000)


@pytest.mark.parametrize(
    ("kw", "blocker"),
    [
        ({"status": status(tier=2, max_auto=2)}, "at maxAutoTier"),
        ({"now": 1059}, "cooldown"),
        ({"status": status(clean=2)}, "clean 2 < 3"),
        ({"min_clean_to_promote": 0}, "clean 3 < 0"),
        ({"status": status(points=1)}, "violation points 1"),
        ({"own_grade": Grade.C}, "own grade C"),
        ({"own_grade": Grade.NONE}, "own grade NONE"),
        ({"clean_orders": clean_orders(5, 5, 5)}, "1 distinct payees < 2"),
        ({"clean_orders": clean_orders(5, 6, matched=False)}, "a clean settlement has no signer decision"),
        ({"signer_reachable": False}, "signer decision log unreachable"),
        ({"pending": 1}, "1 outflow(s) wait for the signer's mined transaction"),
    ],
)
def test_promotion_blockers(kw, blocker):
    p = decide(obs(**kw), CFG)
    assert p.rule == "none" and blocker in p.detail


def test_promotion_for_new_and_b_grades():
    assert decide(obs(own_grade=Grade.NEW), CFG).rule == "promote"
    assert decide(obs(own_grade=Grade.B), CFG).rule == "promote"


def test_signer_alert_is_reported_but_never_freezes():
    p = decide(obs(signer_reachable=False, signer_alert=True), CFG)
    assert p.action == "none" and "signer decision log unreachable" in p.alerts


def test_effective_grade_emulation():
    upgrade = [(1000, Grade.A, Grade.NEW, 500)]
    assert effective_grade_at(upgrade, 1059, 60, 0) == Grade.NEW  # upgrade not yet effective
    assert effective_grade_at(upgrade, 1060, 60, 0) == Grade.A
    first = [(1000, Grade.A, Grade.NONE, 0)]
    assert effective_grade_at(first, 1010, 60, 0) == Grade.NONE
    down = [(1000, Grade.D, Grade.A, 500)]
    assert effective_grade_at(down, 1001, 60, 0) == Grade.D  # downgrades are immediate
    assert effective_grade_at(upgrade, 1000 + 700_000, 60, 604_800) == Grade.NONE  # stale A
    assert effective_grade_at([(1000, Grade.C, Grade.A, 500)], 1000 + 700_000, 60, 604_800) == Grade.C
    assert effective_grade_at([], 5, 60, 0) == Grade.NONE


def test_config_values():
    assert CFG.loss_limit == (0, 500_000, 1_000_000, 2_000_000)
    assert CFG.own_grade_promote == frozenset({Grade.A, Grade.B, Grade.NEW})
    assert (CFG.min_distinct_payees, CFG.poll_s, CFG.signer_unreachable_alert_after) == (2, 30, 3)
    assert CFG.decision_tx_grace_s == 300


ORDER = "0x" + "aa" * 32
TX = "0x" + "bb" * 32


def entry(**kw) -> dict:
    base = {"found": True, "requestId": "r1", "kind": "PAY", "orderId": ORDER, "txHash": TX, "loggedAt": "t"}
    base.update(kw)
    return base


@pytest.mark.parametrize(
    ("e", "age", "verdict"),
    [
        (entry(), 0, "matched"),
        (entry(orderId=ORDER.upper().replace("0X", "0x"), txHash=TX.upper().replace("0X", "0x")), 0, "matched"),
        (entry(kind="ESCROW"), 0, "matched"),
        (entry(kind="PAY_APPROVED"), 0, "matched"),
        (entry(outcome="PAY_CAPPED"), 0, "matched"),
        (None, 0, "unknown"),
        (False, 0, "unmatched"),
        (True, 0, "unmatched"),  # a bare "found" says nothing about which outflow it was
        ({"found": False}, 0, "unmatched"),
        (entry(orderId="0x" + "cc" * 32), 0, "unmatched"),  # a public hash copied into another order
        (entry(orderId=None), 0, "unmatched"),
        (entry(kind="RECORD_DECISION"), 0, "unmatched"),
        (entry(kind="REPORT"), 0, "unmatched"),
        (entry(outcome="REFUSE"), 0, "unmatched"),
        (entry(outcome="NEEDS_OWNER_APPROVAL"), 0, "unmatched"),
        (entry(txHash="0x" + "dd" * 32), 0, "unmatched"),  # the signer mined another transaction
        (entry(txHash=None), 300, "pending"),
        (entry(txHash=None), 301, "unmatched"),
    ],
)
def test_classify_outflow(e, age, verdict):
    assert classify_outflow(e, order_id=ORDER, tx=TX, age_s=age, grace_s=300) == verdict
