"""The registry of ``POST /admin/run`` actions (spec 9.4): market actions on every network, attack
actions on Anvil only."""

from __future__ import annotations

from acb.sim.attacks import ATTACK_ACTIONS
from acb.sim.market import MARKET_ACTIONS, Action

ACTIONS: dict[str, Action] = {**MARKET_ACTIONS, **ATTACK_ACTIONS}
"""Every action of ``POST /admin/run``."""

ANVIL_ONLY: frozenset[str] = frozenset(ATTACK_ACTIONS)
"""Attack actions that refuse to run outside Anvil (chain id 31337)."""

__all__ = ["ACTIONS", "ANVIL_ONLY"]
