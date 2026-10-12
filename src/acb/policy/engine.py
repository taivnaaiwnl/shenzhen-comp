"""Stage 2 of the policy: ``decide`` with checks C01-C18 and the exact reason formation of spec 6.2.

``decide(p, ctx, cfg)`` is pure: every chain value and the time are in the frozen
:class:`~acb.types.PolicyContext`. The agent calls it for pass 1 and the signer for pass 2 on fresh
state. ``p=None`` is preview mode (no LLM merge): the agent shows the preview to the LLM and to
MockLLM (``policy_preview.tool_action``).

Checks (every check runs and is recorded in ``checks``):

- C01-C12 are **gates** (pass/fail). Any failure makes the outcome REFUSE; the deciding code is the
  code of the first failing gate in table order.
- C13 is the matrix of 6.5 (:mod:`acb.policy.matrix`). Only if it gives PAY, PAY_CAPPED or ESCROW are
  the limit checks C14-C17 evaluated (C14 only for a direct payment: PAY or the executable PAY_CAPPED of
  a cap re-quote). The outcome is the strictest among C13-C17;
  the deciding code is that check's code (ties: lowest check number). A limit check C14-C17 that did
  not trigger, or was not evaluated, contributes nothing; C13's own code is a reason only when C13
  decides (a "PAY 2000" text would contradict an escalated outcome).
- C18 merges with the LLM's action: the stricter of the two on PAY < PAY_CAPPED < ESCROW <
  NEEDS_OWNER_APPROVAL < REFUSE, adding 2301 when the policy was stricter and 2302 when the LLM was.

``reasons = dedupe([codes of all failing gates in table order] + [deciding code] + [codes of the other
triggered limit checks in table order] + [2301 or 2302])[:4]``.

``decision_hash = chash(decision_core(decision))``; the core holds orderId, invoiceHash, mandateId,
outcome, amount, quantity, payeeAgentId, reasons, checks, policyVersion and configHash (6.1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from acb.canonical import canonical_bytes, chash, order_id
from acb.injection import detect, invoice_fields
from acb.policy import mandate as mandate_mod
from acb.policy.config import PolicyConfig
from acb.policy.matrix import MatrixResult, evaluate_matrix, grade_cap
from acb.policy.provenance import USDC_AMOUNT_PATTERN, amount_matches, payee_matches, usdc_to_units
from acb.types import (
    DECISION_TO_TOOL_ACTION,
    STRICTNESS,
    TIER_FROZEN,
    TOOL_ACTION_TO_DECISION,
    CheckResult,
    Decision,
    Grade,
    OrderState,
    PolicyContext,
    PolicyDecision,
    Proposal,
    ToolAction,
    mask_has,
)

ZERO_HASH = "0x" + "00" * 32

CODE_INFO_POLICY_STRICTER = 2301
CODE_INFO_AGENT_STRICTER = 2302
CODE_OWNER_APPROVED = 2007
CODE_EXPOSURE = 2009

GATES: tuple[tuple[str, str, int], ...] = (
    ("C01", "SCHEMA", 2114),
    ("C02", "FROZEN", 2101),
    ("C03", "CHAIN_TOKEN", 2111),
    ("C04", "INVOICE_AUTH", 2102),
    ("C05", "DUPLICATE", 2107),
    ("C06", "MANDATE_AUTH", 2104),
    ("C07", "PAYEE_IN_MANDATE", 2103),
    ("C08", "PROVENANCE_PAYEE", 2106),
    ("C09", "PROVENANCE_AMOUNT", 2105),
    ("C10", "INJECTION", 2108),
    ("C11", "DENYLIST", 2120),
    ("C12", "RESERVE", 2109),
)
LIMITS: tuple[tuple[str, str], ...] = (
    ("C13", "GRADE"),
    ("C14", "EXPOSURE"),
    ("C15", "TIER_LIMITS"),
    ("C16", "MANDATE_BUDGET"),
    ("C17", "SPIKE"),
)
CHECK_NAMES: dict[str, str] = {
    **{cid: name for cid, name, _ in GATES},
    **dict(LIMITS),
    "C18": "LLM_MERGE",
}
OUTFLOW_DECISIONS = frozenset({Decision.PAY, Decision.PAY_CAPPED, Decision.ESCROW})
DECIDE_TOOL = "decide_invoice"
DECIDE_ARGS = frozenset({"invoice_id", "action", "payee_agent_id", "amount_usdc", "rationale"})


@dataclass(frozen=True)
class OwnerApproval:
    """The vault's ``Approval`` of an order (``getApproval``), for owner-approved execution (PAY_APPROVED).

    With an approval, ``decide`` checks the same gates except the mandate (approved orders have
    mandateId 0, C06) and compares the approval with the invoice instead (C04, C07); C13 gives PAY or
    ESCROW 2007 unless the payee is D, and the limit checks are not evaluated (the owner approved).
    """

    payee_agent_id: int
    expected_payee: str
    amount: int
    expiry: int
    as_escrow: bool
    used: bool


@lru_cache(maxsize=4096)
def _adverse(fields_json: bytes) -> bool:
    """Detector verdict for the canonical JSON of an invoice's string fields (pure, so it is memoised)."""
    return detect(json.loads(fields_json)).adverse


def _dedupe(codes: list[int]) -> tuple[int, ...]:
    out: list[int] = []
    for c in codes:
        if c not in out:
            out.append(c)
    return tuple(out)


def _check(cid: str, passed: bool, code: int | None, outcome: Decision | None, detail: str) -> CheckResult:
    return CheckResult(id=cid, name=CHECK_NAMES[cid], passed=passed, code=code, outcome=outcome, detail=detail)


def _median_doubled(values: tuple[int, ...]) -> int:
    """Twice the median (an integer, so comparisons stay exact for an even count)."""
    s = sorted(int(v) for v in values)
    n = len(s)
    if n % 2:
        return 2 * s[n // 2]
    return s[n // 2 - 1] + s[n // 2]


# ---------------------------------------------------------------------------
# C01
# ---------------------------------------------------------------------------


def _schema_problem(p: Proposal, invoice_id: str | None) -> str | None:
    """None if the proposal is a well-formed ``decide_invoice`` call for this invoice, else why not."""
    if p.turn != "invoice":
        return "decide_invoice is an invoice-turn tool"
    if p.tool != DECIDE_TOOL:
        return f"tool {p.tool!r} is not {DECIDE_TOOL}"
    if p.action is None:
        return "no action"
    if p.payee_agent_id is None or p.amount is None:
        return "payee or amount missing"
    if invoice_id is not None and p.invoice_id != invoice_id:
        return "invoice_id differs from the invoice"
    if p.source == "llm" or p.raw_args:
        args = p.raw_args
        if set(args) != DECIDE_ARGS:
            return "arguments do not match the decide_invoice schema"
        if args.get("action") != p.action.value:
            return "action argument differs"
        if args.get("invoice_id") != p.invoice_id:
            return "invoice_id argument differs"
        payee = args.get("payee_agent_id")
        if isinstance(payee, bool) or not isinstance(payee, int) or payee < 0 or payee != p.payee_agent_id:
            return "payee_agent_id argument is not the restated integer"
        amount = args.get("amount_usdc")
        if not isinstance(amount, str) or not USDC_AMOUNT_PATTERN.fullmatch(amount):
            return "amount_usdc is not a USDC string"
        if usdc_to_units(amount) != p.amount:
            return "amount_usdc differs from the restated amount"
        rationale = args.get("rationale")
        if not isinstance(rationale, str) or len(rationale) > 300:
            return "rationale must be a string of at most 300 characters"
    return None


# ---------------------------------------------------------------------------
# decide
# ---------------------------------------------------------------------------


def decide(
    p: Proposal | None,
    ctx: PolicyContext,
    cfg: PolicyConfig,
    *,
    owner_approval: OwnerApproval | None = None,
) -> PolicyDecision:
    """Decide one invoice (spec 6.2 stage 2). ``p=None`` is preview mode."""
    vi = ctx.invoice
    inv = vi.signed.invoice if vi is not None else None
    wire = vi.signed.invoice.wire_dict() if vi is not None else None
    vault = ctx.vault
    grade = Grade(ctx.payee.grade)
    checks: dict[str, CheckResult] = {}

    issuer = int(inv.issuer_agent_id) if inv is not None else int(ctx.payee.agent_id)
    invoice_amount = int(inv.amount) if inv is not None else 0
    unit_price = int(inv.items[0].unit_price) if inv is not None and len(inv.items) == 1 else None
    quantity = int(inv.items[0].quantity) if inv is not None and len(inv.items) == 1 else None
    if inv is not None and len(inv.items) > 1:
        quantity = sum(int(i.quantity) for i in inv.items)

    # ---- C01 SCHEMA
    if p is None:
        checks["C01"] = _check("C01", True, None, None, "preview: no proposal")
    else:
        problem = _schema_problem(p, inv.invoice_id if inv is not None else None)
        checks["C01"] = _check("C01", problem is None, None if problem is None else 2114, None, problem or "ok")

    # ---- C02 FROZEN
    frozen = bool(vault.frozen) or vault.tier == TIER_FROZEN
    checks["C02"] = _check("C02", not frozen, 2101 if frozen else None, None, "vault frozen" if frozen else "ok")

    # ---- C03 CHAIN_TOKEN
    if inv is None:
        checks["C03"] = _check("C03", True, None, None, "no invoice (see C04)")
    else:
        ok = inv.chain_id == ctx.chain_id and inv.token == ctx.usdc.lower()
        checks["C03"] = _check("C03", ok, None if ok else 2111, None, "ok" if ok else "wrong chain or token")

    # ---- C04 INVOICE_AUTH
    expected_order = None
    if inv is not None:
        expected_order = order_id(ctx.chain_id, vault.address, issuer, inv.invoice_id)
    if inv is None:
        c04 = (False, 2102, "no validated invoice")
    elif not ctx.invoice_sig_valid:
        c04 = (False, 2102, "signature does not recover to the pinned and live wallet")
    elif ctx.order_id != expected_order:
        c04 = (False, 2102, "order id is not derived from this invoice")
    elif int(ctx.payee.agent_id) != issuer:
        c04 = (False, 2102, "payee view is not the invoice issuer")
    elif owner_approval is not None and (
        int(owner_approval.payee_agent_id) != issuer or int(owner_approval.amount) != invoice_amount
    ):
        c04 = (False, 2102, "owner approval does not match the invoice")
    elif ctx.now > inv.expires_at and owner_approval is None:
        c04 = (False, 2117, "invoice expired")
    else:
        c04 = (True, None, "ok")
    checks["C04"] = _check("C04", c04[0], c04[1], None, c04[2])

    # ---- C05 DUPLICATE
    dup_reasons = []
    if ctx.order_state != OrderState.NONE:
        dup_reasons.append(f"order state {OrderState(ctx.order_state).name}")
    if ctx.order_id in ctx.pending_order_ids:
        dup_reasons.append("order pending")
    if ctx.seen_invoice_conflict:
        dup_reasons.append("(issuer, invoiceId) already seen")
    checks["C05"] = _check("C05", not dup_reasons, 2107 if dup_reasons else None, None, "; ".join(dup_reasons) or "ok")

    # ---- C06 MANDATE_AUTH and C07 PAYEE_IN_MANDATE
    pinned: str | None
    if owner_approval is not None:
        checks["C06"] = _check("C06", True, None, None, "owner-approved order: no mandate needed")
        live = ctx.payee.live_wallet
        pinned = owner_approval.expected_payee.lower()
        ok = live is not None and pinned == live.lower()
        checks["C07"] = _check(
            "C07",
            ok,
            None if ok else 2121,
            None,
            "approved wallet equals the live wallet" if ok else "approved wallet differs from the live wallet",
        )
    else:
        qr_mandate = vi.quote_request.mandate_id if vi is not None else None
        ok, code, detail = mandate_mod.check_mandate(
            ctx.mandate,
            now=ctx.now,
            vault_address=vault.address,
            tier=vault.tier,
            recurring_min_tier=cfg.recurring_min_tier,
            expected_mandate_id=qr_mandate,
        )
        checks["C06"] = _check("C06", ok, code, None, detail)
        ok, code, detail, pinned = mandate_mod.check_payee(ctx.mandate, issuer, ctx.payee.live_wallet)
        checks["C07"] = _check("C07", ok, code, None, detail)

    # ---- C08 / C09 provenance (skipped in preview mode)
    if p is None or inv is None:
        checks["C08"] = _check("C08", True, None, None, "preview" if p is None else "no invoice")
        checks["C09"] = _check("C09", True, None, None, "preview" if p is None else "no invoice")
    else:
        if p.payee_agent_id is None:
            checks["C08"] = _check("C08", True, None, None, "no restated payee (see C01)")
        else:
            ok = payee_matches(p.payee_agent_id, issuer)
            checks["C08"] = _check("C08", ok, None if ok else 2106, None, "ok" if ok else "payee is not the issuer")
        if p.amount is None:
            checks["C09"] = _check("C09", True, None, None, "no restated amount (see C01)")
        else:
            ok = amount_matches(p.amount, invoice_amount)
            checks["C09"] = _check(
                "C09", ok, None if ok else 2105, None, "ok" if ok else "amount differs from the invoice"
            )

    # ---- C10 INJECTION (the detector runs again here: pure and deterministic)
    adverse = False
    if wire is not None:
        adverse = bool(vi.detector.adverse) or _adverse(canonical_bytes(invoice_fields(wire)))
    checks["C10"] = _check(
        "C10", not adverse, 2108 if adverse else None, None, "adverse detector hit" if adverse else "ok"
    )

    # ---- C11 DENYLIST
    deny = ctx.denylisted or issuer in cfg.denylist.agent_ids or (pinned or "") in cfg.denylist.addresses
    checks["C11"] = _check("C11", not deny, 2120 if deny else None, None, "denylisted" if deny else "ok")

    # ---- C12 RESERVE
    free_after = int(vault.balance) - int(vault.escrow_locked) - invoice_amount
    reserve_ok = free_after >= int(vault.reserve_floor)
    checks["C12"] = _check(
        "C12",
        reserve_ok,
        None if reserve_ok else 2109,
        None,
        "ok" if reserve_ok else "payment would break the reserve floor",
    )

    failing_gates = [cid for cid, _, _ in GATES if not checks[cid].passed]

    # ---- C13 GRADE (always evaluated, also when a gate failed, for explanations)
    if owner_approval is not None:
        if grade == Grade.D:
            matrix = MatrixResult(Decision.REFUSE, 2100, 0, detail="grade D")
        else:
            outcome = Decision.ESCROW if owner_approval.as_escrow else Decision.PAY
            matrix = MatrixResult(outcome, CODE_OWNER_APPROVED, 0, detail="owner approved")
    else:
        matrix = evaluate_matrix(
            grade,
            int(vault.tier),
            vault.config,
            invoice_amount,
            unit_price,
            inv.cap_of if inv is not None else "",
            cfg.b_cap_bps,
        )
    checks["C13"] = _check("C13", True, matrix.code, matrix.outcome, matrix.detail)

    effective_amount = invoice_amount
    effective_quantity = quantity
    if matrix.is_preview_capped and unit_price is not None:
        effective_quantity = matrix.requote_quantity
        effective_amount = int(matrix.requote_quantity) * unit_price

    # ---- C14-C17 limit checks (only after a PAY / PAY_CAPPED / ESCROW matrix outcome, no failing gate)
    triggered: list[tuple[str, Decision, int]] = []
    evaluate_limits = (
        not failing_gates and matrix.outcome in OUTFLOW_DECISIONS and owner_approval is None and inv is not None
    )
    if evaluate_limits:
        # C14 EXPOSURE: every direct, unsecured payment, i.e. PAY and the executable PAY_CAPPED of a cap
        # re-quote (both create a Paid order); the non-executable capped preview is checked on its re-quote
        direct = matrix.outcome == Decision.PAY or (
            matrix.outcome == Decision.PAY_CAPPED and not matrix.is_preview_capped
        )
        if direct:
            limit = cfg.exposure_limit_for(grade)
            exposure = int(ctx.payee.unsecured_exposure)
            if exposure + effective_amount > limit:
                c14_outcome = (
                    Decision.ESCROW if mask_has(vault.config.escrow_mask, grade) else Decision.NEEDS_OWNER_APPROVAL
                )
                triggered.append(("C14", c14_outcome, CODE_EXPOSURE))
                checks["C14"] = _check(
                    "C14", False, CODE_EXPOSURE, c14_outcome, f"exposure {exposure} + {effective_amount} > {limit}"
                )
            else:
                checks["C14"] = _check("C14", True, None, None, f"exposure {exposure} + {effective_amount} <= {limit}")
        else:
            checks["C14"] = _check("C14", True, None, None, "not evaluated (not a direct payment)")
        # C15 TIER_LIMITS
        cfg_t = vault.config
        over_window = int(vault.window_spent) + effective_amount > int(cfg_t.window_cap)
        over_count = int(vault.window_count) + 1 > int(cfg_t.max_tx)
        if over_window or over_count:
            triggered.append(("C15", Decision.NEEDS_OWNER_APPROVAL, 2202))
            checks["C15"] = _check(
                "C15",
                False,
                2202,
                Decision.NEEDS_OWNER_APPROVAL,
                "window cap" if over_window else "transaction count",
            )
        else:
            checks["C15"] = _check("C15", True, None, None, "within the rolling window")
        # C16 MANDATE_BUDGET
        m = ctx.mandate
        over_budget = m is None or int(m.spent) + effective_amount > int(m.max_total)
        over_per_tx = m is None or effective_amount > int(m.max_per_tx)
        if over_budget or over_per_tx:
            triggered.append(("C16", Decision.NEEDS_OWNER_APPROVAL, 2203))
            checks["C16"] = _check(
                "C16",
                False,
                2203,
                Decision.NEEDS_OWNER_APPROVAL,
                "mandate per-tx cap" if over_per_tx else "mandate total budget",
            )
        else:
            checks["C16"] = _check("C16", True, None, None, "within the mandate budget")
        # C17 SPIKE
        history = tuple(ctx.payee.payment_history)
        if len(history) >= cfg.spike.min_history and 2 * effective_amount > cfg.spike.multiplier * _median_doubled(
            history
        ):
            triggered.append(("C17", Decision.NEEDS_OWNER_APPROVAL, 2204))
            checks["C17"] = _check("C17", False, 2204, Decision.NEEDS_OWNER_APPROVAL, "amount far above the usual size")
        else:
            checks["C17"] = _check("C17", True, None, None, "no spike")
    else:
        why = "not evaluated" if owner_approval is None else "not evaluated (owner approved)"
        for cid in ("C14", "C15", "C16", "C17"):
            checks[cid] = _check(cid, True, None, None, why)

    # ---- outcome before the merge
    if failing_gates:
        policy_outcome = Decision.REFUSE
        deciding_code = int(checks[failing_gates[0]].code)  # type: ignore[arg-type]
        other_limit_codes: list[int] = []
    else:
        policy_outcome, deciding_code = matrix.outcome, matrix.code
        deciding_check = "C13"
        for cid, outcome, code in triggered:  # table order: ties keep the lowest check number
            if STRICTNESS[outcome] > STRICTNESS[policy_outcome]:
                policy_outcome, deciding_code, deciding_check = outcome, code, cid
        other_limit_codes = [code for cid, _, code in triggered if cid != deciding_check]

    # ---- C18 LLM_MERGE
    final = policy_outcome
    merge_code: int | None = None
    if p is None or p.action is None:
        checks["C18"] = _check("C18", True, None, None, "preview: no merge" if p is None else "no LLM action")
    else:
        llm = TOOL_ACTION_TO_DECISION[ToolAction(p.action)]
        if STRICTNESS[policy_outcome] > STRICTNESS[llm]:
            merge_code = CODE_INFO_POLICY_STRICTER
        elif STRICTNESS[llm] > STRICTNESS[policy_outcome]:
            merge_code = CODE_INFO_AGENT_STRICTER
            final = _feasible_stricter(
                llm,
                grade,
                vault.config,
                inv.cap_of if inv is not None else "",
                invoice_amount,
                cfg.b_cap_bps,
                approved=owner_approval is not None,
            )
        checks["C18"] = _check(
            "C18", True, merge_code, final, f"policy {policy_outcome.name}, LLM {llm.name}, final {final.name}"
        )

    failing_codes = [int(checks[cid].code) for cid in failing_gates]  # type: ignore[arg-type]
    reasons = _dedupe(
        failing_codes + [int(deciding_code)] + other_limit_codes + ([merge_code] if merge_code is not None else [])
    )[:4]

    preview_capped = final == Decision.PAY_CAPPED and matrix.is_preview_capped
    executable = final in OUTFLOW_DECISIONS and not preview_capped
    record_onchain = final in (Decision.REFUSE, Decision.NEEDS_OWNER_APPROVAL) and not (
        "C05" in failing_gates or (checks["C04"].code == 2102)
    )
    amount = effective_amount if final == Decision.PAY_CAPPED and matrix.is_preview_capped else invoice_amount

    core_fields = {
        "outcome": final,
        "tool_action": DECISION_TO_TOOL_ACTION[final],
        "executable": executable,
        "amount": amount,
        "quantity": effective_quantity if amount == effective_amount else quantity,
        "requote_quantity": matrix.requote_quantity if matrix.is_preview_capped else None,
        "payee_agent_id": issuer,
        "order_id": ctx.order_id,
        "reasons": reasons,
        "checks": tuple(checks[cid] for cid in sorted(checks)),
        "record_onchain": record_onchain,
        "policy_version": cfg.policy_version,
        "config_hash": cfg.config_hash,
    }
    invoice_hash = vi.signed.invoice_hash if vi is not None else ZERO_HASH
    mandate_id = ctx.mandate.mandate_id if (ctx.mandate is not None and owner_approval is None) else ZERO_HASH
    draft = PolicyDecision(decision_hash=ZERO_HASH, **core_fields)
    return draft.model_copy(update={"decision_hash": decision_hash(draft, invoice_hash, mandate_id)})


def _feasible_stricter(
    llm: Decision, grade: Grade, cfg: Any, cap_of: str, amount: int, b_cap_bps: int, *, approved: bool = False
) -> Decision:
    """The LLM's stricter choice, escalated to owner approval when the matrix would not allow it as an
    outflow: PAY_CAPPED exists only for a cap re-quote of a B payee within its cap; ESCROW needs the
    grade in the escrow mask and the invoice amount within the grade's cap (a B invoice above the B cap
    must not become an escrow of the full amount). An owner-approved order executes exactly as the owner
    approved it, so any other outflow choice goes back to the owner."""
    if approved and llm in OUTFLOW_DECISIONS:
        return Decision.NEEDS_OWNER_APPROVAL
    cap = grade_cap(grade, cfg, b_cap_bps)
    if llm == Decision.PAY_CAPPED and not (cap_of != "" and grade == Grade.B and amount <= cap):
        return Decision.NEEDS_OWNER_APPROVAL
    if llm == Decision.ESCROW and not (mask_has(cfg.escrow_mask, grade) and amount <= cap):
        return Decision.NEEDS_OWNER_APPROVAL
    return llm


# ---------------------------------------------------------------------------
# decision hash
# ---------------------------------------------------------------------------


def decision_core(decision: PolicyDecision, invoice_hash: str, mandate_id: str) -> dict[str, Any]:
    """The hashed core of a decision (6.1), as canonical JSON-ready data."""
    return {
        "orderId": decision.order_id,
        "invoiceHash": invoice_hash,
        "mandateId": mandate_id,
        "outcome": Decision(decision.outcome).name,
        "amount": str(int(decision.amount)),
        "quantity": decision.quantity,
        "payeeAgentId": str(int(decision.payee_agent_id)),
        "reasons": [int(r) for r in decision.reasons],
        "checks": [
            {
                "id": c.id,
                "name": c.name,
                "passed": c.passed,
                "code": c.code,
                "outcome": Decision(c.outcome).name if c.outcome is not None else None,
                "detail": c.detail,
            }
            for c in decision.checks
        ],
        "policyVersion": decision.policy_version,
        "configHash": decision.config_hash,
    }


def decision_hash(decision: PolicyDecision, invoice_hash: str, mandate_id: str) -> str:
    """``chash(decision_core(...))``."""
    return chash(decision_core(decision, invoice_hash, mandate_id))


def proposed_decision(p: Proposal | None) -> Decision | None:
    """The Decision the proposal asks for (8.3 mapping), or None."""
    if p is None or p.action is None:
        return None
    return TOOL_ACTION_TO_DECISION[ToolAction(p.action)]


def cap_for(grade: Grade, ctx: PolicyContext, cfg: PolicyConfig) -> int:
    """The matrix cap of ``grade`` under the context's tier config."""
    return grade_cap(grade, ctx.vault.config, cfg.b_cap_bps)


__all__ = [
    "CHECK_NAMES",
    "CODE_EXPOSURE",
    "CODE_INFO_AGENT_STRICTER",
    "CODE_INFO_POLICY_STRICTER",
    "CODE_OWNER_APPROVED",
    "GATES",
    "LIMITS",
    "OUTFLOW_DECISIONS",
    "OwnerApproval",
    "cap_for",
    "decide",
    "decision_core",
    "decision_hash",
    "proposed_decision",
]
