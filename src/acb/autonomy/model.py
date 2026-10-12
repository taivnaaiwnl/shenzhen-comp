"""Pure Python model of the on-chain Earned Autonomy rules (spec 5.3), used to cross-check TierVault.

``simulate_onchain(state, events) -> state`` replays a sequence of vault calls with the same rules as
``TierVault.recordViolation``, ``setTier``, ``freeze``, ``unfreeze``, ``setMaxAutoTier`` and the
clean-record rule. A call the contract would reject leaves the state unchanged; :func:`apply` also
returns the name of the custom error the contract reverts with.

Events are dicts with ``op`` and ``at`` (the block timestamp of the call):

- ``{"op": "violation", "severity": 1|2|3}`` (``recordViolation`` by the agent or the operator)
- ``{"op": "clean"}`` (a settlement that passes the clean-record rule: amount >= minCleanAmount, payee A/B)
- ``{"op": "set_tier", "tier": t}`` (autonomy operator)
- ``{"op": "freeze"}`` (owner, operator or agent)
- ``{"op": "unfreeze", "tier": 0|1}`` (owner)
- ``{"op": "set_max_auto_tier", "value": m}`` (owner)
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

TIER_FROZEN = 255
TIER_MAX = 3
U16_MAX = 0xFFFF


@dataclass(frozen=True)
class OnchainState:
    """The autonomy part of a vault's state."""

    tier: int
    tier_before_freeze: int
    max_auto_tier: int
    clean: int
    points: int
    last_tier_change_at: int
    promotion_cooldown: int
    min_clean_to_promote: tuple[int, int, int, int]

    @property
    def frozen(self) -> bool:
        return self.tier == TIER_FROZEN

    def summary(self) -> dict[str, int]:
        return {
            "tier": self.tier,
            "tierBeforeFreeze": self.tier_before_freeze,
            "maxAutoTier": self.max_auto_tier,
            "clean": self.clean,
            "points": self.points,
        }


def _change(state: OnchainState, to: int, at: int) -> OnchainState:
    return replace(state, tier=to, last_tier_change_at=at, clean=0, points=0)


def _freeze(state: OnchainState, at: int) -> OnchainState:
    return _change(replace(state, tier_before_freeze=state.tier), TIER_FROZEN, at)


def apply(state: OnchainState, event: Mapping[str, Any]) -> tuple[OnchainState, str | None]:
    """Apply one call; returns the new state and the contract's error name (None if it succeeds)."""
    op = event["op"]
    at = int(event.get("at", state.last_tier_change_at))
    if op == "violation":
        sev = int(event["severity"])
        if not 1 <= sev <= 3:
            return state, "BadSeverity"
        if state.frozen:
            return state, None
        if sev == 3:
            return _freeze(state, at), None
        if sev == 2 and state.tier > 0:
            return _change(state, state.tier - 1, at), None
        points = min(state.points + sev, U16_MAX)
        state = replace(state, points=points)
        if sev == 1 and points >= 2 and state.tier > 0:
            state = _change(state, state.tier - 1, at)
        return state, None
    if op == "clean":
        return replace(state, clean=min(state.clean + 1, U16_MAX)), None
    if op == "set_tier":
        new = int(event["tier"])
        if state.frozen:
            return state, "Frozen"
        if new > TIER_MAX:
            return state, "BadTier"
        if new == state.tier:
            return state, "SameTier"
        if new > state.tier:
            if new != state.tier + 1:
                return state, "PromotionTooLarge"
            if new > state.max_auto_tier:
                return state, "AboveMaxAutoTier"
            if at < state.last_tier_change_at + state.promotion_cooldown:
                return state, "PromotionCooldown"
            needed = state.min_clean_to_promote[state.tier]
            if needed == 0 or state.clean < needed or state.points != 0:
                return state, "PromotionNotEarned"
        return _change(state, new, at), None
    if op == "freeze":
        if state.frozen:
            return state, "Frozen"
        return _freeze(state, at), None
    if op == "unfreeze":
        to = int(event["tier"])
        if not state.frozen:
            return state, "NotFrozen"
        if to > 1:
            return state, "BadTier"
        return _change(state, to, at), None
    if op == "set_max_auto_tier":
        m = int(event["value"])
        if m > TIER_MAX:
            return state, "BadTier"
        state = replace(state, max_auto_tier=m)
        if state.frozen:
            return replace(state, tier_before_freeze=min(state.tier_before_freeze, m)), None
        if state.tier > m:
            return _change(state, m, at), None
        return state, None
    raise ValueError(f"unknown op {op!r}")


def simulate_onchain(state: OnchainState, events: Iterable[Mapping[str, Any]]) -> OnchainState:
    """Replay ``events`` from ``state`` with the rules of spec 5.3; rejected calls change nothing."""
    for event in events:
        state, _ = apply(state, event)
    return state


__all__ = ["TIER_FROZEN", "OnchainState", "apply", "simulate_onchain"]
