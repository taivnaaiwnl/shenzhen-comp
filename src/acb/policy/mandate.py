"""Helpers over the on-chain mandate view (spec 3.2 "Mandates", 6.2 C06/C07/C16). Pure functions.

A mandate is the owner's standing payment authority registered in the vault: payees with pinned
payout wallets, per-transaction and total budget, expiry, recurring flag. The policy never trusts a
mandate that did not come from the chain (:class:`~acb.types.MandateView`).
"""

from __future__ import annotations

from collections.abc import Iterable

from acb.types import MandateView

CODE_MANDATE_INVALID = 2104
CODE_RECURRING_TIER = 2118
CODE_NOT_IN_MANDATE = 2103
CODE_WALLET_CHANGED = 2121


def pinned_wallet(mandate: MandateView | None, agent_id: int) -> str | None:
    """The payout wallet pinned for ``agent_id`` in ``mandate`` (lowercase), or None if not a payee."""
    if mandate is None:
        return None
    for payee, wallet in zip(mandate.payee_agent_ids, mandate.payee_wallets, strict=False):
        if int(payee) == int(agent_id):
            return wallet.lower()
    return None


def is_active(mandate: MandateView | None, now: int) -> bool:
    """Active and not expired at ``now`` (the vault allows ``block.timestamp <= validUntil``)."""
    return mandate is not None and mandate.active and now <= mandate.valid_until


def remaining_budget(mandate: MandateView) -> int:
    """``maxTotal - spent`` (never negative)."""
    return max(0, int(mandate.max_total) - int(mandate.spent))


def check_mandate(
    mandate: MandateView | None,
    *,
    now: int,
    vault_address: str,
    tier: int,
    recurring_min_tier: int,
    expected_mandate_id: str | None = None,
) -> tuple[bool, int | None, str]:
    """C06 MANDATE_AUTH: ``(passed, code, detail)``.

    Fails with 2104 when the mandate is missing, belongs to another vault, is not the mandate of the
    quote request, is inactive or expired; with 2118 when it is recurring and the tier is below
    ``recurring_min_tier``.
    """
    if mandate is None:
        return False, CODE_MANDATE_INVALID, "no on-chain mandate"
    if mandate.vault.lower() != vault_address.lower():
        return False, CODE_MANDATE_INVALID, "mandate is not registered in this vault"
    if expected_mandate_id is not None and mandate.mandate_id != expected_mandate_id.lower():
        return False, CODE_MANDATE_INVALID, "mandate differs from the quote request's mandate"
    if not mandate.active:
        return False, CODE_MANDATE_INVALID, "mandate revoked"
    if now > mandate.valid_until:
        return False, CODE_MANDATE_INVALID, "mandate expired"
    if mandate.recurring and tier < recurring_min_tier:
        return False, CODE_RECURRING_TIER, f"recurring mandate needs tier {recurring_min_tier}"
    return True, None, "mandate active"


def check_payee(
    mandate: MandateView | None, issuer_agent_id: int, live_wallet: str | None
) -> tuple[bool, int | None, str, str | None]:
    """C07 PAYEE_IN_MANDATE: ``(passed, code, detail, pinned_wallet)``.

    2103 when the issuer has no pinned wallet in the mandate; 2121 when the pinned wallet differs from
    the live ``getAgentWallet`` (payout redirection, A54).
    """
    pinned = pinned_wallet(mandate, issuer_agent_id)
    if pinned is None:
        return False, CODE_NOT_IN_MANDATE, "issuer is not a payee of the mandate", None
    if live_wallet is None or pinned != live_wallet.lower():
        return False, CODE_WALLET_CHANGED, "pinned wallet differs from the live agent wallet", pinned
    return True, None, "pinned wallet equals the live wallet", pinned


def payees_of(mandates: Iterable[MandateView], now: int) -> frozenset[int]:
    """Agent ids of every payee in the active, unexpired mandates."""
    out: set[int] = set()
    for m in mandates:
        if is_active(m, now):
            out.update(int(a) for a in m.payee_agent_ids)
    return frozenset(out)


__all__ = [
    "CODE_MANDATE_INVALID",
    "CODE_NOT_IN_MANDATE",
    "CODE_RECURRING_TIER",
    "CODE_WALLET_CHANGED",
    "check_mandate",
    "check_payee",
    "is_active",
    "payees_of",
    "pinned_wallet",
    "remaining_budget",
]
