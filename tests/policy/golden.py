"""Generator of ``tests/policy/golden_decisions.csv`` (spec 6.7).

The expected outcome of every row is computed here by hand-written code that follows the text of
spec 5.1 (wallet tier profile), 6.2 (reason formation) and 6.5 (decision table, exposure limit). It does
**not** call ``acb.policy.decide``; ``test_golden.py`` runs ``decide`` on each row and compares.

Rows: every grade x tier x frozen flag x amount boundary (cap - 0.01, cap, cap + 0.01 of the grade's
cap, in multiples of 10,000 base units) x LLM action (preview, PAY, ESCROW, REQUEST_OWNER_APPROVAL,
REFUSE, plus PAY_CAPPED for grade B), plus cap re-quote rows for B and A. The context of every row is
otherwise permissive (large mandate, balance and window, no exposure, no payment history), so only the
matrix, the exposure limit (C14), the frozen gate (C02) and the merge (C18) shape the outcome.

Run ``uv run python tests/policy/golden.py`` to rewrite the CSV after an intended change.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

GOLDEN_PATH = Path(__file__).with_name("golden_decisions.csv")

USDC = 1_000_000
STEP = 10_000

# Spec 5.1, wallet profile: per-tx cap, new-payee cap, pay grades, escrow grades.
WALLET_TIERS = {
    0: (0, 0, set(), set()),
    1: (1 * USDC, USDC // 2, {"A", "B"}, {"A", "B", "C", "NEW", "NONE"}),
    2: (2 * USDC, 1 * USDC, {"A", "B"}, {"A", "B", "C", "NEW", "NONE"}),
    3: (4 * USDC, 1 * USDC, {"A", "B", "NEW"}, {"A", "B", "C", "NEW", "NONE"}),
}
# Spec 6.5 / 13.3
B_CAP_BPS = 5000
EXPOSURE_LIMIT = {"A": 1_000_000, "B": 500_000, "NEW": 500_000, "NONE": 500_000, "C": 0}
SCALE = ["PAY", "PAY_CAPPED", "ESCROW", "NEEDS_OWNER_APPROVAL", "REFUSE"]
ACTION_TO_DECISION = {
    "PAY": "PAY",
    "PAY_CAPPED": "PAY_CAPPED",
    "ESCROW": "ESCROW",
    "REQUEST_OWNER_APPROVAL": "NEEDS_OWNER_APPROVAL",
    "REFUSE": "REFUSE",
}
GRADES = ["A", "B", "C", "NEW", "NONE", "D"]
ACTIONS = ["", "PAY", "ESCROW", "REQUEST_OWNER_APPROVAL", "REFUSE"]
COLUMNS = [
    "case",
    "grade",
    "tier",
    "frozen",
    "unit_price",
    "quantity",
    "amount",
    "cap_of",
    "action",
    "outcome",
    "out_amount",
    "out_quantity",
    "requote_quantity",
    "executable",
    "reasons",
]


def cap_of_grade(grade: str, tier: int) -> int:
    per_tx, new_cap, _, _ = WALLET_TIERS[tier]
    if grade in ("A", "C", "D"):
        return per_tx
    if grade == "B":
        return per_tx * B_CAP_BPS // 10_000
    return new_cap  # NEW, NONE


def boundary_amounts(grade: str, tier: int) -> list[int]:
    cap = cap_of_grade(grade, tier)
    if cap < 110_000:  # tier 0 (all caps 0): the smallest valid invoices
        return [100_000, 110_000, 200_000]
    return [cap - STEP, cap, cap + STEP]


def expected(grade: str, tier: int, frozen: bool, amount: int, unit: int, cap_of: str, action: str) -> dict:
    """The outcome by hand, straight from the spec text."""
    quantity = amount // unit
    requote = None
    triggered: list[int] = []
    if frozen:
        policy, deciding = "REFUSE", 2101  # C02 gate
    else:
        _per_tx, _new_cap, pay, escrow = WALLET_TIERS[tier]
        cap = cap_of_grade(grade, tier)
        if grade == "D":
            policy, deciding = "REFUSE", 2100
        elif tier == 0:
            policy, deciding = "NEEDS_OWNER_APPROVAL", 2200
        elif grade in pay:
            if cap_of:
                policy, deciding = (
                    ("PAY_CAPPED", 2002) if (amount <= cap and grade == "B") else ("NEEDS_OWNER_APPROVAL", 2201)
                )
            elif amount <= cap:
                policy, deciding = "PAY", {"A": 2000, "B": 2001, "NEW": 2005, "NONE": 2005}[grade]
            elif grade == "B" and unit <= cap:
                policy, deciding = "PAY_CAPPED", 2002
                requote = cap // unit
            else:
                policy, deciding = "NEEDS_OWNER_APPROVAL", 2201
        elif grade in escrow:
            if amount <= cap:
                policy, deciding = "ESCROW", (2003 if grade == "C" else 2004)
            else:
                policy, deciding = "NEEDS_OWNER_APPROVAL", 2201
        else:
            policy, deciding = "NEEDS_OWNER_APPROVAL", 2201
        # C14 single-name exposure limit for a direct payment: PAY, or the PAY_CAPPED of a cap re-quote
        # (both are unsecured Paid orders; the capped preview is checked on its re-quote). No prior
        # exposure in these rows.
        direct = policy == "PAY" or (policy == "PAY_CAPPED" and requote is None)
        if direct and amount > EXPOSURE_LIMIT[grade]:
            policy, deciding = "ESCROW", 2009
    final = policy
    merge = None
    if action:
        llm = ACTION_TO_DECISION[action]
        if SCALE.index(policy) > SCALE.index(llm):
            merge = 2301
        elif SCALE.index(llm) > SCALE.index(policy):
            merge = 2302
            final = llm
            if not frozen:
                cap = cap_of_grade(grade, tier)
                escrow_grades = WALLET_TIERS[tier][3]
                # the LLM's stricter choice must itself be allowed by the matrix, else owner approval
                if final == "PAY_CAPPED" and not (cap_of and grade == "B" and amount <= cap):
                    final = "NEEDS_OWNER_APPROVAL"
                if final == "ESCROW" and not (grade in escrow_grades and amount <= cap):
                    final = "NEEDS_OWNER_APPROVAL"
    reasons = [deciding, *triggered] + ([merge] if merge else [])
    preview_capped = final == "PAY_CAPPED" and requote is not None
    out_amount = requote * unit if preview_capped else amount
    out_quantity = requote if preview_capped else quantity
    executable = final in ("PAY", "PAY_CAPPED", "ESCROW") and not preview_capped
    return {
        "outcome": final,
        "out_amount": str(out_amount),
        "out_quantity": str(out_quantity),
        "requote_quantity": "" if requote is None else str(requote),
        "executable": "1" if executable else "0",
        "reasons": ";".join(str(r) for r in reasons[:4]),
    }


def rows() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []

    def add(grade: str, tier: int, frozen: bool, amount: int, unit: int, cap_of: str, action: str) -> None:
        row = {
            "case": f"G{len(out) + 1:04d}",
            "grade": grade,
            "tier": str(tier),
            "frozen": "1" if frozen else "0",
            "unit_price": str(unit),
            "quantity": str(amount // unit),
            "amount": str(amount),
            "cap_of": cap_of,
            "action": action,
        }
        row.update(expected(grade, tier, frozen, amount, unit, cap_of, action))
        out.append(row)

    for grade in GRADES:
        for tier in WALLET_TIERS:
            for frozen in (False, True):
                for amount in boundary_amounts(grade, tier):
                    actions = ACTIONS + (["PAY_CAPPED"] if grade == "B" else [])
                    for action in actions:
                        add(grade, tier, frozen, amount, STEP, "", action)
    # B over its cap with a unit price of 0.20: the re-quote quantity is cap // 0.20 (S7)
    for tier in (1, 2, 3):
        above = (cap_of_grade("B", tier) // 200_000 + 1) * 200_000
        for amount in (above, 5 * 200_000):
            for action in ("", "PAY", "PAY_CAPPED"):
                add("B", tier, False, amount, 200_000, "", action)
    # cap re-quotes (capOf set): PAY_CAPPED for B within its cap, otherwise owner approval
    for grade in ("B", "A"):
        for tier in (1, 2, 3):
            cap_b = cap_of_grade("B", tier)
            for amount in (cap_b - STEP, cap_b, cap_b + STEP):
                for action in ("", "PAY_CAPPED", "PAY", "REFUSE"):
                    add(grade, tier, False, amount, STEP, "inv-orig-0001", action)
    return out


def render(table: list[dict[str, str]]) -> str:
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(table)
    return buf.getvalue()


def write(path: Path = GOLDEN_PATH) -> int:
    table = rows()
    path.write_bytes(render(table).encode("ascii"))
    return len(table)


if __name__ == "__main__":
    print(f"wrote {write()} rows to {GOLDEN_PATH}")
