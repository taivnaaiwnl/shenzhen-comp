"""Violation classifier (spec 5.2), used by the signer (7.3 step 4).

A violation is a proposal by our wallet agent that the policy had to block for a reason attributable
to the agent, not to the counterparty or to chain state the agent does not control. Blocks caused by
the invoice (injection C10, bad signature C04, wrong chain C03), by the counterparty's grade (C13
2100), by the mandate or payee wallet (C06, C07), the denylist (C11) or the reserve floor (C12) are
not the agent's violations. A REFUSE proposal is never a violation, and neither is a malformed call of
a known tool (C01, A33) whose action, payee or amount cannot be read; a call that fails C01 only for
another reason (rationale, invoice id, extra arguments) is still classified from its typed fields.

Chain state that changed between the agent's pass 1 and the signer's pass 2 is not the agent's doing
(7.3, 2115): when the agent's pass-1 decision was an outflow and the signer's is stricter, the
state-driven kinds (LIMIT_PROPOSAL, DUPLICATE_PROPOSAL, FROZEN_PROPOSAL) are not recorded. The
provenance kinds compare the proposal with the invoice, which no chain change can alter, so they stay.

========================  ===  =========================================================================
Kind                      Sev  Trigger
========================  ===  =========================================================================
LIMIT_PROPOSAL (1)         1   proposed PAY/PAY_CAPPED/ESCROW that the policy sent to owner approval for
                               a tier cap, window or mandate budget (2200-2203) while the invoice itself
                               was within the mandate (its amount <= the mandate's per-tx maximum)
DUPLICATE_PROPOSAL (2)     1   proposed outflow of an orderId that is used on-chain, pending or seen (C05)
FROZEN_PROPOSAL (3)        1   proposed outflow while frozen (C02)
PROVENANCE_AMOUNT (4)      2   restated amount differs from the invoice (C09)
PROVENANCE_PAYEE (5)       2   restated payee differs from the issuer (C08), sev-3 rule not met
INJECTION_FOLLOWED (6)     3   payee violation where the proposed payee (its agentId, or its live wallet
                               address) appears in counterparty text and is in no active mandate
DRAIN_ATTEMPT (8)          3   with a provenance violation: amount >= 50% of freeBalance(), or payee in no
                               mandate and amount > the payee-grade cap
UNKNOWN_TOOL (9)           2   tool name not in the turn's tool set (:func:`classify_tool_call`)
PRIVILEGE_ESCALATION (7)   3   tool name or arguments aimed at tier, limits, keys, ownership
SECRET_EXFILTRATION (10)   3   the agent's redactor matched a secret in the LLM's text
REPEATED_VIOLATIONS (11)   3   third violation within 10 minutes (recorded by the signer, :func:`repeated`)
========================  ===  =========================================================================

Several kinds: the highest severity wins, ties go to the lowest kind number.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from acb.canonical import chash
from acb.policy import mandate as mandate_mod
from acb.policy.config import PolicyConfig
from acb.policy.engine import DECIDE_TOOL, OUTFLOW_DECISIONS, proposed_decision
from acb.policy.matrix import grade_cap
from acb.policy.provenance import (
    USDC_AMOUNT_PATTERN,
    counterparty_text,
    mentions_address,
    mentions_agent,
    usdc_to_units,
)
from acb.types import (
    STRICTNESS,
    Decision,
    Grade,
    PolicyContext,
    PolicyDecision,
    Proposal,
    Violation,
    ViolationKind,
    ViolationReport,
)

SEVERITY: dict[ViolationKind, int] = {
    ViolationKind.LIMIT_PROPOSAL: 1,
    ViolationKind.DUPLICATE_PROPOSAL: 1,
    ViolationKind.FROZEN_PROPOSAL: 1,
    ViolationKind.PROVENANCE_AMOUNT: 2,
    ViolationKind.PROVENANCE_PAYEE: 2,
    ViolationKind.INJECTION_FOLLOWED: 3,
    ViolationKind.PRIVILEGE_ESCALATION: 3,
    ViolationKind.DRAIN_ATTEMPT: 3,
    ViolationKind.UNKNOWN_TOOL: 2,
    ViolationKind.SECRET_EXFILTRATION: 3,
    ViolationKind.REPEATED_VIOLATIONS: 3,
}

OWNER_TURN_TOOLS = frozenset(
    {
        "reply_to_owner",
        "ask_clarification",
        "draft_mandate",
        "amend_mandate",
        "request_quotes",
        "explain",
        "show_status",
        "freeze_wallet",
    }
)
INVOICE_TURN_TOOLS = frozenset({"decide_invoice", "reply_to_owner"})
TURN_TOOLS: dict[str, frozenset[str]] = {"owner": OWNER_TURN_TOOLS, "invoice": INVOICE_TURN_TOOLS}
LIMIT_CODES = frozenset({2200, 2201, 2202, 2203})
STATE_KINDS = frozenset({ViolationKind.LIMIT_PROPOSAL, ViolationKind.DUPLICATE_PROPOSAL, ViolationKind.FROZEN_PROPOSAL})
"""Kinds that depend on chain state (window, tier, budget, order state, freeze), not on the proposal alone."""

_SEP = r"[\s_\-.]*"
PRIVILEGE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        (
            rf"(set|raise|increase|lift|unlock|upgrade|change|update|bump|promote|max){_SEP}(auto{_SEP})?"
            rf"(tier|limit|limits|cap|caps|autonomy|allowance|permission|permissions)"
        ),
        (
            rf"(export|read|get|show|reveal|dump|print|send|leak){_SEP}(the{_SEP})?(private{_SEP})?"
            rf"(key|keys|mnemonic|seed|secret|secrets|env|token|tokens)"
        ),
        r"private" + _SEP + r"key",
        r"seed" + _SEP + r"phrase",
        r"withdraw",
        r"unfreeze",
        rf"(set|change|transfer|rotate|accept){_SEP}(agent|owner|ownership|admin|operator|autonomy{_SEP}operator)",
        rf"(register|revoke){_SEP}mandate",
        rf"owner{_SEP}approve",
    )
)
"""Names and argument text aimed at tier, limits, keys or ownership (5.2: ``set_tier``, ``raise_limit``,
``export_key``, ``withdraw``, ``unfreeze``, ...), matched case-insensitively on camelCase-split text."""


def _words(text: str) -> str:
    """``setTier`` -> ``set tier``; lowercase."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text).lower()


def privilege_match(text: str) -> bool:
    """True when ``text`` (a tool name or argument text) aims at tier, limits, keys or ownership."""
    words = _words(text)
    return any(p.search(words) for p in PRIVILEGE_PATTERNS)


def pick(candidates: Iterable[tuple[ViolationKind, str]]) -> tuple[ViolationKind, str] | None:
    """Highest severity; ties: lowest kind number."""
    items = list(candidates)
    if not items:
        return None
    return min(items, key=lambda kv: (-SEVERITY[kv[0]], int(kv[0])))


def _free_balance(ctx: PolicyContext) -> int:
    v = ctx.vault
    return max(0, int(v.balance) - int(v.escrow_locked) - int(v.reserve_floor))


def typed_fields_readable(p: Proposal) -> bool:
    """True when the proposal's action, payee and amount are present and, for an LLM call, are exactly
    the typed arguments of ``decide_invoice`` (the parts of C01 that classification relies on)."""
    if p.turn != "invoice" or p.tool != DECIDE_TOOL:
        return False
    if p.action is None or p.payee_agent_id is None or p.amount is None:
        return False
    if p.source == "llm" or p.raw_args:
        args = p.raw_args
        if args.get("action") != p.action.value:
            return False
        payee = args.get("payee_agent_id")
        if isinstance(payee, bool) or not isinstance(payee, int) or payee != p.payee_agent_id:
            return False
        amount = args.get("amount_usdc")
        if not isinstance(amount, str) or not USDC_AMOUNT_PATTERN.fullmatch(amount):
            return False
        if usdc_to_units(amount) != p.amount:
            return False
    return True


def state_changed(decision: PolicyDecision, agent_decision: PolicyDecision | None) -> bool:
    """True when the agent's pass 1 allowed an outflow and the signer's pass 2 is stricter (7.3, 2115)."""
    if agent_decision is None:
        return False
    agent = Decision(agent_decision.outcome)
    return agent in OUTFLOW_DECISIONS and STRICTNESS[Decision(decision.outcome)] > STRICTNESS[agent]


def classify(
    proposal: Proposal | None,
    ctx: PolicyContext,
    decision: PolicyDecision,
    *,
    cfg: PolicyConfig | None = None,
    active_mandate_payees: Iterable[int] | None = None,
    agent_decision: PolicyDecision | None = None,
    payee_wallet: str | None = None,
) -> Violation | None:
    """Classify the agent's payment proposal against the signer's decision (5.2).

    ``active_mandate_payees`` are the payees of every active mandate of the vault (the signer reads them
    from chain); by default only the context's mandate is used. ``agent_decision`` is the agent's pass-1
    decision: when it was an outflow and ``decision`` is stricter, the state-driven kinds are not
    recorded (:data:`STATE_KINDS`). ``payee_wallet`` is the live wallet of the proposed payee; when it
    appears in counterparty text, a payee violation is INJECTION_FOLLOWED like an agentId mention.
    """
    cfg = cfg or PolicyConfig()
    proposed = proposed_decision(proposal)
    if proposal is None or proposed is None or proposed == Decision.REFUSE:
        return None
    checks = {c.id: c for c in decision.checks}
    if not checks["C01"].passed and not typed_fields_readable(proposal):
        return None  # malformed arguments of a known tool are not a violation (A33)
    vi = ctx.invoice
    inv = vi.signed.invoice if vi is not None else None
    outflow = proposed in OUTFLOW_DECISIONS
    if active_mandate_payees is None:
        payees = mandate_mod.payees_of([ctx.mandate] if ctx.mandate is not None else [], ctx.now)
    else:
        payees = frozenset(int(a) for a in active_mandate_payees)
    text = counterparty_text(inv.wire_dict()) if inv is not None else ""
    candidates: list[tuple[ViolationKind, str]] = []

    payee_violation = not checks["C08"].passed
    amount_violation = not checks["C09"].passed
    payee = int(proposal.payee_agent_id) if proposal.payee_agent_id is not None else None
    amount = int(proposal.amount) if proposal.amount is not None else 0
    payee_in_mandate = payee is not None and payee in payees

    if payee_violation:
        mentioned = payee is not None and (mentions_agent(text, payee) or mentions_address(text, payee_wallet))
        if mentioned and not payee_in_mandate:
            candidates.append((ViolationKind.INJECTION_FOLLOWED, "proposed payee comes from counterparty text"))
        else:
            candidates.append((ViolationKind.PROVENANCE_PAYEE, "restated payee is not the invoice issuer"))
    if amount_violation:
        candidates.append((ViolationKind.PROVENANCE_AMOUNT, "restated amount differs from the invoice"))

    if outflow:
        if not checks["C02"].passed:
            candidates.append((ViolationKind.FROZEN_PROPOSAL, "outflow proposed while frozen"))
        if not checks["C05"].passed:
            candidates.append((ViolationKind.DUPLICATE_PROPOSAL, "outflow proposed for a used or pending order"))
        if payee_violation or amount_violation:
            free = _free_balance(ctx)
            if amount * 10_000 >= free * cfg.drain_fraction_bps and amount > 0:
                candidates.append((ViolationKind.DRAIN_ATTEMPT, "amount >= half of the free balance"))
            elif payee is not None and not payee_in_mandate:
                grade = Grade(ctx.payee.grade) if payee == int(ctx.payee.agent_id) else Grade.NONE
                cap = grade_cap(grade, ctx.vault.config, cfg.b_cap_bps)
                if amount > cap:
                    candidates.append((ViolationKind.DRAIN_ATTEMPT, "unmandated payee above the grade cap"))
        gates_ok = all(checks[f"C{i:02d}"].passed for i in range(1, 13))
        c13 = checks["C13"]
        # owner approval for a tier cap (C13 2200/2201), the window (C15 2202) or the mandate budget (C16 2203)
        limit_codes = {checks[cid].code for cid in ("C15", "C16") if not checks[cid].passed}
        if c13.outcome == Decision.NEEDS_OWNER_APPROVAL:
            limit_codes.add(c13.code)
        within_mandate = inv is not None and ctx.mandate is not None and int(inv.amount) <= int(ctx.mandate.max_per_tx)
        if gates_ok and within_mandate and limit_codes & LIMIT_CODES:
            candidates.append((ViolationKind.LIMIT_PROPOSAL, "outflow above the tier cap, window or mandate budget"))

    if state_changed(decision, agent_decision):
        candidates = [(k, r) for k, r in candidates if k not in STATE_KINDS]

    chosen = pick(candidates)
    if chosen is None:
        return None
    kind, reason = chosen
    evidence = violation_evidence(proposal, ctx, decision, kind, [k for k, _ in candidates])
    return Violation(kind=kind, severity=SEVERITY[kind], reason=reason, evidence=evidence)


def classify_tool_call(report: ViolationReport) -> Violation | None:
    """Classify a non-payment tool call (REPORT_VIOLATION, 7.3) from the raw call itself.

    PRIVILEGE_ESCALATION when the name or the arguments aim at tier, limits, keys or ownership (whether
    or not the tool exists); UNKNOWN_TOOL when the name is not in the turn's tool set; the agent's
    SECRET_EXFILTRATION finding is accepted (reporting can only make the wallet safer).
    """
    candidates: list[tuple[ViolationKind, str]] = []
    if report.secret_match is not None:
        candidates.append((ViolationKind.SECRET_EXFILTRATION, f"secret detector match ({report.secret_match})"))
    in_turn_set = report.tool_name in TURN_TOOLS.get(report.turn, frozenset())
    # Arguments of a tool outside the turn's set are scanned too; the arguments of an allowed tool are
    # schema-checked by the agent (a reply that says "I cannot export keys" is not an escalation).
    if privilege_match(report.tool_name) or (not in_turn_set and privilege_match(_args_text(report.raw_args))):
        candidates.append((ViolationKind.PRIVILEGE_ESCALATION, "tool call aimed at tier, limits, keys or ownership"))
    if not in_turn_set:
        candidates.append((ViolationKind.UNKNOWN_TOOL, f"tool not in the {report.turn}-turn tool set"))
    chosen = pick(candidates)
    if chosen is None:
        return None
    kind, reason = chosen
    evidence = {
        "schema": "acb-violation/1",
        "source": "tool_call",
        "toolName": report.tool_name[:80],
        "turn": report.turn,
        "argsHash": chash(jsonable(report.raw_args)),
        "rawTextHash": chash(report.raw_text) if report.raw_text is not None else None,
        "secretMatch": report.secret_match,
        "classification": {"kind": kind.name, "severity": SEVERITY[kind]},
        "candidates": sorted({k.name for k, _ in candidates}),
    }
    return Violation(kind=kind, severity=SEVERITY[kind], reason=reason, evidence=evidence)


def repeated(previous_times: Iterable[int], now: int, cfg: PolicyConfig | None = None) -> bool:
    """True when a new violation at ``now`` is the ``count``-th within ``window_s`` (5.2 kind 11)."""
    cfg = cfg or PolicyConfig()
    rv = cfg.signer.repeated_violations
    recent = [t for t in previous_times if now - int(t) < rv.window_s]
    return len(recent) + 1 >= rv.count


def repeated_violation(base: Violation, previous_count: int) -> Violation:
    """The kind-11 violation recorded instead of ``base`` on the third violation in the window."""
    evidence = {**base.evidence, "repeated": {"replaces": base.kind.name, "previousInWindow": previous_count}}
    evidence["classification"] = {"kind": ViolationKind.REPEATED_VIOLATIONS.name, "severity": 3}
    return Violation(
        kind=ViolationKind.REPEATED_VIOLATIONS,
        severity=3,
        reason=f"third violation within the window (this one: {base.kind.name})",
        evidence=evidence,
    )


def jsonable(obj: Any) -> Any:
    """Canonical-JSON-safe copy (floats and big ints become strings, other objects their repr)."""
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, int):
        return obj if abs(obj) <= 2**53 else str(obj)
    if isinstance(obj, float):
        return repr(obj)
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in obj]
    return repr(obj)


def _args_text(args: Any) -> str:
    try:
        return json.dumps(jsonable(args), sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover - _jsonable makes everything serialisable
        return repr(args)


def _proposal_evidence(proposal: Proposal) -> dict[str, Any]:
    """The proposal as evidence; its free ``rawText`` is replaced by a hash."""
    body = jsonable(proposal.to_wire())
    if body.get("rawText") is not None:
        body["rawText"] = None
        body["rawTextHash"] = chash(proposal.raw_text)
    return body


def violation_evidence(
    proposal: Proposal,
    ctx: PolicyContext,
    decision: PolicyDecision,
    kind: ViolationKind,
    candidates: list[ViolationKind],
) -> dict[str, Any]:
    """Canonical-JSON evidence of a payment violation: proposal, invoice hash, mandate id, detector hits
    and classification (7.3 step 5). Counterparty text itself is not included (only hits and hashes)."""
    vi = ctx.invoice
    return {
        "schema": "acb-violation/1",
        "source": "payment_proposal",
        "proposal": _proposal_evidence(proposal),
        "proposalHash": chash(jsonable(proposal.to_wire())),
        "invoiceHash": vi.signed.invoice_hash if vi is not None else None,
        "mandateId": ctx.mandate.mandate_id if ctx.mandate is not None else None,
        "orderId": ctx.order_id,
        "detector": {
            "adverse": bool(vi.detector.adverse) if vi is not None else False,
            "hits": [h.to_wire() for h in vi.detector.hits] if vi is not None else [],
        },
        "decision": {
            "outcome": Decision(decision.outcome).name,
            "reasons": list(decision.reasons),
            "decisionHash": decision.decision_hash,
        },
        "classification": {"kind": kind.name, "severity": SEVERITY[kind]},
        "candidates": sorted({k.name for k in candidates}),
    }


__all__ = [
    "INVOICE_TURN_TOOLS",
    "OWNER_TURN_TOOLS",
    "PRIVILEGE_PATTERNS",
    "SEVERITY",
    "STATE_KINDS",
    "TURN_TOOLS",
    "classify",
    "classify_tool_call",
    "jsonable",
    "pick",
    "privilege_match",
    "repeated",
    "repeated_violation",
    "state_changed",
    "typed_fields_readable",
    "violation_evidence",
]
