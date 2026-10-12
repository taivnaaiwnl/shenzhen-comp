"""The decision table of spec 6.5 (check C13), generic over the tier's on-chain masks.

The matrix reads the **same** pay and escrow grade masks, per-transaction cap and new-payee cap that
the vault enforces, so a PAY or ESCROW outcome is always accepted by the vault (parity test,
``tests/policy/test_vault_parity.py``). The B-grade cap (``b_cap_bps`` of the per-transaction cap) is a
policy-only rule and is stricter than the vault.

::

    if g == D:                         REFUSE 2100
    elif tier == 0:                    NEEDS_OWNER_APPROVAL 2200
    elif g in cfg.pay_mask:
        if invoice.capOf != "":        a <= cap(g) and g == B -> PAY_CAPPED 2002; else NEEDS_OWNER_APPROVAL 2201
        elif a <= cap(g):              PAY 2000 (A) / 2001 (B) / 2005 (NEW, NONE)
        elif g == B and u <= cap(g):   PAY_CAPPED preview 2002, q' = cap(g) // u, not executable (re-quote)
        else:                          NEEDS_OWNER_APPROVAL 2201
    elif g in cfg.escrow_mask:
        a <= cap(g) -> ESCROW 2003 (C) / 2004 (NEW, NONE, or A/B where pay is not allowed); else NEEDS_OWNER_APPROVAL 2201
    else:                              NEEDS_OWNER_APPROVAL 2201

A frozen vault (tier 255) never reaches the matrix: gate C02 refuses first. If it does (preview of a
hand-built context), its all-zero config sends everything to owner approval.
"""

from __future__ import annotations

from dataclasses import dataclass

from acb.types import TIER_FROZEN, Decision, Grade, TierConfig, mask_has

PAY_CODES: dict[Grade, int] = {Grade.A: 2000, Grade.B: 2001, Grade.NEW: 2005, Grade.NONE: 2005}
ESCROW_CODE_C = 2003
ESCROW_CODE_OTHER = 2004
PAY_CAPPED_CODE = 2002
REFUSE_D_CODE = 2100
APPROVAL_TIER0_CODE = 2200
APPROVAL_OVER_CAP_CODE = 2201


@dataclass(frozen=True)
class MatrixResult:
    """Outcome of C13. ``requote_quantity`` (q') is set only for the non-executable PAY_CAPPED preview."""

    outcome: Decision
    code: int
    cap: int
    requote_quantity: int | None = None
    executable: bool = True
    detail: str = ""

    @property
    def is_preview_capped(self) -> bool:
        return self.outcome == Decision.PAY_CAPPED and self.requote_quantity is not None


def grade_cap(grade: Grade, cfg: TierConfig, b_cap_bps: int) -> int:
    """Per-grade cap: A and C ``perTx``; B ``perTx * b_cap_bps // 10000``; NEW and NONE ``newPayeeCap``; D 0."""
    grade = Grade(grade)
    if grade in (Grade.A, Grade.C):
        return int(cfg.per_tx)
    if grade == Grade.B:
        return int(cfg.per_tx) * int(b_cap_bps) // 10_000
    if grade in (Grade.NEW, Grade.NONE):
        return int(cfg.new_payee_cap)
    return 0


def evaluate_matrix(
    grade: Grade,
    tier: int,
    cfg: TierConfig,
    amount: int,
    unit_price: int | None,
    cap_of: str,
    b_cap_bps: int,
) -> MatrixResult:
    """Spec 6.5 for one invoice. ``unit_price`` is the single item's unit price (None for several items,
    which can never be re-quoted)."""
    g = Grade(grade)
    cap = grade_cap(g, cfg, b_cap_bps)
    if g == Grade.D:
        return MatrixResult(Decision.REFUSE, REFUSE_D_CODE, cap, detail="grade D")
    if tier == 0:
        return MatrixResult(Decision.NEEDS_OWNER_APPROVAL, APPROVAL_TIER0_CODE, cap, detail="tier 0: maker-checker")
    if tier == TIER_FROZEN:
        return MatrixResult(Decision.NEEDS_OWNER_APPROVAL, APPROVAL_OVER_CAP_CODE, cap, detail="frozen: no tier config")
    if mask_has(cfg.pay_mask, g):
        if cap_of != "":
            if amount <= cap and g == Grade.B:
                return MatrixResult(Decision.PAY_CAPPED, PAY_CAPPED_CODE, cap, detail=f"cap re-quote within {cap}")
            return MatrixResult(
                Decision.NEEDS_OWNER_APPROVAL, APPROVAL_OVER_CAP_CODE, cap, detail="cap re-quote not payable"
            )
        if amount <= cap:
            return MatrixResult(Decision.PAY, PAY_CODES[g], cap, detail=f"pay grade {g.name} within {cap}")
        if g == Grade.B and unit_price is not None and 0 < unit_price <= cap:
            q = cap // unit_price
            return MatrixResult(
                Decision.PAY_CAPPED,
                PAY_CAPPED_CODE,
                cap,
                requote_quantity=q,
                executable=False,
                detail=f"preview: re-quote {q} units",
            )
        return MatrixResult(Decision.NEEDS_OWNER_APPROVAL, APPROVAL_OVER_CAP_CODE, cap, detail=f"above cap {cap}")
    if mask_has(cfg.escrow_mask, g):
        if amount <= cap:
            code = ESCROW_CODE_C if g == Grade.C else ESCROW_CODE_OTHER
            return MatrixResult(Decision.ESCROW, code, cap, detail=f"escrow grade {g.name} within {cap}")
        return MatrixResult(Decision.NEEDS_OWNER_APPROVAL, APPROVAL_OVER_CAP_CODE, cap, detail=f"above cap {cap}")
    return MatrixResult(Decision.NEEDS_OWNER_APPROVAL, APPROVAL_OVER_CAP_CODE, cap, detail="grade not in any mask")


__all__ = [
    "APPROVAL_OVER_CAP_CODE",
    "APPROVAL_TIER0_CODE",
    "ESCROW_CODE_C",
    "ESCROW_CODE_OTHER",
    "PAY_CAPPED_CODE",
    "PAY_CODES",
    "REFUSE_D_CODE",
    "MatrixResult",
    "evaluate_matrix",
    "grade_cap",
]
