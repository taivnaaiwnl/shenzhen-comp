"""Purchase runtime of the wallet agent (spec 2.4, 6.2, 6.5, 8.1, 9.3, 9.5).

For each item of the last ``request_quotes`` (or of the runner's ``/purchase`` call):

1. a single-use quote request bound to mandate, payee, sku and quantity is created and persisted;
2. the provider (pinned key, ``127.0.0.1:<SIM_PORT>/p/<key>/quote``) answers with a signed invoice;
3. **before any LLM sees it** the invoice is checked for duplicates (2107) and validated (V01-V09); a
   rejected invoice is dropped off-chain (2119, no LLM turn, no transaction). A signed unsolicited
   invoice is kept as spam evidence; three from one issuer make code file SPAM;
4. the policy preview (``decide(None, ...)``) runs. When it is the non-executable PAY_CAPPED the invoice
   is closed as ``superseded`` (off-chain, no LLM turn) and code re-quotes ``q'`` units with ``capOf``;
5. an invoice turn: the LLM sees **one** invoice from **one** counterparty (structured fields, grade,
   preview, detector flags) with its memo and descriptions quarantined in DATA blocks, and must call
   ``decide_invoice``. The call is validated (amount strings with ``Decimal``, restated payee and amount
   checked by the policy: echo-and-verify); a tool outside the invoice-turn set is reported to the signer
   (``REPORT_VIOLATION``); malformed arguments become a REFUSE 2114 without a violation (A33);
6. ``decide`` (pass 1) and an ``ExecuteRequest`` to the signer (PAY, ESCROW, or RECORD_DECISION for a
   refusal or an owner approval), with the pass-1 decision attached;
7. delivery: escrow -> wait for the provider's on-chain claim, run the deterministic payload check, ask
   RELEASE; direct pay -> poll until ``deadline + grace``, judge ON_TIME/LATE by the chain-clock receipt
   time, ask RECORD_DELIVERY; no delivery -> REFUND 2400 (escrow) or RECORD_DELIVERY FAILED (pay);
8. reports are filed by code, never by the LLM: INJECTION_ATTEMPT after a recorded REFUSE of an adverse
   invoice, NON_DELIVERY after a FAILED outcome, SPAM after three signed unsolicited invoices, BAD_DATA
   only with the owner's signature. The signer verifies each one.
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from acb.agent.delivery_check import check_delivery
from acb.agent.prompts import invoice_messages
from acb.agent.quarantine import data_message, quarantine
from acb.agent.quotes import deliver_body, quote_body
from acb.agent.signer_client import new_request_id
from acb.agent.tools import parse_call, tool_specs, usdc_text, usdc_units
from acb.canonical import canonical_bytes, chash, invoice_hash, order_id, recover_eip191, recover_invoice_signer
from acb.policy import decide
from acb.policy.context import build_policy_context, build_validation_context, pinned_wallets_for
from acb.policy.engine import OwnerApproval
from acb.policy.invoice import signature_valid, validate_invoice, write_spam_evidence
from acb.types import (
    DECISION_TO_TOOL_ACTION,
    Decision,
    DecisionCard,
    ExecuteRequest,
    ExecuteResponse,
    Grade,
    InvoiceRejection,
    MandateView,
    OrderState,
    Outcome,
    PolicyDecision,
    Proposal,
    QuoteRequest,
    ReportRequest,
    SignedDelivery,
    SignedInvoice,
    ToolAction,
    ValidatedInvoice,
)

if TYPE_CHECKING:
    from acb.agent.wallet import WalletAgent

ZERO_HASH = "0x" + "00" * 32
CODE_FALLBACK = 2300
CODE_INVOICE_REJECTED = 2119
CODE_DUPLICATE = 2107
CODE_INJECTION = 2108
OUTFLOW = {Decision.PAY: "PAY", Decision.PAY_CAPPED: "PAY", Decision.ESCROW: "ESCROW"}
OPEN_ORDER_STATUSES = frozenset({"awaiting_delivery"})
# orders whose signer answer is unknown: reconciled from the signer's request log and the chain
RECONCILE_STATUSES = frozenset({"in_flight", "interrupted"})
# how long (wall clock) an unknown answer is looked up before the order is marked interrupted
UNKNOWN_GIVE_UP_S = 600.0


def refusal_reply(payee_name: str, report_status: str | None) -> str:
    """The owner-facing reply for a refused invoice with injected text (spec 8.6, S15)."""
    text = (
        f"I refused the invoice from {payee_name}: its text contains instructions aimed at the wallet, and I "
        "do not follow instructions from counterparties."
    )
    if report_status == "executed":
        return text + " I reported the sender to the credit bureau (INJECTION_ATTEMPT)."
    if report_status is not None:
        return text + " A report on the sender was requested but not filed; check the signer."
    return text


class SlidingLimiter:
    """At most ``limit`` events per ``window`` seconds per key (monotonic clock)."""

    def __init__(self, limit: int, window: float = 60.0, clock=time.monotonic):
        self.limit = int(limit)
        self.window = float(window)
        self.clock = clock
        self._events: dict[Any, deque[float]] = {}

    def allow(self, key: Any = None) -> bool:
        now = self.clock()
        q = self._events.setdefault(key, deque())
        while q and now - q[0] >= self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


def _seen_key(raw: Any) -> tuple[int, str] | None:
    """``(issuerAgentId, invoiceId)`` of a raw invoice when both are readable."""
    if not isinstance(raw, dict):
        return None
    issuer, inv_id = raw.get("issuerAgentId"), raw.get("invoiceId")
    if not isinstance(issuer, str) or not issuer.isdigit() or len(issuer) > 78 or not isinstance(inv_id, str):
        return None
    if not 1 <= len(inv_id) <= 40:
        return None
    return int(issuer), inv_id


def _safe_hash(raw: Any) -> str | None:
    try:
        return invoice_hash(raw) if isinstance(raw, dict) else None
    except (TypeError, ValueError):
        return None


class Runtime:
    """Invoices, executions, deliveries, approvals and reports of one :class:`WalletAgent`."""

    def __init__(self, agent: WalletAgent):
        self.a = agent
        inbox = agent.cfg.inbox
        self.issuer_limiter = SlidingLimiter(inbox.per_issuer_per_minute)

    # ================================================================== purchase

    def run_purchase(self, mandate_id: str, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Quote-and-decide for the items of the last ``request_quotes`` under ``mandate_id``."""
        a = self.a
        mandate_id = mandate_id.lower()
        with a.lock:
            plan = items if items is not None else a.store.read("purchases").get(mandate_id)
            if not plan:
                return {"status": "error", "error": "no request_quotes items for this mandate"}
            check = self.validate_items(mandate_id, plan)
            if isinstance(check, str):
                return {"status": "error", "error": check}
            block = a.chain.block()
            mandate = a.chain.mandate(mandate_id, block)
            results: list[dict[str, Any]] = []
            for item in check:
                results.extend(self.request_quote(mandate, item["payee_agent_id"], item["sku"], item["quantity"]))
            return {"status": "done", "mandateId": mandate_id, "results": results}

    def validate_items(self, mandate_id: str, items: list[dict[str, Any]]) -> list[dict[str, Any]] | str:
        """Items are built by code from structured fields only: payee in the mandate, sku in its catalogue."""
        a = self.a
        rec = a.store.read("mandates").get(mandate_id.lower())
        if rec is None or rec.get("status") != "active":
            return "mandate is not an active mandate verified by the agent"
        block = a.chain.block()
        mandate = a.chain.mandate(mandate_id, block)
        if mandate is None or not mandate.active or a.chain.now(block) > mandate.valid_until:
            return "mandate is not active on-chain"
        payees = {int(p) for p in mandate.payee_agent_ids}
        out = []
        if not isinstance(items, list) or not 1 <= len(items) <= 6:
            return "between 1 and 6 items"
        for item in items:
            if not isinstance(item, dict):
                return "malformed item"
            try:
                aid = item["payee_agent_id"] if "payee_agent_id" in item else item["payeeAgentId"]
                aid = int(aid)
                sku = str(item["sku"])
                qty = item["quantity"]
            except (KeyError, TypeError, ValueError):
                return "malformed item"
            if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= 1000:
                return "quantity must be 1..1000"
            if aid not in payees:
                return f"agent #{aid} is not a payee of the mandate"
            entry = a.directory.get(aid)
            if entry is None or sku not in entry.skus:
                return f"sku {sku!r} is not in agent #{aid}'s pinned catalogue"
            out.append({"payee_agent_id": aid, "sku": sku, "quantity": qty})
        return out

    def request_quote(
        self, mandate: MandateView | None, issuer: int, sku: str, quantity: int, *, cap_of: str = "", depth: int = 0
    ) -> list[dict[str, Any]]:
        """One quote request to one provider and the handling of its invoice."""
        a = self.a
        if mandate is None:
            return [{"status": "error", "error": "mandate not found on-chain"}]
        entry = a.directory.get(issuer)
        if entry is None:
            return [{"status": "error", "error": f"agent #{issuer} is not in the pinned directory"}]
        now = a.chain.now()
        qr = a.quotes.create(
            mandate_id=mandate.mandate_id, issuer_agent_id=issuer, sku=sku, quantity=quantity, now=now, cap_of=cap_of
        )
        body = quote_body(qr, buyer_agent_id=a.agent_id, buyer_vault=a.vault_address)
        a.bus.emit(
            "info",
            {
                "message": "quote requested",
                "quoteRequestId": qr.quote_request_id,
                "payee": {"agentId": str(issuer), "name": entry.name},
                "sku": sku,
                "quantity": quantity,
                "capOf": cap_of,
                "mandateId": mandate.mandate_id,
            },
        )
        answer = a.provider.quote(entry.key, body)
        if not isinstance(answer, dict) or answer.get("status") == "error":
            a.bus.emit(
                "error", {"message": "quote failed", "quoteRequestId": qr.quote_request_id, "provider": entry.key}
            )
            return [
                {
                    "status": "error",
                    "error": "the provider did not answer with an invoice",
                    "quoteRequestId": qr.quote_request_id,
                }
            ]
        return self.handle_invoice(answer, source="quote", depth=depth)

    # ================================================================== invoices

    def handle_invoice(self, raw: Any, *, source: str, depth: int = 0) -> list[dict[str, Any]]:
        """Validate one invoice and, if it is accepted, run the invoice turn and the execution."""
        a = self.a
        with a.lock:
            block = a.chain.block()
            now = a.chain.now(block)
            key = _seen_key(raw)
            ihash = _safe_hash(raw)
            seen = a.store.read("seenInvoices")
            if key is not None and a.store.seen_key(*key) in seen:
                return [self._dropped(raw, ihash, CODE_DUPLICATE, "DUPLICATE", "invoice already seen", source, block)]

            qrs = a.quotes.open_requests(now)
            issuer = key[0] if key else None
            mandates: dict[str, MandateView] = {}
            for qr in qrs:
                if issuer is not None and int(qr.issuer_agent_id) == issuer and qr.mandate_id not in mandates:
                    view = a.chain.mandate(qr.mandate_id, block)
                    if view is not None:
                        mandates[qr.mandate_id.lower()] = view
            relevant = [q for q in qrs if issuer is not None and int(q.issuer_agent_id) == issuer]
            vctx = build_validation_context(
                now=now,
                chain_id=a.chain.chain_id,
                usdc=a.chain.usdc,
                vault_address=a.vault_address,
                vault_agent_id=a.agent_id,
                catalogue=a.directory.catalogue(),
                quote_requests=qrs,
                pinned_wallets=pinned_wallets_for(relevant, mandates),
                live_wallets={issuer: a.chain.live_wallet(issuer, block)} if issuer is not None else {},
                superseded=a.quotes.superseded(),
            )
            result = validate_invoice(raw, vctx, a.pcfg)
            if isinstance(result, InvoiceRejection):
                return [self._rejected(raw, result, source, block)]

            vi: ValidatedInvoice = result
            si = vi.signed
            inv = si.invoice
            qr = vi.quote_request
            with a.store.edit() as data:
                data["seenInvoices"][a.store.seen_key(int(inv.issuer_agent_id), inv.invoice_id)] = si.invoice_hash
                rec = data["quoteRequests"].get(qr.quote_request_id)
                if rec is not None:
                    rec["usedBy"] = si.invoice_hash
            qr = qr.model_copy(update={"used_by": si.invoice_hash})
            mandate = mandates.get(qr.mandate_id.lower()) or a.chain.mandate(qr.mandate_id, block)

            ctx = self._policy_ctx(vi, mandate, vctx, block, now)
            preview = decide(None, ctx, a.pcfg)
            if preview.outcome == Decision.PAY_CAPPED and not preview.executable and preview.requote_quantity:
                return self._supersede(vi, preview, mandate, source, depth)

            proposal, meta = self._invoice_turn(vi, preview, mandate, ctx, block)
            agent_decision = decide(proposal, ctx, a.pcfg)
            return [self._execute_invoice(vi, qr, mandate, proposal, agent_decision, meta, source)]

    def _policy_ctx(
        self,
        vi: ValidatedInvoice,
        mandate: MandateView | None,
        vctx: Any,
        block: int,
        now: int,
        *,
        exclude_pending: str | None = None,
        approval: OwnerApproval | None = None,
    ):
        a = self.a
        inv = vi.signed.invoice
        issuer = int(inv.issuer_agent_id)
        oid = order_id(a.chain.chain_id, a.vault_address, issuer, inv.invoice_id)
        payee = a.chain.payee_view(issuer, mandate, block)
        if approval is not None:
            payee = payee.model_copy(update={"pinned_wallet": approval.expected_payee})
        return build_policy_context(
            now=now,
            chain_id=a.chain.chain_id,
            usdc=a.chain.usdc,
            vault=a.chain.vault_state(block),
            payee=payee,
            invoice=vi,
            invoice_sig_valid=signature_valid(vi.signed.wire_dict(), vctx),
            mandate=mandate,
            order_state=a.chain.order(oid, block).state,
            pending_order_ids=a.store.pending_order_ids(exclude=exclude_pending),
            seen_invoice_conflict=False,
            denylisted=False,
        )

    # ------------------------------------------------------------------ drops

    def _card_payee(self, issuer: int | None, block: int | None) -> Any:
        """The card's payee: chain reads only for agents of the pinned directory."""
        return self.a.payee_candidate(int(issuer or 0), block)

    def _drop_card(self, raw: Any, code: int, block: int | None) -> DecisionCard:
        a = self.a
        key = _seen_key(raw)
        oid = ZERO_HASH
        if key is not None:
            try:
                oid = order_id(a.chain.chain_id, a.vault_address, key[0], key[1])
            except Exception:  # noqa: BLE001 - an unreadable id gives no order
                oid = ZERO_HASH
        amount = 0
        if isinstance(raw, dict) and isinstance(raw.get("amount"), str) and raw["amount"].isdigit():
            amount = int(raw["amount"]) if len(raw["amount"]) <= 18 else 0
        payee = self._card_payee(key[0] if key else None, block)
        return DecisionCard(
            order_id=oid,
            payee=payee,
            amount=amount,
            outcome=Decision.REFUSE,
            reasons=(code,),
            reason_texts=a.reason_texts((code,)),
            tx_hash=None,
            offchain=True,
        )

    def _dropped(self, raw: Any, ihash: str | None, code: int, rule: str, detail: str, source: str, block: int) -> dict:
        a = self.a
        card = self._drop_card(raw, code, block)
        memo = raw.get("memo") if isinstance(raw, dict) and isinstance(raw.get("memo"), str) else ""
        q = quarantine("dropped-memo", memo, mask=False, max_chars=200)
        a.bus.emit(
            "decision",
            {
                "orderId": card.order_id,
                "invoiceHash": ihash,
                "payee": {
                    "agentId": str(card.payee.agent_id),
                    "name": card.payee.name,
                    "grade": Grade(card.payee.grade).name,
                },
                "amount": str(card.amount),
                "outcome": "DROPPED",
                "rule": rule,
                "detail": detail[:200],
                "source": source,
                "offchain": True,
                "memo": q.text,
                "flagged": q.flagged,
                "detectorRules": list(q.rules),
            },
            reason_codes=[code],
        )
        return {"status": "dropped", "invoiceHash": ihash, "reasons": [code], "rule": rule, "card": card.to_wire()}

    def _rejected(self, raw: Any, rej: InvoiceRejection, source: str, block: int) -> dict:
        result = self._dropped(raw, rej.invoice_hash, rej.code, rej.rule, rej.detail, source, block)
        if rej.keep_as_spam_evidence and isinstance(raw, dict):
            self._keep_spam(raw)
        return result

    def _keep_spam(self, raw: dict[str, Any]) -> None:
        """A signed unsolicited invoice: keep it as evidence; three distinct ones from an issuer -> SPAM."""
        a = self.a
        key = _seen_key(raw)
        if key is None:
            return
        try:
            si = SignedInvoice.from_wire(raw)
        except ValueError:
            return
        write_spam_evidence(a.out_dir, raw)
        issuer = str(key[0])
        with a.store.edit() as data:
            data["seenInvoices"][a.store.seen_key(*key)] = si.invoice_hash
            hashes = data["spam"].setdefault(issuer, [])
            if si.invoice_hash not in hashes:
                hashes.append(si.invoice_hash)
        self.maybe_spam_report(key[0])

    def maybe_spam_report(self, issuer: int) -> None:
        a = self.a
        spam = a.store.read("spam").get(str(int(issuer))) or []
        if len(spam) < a.cfg.spam_report_min or str(int(issuer)) in a.store.read("spamReported"):
            return
        invoices = []
        folder = Path(a.out_dir) / "agent" / "spam_evidence" / str(int(issuer))
        for h in spam:
            path = folder / f"{h}.json"
            if path.is_file():
                import json

                try:
                    invoices.append(SignedInvoice.from_wire(json.loads(path.read_text(encoding="utf-8"))))
                except ValueError:
                    continue
        if len(invoices) < a.cfg.spam_report_min:
            return
        resp = self.file_report("SPAM", int(issuer), spam_invoices=tuple(invoices))
        if resp is not None and resp.status == "executed":
            with a.store.edit() as data:
                data["spamReported"][str(int(issuer))] = sorted(spam)

    # ------------------------------------------------------------------ PAY_CAPPED re-quote

    def _supersede(
        self, vi: ValidatedInvoice, preview: PolicyDecision, mandate: MandateView | None, source: str, depth: int
    ) -> list[dict[str, Any]]:
        a = self.a
        inv = vi.signed.invoice
        issuer = int(inv.issuer_agent_id)
        q = int(preview.requote_quantity or 0)
        a.quotes.supersede(issuer, inv.invoice_id, q)
        a.bus.emit(
            "decision",
            {
                "orderId": preview.order_id,
                "invoiceHash": vi.signed.invoice_hash,
                "payee": self._payee_data(issuer),
                "amount": str(int(inv.amount)),
                "outcome": "SUPERSEDED",
                "requoteQuantity": q,
                "offchain": True,
                "source": source,
            },
            reason_codes=list(preview.reasons),
        )
        out: list[dict[str, Any]] = [
            {
                "status": "superseded",
                "invoiceHash": vi.signed.invoice_hash,
                "orderId": preview.order_id,
                "requoteQuantity": q,
                "reasons": list(preview.reasons),
            }
        ]
        if depth >= 1 or q < 1:
            return out
        out.extend(self.request_quote(mandate, issuer, inv.items[0].sku, q, cap_of=inv.invoice_id, depth=depth + 1))
        return out

    def _payee_data(self, issuer: int, grade: Grade | None = None) -> dict[str, Any]:
        data = {"agentId": str(int(issuer)), "name": self.a.directory.name_of(issuer)}
        if grade is not None:
            data["grade"] = Grade(grade).name
        return data

    # ------------------------------------------------------------------ the invoice turn

    def _invoice_context(self, vi: ValidatedInvoice, preview: PolicyDecision, mandate: MandateView | None, ctx) -> dict:
        a = self.a
        inv = vi.signed.invoice
        issuer = int(inv.issuer_agent_id)
        mandates = []
        if mandate is not None:
            # one counterparty per context (8.4): the mandate's budget, and of its payees only this issuer
            m = a.mandate_context(mandate)
            m["payees"] = [p for p in m["payees"] if int(p["agentId"]) == issuer]
            mandates.append(m)
        return {
            "status": a.status_context(ctx.vault),
            "mandates": mandates,
            "invoice": {
                "invoiceId": inv.invoice_id,
                "issuerAgentId": issuer,
                "payeeName": a.directory.name_of(issuer),
                "grade": Grade(ctx.payee.grade).name,
                "items": [
                    {"sku": i.sku, "quantity": int(i.quantity), "unit_price_usdc": usdc_text(int(i.unit_price))}
                    for i in inv.items
                ],
                "amount": str(int(inv.amount)),
                "amount_usdc": usdc_text(int(inv.amount)),
                "capOf": inv.cap_of,
                "quoteRequestId": inv.quote_request_id,
                "deliveryDeadline": int(inv.delivery_deadline),
                "expiresAt": int(inv.expires_at),
            },
            "policy_preview": {
                "outcome": Decision(preview.outcome).name,
                "tool_action": DECISION_TO_TOOL_ACTION[Decision(preview.outcome)].value,
                "amount_usdc": usdc_text(int(preview.amount)),
                "executable": bool(preview.executable),
                "reasons": list(preview.reasons),
                "reason_texts": list(a.reason_texts(preview.reasons)),
            },
            "detector": {
                "quarantine": bool(vi.detector.quarantine),
                "adverse": bool(vi.detector.adverse),
                "rules": sorted({h.rule for h in vi.detector.hits}),
            },
        }

    def _invoice_turn(
        self, vi: ValidatedInvoice, preview: PolicyDecision, mandate: MandateView | None, ctx, block: int
    ) -> tuple[Proposal, dict[str, Any]]:
        """One invoice turn with the LLM; returns ``(proposal, meta)``.

        Without a usable ``decide_invoice`` (no tool, a reply, a reported call) code builds the proposal
        (:meth:`_code_proposal`), so every accepted invoice ends in an executed or recorded decision.
        """
        a = self.a
        inv = vi.signed.invoice
        qcfg = a.cfg.quarantine
        mask = a.mask_flagged_spans
        blocks = [quarantine(f"invoice:{inv.invoice_id}:memo", inv.memo, mask=mask, max_chars=qcfg.max_chars)]
        for i, item in enumerate(inv.items):
            blocks.append(
                quarantine(
                    f"invoice:{inv.invoice_id}:items[{i}].description",
                    item.description,
                    mask=mask,
                    max_chars=qcfg.description_max_chars,
                )
            )
        context = self._invoice_context(vi, preview, mandate, ctx)
        messages = invoice_messages(context, data_message(blocks))
        result, fallback = a.complete(messages, tool_specs("invoice"))
        meta: dict[str, Any] = {
            "fallback": fallback,
            "model": result.model,
            "latencyMs": result.latency_ms,
            "llmError": result.error,
            "masked": mask,
            "flagged": any(b.flagged for b in blocks),
        }
        if not result.tool_calls:
            # no decision: code asks the owner (or records the policy's REFUSE); any text goes to the owner
            meta["note"] = "the LLM called no tool"
            reply = a.redactor.scan(result.text).text if result.text else ""
            if reply:
                meta["reply"] = reply
            return self._code_proposal(vi, preview, result.model, reply or None, "The LLM did not decide."), meta
        call = result.tool_calls[0]
        # exactly one tool per turn (8.1): further calls outside the invoice set are reported too
        extra_bad = a.report_extra_calls(result.tool_calls[1:], "invoice")
        args, kinds_args = a.redactor.scan_value(call.arguments)
        text_r = a.redactor.scan(result.text)
        kinds = tuple(dict.fromkeys(kinds_args + text_r.kinds))
        raw_text = text_r.text if result.text is not None else None
        from acb.agent.redact import strongest

        secret = strongest(kinds)
        parsed = parse_call("invoice", call.model_copy(update={"arguments": args}))
        if secret is not None or parsed.status == "not_allowed":
            resp = a.report_violation(
                tool_name=call.name, raw_args=args, raw_text=raw_text, secret_match=secret, turn="invoice"
            )
            meta["violationReport"] = resp.status if resp is not None else "signer_unavailable"
        if parsed.status == "not_allowed" or extra_bad or secret is not None:
            # a reported turn never moves money: code records the policy's REFUSE (so the adverse-invoice
            # report still follows) or asks the owner; the LLM's own decision, if any, is not used
            if parsed.status == "not_allowed":
                meta["note"] = f"the LLM called {call.name!r}, which is not an invoice-turn tool"
            elif extra_bad:
                meta["note"] = "the LLM called more than one tool, one of them outside the invoice-turn set"
            else:
                meta["note"] = "the LLM output carried a secret; it was removed and reported"
            return self._code_proposal(vi, preview, result.model, raw_text, "The LLM turn was reported."), meta
        source = "fallback" if fallback else "llm"
        if parsed.status == "ok" and parsed.name == "reply_to_owner":
            # no decision: code escalates to the owner (the safe outcome); the reply goes to the owner only
            meta["reply"] = parsed.args.text
            return self._code_proposal(vi, preview, result.model, raw_text, "The LLM replied without deciding."), meta
        if parsed.status == "ok":
            d = parsed.args
            return (
                Proposal(
                    source=source,
                    turn="invoice",
                    tool="decide_invoice",
                    action=ToolAction(d.action),
                    invoice_id=d.invoice_id,
                    payee_agent_id=int(d.payee_agent_id),
                    amount=usdc_units(d.amount_usdc),
                    raw_args=dict(args),
                    raw_text=raw_text,
                    llm_model=result.model,
                ),
                meta,
            )
        # malformed arguments of decide_invoice (A33): what is readable is kept; C01 refuses with 2114
        # (typed values are read from the arguments as given: a float amount is never accepted)
        meta["note"] = parsed.error
        original = args if isinstance(args, dict) else {}
        safe = _jsonable_args(original)
        action = original.get("action")
        payee = original.get("payee_agent_id")
        amount = original.get("amount_usdc")
        try:
            amount_units = usdc_units(amount) if isinstance(amount, str) else None
        except ValueError:
            amount_units = None
        return (
            Proposal(
                source=source,
                turn="invoice",
                tool=call.name,
                action=ToolAction(action) if isinstance(action, str) and action in ToolAction.__members__ else None,
                invoice_id=original.get("invoice_id") if isinstance(original.get("invoice_id"), str) else None,
                payee_agent_id=payee if isinstance(payee, int) and not isinstance(payee, bool) and payee >= 0 else None,
                amount=amount_units,
                raw_args=safe,
                raw_text=raw_text,
                llm_model=result.model,
            ),
            meta,
        )

    def _code_proposal(
        self, vi: ValidatedInvoice, preview: PolicyDecision, model: str | None, raw_text: str | None, why: str
    ) -> Proposal:
        """The proposal code makes when the invoice turn gave no usable decision (source ``fallback``).

        It never moves money: the policy's REFUSE is recorded as such (an adverse invoice then gets its
        verified report), anything else becomes REQUEST_OWNER_APPROVAL (spec 8.6).
        """
        inv = vi.signed.invoice
        refuse = Decision(preview.outcome) == Decision.REFUSE
        action = ToolAction.REFUSE if refuse else ToolAction.REQUEST_OWNER_APPROVAL
        args_out = {
            "invoice_id": inv.invoice_id,
            "action": action.value,
            "payee_agent_id": int(inv.issuer_agent_id),
            "amount_usdc": usdc_text(int(inv.amount)),
            "rationale": f"{why} Code applies the policy: {'refuse' if refuse else 'ask the owner'}.",
        }
        return Proposal(
            source="fallback",
            turn="invoice",
            tool="decide_invoice",
            action=action,
            invoice_id=inv.invoice_id,
            payee_agent_id=int(inv.issuer_agent_id),
            amount=int(inv.amount),
            raw_args=args_out,
            raw_text=raw_text,
            llm_model=model,
        )

    # ------------------------------------------------------------------ execution

    def _execute_invoice(
        self,
        vi: ValidatedInvoice,
        qr: QuoteRequest,
        mandate: MandateView | None,
        proposal: Proposal,
        agent_decision: PolicyDecision,
        meta: dict[str, Any],
        source: str,
    ) -> dict[str, Any]:
        a = self.a
        si = vi.signed
        inv = si.invoice
        issuer = int(inv.issuer_agent_id)
        oid = agent_decision.order_id
        kind = OUTFLOW.get(Decision(agent_decision.outcome), "RECORD_DECISION")
        req = ExecuteRequest(
            request_id=new_request_id(),
            kind=kind,  # type: ignore[arg-type]
            proposal=proposal,
            invoice=si,
            quote_request=qr,
            mandate_id=qr.mandate_id,
            agent_decision=agent_decision,
            order_id=oid,
        )
        record = {
            "orderId": oid,
            "invoice": si.wire_dict(),
            "invoiceHash": si.invoice_hash,
            "quoteRequest": qr.to_wire(),
            "mandateId": qr.mandate_id,
            "issuer": issuer,
            "provider": (a.directory.get(issuer).key if a.directory.get(issuer) else None),
            "amount": str(int(inv.amount)),
            "status": "in_flight",
            "kind": kind,
            "requestId": req.request_id,
            "source": source,
            "adverse": bool(vi.detector.adverse),
        }
        a.store.put_order(oid, record)
        resp = a.execute(req)
        return self._after_execute(vi, proposal, agent_decision, resp, meta, kind)

    def _after_execute(
        self,
        vi: ValidatedInvoice,
        proposal: Proposal | None,
        agent_decision: PolicyDecision,
        resp: ExecuteResponse | None,
        meta: dict[str, Any],
        kind: str,
    ) -> dict[str, Any]:
        a = self.a
        si = vi.signed
        inv = si.invoice
        issuer = int(inv.issuer_agent_id)
        oid = agent_decision.order_id
        decision = resp.decision if resp is not None and resp.decision is not None else agent_decision
        outcome = Decision(decision.outcome)
        status = resp.status if resp is not None else "signer_unavailable"
        reasons = list(resp.reason_codes) if resp is not None and resp.reason_codes else list(decision.reasons)
        if meta.get("fallback") and CODE_FALLBACK not in reasons:
            reasons.append(CODE_FALLBACK)
        txs = [t for t in (resp.tx if resp is not None else ())]
        tx_hash = txs[-1].hash if txs else None
        outflow_tx = next(
            (t.hash for t in txs if t.function in ("pay", "escrow", "executeApproved") and t.status == 1), None
        )
        recorded = any(t.function == "recordDecision" and t.status == 1 for t in txs)

        if status == "executed":
            order_status = "awaiting_delivery"
            delivery_kind = "escrow" if outcome == Decision.ESCROW else "pay"
        elif status == "refused" and outcome == Decision.NEEDS_OWNER_APPROVAL:
            order_status, delivery_kind = "awaiting_approval", None
        elif status == "refused":
            order_status, delivery_kind = "refused", None
        elif resp is None:
            # the signer's answer was lost: the order stays in flight and is reconciled from the signer's
            # request log and the chain on every tick (:meth:`reconcile`)
            order_status, delivery_kind = "in_flight", None
        else:
            order_status, delivery_kind = status, None
        a.store.update_order(
            oid,
            status=order_status,
            deliveryKind=delivery_kind,
            txHash=outflow_tx,
            decisionHash=decision.decision_hash,
            outcome=outcome.name,
            reasons=reasons,
            **({"unknownAt": int(time.time())} if resp is None else {}),
        )
        block = None
        payee = a.payee_candidate(issuer, block)
        card = DecisionCard(
            order_id=oid,
            payee=payee,
            amount=int(decision.amount),
            outcome=outcome,
            reasons=tuple(reasons[:6]),
            reason_texts=a.reason_texts(reasons[:6]),
            tx_hash=tx_hash,
            offchain=not txs,
        )
        violation = None
        if resp is not None and resp.violation is not None:
            violation = {
                "kind": resp.violation.kind.name,
                "severity": resp.violation.severity,
                "txHash": resp.violation.tx_hash,
            }
        decision_record = {
            "orderId": oid,
            "invoiceId": inv.invoice_id,
            "invoiceHash": si.invoice_hash,
            "issuer": issuer,
            "payee": payee.to_wire(),
            "amount": str(int(decision.amount)),
            "outcome": outcome.name,
            "reasons": reasons,
            "status": status,
            "kind": kind,
            "decisionHash": decision.decision_hash,
            "agentDecision": {
                "outcome": Decision(agent_decision.outcome).name,
                "reasons": list(agent_decision.reasons),
            },
            "checks": [c.to_wire() for c in decision.checks],
            "txHashes": [t.hash for t in txs],
            "violation": violation,
            "fallback": bool(meta.get("fallback")),
            "llmModel": meta.get("model"),
            "llmReply": (meta.get("reply") or "")[:1200] or None,
            "proposal": {
                "action": proposal.action.value if proposal is not None and proposal.action is not None else None,
                "payeeAgentId": proposal.payee_agent_id if proposal is not None else None,
                "amount": str(proposal.amount) if proposal is not None and proposal.amount is not None else None,
                "source": proposal.source if proposal is not None else None,
            },
            "chainTime": a.chain.now(),
        }
        # reports are filed by code (9.5): a recorded REFUSE of an adverse invoice -> INJECTION_ATTEMPT
        report_status = None
        if status == "refused" and outcome == Decision.REFUSE and recorded and vi.detector.adverse:
            report = self.file_report("INJECTION_ATTEMPT", issuer, order_id=oid, invoice=si)
            report_status = report.status if report is not None else "not_filed"
        owner_reply = None
        if outcome == Decision.REFUSE and CODE_INJECTION in reasons:
            owner_reply = refusal_reply(payee.name, report_status)
            decision_record["ownerReply"] = owner_reply
        with a.store.edit() as data:
            data["decisions"][oid] = decision_record
        a.bus.emit(
            "decision",
            {
                "orderId": oid,
                "invoiceHash": si.invoice_hash,
                "payee": self._payee_data(issuer, payee.grade),
                "amount": str(int(decision.amount)),
                "outcome": outcome.name,
                "status": status,
                "tier": _tier_of(txs) if _tier_of(txs) is not None else self._tier_now(),
                "fallback": bool(meta.get("fallback")),
                "llmReply": (meta.get("reply") or "")[:1200] or None,
                "ownerReply": owner_reply,
                "llm": {"model": meta.get("model"), "masked": meta.get("masked"), "flagged": meta.get("flagged")},
                "violation": violation,
            },
            reason_codes=reasons,
            tx_hash=tx_hash,
        )
        out: dict[str, Any] = {
            "status": status,
            "orderId": oid,
            "invoiceHash": si.invoice_hash,
            "outcome": outcome.name,
            "reasons": reasons,
            "txHashes": [t.hash for t in txs],
            "violation": violation,
            "card": card.to_wire(),
        }
        if owner_reply is not None:
            out["reply"] = owner_reply
        if order_status == "awaiting_approval":
            from acb.types import ApprovalRequest

            approval = ApprovalRequest(order_id=oid, invoice=si, reasons=tuple(reasons[:4]))
            out["approval"] = approval.to_wire()
            a.bus.emit(
                "approval",
                {
                    "orderId": oid,
                    "payee": self._payee_data(issuer),
                    "amount": str(int(inv.amount)),
                    "status": "requested",
                },
                reason_codes=reasons,
            )
        if status == "executed":
            self.process_order(oid)
        return out

    def _tier_now(self) -> int | None:
        try:
            return int(self.a.chain.vault_state().tier)
        except Exception:  # noqa: BLE001 - the tier is informational in the event
            return None

    # ================================================================== restart

    def recover(self) -> list[str]:
        """After a restart: every order whose signer answer is unknown takes its state from the signer's
        request log and the chain (:meth:`reconcile`); one still unknown is ``interrupted``."""
        a = self.a
        changed = []
        with a.lock:
            for oid, rec in a.store.read("orders").items():
                if rec.get("status") != "in_flight":
                    continue
                if self.reconcile(oid, at_start=True):
                    changed.append(oid)
        return changed

    def reconcile(self, oid: str, *, at_start: bool = False) -> str | None:
        """Settle an order whose signer answer is unknown (``in_flight``) or was given up (``interrupted``).

        The signer never executes a request id twice, so the stored ``requestId`` is looked up
        (``GET /v1/requests/{id}``) and the order is read from the chain: PAID / ESCROWED wait for
        delivery, RELEASED / REFUNDED are closed, REFUSED is refused (with the INJECTION_ATTEMPT report
        for an adverse invoice), a recorded NEEDS_OWNER_APPROVAL waits for the owner. An ``in_flight``
        order that stays unknown is looked up again on every tick and marked ``interrupted`` after
        :data:`UNKNOWN_GIVE_UP_S` (at once after a restart); an ``interrupted`` order is still read from
        the chain on every tick, so a late execution is never orphaned. Returns the new status, if any.
        """
        a = self.a
        with a.lock:
            rec = a.store.order(oid)
            if rec is None or rec.get("status") not in RECONCILE_STATUSES:
                return None
            status = rec["status"]
            resp: ExecuteResponse | None = None
            lookup = getattr(a.signer, "lookup", None)
            if status == "in_flight" and rec.get("requestId") and callable(lookup):
                try:
                    resp = lookup(rec["requestId"])
                except Exception:  # noqa: BLE001 - unknown stays unknown; tried again on the next tick
                    resp = None
            try:
                state = a.chain.order(oid).state
            except Exception as exc:  # noqa: BLE001 - chain unreachable: tried again on the next tick
                a.bus.emit(
                    "error", {"message": "order reconciliation postponed", "orderId": oid, "error": type(exc).__name__}
                )
                return None
            updates: dict[str, Any] = {}
            new: str | None = None
            if state in (OrderState.PAID, OrderState.ESCROWED):
                new = "awaiting_delivery"
                updates["deliveryKind"] = "escrow" if state == OrderState.ESCROWED else "pay"
            elif state in (OrderState.RELEASED, OrderState.REFUNDED):
                new = f"closed_{OrderState(state).name.lower()}"
            elif state == OrderState.REFUSED:
                new = "refused"
            elif resp is not None and resp.status in ("refused", "rejected"):
                recorded = any(t.function == "recordDecision" and t.status == 1 for t in resp.tx)
                needs_owner = (
                    resp.decision is not None and Decision(resp.decision.outcome) == Decision.NEEDS_OWNER_APPROVAL
                )
                new = "awaiting_approval" if (needs_owner and recorded) else resp.status
            elif status == "in_flight":
                since = rec.get("unknownAt")
                if at_start or not isinstance(since, (int, float)) or time.time() - since > UNKNOWN_GIVE_UP_S:
                    new = "interrupted"
            if new is None or new == status:
                return None
            if resp is not None:
                outflow = next(
                    (t.hash for t in resp.tx if t.function in ("pay", "escrow", "executeApproved") and t.status == 1),
                    None,
                )
                if outflow:
                    updates["txHash"] = outflow
                if resp.decision is not None:
                    updates["decisionHash"] = resp.decision.decision_hash
                    updates["outcome"] = Decision(resp.decision.outcome).name
            a.store.update_order(oid, status=new, **updates)
            with a.store.edit() as data:
                drec = data["decisions"].get(oid)
                if drec is not None:
                    drec["status"] = "executed" if new == "awaiting_delivery" else new
            a.bus.emit(
                "info",
                {
                    "message": "order reconciled" + (" after restart" if at_start else ""),
                    "orderId": oid,
                    "state": OrderState(state).name,
                    "status": new,
                    "signerAnswer": resp.status if resp is not None else None,
                },
            )
            if new == "refused" and rec.get("adverse") and rec.get("issuer") is not None and rec.get("invoice"):
                try:
                    si = SignedInvoice.from_wire(rec["invoice"])
                except (TypeError, ValueError):
                    si = None
                if si is not None:
                    self.file_report("INJECTION_ATTEMPT", int(rec["issuer"]), order_id=oid, invoice=si)
            return new

    # ================================================================== approvals

    def execute_approved(self, order_id_: str) -> dict[str, Any]:
        """``POST /approvals/{order_id}/executed-request``: the owner's approval is mined; ask for PAY_APPROVED."""
        a = self.a
        oid = order_id_.lower()
        with a.lock:
            rec = a.store.order(oid)
            if rec is None or rec.get("status") != "awaiting_approval":
                return {"status": "error", "error": "no order awaiting the owner's approval"}
            block = a.chain.block()
            now = a.chain.now(block)
            info = a.chain.approval(oid, block)
            if not info.exists or info.used or now > info.expiry:
                return {"status": "error", "error": "no usable owner approval on-chain"}
            approval = OwnerApproval(
                payee_agent_id=info.payee_agent_id,
                expected_payee=info.expected_payee,
                amount=info.amount,
                expiry=info.expiry,
                as_escrow=info.as_escrow,
                used=info.used,
            )
            si = SignedInvoice.from_wire(rec["invoice"])
            qr = QuoteRequest.model_validate(rec["quoteRequest"])
            inv = si.invoice
            issuer = int(inv.issuer_agent_id)
            vctx = build_validation_context(
                now=now,
                chain_id=a.chain.chain_id,
                usdc=a.chain.usdc,
                vault_address=a.vault_address,
                vault_agent_id=a.agent_id,
                catalogue=a.directory.catalogue(),
                quote_requests=(qr,),
                pinned_wallets={issuer: info.expected_payee},
                live_wallets={issuer: a.chain.live_wallet(issuer, block)},
                superseded=a.quotes.superseded(),
            )
            result = validate_invoice(
                si.wire_dict(), vctx, a.pcfg, check_signature=True, check_expiry=False, check_quote_open=False
            )
            if isinstance(result, InvoiceRejection):
                return {"status": "error", "error": f"stored invoice no longer validates: {result.rule}"}
            ctx = self._policy_ctx(result, None, vctx, block, now, exclude_pending=oid, approval=approval)
            action = ToolAction.ESCROW if info.as_escrow else ToolAction.PAY
            proposal = Proposal(
                source="owner",
                turn="invoice",
                tool="decide_invoice",
                action=action,
                invoice_id=inv.invoice_id,
                payee_agent_id=issuer,
                amount=int(inv.amount),
                raw_args={},
                raw_text=None,
                llm_model=None,
            )
            agent_decision = decide(proposal, ctx, a.pcfg, owner_approval=approval)
            req = ExecuteRequest(
                request_id=new_request_id(),
                kind="PAY_APPROVED",
                proposal=proposal,
                invoice=si,
                quote_request=qr,
                agent_decision=agent_decision,
                order_id=oid,
            )
            a.store.update_order(oid, status="in_flight", requestId=req.request_id, kind="PAY_APPROVED")
            resp = a.execute(req)
            a.bus.emit(
                "approval",
                {
                    "orderId": oid,
                    "payee": self._payee_data(issuer),
                    "amount": str(int(inv.amount)),
                    "status": "executing",
                },
            )
            return self._after_execute(result, proposal, agent_decision, resp, {"fallback": False}, "PAY_APPROVED")

    def reject_approval(self, order_id_: str) -> dict[str, Any]:
        """The owner rejected an approval card (no transaction): the order is closed off-chain."""
        a = self.a
        oid = order_id_.lower()
        with a.lock:
            rec = a.store.order(oid)
            if rec is None or rec.get("status") != "awaiting_approval":
                return {"status": "error", "error": "no order awaiting the owner's approval"}
            a.store.update_order(oid, status="approval_rejected")
            a.bus.emit("approval", {"orderId": oid, "status": "rejected by the owner"})
            return {"status": "closed", "orderId": oid}

    # ================================================================== deliveries

    def tick(self) -> list[str]:
        """One pass over every order with an unknown signer answer and every order waiting for delivery
        (the service runs it every few seconds)."""
        a = self.a
        with a.lock:
            for oid, rec in a.store.read("orders").items():
                if rec.get("status") in RECONCILE_STATUSES:
                    try:
                        self.reconcile(oid)
                    except Exception as exc:  # noqa: BLE001 - one order must not stop the others
                        a.bus.emit(
                            "error",
                            {"message": "order reconciliation failed", "orderId": oid, "error": type(exc).__name__},
                        )
            open_ids = [oid for oid, rec in a.store.read("orders").items() if rec.get("status") in OPEN_ORDER_STATUSES]
            for oid in open_ids:
                try:
                    self.process_order(oid)
                except Exception as exc:  # noqa: BLE001 - one order must not stop the others
                    a.bus.emit(
                        "error", {"message": "delivery processing failed", "orderId": oid, "error": type(exc).__name__}
                    )
            return open_ids

    def process_order(self, oid: str) -> str | None:
        """Advance one order: fetch and check the delivery, then RELEASE / RECORD_DELIVERY / REFUND."""
        a = self.a
        with a.lock:
            rec = a.store.order(oid)
            if rec is None or rec.get("status") not in OPEN_ORDER_STATUSES:
                return None
            block = a.chain.block()
            now = a.chain.now(block)
            order = a.chain.order(oid, block)
            frozen = bool(a.chain.vault_state(block).frozen)
            escrow = rec.get("deliveryKind") == "escrow"
            if order.state in (OrderState.RELEASED, OrderState.REFUNDED) or (
                order.state == OrderState.PAID and order.outcome != Outcome.NONE
            ):
                # settled while the signer's answer was lost: a FAILED outcome still gets its report
                a.store.update_order(oid, status=f"closed_{OrderState(order.state).name.lower()}")
                if order.outcome == Outcome.FAILED and rec.get("issuer") is not None:
                    self.file_report("NON_DELIVERY", int(rec["issuer"]), order_id=oid)
                return "closed"
            si = SignedInvoice.from_wire(rec["invoice"])
            inv = si.invoice
            if rec.get("delivery") is None and rec.get("provider"):
                answer = a.provider.deliver(rec["provider"], deliver_body(oid, inv.invoice_id, rec.get("txHash")))
                if isinstance(answer, dict) and answer.get("status") == "delivered":
                    order = a.chain.order(oid)  # the claim (escrow) happened just before the hand-over
                    check = check_delivery(
                        receipt_raw=answer.get("receipt"),
                        payload=answer.get("payload"),
                        invoice=inv,
                        order_id=oid,
                        payee_wallet=order.payee,
                        claimed_hash=order.delivery_hash if escrow else None,
                    )
                    if check.ok and check.receipt is not None:
                        received_at = a.chain.now()
                        rec = (
                            a.store.update_order(
                                oid,
                                delivery=check.receipt.wire_dict(),
                                payloadHash=check.payload_hash,
                                payloadSize=check.size,
                                receivedAt=received_at,
                            )
                            or rec
                        )
                        a.bus.emit(
                            "info",
                            {
                                "message": "delivery received",
                                "orderId": oid,
                                "payloadHash": check.payload_hash,
                                "size": check.size,
                                "receivedAt": received_at,
                                "deadline": order.deadline,
                            },
                        )
                    elif rec.get("lastCheck") != check.reason:
                        a.store.update_order(oid, lastCheck=check.reason)
                        a.bus.emit(
                            "error", {"message": "delivery check failed", "orderId": oid, "reason": check.reason}
                        )
            if rec.get("delivery") is not None:
                if frozen:
                    return "frozen"
                delivery = SignedDelivery.from_wire(rec["delivery"])
                if escrow:
                    if order.claimed_at == 0:
                        return "waiting_claim"
                    resp = a.execute(
                        ExecuteRequest(request_id=new_request_id(), kind="RELEASE", order_id=oid, delivery=delivery)
                    )
                    return self._settled(oid, resp, "released")
                resp = a.execute(
                    ExecuteRequest(
                        request_id=new_request_id(),
                        kind="RECORD_DELIVERY",
                        order_id=oid,
                        delivery=delivery,
                        received_at=int(rec["receivedAt"]),
                    )
                )
                return self._settled(oid, resp, "delivered")
            # no valid delivery yet
            if now <= order.deadline + a.pcfg.delivery.grace_s:
                return "waiting"
            if escrow:
                if order.claimed_at != 0:
                    if rec.get("alerted") is None:
                        a.store.update_order(oid, alerted=now)
                        a.bus.emit(
                            "error",
                            {"message": "claimed but not delivered: the owner may dispute (2403)", "orderId": oid},
                        )
                    return "claimed_undelivered"
                if frozen:
                    return "frozen"
                resp = a.execute(
                    ExecuteRequest(request_id=new_request_id(), kind="REFUND", order_id=oid, reason_code=2400)
                )
                state = self._settled(oid, resp, "refunded")
            else:
                if frozen:
                    return "frozen"
                resp = a.execute(ExecuteRequest(request_id=new_request_id(), kind="RECORD_DELIVERY", order_id=oid))
                state = self._settled(oid, resp, "failed")
            if resp is not None and resp.status == "executed":
                self.file_report("NON_DELIVERY", int(inv.issuer_agent_id), order_id=oid)
            return state

    def _settled(self, oid: str, resp: ExecuteResponse | None, label: str) -> str:
        a = self.a
        if resp is None or resp.status != "executed":
            a.bus.emit(
                "error",
                {
                    "message": f"{label}: signer did not execute",
                    "orderId": oid,
                    "status": resp.status if resp is not None else "signer_unavailable",
                },
                reason_codes=resp.reason_codes if resp is not None else (),
            )
            return "retry"
        a.store.update_order(oid, status=label, settleTx=resp.tx[-1].hash if resp.tx else None)
        with a.store.edit() as data:
            rec = data["decisions"].get(oid)
            if rec is not None:
                rec.setdefault("txHashes", []).extend(t.hash for t in resp.tx)
                rec["settlement"] = label
        return label

    # ================================================================== reports

    def file_report(
        self,
        kind: str,
        subject: int,
        *,
        order_id: str | None = None,
        invoice: SignedInvoice | None = None,
        spam_invoices: tuple[SignedInvoice, ...] = (),
        owner_signature: str | None = None,
    ) -> ExecuteResponse | None:
        """Ask the signer to file a report (it verifies the evidence; at most one per subject/kind/order)."""
        a = self.a
        key = f"{kind}:{int(subject)}:{(order_id or ZERO_HASH).lower()}"
        if key in a.store.read("reports"):
            return None
        req = ExecuteRequest(
            request_id=new_request_id(),
            kind="REPORT",
            report=ReportRequest(
                kind=kind,  # type: ignore[arg-type]
                subject_agent_id=int(subject),
                order_id=order_id,
                invoice=invoice,
                spam_invoices=spam_invoices,
                owner_signature=owner_signature,
            ),
        )
        resp = a.execute(req)
        tx = resp.tx[-1].hash if resp is not None and resp.tx else None
        if resp is not None and resp.status == "executed":
            with a.store.edit() as data:
                data["reports"][key] = tx
        a.bus.emit(
            "report",
            {
                "kind": kind,
                "subject": self._payee_data(int(subject)),
                "orderId": order_id,
                "status": resp.status if resp is not None else "signer_unavailable",
            },
            reason_codes=resp.reason_codes if resp is not None else (),
            tx_hash=tx,
        )
        return resp

    def bad_data(self, order_id_: str, owner_signature: str) -> dict[str, Any]:
        """``POST /reports/bad-data``: an owner-signed BAD_DATA report (the signer checks the signature)."""
        a = self.a
        oid = order_id_.lower()
        with a.lock:
            rec = a.store.order(oid)
            subject = int(rec["issuer"]) if rec is not None else int(a.chain.order(oid).payee_agent_id)
            resp = self.file_report("BAD_DATA", subject, order_id=oid, owner_signature=owner_signature)
            if resp is None:
                return {"status": "duplicate"}
            return {"status": resp.status, "reasonCodes": list(resp.reason_codes), "tx": [t.hash for t in resp.tx]}

    # ================================================================== inbox

    def inbox_invoice(self, raw: Any) -> None:
        """A pushed invoice (public, untrusted): per-issuer rate limit for verified issuers, then validation."""
        a = self.a
        key = _seen_key(raw)
        if key is not None:
            try:
                signer = recover_invoice_signer(raw)
            except (KeyError, TypeError, ValueError):
                signer = None
            live = a.chain.live_wallet(key[0]) if signer else None
            if live is not None and signer == live and not self.issuer_limiter.allow(key[0]):
                a.bus.emit("info", {"message": "inbox rate limit", "issuer": str(key[0])})
                return
        self.handle_invoice(raw, source="inbox")

    MESSAGE_KEYS = frozenset({"schema", "messageId", "fromAgentId", "toAgentId", "sentAt", "text", "signature"})

    def inbox_message(self, raw: Any) -> str:
        """A pushed counterparty message (``acb-message/1``): shown to the owner only, never an LLM turn.

        It must be signed (EIP-191 over ``chash`` of the message without ``signature``) by the live agent
        wallet of an agent in the pinned directory and addressed to us; anything else is dropped.
        Returns what happened (for tests; the HTTP answer is always the constant ``accepted``).
        """
        a = self.a
        if not isinstance(raw, dict) or set(raw) != self.MESSAGE_KEYS or raw.get("schema") != "acb-message/1":
            return "dropped: not an acb-message/1 object"
        sender, to, text, sig = raw["fromAgentId"], raw["toAgentId"], raw["text"], raw["signature"]
        if not (isinstance(sender, str) and sender.isdigit() and len(sender) <= 78 and isinstance(to, str)):
            return "dropped: bad agent ids"
        if not isinstance(text, str) or len(text) > 2000 or not isinstance(sig, str):
            return "dropped: bad text or signature"
        if isinstance(raw["sentAt"], bool) or not isinstance(raw["sentAt"], int):
            return "dropped: bad sentAt"
        if not isinstance(raw["messageId"], str) or not 1 <= len(raw["messageId"]) <= 40:
            return "dropped: bad message id"
        if to != str(a.agent_id):
            return "dropped: not addressed to this wallet"
        entry = a.directory.get(int(sender))
        if entry is None:
            return "dropped: sender not in the pinned directory"
        try:
            signer = recover_eip191(chash({k: v for k, v in raw.items() if k != "signature"}), sig)
        except (ValueError, TypeError):
            return "dropped: bad signature"
        live = a.chain.live_wallet(int(sender))
        if live is None or signer != live:
            return "dropped: not signed by the sender's agent wallet"
        if not self.issuer_limiter.allow(("message", int(sender))):
            return "dropped: rate limit"
        q = quarantine(f"message:{raw['messageId']}", text, mask=False, max_chars=a.cfg.quarantine.max_chars)
        record = {
            "messageId": raw["messageId"],
            "from": {"agentId": sender, "name": entry.name},
            "sentAt": int(raw["sentAt"]),
            "receivedAt": a.chain.now(),
            "text": q.text,
            "flagged": q.flagged,
            "detectorRules": list(q.rules),
        }
        with a.store.edit() as data:
            data["messages"].append(record)
            data["messages"] = data["messages"][-int(a.cfg.messages_kept) :]
        a.bus.emit("message", record)
        return "stored"


def _jsonable_args(args: Any) -> dict[str, Any]:
    from acb.agent.wallet import jsonable

    value = jsonable(args)
    return value if isinstance(value, dict) else {"value": value}


def _tier_of(txs: list[Any]) -> int | None:
    """The vault tier after a request, from a ``TierChanged`` log when one was emitted."""
    for t in reversed(txs):
        for log in t.logs:
            if log.get("event") == "TierChanged":
                try:
                    return int(log["args"]["toTier"])
                except (KeyError, TypeError, ValueError):
                    return None
    return None


def payload_digest(payload: Any) -> str | None:
    """``chash`` of a payload, or None when it is not canonical JSON."""
    try:
        canonical_bytes(payload)
    except TypeError:
        return None
    return chash(payload)


__all__ = ["CODE_FALLBACK", "Runtime", "SlidingLimiter", "payload_digest"]
