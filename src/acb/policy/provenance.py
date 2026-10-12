"""Provenance rules (spec 6.3, CaMeL-style): where each security-relevant value may come from.

=======================  ========================================================================
Argument                 Allowed source
=======================  ========================================================================
Payee (agentId)          On-chain mandate AND equal to the validated invoice's issuer (V08, C07, C08)
Payee address            The wallet pinned in the on-chain mandate, equal to the live wallet (C07)
Amount                   The validated invoice ``amount`` (V03, V04, C09), bounded by mandate and caps
Token                    Network configuration only (V05, C03)
Order id                 Code-derived (6.6), :func:`acb.canonical.order_id`
Decision                 The policy, possibly stricter by the LLM's choice (C18)
Quote content            Catalogue enum and integer chosen in an owner turn (V04, V08)
=======================  ========================================================================

Counterparty free text (memo, item description, invoice id, sku) is never parsed for payees,
amounts or tokens. It is read here only to tell whether a proposed payee **appears** in it, which turns
a payee violation into INJECTION_FOLLOWED (5.2).
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from acb.injection import invoice_fields, normalize

USDC_AMOUNT_PATTERN = re.compile(r"^[0-9]{1,6}(\.[0-9]{1,2})?$")
"""Amount strings of the LLM tools (8.3): USDC with at most two decimals."""

USDC_UNIT = 10**6


def usdc_to_units(text: str) -> int:
    """``"0.80"`` -> ``800000``. Exact decimal arithmetic; raises ``ValueError`` on anything else."""
    if not isinstance(text, str) or not USDC_AMOUNT_PATTERN.fullmatch(text):
        raise ValueError("amount must be a USDC string like 0.80")
    try:
        value = Decimal(text) * USDC_UNIT
    except InvalidOperation as exc:  # pragma: no cover - the pattern already excludes this
        raise ValueError("bad amount") from exc
    if value != value.to_integral_value():
        raise ValueError("amount is not a whole number of base units")
    return int(value)


def units_to_usdc(units: int) -> str:
    """``800000`` -> ``"0.80"`` (two decimals; exact for multiples of 10,000)."""
    whole, frac = divmod(int(units), USDC_UNIT)
    if frac % 10_000:
        return f"{whole}.{frac:06d}"
    return f"{whole}.{frac // 10_000:02d}"


def payee_matches(proposed: int | None, issuer: int) -> bool:
    """C08: the restated payee must be the invoice issuer."""
    return proposed is not None and int(proposed) == int(issuer)


def amount_matches(proposed: int | None, invoice_amount: int) -> bool:
    """C09: the restated amount must be the invoice amount."""
    return proposed is not None and int(proposed) == int(invoice_amount)


def counterparty_text(invoice_wire: dict[str, Any]) -> str:
    """All counterparty strings of a wire invoice joined (for :func:`mentions_agent` only)."""
    return "\n".join(invoice_fields(invoice_wire).values())


def mentions_agent(text: str, agent_id: int) -> bool:
    """True when ``text`` names ``agent_id`` the way the injection rules see it: ``agent #N``,
    ``agent N``, ``agent id N``, ``#N`` or ``id N`` (after NFKC, casefold and invisible-character
    removal)."""
    norm = normalize(text or "")
    pattern = rf"(?:agent\s*(?:#|no\.?|id|number)?|#|\bid)\s*[:#]?\s*0*{int(agent_id)}(?![0-9])"
    return re.search(pattern, norm) is not None


def mentions_address(text: str, address: str | None) -> bool:
    """True when ``text`` contains ``address`` (case-insensitive, as a whole 40-hex address)."""
    if not address:
        return False
    norm = normalize(text or "")
    return re.search(rf"{re.escape(address.lower())}(?![0-9a-f])", norm) is not None


__all__ = [
    "USDC_AMOUNT_PATTERN",
    "USDC_UNIT",
    "amount_matches",
    "counterparty_text",
    "mentions_address",
    "mentions_agent",
    "payee_matches",
    "units_to_usdc",
    "usdc_to_units",
]
