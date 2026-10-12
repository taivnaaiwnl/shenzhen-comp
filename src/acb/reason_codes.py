"""Reason-code catalogue (spec Appendix A). Texts are shown verbatim in the UI and the documents.

Codes are ``uint16`` on-chain. Retired codes stay listed (``retired=True``) and are never reused;
2110 is intentionally unused.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


@dataclass(frozen=True)
class ReasonInfo:
    code: int
    name: str
    category: str
    text: str
    retired: bool = False


# category by code range
_CATEGORIES: tuple[tuple[int, int, str], ...] = (
    (1000, 1099, "grade_adverse"),
    (1100, 1199, "grade_info"),
    (2000, 2099, "decision_ok"),
    (2100, 2199, "decision_refuse"),
    (2200, 2299, "decision_approval"),
    (2300, 2399, "decision_info"),
    (2400, 2499, "refund"),
    (3000, 3099, "autonomy_promote"),
    (3100, 3199, "autonomy_demote"),
    (3200, 3299, "autonomy_freeze"),
    (3300, 3399, "autonomy_unfreeze"),
    (4000, 4099, "signer_ok"),
    (4100, 4199, "signer_reject"),
    (4200, 4299, "tx"),
)

CATEGORIES: tuple[str, ...] = tuple(c for _, _, c in _CATEGORIES)


def _category_of(code: int) -> str:
    for low, high, name in _CATEGORIES:
        if low <= code <= high:
            return name
    raise KeyError(code)


_TABLE: tuple[tuple[int, str, str], ...] = (
    (1001, "LOW_FULFILMENT_RATE", "Share of orders delivered on time is low"),
    (1002, "FAILED_DELIVERIES", "Orders were paid but not delivered"),
    (
        1003,
        "NEGATIVE_REPORTS",
        "Trading partners in good standing reported bad data, or the agent clawed back delivered orders",
    ),
    (1004, "INJECTION_FLAG", "Signed content that tried to manipulate a paying agent"),
    (1005, "SPAM_FLAG", "Signed unsolicited invoices"),
    (1006, "LIMITED_HISTORY", "Few completed orders"),
    (1007, "SHORT_ACCOUNT_AGE", "Identity registered recently"),
    (1008, "LOW_COUNTERPARTY_DIVERSITY", "Trades with few distinct, well-graded partners"),
    (1009, "LOW_QUALITY_COUNTERPARTIES", "Most volume is with poorly graded or new partners"),
    (1010, "CIRCULAR_VOLUME", "Volume flows in a circle back to the agent (wash trading)"),
    (1011, "VOLUME_SPIKE", "Recent order sizes are far above its usual size"),
    (1012, "NO_GOOD_STANDING_COUNTERPARTY", "No completed trade with a partner in good standing"),
    (1100, "STRONG_RECORD", "No adverse factors"),
    (1101, "COLD_START", "Not enough history yet: treated as new"),
    (1102, "APPEAL_UPHELD", "Grade revised after an upheld appeal"),
    (1103, "APPEAL_REJECTED", "Appeal reviewed and rejected"),
    (1104, "MANUAL_OVERRIDE", "Grade set by a documented manual override"),
    (2000, "PAY_GRADE_A", "Paid: grade A counterparty within tier limits"),
    (2001, "PAY_GRADE_B", "Paid: grade B counterparty within the B-grade cap"),
    (2002, "PAY_CAPPED", "Paid for a reduced quantity: the invoice exceeded the cap for this grade"),
    (2003, "ESCROW_GRADE_C", "Escrow required: grade C counterparty"),
    (2004, "ESCROW_UNRATED", "Escrow required: counterparty is new or unrated"),
    (2005, "PAY_NEW_SMALL", "Small direct payment to a new counterparty (Tier 3)"),
    (2007, "OWNER_APPROVED", "Executed under explicit owner approval"),
    (2008, "RELEASED_DELIVERED", "Escrow released after the provider's delivery claim"),
    (2009, "ESCROW_EXPOSURE_LIMIT", "Escrow required: unsecured exposure to this counterparty is at its limit"),
    (2100, "REFUSE_GRADE_D", "Refused: counterparty grade D"),
    (2101, "REFUSE_FROZEN", "Refused: the wallet is frozen"),
    (
        2102,
        "REFUSE_INVOICE_AUTH",
        "Refused: invoice not signed by the counterparty's pinned wallet, or inconsistent",
    ),
    (2103, "REFUSE_NOT_IN_MANDATE", "Refused: payee is not in the owner's mandate"),
    (2104, "REFUSE_MANDATE_INVALID", "Refused: mandate missing, expired or revoked on-chain"),
    (2105, "REFUSE_PROVENANCE_AMOUNT", "Refused: proposed amount does not match the signed invoice"),
    (2106, "REFUSE_PROVENANCE_PAYEE", "Refused: proposed payee does not match the invoice issuer"),
    (2107, "REFUSE_DUPLICATE", "Refused: duplicate invoice or order"),
    (2108, "REFUSE_INJECTION", "Refused: the invoice text contains instructions aimed at the wallet"),
    (2109, "REFUSE_RESERVE_FLOOR", "Refused: payment would break the reserve floor"),
    (2111, "REFUSE_CHAIN_TOKEN", "Refused: wrong chain or token"),
    (2112, "REFUSE_NOT_RECORDED", "Refused (not recorded on-chain): on-chain recording budget reached"),
    (2113, "REFUSE_AGENT_JUDGEMENT", "Refused on the agent's own judgement"),
    (2114, "REFUSE_INVALID_PROPOSAL", "Refused: the proposed action was malformed or unknown"),
    (2115, "REFUSE_STATE_CHANGED", "Refused at signing: counterparty grade or wallet state changed"),
    (2117, "REFUSE_INVOICE_EXPIRED", "Refused: invoice expired"),
    (2118, "REFUSE_RECURRING_TIER", "Refused: recurring mandates need Tier 2 or higher"),
    (
        2119,
        "INVOICE_REJECTED",
        "Invoice dropped before any decision: malformed, unsolicited, off-catalogue or badly signed",
    ),
    (2120, "REFUSE_DENYLISTED", "Refused: counterparty or payout address is on the denylist"),
    (
        2121,
        "REFUSE_PAYEE_WALLET_CHANGED",
        "Refused: the counterparty's payout wallet changed since the owner's mandate",
    ),
    (2200, "APPROVAL_TIER0", "Owner approval needed: the wallet is at Tier 0 (maker-checker)"),
    (2201, "APPROVAL_OVER_CAP", "Owner approval needed: above the cap for this tier and grade"),
    (2202, "APPROVAL_WINDOW", "Owner approval needed: 24-hour limit or transaction count reached"),
    (2203, "APPROVAL_BUDGET", "Owner approval needed: mandate budget exceeded"),
    (2204, "APPROVAL_SPIKE", "Owner approval needed: amount far above this payee's usual size"),
    (2300, "INFO_LLM_FALLBACK", "Decided in rule-based mode (AI unavailable)"),
    (2301, "INFO_POLICY_STRICTER", "The policy applied a stricter action than the AI proposed"),
    (2302, "INFO_AGENT_STRICTER", "The AI chose a stricter action than the policy required"),
    (2400, "REFUND_NON_DELIVERY", "Escrow refunded: not delivered by the deadline"),
    (2401, "REFUND_RISK_ROLLBACK", "Escrow refunded before delivery: risk rollback (not the counterparty's fault)"),
    (2402, "REFUND_TIMEOUT", "Escrow refunded by a third party after the deadline and dispute window"),
    (2403, "REFUND_DISPUTED_AFTER_CLAIM", "Escrow refunded by the owner after a delivery claim (dispute)"),
    (3000, "PROMOTE_CLEAN_RECORD", "Promoted one tier: clean record and cooldown passed"),
    (3100, "DEMOTE_SERIOUS_VIOLATION", "Demoted one tier: serious violation"),
    (3101, "DEMOTE_MINOR_VIOLATIONS", "Demoted one tier: repeated minor violations"),
    (3102, "DEMOTE_OWN_GRADE_C", "Demoted: the wallet's own grade fell to C"),
    (3103, "DEMOTE_UNSECURED_LOSS", "Demoted: losses on unsecured payments"),
    (3104, "DEMOTE_OWNER_LIMIT", "Demoted: owner lowered the maximum tier"),
    (3105, "DEMOTE_OWN_GRADE_D", "Demoted to maker-checker: the wallet's own grade fell to D"),
    (3200, "FREEZE_SEVERE_VIOLATION", "Frozen: severe violation (circuit breaker)"),
    (3202, "FREEZE_BY_OWNER", "Frozen by the owner"),
    (3203, "FREEZE_BY_AGENT", "Frozen by the agent itself"),
    (3204, "FREEZE_BY_OPERATOR", "Frozen by the autonomy operator"),
    (3205, "FREEZE_UNMATCHED_OUTFLOW", "Frozen: an outflow had no matching signer decision (possible key theft)"),
    (3300, "UNFREEZE_BY_OWNER", "Unfrozen by the owner (human reset)"),
    (4000, "SIGNED", "Signed and sent"),
    (4100, "REJECT_AUTH", "Signer rejected: authentication failed"),
    (4101, "REJECT_MISMATCH", "Signer applied a stricter decision than the agent"),
    (4102, "REJECT_SIMULATION", "Signer rejected: the transaction would revert"),
    (4103, "REJECT_RATE_LIMIT", "Signer rejected: rate limit"),
    (4104, "REJECT_NOT_ALLOWED", "Signer rejected: action not allowed"),
    (4200, "TX_REVERTED", "Transaction reverted on-chain"),
    (4201, "TX_TIMEOUT", "Transaction not mined in time"),
)

_RETIRED: tuple[tuple[int, str], ...] = (
    (2006, "Was CREDIT_LINE; credit lines were removed in v1.1"),
    (2116, "Was REFUSE_CREDIT_NOT_ALLOWED"),
    (3201, "Was FREEZE_OWN_GRADE_D"),
)

CATALOGUE: dict[int, ReasonInfo] = {
    code: ReasonInfo(code=code, name=name, category=_category_of(code), text=text) for code, name, text in _TABLE
}
RETIRED: dict[int, ReasonInfo] = {
    code: ReasonInfo(code=code, name="(retired)", category="retired", text=text, retired=True)
    for code, text in _RETIRED
}
UNUSED: frozenset[int] = frozenset({2110})

ReasonCode = IntEnum("ReasonCode", {info.name: code for code, info in CATALOGUE.items()})
"""Every active reason code by name, e.g. ``ReasonCode.REFUSE_GRADE_D == 2100``."""


def info(code: int) -> ReasonInfo:
    """Catalogue entry of ``code`` (retired codes included); ``KeyError`` for an unknown code."""
    code = int(code)
    if code in CATALOGUE:
        return CATALOGUE[code]
    if code in RETIRED:
        return RETIRED[code]
    raise KeyError(f"unknown reason code {code}")


def text(code: int) -> str:
    """Human-readable text of ``code``, verbatim from Appendix A."""
    return info(code).text


def name(code: int) -> str:
    """Name of ``code`` (``"(retired)"`` for retired codes)."""
    return info(code).name


def category(code: int) -> str:
    """Category of ``code`` derived from its range (for example ``decision_refuse`` for 21xx)."""
    return info(code).category


def is_known(code: int) -> bool:
    """True for an active (not retired) catalogue code."""
    return int(code) in CATALOGUE


def describe(code: int) -> str:
    """``"<code>: <text>"`` as used in explanations and log lines."""
    return f"{int(code)}: {text(code)}"


def texts(codes: tuple[int, ...] | list[int]) -> tuple[str, ...]:
    """Texts for a list of codes (skips the padding value 0 used in bundles)."""
    return tuple(text(c) for c in codes if int(c) != 0)


__all__ = [
    "CATALOGUE",
    "CATEGORIES",
    "RETIRED",
    "UNUSED",
    "ReasonCode",
    "ReasonInfo",
    "category",
    "describe",
    "info",
    "is_known",
    "name",
    "text",
    "texts",
]
