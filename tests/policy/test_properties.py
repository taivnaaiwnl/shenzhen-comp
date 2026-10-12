"""Property tests of ``decide`` (spec 6.7): seeded ``random.Random(2026)``, 5,000 cases.

- the outcome is never less strict than the LLM's choice;
- never an outflow to a D payee;
- never above the caps the vault enforces, the policy's grade caps, the exposure limit, the window,
  the mandate or the reserve floor;
- the same input gives the same ``decision_hash``.
"""

from __future__ import annotations

import random
from functools import cache

from policy_factory import CFG, U, context, invoice, mandate, proposal, quote_request, vault_state

from acb.policy import decide
from acb.policy.matrix import grade_cap
from acb.types import STRICTNESS, TOOL_ACTION_TO_DECISION, Decision, Grade, ToolAction, mask_has

CASES = 5_000
OUTFLOWS = {Decision.PAY, Decision.PAY_CAPPED, Decision.ESCROW}


@cache
def _invoice(quantity: int, cap_of: str):
    qr = quote_request("test", sku="unit", quantity=quantity, cap_of=cap_of)
    raw = invoice("test", qr=qr, invoice_id=f"inv-p-{quantity}-{cap_of or 'x'}", sign=False)
    base = context(
        raw,
        qr,
        check_signature=False,
        superseded={(20, cap_of): quantity} if cap_of else None,
    )
    return raw, base


def _case(rng: random.Random):
    grade = rng.choice(list(Grade))
    tier = rng.choice([0, 1, 2, 3])
    frozen = rng.random() < 0.08
    quantity = rng.randint(10, 500)  # 0.10 .. 5.00 USDC at 0.01 per unit
    cap_of = "inv-orig-0001" if rng.random() < 0.1 else ""
    raw, base = _invoice(quantity, cap_of)
    vault = vault_state(
        tier=tier,
        frozen=frozen,
        balance=rng.randint(0, 20) * U // 2,
        escrow_locked=rng.choice([0, 0, U // 2]),
        window_spent=rng.randint(0, 20) * U // 4,
        window_count=rng.randint(0, 33),
    )
    m = mandate(max_per_tx=rng.choice([U // 2, U, 3 * U]), max_total=4 * U, spent=rng.randint(0, 16) * U // 4)
    payee = base.payee.model_copy(
        update={
            "grade": grade,
            "unsecured_exposure": rng.choice([0, 0, U // 4, U]),
            "payment_history": tuple(rng.randint(10, 100) * 10_000 for _ in range(rng.randint(0, 5))),
        }
    )
    ctx = base.model_copy(update={"vault": vault, "mandate": m, "payee": payee})
    action = rng.choice([None, *ToolAction])
    p = None
    if action is not None:
        p = proposal(raw, action)
        if rng.random() < 0.1:  # a restatement that differs from the invoice
            p = proposal(raw, action, amount=int(raw["amount"]) + 10_000)
    return grade, ctx, p


def test_decide_properties_5000_seeded_cases():
    rng = random.Random(2026)
    outcomes: dict[Decision, int] = {}
    for _ in range(CASES):
        grade, ctx, p = _case(rng)
        d = decide(p, ctx, CFG)
        outcomes[d.outcome] = outcomes.get(d.outcome, 0) + 1
        if p is not None:
            assert STRICTNESS[d.outcome] >= STRICTNESS[TOOL_ACTION_TO_DECISION[p.action]]
        if d.outcome in OUTFLOWS:
            assert grade != Grade.D
        if d.executable:
            assert d.outcome in OUTFLOWS
            cfg = ctx.vault.config
            amount = d.amount
            assert not ctx.vault.frozen and ctx.vault.tier in (1, 2, 3)
            # what the vault enforces
            assert amount <= cfg.per_tx
            mask = cfg.escrow_mask if d.outcome == Decision.ESCROW else cfg.pay_mask
            assert mask_has(mask, grade)
            if grade in (Grade.NEW, Grade.NONE):
                assert amount <= cfg.new_payee_cap
            assert ctx.vault.window_spent + amount <= cfg.window_cap
            assert ctx.vault.window_count + 1 <= cfg.max_tx
            assert ctx.mandate.spent + amount <= ctx.mandate.max_total and amount <= ctx.mandate.max_per_tx
            assert ctx.vault.balance - ctx.vault.escrow_locked - amount >= ctx.vault.reserve_floor
            # the policy's own caps
            assert amount <= grade_cap(grade, cfg, CFG.b_cap_bps)
            if d.outcome == Decision.PAY:
                assert ctx.payee.unsecured_exposure + amount <= CFG.exposure_limit_for(grade)
            if d.outcome == Decision.PAY_CAPPED:
                assert grade == Grade.B and ctx.invoice.signed.invoice.cap_of != ""
            assert amount == int(ctx.invoice.signed.invoice.amount)
            if p is not None:
                assert p.amount == amount and p.payee_agent_id == d.payee_agent_id
        assert decide(p, ctx, CFG).decision_hash == d.decision_hash
    # the generator reaches every outcome
    assert set(outcomes) == {
        Decision.PAY,
        Decision.PAY_CAPPED,
        Decision.ESCROW,
        Decision.NEEDS_OWNER_APPROVAL,
        Decision.REFUSE,
    }
