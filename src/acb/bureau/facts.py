"""Chain facts the scorecard needs besides :class:`~acb.types.ScoreInputs` (spec 4.1, 4.2).

``ScoreInputs`` (Appendix B) carries agents, orders and reports. Report binding and verification also
need histories that are not part of it: which address was a vault's ``agent`` at a block
(``VaultInitialized`` / ``AgentChanged``), which address was an agent's ``agentWallet`` at a block
(``MetadataSet("agentWallet")``), the vaults' ``DecisionRecorded(REFUSE)`` records and the list of
registered, config-bound vaults. :class:`ChainFacts` holds them; the indexer builds it from logs
(:mod:`acb.bureau.inputs`) and tests build it by hand. Everything here is pure data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

Position = tuple[int, int]
"""``(block, logIndex)`` of a log, the order of events on the chain."""


@dataclass(frozen=True)
class HistoryEntry:
    """A value that became effective at ``(block, log_index)``; ``value`` None means unset."""

    block: int
    log_index: int
    value: str | None


def value_at(history: tuple[HistoryEntry, ...] | None, block: int, log_index: int | None = None) -> str | None:
    """The value in force at ``(block, log_index)``: the last entry at or before that position.

    With ``log_index`` None every entry of ``block`` counts (the value at the end of the block).
    """
    if not history:
        return None
    current: str | None = None
    for entry in history:
        if entry.block > block:
            break
        if entry.block == block and log_index is not None and entry.log_index > log_index:
            break
        current = entry.value
    return current


@dataclass(frozen=True)
class RefusalRecord:
    """A vault's ``DecisionRecorded`` with ``decision == REFUSE`` (backs INJECTION_ATTEMPT, spec 4.2)."""

    vault: str
    order_id: str
    payee_agent_id: int
    invoice_hash: str
    block: int
    log_index: int
    tx: str


def order_key(vault: str, order_id: str) -> str:
    """Key of an order (or of its refusal record) in the fact maps."""
    return f"{vault.lower()}:{order_id.lower()}"


@dataclass(frozen=True)
class ChainFacts:
    """Histories and records as of ``asOfBlock`` (all addresses and hashes lowercase)."""

    vaults: tuple[str, ...] = ()
    """Registered vaults whose ``token()``, ``bureau()`` and ``identity()`` match the deployment."""
    vault_agent_ids: Mapping[str, int] = field(default_factory=dict)
    """vault -> ``vaultAgentId`` from ``VaultRegistered``."""
    vault_agents: Mapping[str, tuple[HistoryEntry, ...]] = field(default_factory=dict)
    """vault -> history of its ``agent`` address."""
    wallets: Mapping[int, tuple[HistoryEntry, ...]] = field(default_factory=dict)
    """agentId -> history of its ``agentWallet`` (None = unset)."""
    refusals: Mapping[str, RefusalRecord] = field(default_factory=dict)
    """:func:`order_key` -> ``DecisionRecorded(REFUSE)`` of that order."""
    invoice_notes: Mapping[str, str] = field(default_factory=dict)
    """:func:`order_key` -> ``no_invoice`` or ``bad_invoice`` for orders whose ``invoice_ok`` is false."""

    def agent_of_vault(self, vault: str, block: int, log_index: int | None = None) -> str | None:
        return value_at(self.vault_agents.get(vault.lower()), block, log_index)

    def wallet_of(self, agent_id: int, block: int, log_index: int | None = None) -> str | None:
        return value_at(self.wallets.get(int(agent_id)), block, log_index)


EMPTY_FACTS = ChainFacts()

__all__ = ["EMPTY_FACTS", "ChainFacts", "HistoryEntry", "Position", "RefusalRecord", "order_key", "value_at"]
