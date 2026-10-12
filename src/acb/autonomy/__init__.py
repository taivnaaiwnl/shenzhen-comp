"""Autonomy operator (package WP4, process P5): the off-chain rules of Earned Autonomy (spec 5.4) and a
pure model of the on-chain rules (5.3). The operator holds only ``AUTONOMY_PRIVATE_KEY``; the party that
grades (P4) is not the party that promotes (P5)."""

from __future__ import annotations

from acb.autonomy.model import OnchainState, apply, simulate_onchain

__all__ = ["OnchainState", "apply", "simulate_onchain"]
