"""The signer's executor (spec 7.3): re-validation on fresh chain state, violations first, the decision log
before any outflow, simulation before sending, and the only call builders of the wallet agent's key.

Whitelist (7.3): the signer only builds calls to ``TierVault.pay, escrow, release, refund,
executeApproved, recordDecision, recordDelivery, recordViolation, freeze`` and
``ReputationRegistry.giveFeedback``. Every one of them goes through :class:`CallBuilder`, and every send
goes through :meth:`Executor._send`, which checks the function name and the target address again. There
is no code path for ``setTier``, ``unfreeze``, ``withdraw``, ``setAgent``, mandate or owner functions, or
arbitrary calldata.

Flow for PAY, ESCROW, PAY_APPROVED and RECORD_DECISION (7.3 steps 1-9):

1. read fresh state at one ``latest`` block: vault state and tier config, the mandate and its pinned
   wallet (from chain, never from the request), the payee's effective grade, live wallet, unsecured
   exposure and payment history, the order and, for PAY_APPROVED, the owner's approval;
2. re-run ``validate_invoice`` (V01-V08 here; the signature and expiry become gate C04);
3. ``decide`` on that state; 4. ``classify`` the proposal (with the agent's pass-1 decision for this
   order: a stricter pass 2 after a pass-1 outflow is a state change, 2115, not a limit, duplicate or
   frozen violation; and with the proposed payee's live wallet, so an address in the memo counts as a
   mention for INJECTION_FOLLOWED);
5. record a violation first (kind 11 on the third within 10 minutes);
6. append the decision to the decision log;
7. REFUSE / NEEDS_OWNER_APPROVAL: ``recordDecision`` within the hourly budget (else 2112), ``refused``;
   a decision stricter than the agent's pass 1 adds 2115;
8. otherwise ``eth_call`` the exact call: a revert is ``rejected`` 4102 and nothing is sent;
9. send, wait for the receipt, ``executed``.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from acb.canonical import (
    atomic_write_bytes,
    canonical_bytes,
    chash,
    invoice_hash,
    order_id,
    recover_eip191,
)
from acb.chain.contracts import (
    ZERO_HASH,
    DecisionParams,
    OutflowParams,
    ReputationContract,
    VaultContract,
    bind,
)
from acb.chain.tx import TxError, TxSender, TxSimulationError, TxTimeout
from acb.logbus import EventBus
from acb.policy import PolicyConfig, decide
from acb.policy.chainview import ChainView
from acb.policy.context import (
    build_policy_context,
    build_validation_context,
    catalogue_from_deployments,
)
from acb.policy.engine import OwnerApproval
from acb.policy.invoice import signature_valid, superseded_key, validate_invoice
from acb.policy.mandate import payees_of
from acb.policy.matrix import grade_cap
from acb.policy.severity import classify, classify_tool_call, jsonable, repeated, repeated_violation
from acb.signer.decision_log import DecisionLog
from acb.signer.reports import REPORT_TAG, ReportRejected, plan_report
from acb.signer.state import IN_PROGRESS, RateLimiter, SignerState, valid_request_id
from acb.types import (
    STRICTNESS,
    Decision,
    Deployments,
    ExecuteRequest,
    ExecuteResponse,
    Grade,
    InvoiceRejection,
    OrderState,
    Outcome,
    PolicyDecision,
    TxResult,
    Violation,
    ViolationOut,
)

# ---------------------------------------------------------------------------
# whitelist and call builders
# ---------------------------------------------------------------------------

VAULT_FUNCTIONS = frozenset(
    {
        "pay",
        "escrow",
        "release",
        "refund",
        "executeApproved",
        "recordDecision",
        "recordDelivery",
        "recordViolation",
        "freeze",
    }
)
REPUTATION_FUNCTIONS = frozenset({"giveFeedback"})
WHITELIST: frozenset[tuple[str, str]] = frozenset(
    {("TierVault", f) for f in VAULT_FUNCTIONS} | {("ReputationRegistry", f) for f in REPUTATION_FUNCTIONS}
)
OUTFLOW_KINDS = frozenset({"PAY", "ESCROW", "PAY_APPROVED"})
INVOICE_KINDS = frozenset({"PAY", "ESCROW", "PAY_APPROVED", "RECORD_DECISION"})

CODE_STATE_CHANGED = 2115
CODE_NOT_RECORDED = 2112
CODE_POLICY_STRICTER = 2301
CODE_REJECT_SIMULATION = 4102
CODE_REJECT_RATE = 4103
CODE_NOT_ALLOWED = 4104
CODE_TX_REVERTED = 4200
CODE_TX_TIMEOUT = 4201
CODE_SELF_FREEZE = 3203
REFUND_REASONS = frozenset({2400, 2401})


class NotWhitelisted(RuntimeError):
    """A call outside the signer's whitelist was about to be built or sent."""


class CallBuilder:
    """The only call builders of the signer: one method per whitelisted function (7.3)."""

    BUILDERS = (
        "pay",
        "escrow",
        "release",
        "refund",
        "execute_approved",
        "record_decision",
        "record_delivery",
        "record_violation",
        "freeze",
        "give_feedback",
    )

    def __init__(self, vault: VaultContract, reputation: ReputationContract):
        self.vault = vault
        self.reputation = reputation

    def _vault(self, fn: Any) -> Any:
        return self.check(fn, "TierVault")

    def check(self, fn: Any, contract: str) -> Any:
        """Refuse any function outside the whitelist or aimed at another contract."""
        if (contract, fn.fn_name) not in WHITELIST:
            raise NotWhitelisted(f"{contract}.{fn.fn_name} is not a signer function")
        target = self.vault.address if contract == "TierVault" else self.reputation.address
        if str(fn.address).lower() != target:
            raise NotWhitelisted(f"{contract}.{fn.fn_name} aimed at {fn.address}")
        return fn

    def pay(self, params: OutflowParams) -> Any:
        return self._vault(self.vault.pay(params))

    def escrow(self, params: OutflowParams) -> Any:
        return self._vault(self.vault.escrow(params))

    def release(self, order: str) -> Any:
        return self._vault(self.vault.release(order))

    def refund(self, order: str, reason_code: int) -> Any:
        return self._vault(self.vault.refund(order, reason_code))

    def execute_approved(self, order: str, invoice_hash_: str, decision_hash: str) -> Any:
        return self._vault(self.vault.execute_approved(order, invoice_hash_, decision_hash))

    def record_decision(self, params: DecisionParams) -> Any:
        return self._vault(self.vault.record_decision(params))

    def record_delivery(self, order: str, outcome: int, delivery_hash: str) -> Any:
        return self._vault(self.vault.record_delivery(order, outcome, delivery_hash))

    def record_violation(self, kind: int, severity: int, evidence_hash: str) -> Any:
        return self._vault(self.vault.record_violation(kind, severity, evidence_hash))

    def freeze(self, reason_code: int, evidence_hash: str) -> Any:
        return self._vault(self.vault.freeze(reason_code, evidence_hash))

    def give_feedback(self, subject: int, kind: str, endpoint: str, feedback_uri: str, feedback_hash: str) -> Any:
        fn = self.reputation.give_feedback(subject, -1, 0, REPORT_TAG, kind, endpoint, feedback_uri, feedback_hash)
        return self.check(fn, "ReputationRegistry")


# ---------------------------------------------------------------------------
# one request
# ---------------------------------------------------------------------------


class _Stop(Exception):
    """Ends a request early; the run already holds its status and reasons."""


@dataclass
class _Run:
    req: ExecuteRequest
    started: float
    status: str = "failed"
    reasons: list[int] = field(default_factory=list)
    decision: PolicyDecision | None = None
    txs: list[TxResult] = field(default_factory=list)
    violation: ViolationOut | None = None
    violation_evidence: str | None = None
    invoice_hash: str | None = None
    mandate_id: str | None = None
    order_id: str | None = None
    payee: dict[str, Any] | None = None
    amount: int | None = None
    tier: int | None = None
    detail: str = ""


class Executor:
    """Runs execute requests one at a time (one transaction in flight)."""

    def __init__(
        self,
        *,
        w3: Any,
        dep: Deployments,
        account: Any,
        state_dir: str | os.PathLike[str],
        out_dir: str | os.PathLike[str],
        cfg: PolicyConfig | None = None,
        bus: EventBus | None = None,
        vault_name: str = "wallet",
        evidence_base_uri: str | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ):
        self.w3 = w3
        self.dep = dep
        self.cfg = cfg or PolicyConfig.load()
        self.contracts = bind(w3, dep)
        self.view = ChainView(self.contracts, dep, vault_name)
        self.vault = self.contracts.vault(vault_name)
        self.vault_entry = dep.vaults[vault_name]
        self.calls = CallBuilder(self.vault, self.contracts.reputation)
        self.sender = TxSender(w3, account, receipt_timeout=float(self.cfg.signer.receipt_timeout_s))
        self.address = account.address.lower()
        self.state = SignerState(state_dir)
        self.decisions = DecisionLog(Path(state_dir) / "decisions.jsonl")
        self.out_dir = Path(out_dir)
        self.bus = bus or EventBus("signer", chain_id=dep.chain_id, out_dir=self.out_dir)
        self.rate = RateLimiter(self.cfg.signer.max_outflows_per_minute, 60.0, monotonic)
        self.monotonic = monotonic
        self.wall = wall
        base = evidence_base_uri if evidence_base_uri is not None else os.environ.get("EVIDENCE_BASE_URI", "")
        self.evidence_base_uri = base if not base or base.endswith("/") else base + "/"
        self.usdc = dep.contracts["USDC"].address
        self.catalogue = catalogue_from_deployments(dep)
        self._lock = threading.Lock()
        self.handlers: dict[str, Callable[[_Run], None]] = {
            "PAY": self._invoice_request,
            "ESCROW": self._invoice_request,
            "PAY_APPROVED": self._invoice_request,
            "RECORD_DECISION": self._invoice_request,
            "RELEASE": self._release,
            "REFUND": self._refund,
            "RECORD_DELIVERY": self._record_delivery,
            "REPORT": self._report,
            "SELF_FREEZE": self._self_freeze,
            "REPORT_VIOLATION": self._report_violation,
        }

    # ------------------------------------------------------------------ public

    @property
    def chain_id(self) -> int:
        return int(self.dep.chain_id)

    def health(self) -> dict[str, Any]:
        """``{"ok", "address", "chainId", "vault", "nonce"}`` (7.2); never key material."""
        nonce = int(self.w3.eth.get_transaction_count(self.sender.checksum_address, "pending"))
        return {
            "ok": True,
            "address": self.address,
            "chainId": self.chain_id,
            "vault": self.vault.address,
            "nonce": nonce,
        }

    def stored(self, request_id: str) -> ExecuteResponse | None:
        if not valid_request_id(request_id):
            return None
        found = self.state.stored(request_id)
        return found if isinstance(found, ExecuteResponse) else None

    def execute(self, req: ExecuteRequest) -> ExecuteResponse:
        """Execute one request (idempotent by ``request_id``)."""
        if not valid_request_id(req.request_id):
            raise ValueError("request_id must be a lowercase UUID4 string")
        with self._lock:
            previous = self.state.stored(req.request_id)
            if isinstance(previous, ExecuteResponse):
                return previous
            run = _Run(req=req, started=self.monotonic())
            if previous == IN_PROGRESS:
                # it started before and never finished (crash): never re-send, report it as failed
                run.status, run.detail = "failed", "request was interrupted earlier; not executed again"
                run.reasons.append(CODE_NOT_ALLOWED)
                return self._finish(run, store=False)
            self.state.begin(req.request_id)
            try:
                handler = self.handlers.get(req.kind)
                if handler is None:
                    self._reject(run, CODE_NOT_ALLOWED, "unknown kind")
                handler(run)
            except _Stop:
                pass
            except TxTimeout as exc:
                run.status, run.detail = "failed", f"receipt timeout {exc.tx_hash}"
                run.reasons.append(CODE_TX_TIMEOUT)
            except TxError as exc:
                run.status, run.detail = "failed", f"transaction error: {type(exc).__name__}"
                run.reasons.append(CODE_TX_REVERTED)
            except NotWhitelisted as exc:  # pragma: no cover - a programming error, never reachable by input
                run.status, run.detail = "rejected", str(exc)
                run.reasons.append(CODE_NOT_ALLOWED)
            except Exception as exc:  # noqa: BLE001 - an RPC or other failure ends this request, not the service
                run.status, run.detail = "failed", f"internal error: {type(exc).__name__}"
                self.bus.emit("error", {"requestId": req.request_id, "error": type(exc).__name__})
            return self._finish(run)

    # ------------------------------------------------------------------ plumbing

    def _reject(self, run: _Run, code: int, detail: str, status: str = "rejected") -> None:
        run.status = status
        run.detail = detail
        if code not in run.reasons:
            run.reasons.append(code)
        raise _Stop

    def _send(self, run: _Run, fn: Any) -> TxResult:
        """Send a whitelisted call (simulated and gas-estimated by TxSender) and keep its result."""
        contract = "ReputationRegistry" if fn.fn_name in REPUTATION_FUNCTIONS else "TierVault"
        self.calls.check(fn, contract)
        result = self.sender.send(fn)
        run.txs.append(result)
        self.bus.emit(
            "tx",
            {
                "requestId": run.req.request_id,
                "function": result.function,
                "status": result.status,
                "nonce": result.nonce,
                "gasUsed": result.gas_used,
                "revert": result.revert_error,
            },
            tx_hash=result.hash,
        )
        return result

    def _simulate_or_reject(self, run: _Run, fn: Any) -> None:
        """7.3 step 8: ``eth_call`` the exact call; a revert is ``rejected`` 4102 (not a violation)."""
        contract = "ReputationRegistry" if fn.fn_name in REPUTATION_FUNCTIONS else "TierVault"
        self.calls.check(fn, contract)
        decoded = self.sender.simulate(fn)
        if decoded is not None:
            self.bus.emit("error", {"requestId": run.req.request_id, "simulatedRevert": decoded.text})
            self._reject(run, CODE_REJECT_SIMULATION, f"simulated revert {decoded.text}")

    def _send_checked(self, run: _Run, fn: Any) -> TxResult:
        """Simulate, then send; a mined revert ends the request as ``failed`` 4200."""
        self._simulate_or_reject(run, fn)
        try:
            result = self._send(run, fn)
        except TxSimulationError as exc:  # state moved between the two eth_calls
            self.bus.emit("error", {"requestId": run.req.request_id, "simulatedRevert": exc.revert_error})
            self._reject(run, CODE_REJECT_SIMULATION, f"simulated revert {exc.revert_error}")
        if result.status != 1:
            run.status, run.detail = "failed", f"reverted {result.revert_error}"
            run.reasons.append(CODE_TX_REVERTED)
            raise _Stop
        return result

    def _write_artefact(self, folder: str, obj: dict[str, Any]) -> str:
        """Canonical JSON under ``$ACB_OUT_DIR/<folder>/<chash>.json``; returns the hash."""
        data = canonical_bytes(obj)
        h = chash(obj)
        atomic_write_bytes(self.out_dir / folder / f"{h}.json", data)
        return h

    def _record_violation(self, run: _Run, violation: Violation, now: int) -> None:
        """7.3 step 5: ``recordViolation`` first; the third violation in the window becomes kind 11."""
        rv = self.cfg.signer.repeated_violations
        previous = self.state.violations_in_window(now, rv.window_s)
        if repeated(previous, now, self.cfg):
            violation = repeated_violation(violation, len(previous))
        evidence = {**violation.evidence, "requestId": run.req.request_id, "chainTime": now}
        evidence_hash = self._write_artefact("violations", jsonable(evidence))
        self.state.add_violation(now)
        tx_hash = None
        try:
            result = self._send(
                run, self.calls.record_violation(int(violation.kind), violation.severity, evidence_hash)
            )
            tx_hash = result.hash if result.status == 1 else None
        except TxSimulationError as exc:
            self.bus.emit("error", {"requestId": run.req.request_id, "recordViolation": exc.revert_error})
        run.violation = ViolationOut(kind=violation.kind, severity=violation.severity, tx_hash=tx_hash)
        run.violation_evidence = evidence_hash
        self.bus.emit(
            "violation",
            {
                "requestId": run.req.request_id,
                "kind": violation.kind.name,
                "severity": violation.severity,
                "reason": violation.reason,
                "evidenceHash": evidence_hash,
            },
            tx_hash=tx_hash,
        )

    def _record_decision(self, run: _Run, decision: PolicyDecision, ihash: str) -> None:
        """7.3 step 7: ``recordDecision`` for REFUSE / NEEDS_OWNER_APPROVAL within the hourly budget."""
        if not decision.record_onchain:
            return
        if not self.state.recording_allowed(self.wall(), self.cfg.signer.max_recorded_decisions_per_hour):
            run.reasons.append(CODE_NOT_RECORDED)
            return
        params = DecisionParams(
            order_id=decision.order_id,
            payee_agent_id=decision.payee_agent_id,
            amount=decision.amount,
            decision=int(decision.outcome),
            reason_code=decision.reasons[0],
            invoice_hash=ihash,
            decision_hash=decision.decision_hash,
        )
        try:
            self._send(run, self.calls.record_decision(params))
            self.state.add_recorded(self.wall())
        except TxSimulationError as exc:
            self.bus.emit("error", {"requestId": run.req.request_id, "recordDecision": exc.revert_error})
            run.reasons.append(CODE_NOT_RECORDED)

    # ------------------------------------------------------------------ invoice kinds

    def _invoice_request(self, run: _Run) -> None:
        req = run.req
        if req.proposal is None or req.invoice is None:
            self._reject(run, CODE_NOT_ALLOWED, "proposal and invoice are required")
        if req.quote_request is None:
            self._reject(run, CODE_NOT_ALLOWED, "the quote request record is required")
        qr = req.quote_request
        inv = req.invoice.invoice
        raw = req.invoice.wire_dict()
        ihash = invoice_hash(raw)
        issuer = int(inv.issuer_agent_id)
        run.invoice_hash, run.amount = ihash, int(inv.amount)
        order = order_id(self.chain_id, self.vault.address, issuer, inv.invoice_id)
        run.order_id = order
        if req.order_id is not None and req.order_id != order:
            self._reject(run, CODE_NOT_ALLOWED, "order id is not derived from the invoice")

        # 1. fresh state at one block
        block = self.view.block()
        now = self.view.now(block)
        vault_state = self.view.vault_state(block)
        run.tier = vault_state.tier
        approval: OwnerApproval | None = None
        mandate = None
        if req.kind == "PAY_APPROVED":
            info = self.view.approval(order, block)
            if not info.exists or info.used or now > info.expiry:
                why = "missing" if not info.exists else ("used" if info.used else "expired")
                self._reject(run, CODE_NOT_ALLOWED, f"owner approval {why}")
            approval = OwnerApproval(
                payee_agent_id=info.payee_agent_id,
                expected_payee=info.expected_payee,
                amount=info.amount,
                expiry=info.expiry,
                as_escrow=info.as_escrow,
                used=info.used,
            )
            pinned = info.expected_payee
        else:
            if req.mandate_id is not None and req.mandate_id != qr.mandate_id:
                self._reject(run, CODE_NOT_ALLOWED, "mandate id differs from the quote request's")
            mandate = self.view.mandate(qr.mandate_id, block)  # from chain only
            run.mandate_id = mandate.mandate_id if mandate is not None else qr.mandate_id
            pinned = self.view.mandate_payee(qr.mandate_id, issuer, block) if mandate is not None else None
        live = self.view.live_wallet(issuer, block)

        # the signer's own record of quote-request use replaces the agent's claim; the window is clamped
        cfg = self.cfg
        max_open = cfg.invoice.max_validity_s + cfg.quote.close_after_s
        qr_checked = qr.model_copy(
            update={
                "used_by": self.state.quote_request_used_by(qr.quote_request_id),
                "closes_at": min(int(qr.closes_at), int(qr.created_at) + max_open),
            }
        )
        superseded = {}
        if inv.cap_of and len(inv.items) == 1 and int(inv.items[0].unit_price) > 0:
            cap_b = grade_cap(Grade.B, vault_state.config, cfg.b_cap_bps)
            superseded[superseded_key(issuer, inv.cap_of)] = cap_b // int(inv.items[0].unit_price)
        vctx = build_validation_context(
            now=now,
            chain_id=self.chain_id,
            usdc=self.usdc,
            vault_address=self.vault.address,
            vault_agent_id=self.vault_entry.agent_id,
            catalogue=self.catalogue,
            quote_requests=(qr_checked,),
            pinned_wallets={issuer: pinned} if pinned else {},
            live_wallets={issuer: live},
            superseded=superseded,
        )

        # 2. invoice validation (V01-V08; V09 and expiry are gate C04 of pass 2)
        result = validate_invoice(
            raw, vctx, cfg, check_signature=False, check_expiry=False, check_quote_open=req.kind != "PAY_APPROVED"
        )
        if isinstance(result, InvoiceRejection):
            self._reject(run, result.code, f"{result.rule}: {result.detail}")

        # 3. policy on fresh state
        payee = self.view.payee_view(issuer, max_grade_age=vault_state.max_grade_age, mandate=mandate, block=block)
        if approval is not None:
            payee = payee.model_copy(update={"pinned_wallet": approval.expected_payee})
        run.payee = {"agentId": str(issuer), "name": payee.display_name, "grade": Grade(payee.grade).name}
        denylisted = self.chain_id != 31337 and self.view.usdc_blacklisted(pinned, block)
        ctx = build_policy_context(
            now=now,
            chain_id=self.chain_id,
            usdc=self.usdc,
            vault=vault_state,
            payee=payee,
            invoice=result,
            invoice_sig_valid=signature_valid(raw, vctx),
            mandate=mandate,
            order_state=self.view.order_state(order, block),
            seen_invoice_conflict=self.state.seen_conflict(issuer, inv.invoice_id, ihash),
            denylisted=denylisted,
        )
        decision = decide(req.proposal, ctx, cfg, owner_approval=approval)
        run.decision = decision

        # 4. classify, 5. violation first
        # every active mandate's payees and the proposed payee's live wallet matter only for a payee
        # violation (INJECTION_FOLLOWED); the reads are skipped otherwise
        c08 = next(c for c in decision.checks if c.id == "C08")
        active, proposed_wallet = None, None
        if not c08.passed:
            active = payees_of(self.view.active_mandates(now, block), now)
            proposed_wallet = self._live_wallet_or_none(req.proposal.payee_agent_id, block)
        # pass 1 counts only when it is a decision on this order (2115: chain state changed in between)
        agent = req.agent_decision if req.agent_decision is not None and req.agent_decision.order_id == order else None
        violation = classify(
            req.proposal,
            ctx,
            decision,
            cfg=cfg,
            active_mandate_payees=active,
            agent_decision=agent,
            payee_wallet=proposed_wallet,
        )
        if violation is not None:
            self._record_violation(run, violation, now)

        # 6. decision log before any outflow or record
        self.decisions.append(
            decision_hash=decision.decision_hash,
            request_id=req.request_id,
            kind=req.kind,
            order_id=order,
            outcome=decision.outcome.name,
            reasons=decision.reasons,
        )

        reasons = list(decision.reasons)
        if agent is not None and STRICTNESS[decision.outcome] > STRICTNESS[Decision(agent.outcome)]:
            # pass 2 is stricter than pass 1 on the same proposal: the state changed (A30)
            reasons = [r for r in reasons if r != CODE_POLICY_STRICTER] + [CODE_STATE_CHANGED]
        run.reasons = reasons

        # 7. refusals and approvals are recorded, never executed
        if decision.outcome in (Decision.REFUSE, Decision.NEEDS_OWNER_APPROVAL) or violation is not None:
            self._record_decision(run, decision, ihash)
            if decision.record_onchain:
                self.state.mark_invoice(qr.quote_request_id, issuer, inv.invoice_id, ihash)
            run.status = "refused"
            raise _Stop
        if not decision.executable:
            self._reject(run, CODE_NOT_ALLOWED, "capped preview: the invoice must be re-quoted first")
        if req.kind == "RECORD_DECISION":
            self._reject(run, CODE_NOT_ALLOWED, "the policy allows an outflow; RECORD_DECISION only records refusals")
        if req.kind == "ESCROW" and decision.outcome != Decision.ESCROW:
            self._reject(run, CODE_NOT_ALLOWED, f"ESCROW requested but the decision is {decision.outcome.name}")

        # 8. rate limit, simulate, 9. send
        if not self.rate.allowed():
            self._reject(run, CODE_REJECT_RATE, "outflow rate limit")
        if approval is not None:
            fn = self.calls.execute_approved(order, ihash, decision.decision_hash)
        else:
            params = OutflowParams(
                order_id=order,
                payee_agent_id=issuer,
                amount=decision.amount,
                deadline=int(inv.delivery_deadline),
                mandate_id=mandate.mandate_id if mandate is not None else ZERO_HASH,
                invoice_hash=ihash,
                decision_hash=decision.decision_hash,
                decision=int(decision.outcome),
                reason_code=decision.reasons[0],
            )
            fn = self.calls.escrow(params) if decision.outcome == Decision.ESCROW else self.calls.pay(params)
        self._simulate_or_reject(run, fn)  # 8. a simulated revert is rejected 4102 and uses no rate slot
        self.rate.record()
        result_tx = self._send_checked(run, fn)
        self.decisions.add_tx(decision.decision_hash, result_tx.hash)
        self.state.mark_invoice(qr.quote_request_id, issuer, inv.invoice_id, ihash)
        run.status = "executed"

    def _live_wallet_or_none(self, agent_id: int | None, block: int) -> str | None:
        """The live wallet of a proposed payee, or None when it has none or the identity read fails."""
        if agent_id is None:
            return None
        try:
            return self.view.live_wallet(int(agent_id), block)
        except Exception:  # noqa: BLE001 - an unknown agentId reverts in the registry: no wallet to match
            return None

    # ------------------------------------------------------------------ other kinds

    def _order(self, run: _Run) -> tuple[str, Any, int, int]:
        if run.req.order_id is None:
            self._reject(run, CODE_NOT_ALLOWED, "order_id is required")
        order = run.req.order_id
        run.order_id = order
        block = self.view.block()
        info = self.view.order(order, block)
        run.amount = info.amount
        run.payee = {"agentId": str(info.payee_agent_id)}
        return order, info, block, self.view.now(block)

    def _verify_delivery(self, run: _Run, order: str, info: Any) -> None:
        d = run.req.delivery
        if d is None:
            self._reject(run, CODE_NOT_ALLOWED, "a signed delivery receipt is required")
        if d.order_id != order or int(d.issuer_agent_id) != int(info.payee_agent_id):
            self._reject(run, CODE_NOT_ALLOWED, "the delivery receipt is for another order or payee")
        try:
            signer = recover_eip191(chash(d.unsigned_wire_dict()), d.signature)
        except ValueError:
            self._reject(run, CODE_NOT_ALLOWED, "bad delivery signature")
        if signer != info.payee:
            self._reject(run, CODE_NOT_ALLOWED, "the delivery receipt is not signed by the payee's pinned wallet")

    def _release(self, run: _Run) -> None:
        order, info, _block, _now = self._order(run)
        if info.state != OrderState.ESCROWED or info.claimed_at == 0:
            self._reject(run, CODE_NOT_ALLOWED, "order is not an escrow with a delivery claim")
        self._verify_delivery(run, order, info)
        if run.req.delivery.payload_hash != info.delivery_hash:
            self._reject(run, CODE_NOT_ALLOWED, "payload hash differs from the on-chain claim")
        self._send_checked(run, self.calls.release(order))
        run.reasons.append(2008)
        run.status = "executed"

    def _refund(self, run: _Run) -> None:
        order, info, _block, now = self._order(run)
        reason = run.req.reason_code
        if info.state != OrderState.ESCROWED or info.claimed_at != 0:
            self._reject(run, CODE_NOT_ALLOWED, "order is not an escrow without a claim")
        if reason not in REFUND_REASONS:
            self._reject(run, CODE_NOT_ALLOWED, "refund reason must be 2400 or 2401")
        if reason == 2400 and not now > info.deadline:
            self._reject(run, CODE_NOT_ALLOWED, "2400 needs the delivery deadline to have passed")
        self._send_checked(run, self.calls.refund(order, reason))
        run.reasons.append(reason)
        run.status = "executed"

    def _record_delivery(self, run: _Run) -> None:
        order, info, _block, now = self._order(run)
        if info.state != OrderState.PAID or info.outcome != Outcome.NONE:
            self._reject(run, CODE_NOT_ALLOWED, "order is not a paid order without an outcome")
        if run.req.delivery is not None:
            self._verify_delivery(run, order, info)
            received = run.req.received_at
            if received is None or received > now or received < info.created_at:
                self._reject(run, CODE_NOT_ALLOWED, "received_at must be the agent's chain-clock receipt time")
            outcome = Outcome.ON_TIME if received <= info.deadline else Outcome.LATE
            delivery_hash = run.req.delivery.payload_hash
            # the provider-signed deliveredAt is logged, never used to decide (7.3)
            run.detail = f"deliveredAt={int(run.req.delivery.delivered_at)} receivedAt={int(received)}"
        else:
            if not now > info.deadline + self.cfg.delivery.grace_s:
                self._reject(run, CODE_NOT_ALLOWED, "FAILED needs the deadline plus grace to have passed")
            outcome, delivery_hash = Outcome.FAILED, ZERO_HASH
        self._send_checked(run, self.calls.record_delivery(order, int(outcome), delivery_hash))
        run.detail = f"{outcome.name} {run.detail}".strip()
        run.status = "executed"

    def _report(self, run: _Run) -> None:
        req = run.req.report
        if req is None:
            self._reject(run, CODE_NOT_ALLOWED, "report is required")
        block = self.view.block()
        try:
            plan = plan_report(
                req,
                view=self.view,
                block=block,
                chain_id=self.chain_id,
                vault=self.vault.address,
                reporter_agent_id=self.vault_entry.agent_id,
            )
        except ReportRejected as exc:
            self._reject(run, CODE_NOT_ALLOWED, f"report not allowed: {exc}")
        run.order_id = plan.order_id
        run.payee = {"agentId": str(plan.subject_agent_id)}
        key = SignerState.report_key(plan.subject_agent_id, plan.kind, plan.order_id)
        if self.state.report_filed(key):
            self._reject(run, CODE_NOT_ALLOWED, "already reported (one report per subject, kind and order)")
        feedback_hash = self._write_artefact("reports", plan.payload)
        uri = f"{self.evidence_base_uri}reports/{feedback_hash}.json"
        self._send_checked(
            run, self.calls.give_feedback(plan.subject_agent_id, plan.kind, plan.endpoint, uri, feedback_hash)
        )
        self.state.add_report(key, feedback_hash)
        run.detail = f"{plan.kind} {feedback_hash}"
        run.status = "executed"

    def _self_freeze(self, run: _Run) -> None:
        p = run.req.proposal
        if p is None or p.turn != "owner" or p.tool != "freeze_wallet":
            self._reject(run, CODE_NOT_ALLOWED, "self-freeze only from an owner turn (freeze_wallet)")
        evidence = {
            "schema": "acb-self-freeze/1",
            "requestId": run.req.request_id,
            "argsHash": chash(jsonable(p.raw_args)),
        }
        evidence_hash = self._write_artefact("violations", evidence)
        self._send_checked(run, self.calls.freeze(CODE_SELF_FREEZE, evidence_hash))
        run.reasons.append(CODE_SELF_FREEZE)
        run.status = "executed"

    def _report_violation(self, run: _Run) -> None:
        report = run.req.violation_report
        if report is None:
            self._reject(run, CODE_NOT_ALLOWED, "violation_report is required")
        violation = classify_tool_call(report)
        if violation is None:
            self._reject(run, CODE_NOT_ALLOWED, "the tool call is not a violation")
        block = self.view.block()
        self._record_violation(run, violation, self.view.now(block))
        run.status = "executed"

    # ------------------------------------------------------------------ response and log line

    def _finish(self, run: _Run, *, store: bool = True) -> ExecuteResponse:
        req = run.req
        d = run.decision
        record = {
            "requestId": req.request_id,
            "kind": req.kind,
            "status": run.status,
            "detail": run.detail[:200],
            "proposalHash": chash(jsonable(req.proposal.to_wire())) if req.proposal is not None else None,
            "invoiceHash": run.invoice_hash,
            "mandateId": run.mandate_id,
            "agentDecision": (
                {"outcome": Decision(req.agent_decision.outcome).name, "reasons": list(req.agent_decision.reasons)}
                if req.agent_decision is not None
                else None
            ),
            "signerDecision": (
                {"outcome": d.outcome.name, "reasons": list(d.reasons), "decisionHash": d.decision_hash}
                if d is not None
                else None
            ),
            "violation": (
                {
                    "kind": run.violation.kind.name,
                    "severity": run.violation.severity,
                    "evidenceHash": run.violation_evidence,
                }
                if run.violation is not None
                else None
            ),
            "checks": [{"name": c.name, "passed": c.passed, "code": c.code} for c in d.checks] if d is not None else [],
            "tx": [
                {"hash": t.hash, "status": t.status, "nonce": t.nonce, "gasUsed": t.gas_used, "function": t.function}
                for t in run.txs
            ],
            "durationMs": int((self.monotonic() - run.started) * 1000),
        }
        data: dict[str, Any] = {"request": record, "orderId": run.order_id, "status": run.status, "kind": req.kind}
        if run.payee is not None:
            data["payee"] = run.payee
        if run.amount is not None:
            data["amount"] = str(run.amount)
        if d is not None:
            data["outcome"] = d.outcome.name
        if run.tier is not None:
            data["tier"] = run.tier
        event_type = {
            "REPORT": "report",
            "REPORT_VIOLATION": "violation",
            "SELF_FREEZE": "tier",
            "RELEASE": "tx",
            "REFUND": "tx",
            "RECORD_DELIVERY": "tx",
        }.get(req.kind, "decision")
        last_tx = run.txs[-1].hash if run.txs else None
        event = self.bus.emit(event_type, data, reason_codes=run.reasons, tx_hash=last_tx)
        response = ExecuteResponse(
            request_id=req.request_id,
            status=run.status,
            decision=d,
            tx=tuple(run.txs),
            violation=run.violation,
            reason_codes=tuple(run.reasons),
            log_seq=event.seq,
        )
        if store:
            self.state.finish(response)
        return response


__all__ = [
    "INVOICE_KINDS",
    "OUTFLOW_KINDS",
    "REPUTATION_FUNCTIONS",
    "VAULT_FUNCTIONS",
    "WHITELIST",
    "CallBuilder",
    "Executor",
    "NotWhitelisted",
]
