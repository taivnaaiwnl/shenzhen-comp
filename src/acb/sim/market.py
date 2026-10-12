"""Scripted market behaviours for the scenario runner (spec 9.4 ``POST /admin/run``, 9.5, 10.2).

Every action is a function ``(sim, args) -> dict`` registered in :data:`ACTIONS`. The runner calls them
through the service (``SIM_TOKEN``) or ``acb sim run``; the result lists every transaction sent
(including provider claims made while delivering).

Actions on every network:

- ``warmup`` (S4): Beacon's owner registers mandate MB in Beacon's vault, then 19 orders: Beacon
  escrows, the provider claims before handing over the data, Beacon checks the signed receipt and
  releases. Slowpoke's odd orders are claimed after the deadline (LATE). Resumable: each order slot is
  driven from its on-chain state, so a second run sends nothing new.
- ``ghost_default_round`` (S11): Beacon escrows 3 x 0.20 with Ghost (orders 5-7, never claimed), and
  after the deadline refunds them with reason 2400 and files NON_DELIVERY (informational).
  ``args.phase``: ``open``, ``close`` or ``all`` (default).
- ``boost_ghost`` / ``smear_atlas`` (S11, A34, A35): Echo identities post POSITIVE feedback for Ghost /
  unbacked NON_DELIVERY reports on Atlas.
- ``push_spam`` / ``push_duplicate`` (S19, A46): signed invoices with an empty ``quoteRequestId`` pushed
  to the wallet inbox; the duplicate re-pushes one of them unchanged.
- ``push_message`` (A04, A48-A50): a signed ``acb-message/1`` pushed to the wallet inbox.
- ``ghost_appeal`` (S20): Ghost's owner files ``ACB.fileAppeal`` with a payload claiming delivery.
- ``set_override``: override a persona's script (``honest``, ``default``, ``inject``, ``none``).
- ``status``: counters, overrides and attack entities (read only).

Anvil-only attack actions are in :mod:`acb.sim.attacks`; :mod:`acb.sim.actions` registers both.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from eth_account.signers.local import LocalAccount

from acb.canonical import chash, write_canonical
from acb.chain.contracts import ZERO_ADDRESS, OutflowParams
from acb.sim.core import Party, SimError, Simulator, Web3ChainView
from acb.sim.invoices import (
    appeal_payload,
    build_message,
    evidence_uri,
    report_endpoint,
    report_payload,
    verify_receipt,
)
from acb.types import Decision, Grade, OrderState, Outcome, TxResult

MAX_MANDATE_DURATION = 7 * 24 * 3600
REPORT_TAG = "acb-report"
POSITIVE_KINDS = frozenset({"POSITIVE"})

Action = Callable[[Simulator, dict[str, Any]], dict[str, Any]]


# ---------------------------------------------------------------------------
# helpers: buyers, mandates, orders
# ---------------------------------------------------------------------------


def contracts(sim: Simulator) -> Any:
    """The bound contracts (scripted actions need the real chain)."""
    chain = sim.chain
    if not isinstance(chain, Web3ChainView):
        raise SimError(409, "this action needs a chain connection")
    return chain.contracts


@dataclass(frozen=True)
class Buyer:
    """A vault the simulator buys from: Beacon's, an Echo vault or an attack vault."""

    name: str
    address: str
    agent_id: int
    agent: LocalAccount
    owner: LocalAccount


def buyer(sim: Simulator, name: str) -> Buyer:
    """The vault ``name`` of the deployment (or of the attack state) with the keys the simulator holds."""
    entry = sim.dep.vaults.get(name)
    if entry is not None:
        return Buyer(
            name, entry.address, int(entry.agent_id), sim.account_for(entry.agent), sim.account_for(entry.owner)
        )
    rec = sim.state.data["vaults"].get(name)
    if rec is None:
        raise SimError(404, {"status": "unknown_vault", "vault": name})
    return Buyer(
        name, rec["address"], int(rec["agentId"]), sim.account_for(rec["agent"]), sim.account_for(rec["owner"])
    )


def send(sim: Simulator, account: LocalAccount, fn: Any, *, expect_revert: bool = False) -> TxResult:
    """Send one call; a call that would revert raises ``SimError`` unless ``expect_revert``."""
    from acb.chain.tx import TxSimulationError

    try:
        return sim.sender(account).send(fn, expect_revert=expect_revert)
    except TxSimulationError as exc:
        raise SimError(409, {"status": "would_revert", "function": exc.function, "error": exc.revert_error}) from None


def ensure_mandate(
    sim: Simulator,
    b: Buyer,
    label: str,
    payees: Iterable[Party],
    *,
    max_per_tx: int,
    max_total: int,
    valid_s: int,
    purpose: str,
    need: int = 0,
) -> tuple[str, TxResult | None]:
    """A mandate in ``b``'s vault naming ``payees`` (registered by the vault owner if needed).

    A stored mandate is reused while it is active, has at least an hour left, pins the current wallet
    of every payee and has ``need`` budget left. Mandate ids follow 3.2 (``chash`` with a random salt).
    """
    payees = list(payees)
    vault = contracts(sim).vault_at(b.address)
    key = f"{b.name}:{label}"
    now = sim.now()
    stored = sim.state.data["mandates"].get(key)
    if stored is not None:
        info = vault.get_mandate(stored["mandateId"])
        if (
            info.active
            and info.valid_until > now + 3600
            and info.max_total - info.spent >= need
            and all(vault.mandate_payee(stored["mandateId"], p.agent_id) == p.address for p in payees)
        ):
            return stored["mandateId"], None
    identity = contracts(sim).identity
    wallets = [identity.get_agent_wallet(p.agent_id) for p in payees]
    valid_until = now + min(int(valid_s), MAX_MANDATE_DURATION)
    purpose_hash = chash({"purpose": purpose})
    mandate_id = chash(
        {
            "vault": b.address,
            "payeeAgentIds": [str(p.agent_id) for p in payees],
            "payeeWallets": wallets,
            "maxPerTx": str(int(max_per_tx)),
            "maxTotal": str(int(max_total)),
            "validUntil": valid_until,
            "recurring": False,
            "purposeHash": purpose_hash,
            "salt": "0x" + secrets.token_hex(32),
        }
    )
    fn = vault.register_mandate(
        mandate_id, [p.agent_id for p in payees], wallets, max_per_tx, max_total, valid_until, False, purpose_hash
    )
    result = send(sim, b.owner, fn)
    with sim.state.edit() as data:
        data["mandates"][key] = {
            "mandateId": mandate_id,
            "payees": {p.key: p.agent_id for p in payees},
            "validUntil": valid_until,
            "tx": result.hash,
        }
    sim.emit(
        "mandate",
        {
            "message": f"mandate {label} registered in vault {b.name}",
            "vault": b.address,
            "mandateId": mandate_id,
            "payees": [{"agentId": str(p.agent_id), "name": p.name or p.key} for p in payees],
            "maxPerTx": str(max_per_tx),
            "maxTotal": str(max_total),
            "validUntil": valid_until,
        },
        tx_hash=result.hash,
    )
    return mandate_id, result


def beacon_mandate(sim: Simulator, need: int = 0) -> tuple[Buyer, str, TxResult | None]:
    """Beacon's mandate MB (spec 10.2 S4: Atlas, Slowpoke, Ghost, Orion; 1.00 per tx; 6.00; 7 days)."""
    m = sim.cfg.market["mandate"]
    b = buyer(sim, sim.cfg.market.get("buyer", "beacon"))
    payees = [sim.party(k) for k in m["payees"]]
    mandate_id, tx = ensure_mandate(
        sim,
        b,
        m.get("label", "MB"),
        payees,
        max_per_tx=int(m["max_per_tx"]),
        max_total=int(m["max_total"]),
        valid_s=int(m.get("valid_s", MAX_MANDATE_DURATION)),
        purpose=str(m.get("purpose", "")),
        need=need,
    )
    return b, mandate_id, tx


@dataclass(frozen=True)
class Slot:
    """One scripted order: a stable label, the provider, the invoice lines and the payment kind."""

    label: str
    provider: str
    sku: str
    quantity: int
    kind: str = "escrow"  # "escrow" or "pay"
    unit_price: int | None = None  # set for trade invoices outside a catalogue (attack identities)
    description: str = ""


def _reason(sim: Simulator, b: Buyer, payee_agent_id: int, kind: str) -> int:
    vault = contracts(sim).vault_at(b.address)
    grade = contracts(sim).acb.effective_grade(payee_agent_id, vault.max_grade_age())
    if kind == "escrow":
        return 2003 if grade == Grade.C else 2004
    return {Grade.A: 2000, Grade.B: 2001}.get(grade, 2005)


def _quote(sim: Simulator, b: Buyer, slot: Slot) -> tuple[dict[str, Any], str]:
    qr = "qr-" + secrets.token_hex(16)
    if slot.unit_price is None:
        signed = sim.quote(
            slot.provider,
            {
                "quoteRequestId": qr,
                "buyerAgentId": str(b.agent_id),
                "buyerVault": b.address,
                "sku": slot.sku,
                "quantity": slot.quantity,
                "capOf": "",
            },
        )
        record = sim.invoice_record(slot.provider, signed["invoiceId"])
        assert record is not None
        return signed, record["hash"]
    party = sim.party(slot.provider)
    return sim.issue_invoice(
        party,
        buyer_vault=b.address,
        buyer_agent_id=b.agent_id,
        lines=[(slot.sku, slot.description or slot.sku, slot.unit_price, slot.quantity)],
        quote_request_id=qr,
    )


def open_slot(sim: Simulator, b: Buyer, mandate_id: str, slot: Slot) -> dict[str, Any]:
    """Quote and open (escrow or pay) one slot unless its order already exists on-chain."""
    labels = sim.state.data["labels"]
    rec = labels.get(slot.label)
    now = sim.now()
    if rec is not None:
        if sim.chain.order(b.address, rec["orderId"]).state != OrderState.NONE:
            return dict(rec)
        if not (rec["expiresAt"] > now + 10 and rec["deliveryDeadline"] > now + 30):
            rec = None
    if rec is None:
        signed, invoice_hash = _quote(sim, b, slot)
        record = sim.invoice_record(slot.provider, signed["invoiceId"])
        assert record is not None
        rec = {
            "label": slot.label,
            "provider": slot.provider,
            "vault": b.name,
            "invoiceId": signed["invoiceId"],
            "invoiceHash": invoice_hash,
            "orderId": record["orderId"],
            "amount": int(signed["amount"]),
            "deliveryDeadline": int(signed["deliveryDeadline"]),
            "expiresAt": int(signed["expiresAt"]),
            "kind": slot.kind,
            "openTx": None,
        }
        with sim.state.edit() as data:
            data["labels"][slot.label] = rec
    party = sim.party(slot.provider)
    reason = _reason(sim, b, party.agent_id, slot.kind)
    decision = Decision.ESCROW if slot.kind == "escrow" else Decision.PAY
    params = OutflowParams(
        order_id=rec["orderId"],
        payee_agent_id=party.agent_id,
        amount=rec["amount"],
        deadline=rec["deliveryDeadline"],
        mandate_id=mandate_id,
        invoice_hash=rec["invoiceHash"],
        decision_hash=chash(
            {
                "schema": "acb-sim-decision/1",
                "buyer": b.name,
                "orderId": rec["orderId"],
                "invoiceHash": rec["invoiceHash"],
                "decision": decision.name,
                "reasonCode": reason,
            }
        ),
        decision=int(decision),
        reason_code=reason,
    )
    vault = contracts(sim).vault_at(b.address)
    fn = vault.escrow(params) if slot.kind == "escrow" else vault.pay(params)
    result = send(sim, b.agent, fn)
    with sim.state.edit() as data:
        data["labels"][slot.label]["openTx"] = result.hash
        rec = dict(data["labels"][slot.label])
    return rec


def ask_delivery(sim: Simulator, ticket: Mapping[str, Any]) -> dict[str, Any]:
    """The buyer asks the provider to deliver (the same code path as ``POST /p/{key}/deliver``)."""
    return sim.deliver(
        ticket["provider"],
        {"orderId": ticket["orderId"], "invoiceId": ticket["invoiceId"], "txHash": ticket.get("openTx")},
    )


def settle(sim: Simulator, b: Buyer, ticket: Mapping[str, Any], answer: Mapping[str, Any]) -> TxResult | None:
    """Check the receipt and payload, then release the escrow (or record the delivery of a payment)."""
    party = sim.party(ticket["provider"])
    order_id = ticket["orderId"]
    digest = verify_receipt(answer["receipt"], answer["payload"], signer=party.address, order_id=order_id)
    vault = contracts(sim).vault_at(b.address)
    order = vault.get_order(order_id)
    if order.state == OrderState.ESCROWED:
        if order.claimed_at == 0 or order.delivery_hash != digest:
            raise SimError(409, {"status": "claim_mismatch", "orderId": order_id})
        return send(sim, b.agent, vault.release(order_id))
    if order.state == OrderState.PAID and order.outcome == Outcome.NONE:
        received = sim.now()
        outcome = Outcome.ON_TIME if received <= order.deadline else Outcome.LATE
        return send(sim, b.agent, vault.record_delivery(order_id, int(outcome), digest))
    return None


def _step(sim: Simulator, b: Buyer, ticket: Mapping[str, Any], *, do_settle: bool) -> str:
    order = sim.chain.order(b.address, ticket["orderId"])
    if order.state == OrderState.RELEASED or (order.state == OrderState.PAID and order.outcome != Outcome.NONE):
        return "settled"
    if order.state not in (OrderState.PAID, OrderState.ESCROWED):
        return order.state.name.lower()
    answer = ask_delivery(sim, ticket)
    if answer.get("status") != "delivered":
        return "pending"
    if do_settle:
        settle(sim, b, ticket, answer)
        return "settled"
    return "delivered"


def drive_slots(
    sim: Simulator,
    b: Buyer,
    mandate_id: str,
    slots: Iterable[Slot],
    *,
    do_settle: bool = True,
    wait_late: bool = True,
) -> list[dict[str, Any]]:
    """Open every slot, ask for delivery, wait (warp on Anvil) for late ones, settle; returns a summary."""
    from acb.chain.clock import advance_to

    tickets = [open_slot(sim, b, mandate_id, s) for s in slots]
    status = {t["label"]: _step(sim, b, t, do_settle=do_settle) for t in tickets}
    late = [t for t in tickets if status[t["label"]] == "pending" and sim.order_mode(t["orderId"]) == "late"]
    if late and wait_late:
        ready = max(int(sim.ready_at(t["orderId"]) or 0) for t in late)
        advance_to(sim.w3, ready, timeout_s=float(MAX_MANDATE_DURATION))
        for t in late:
            status[t["label"]] = _step(sim, b, t, do_settle=do_settle)
    return [summary(sim, b, t, status[t["label"]]) for t in tickets]


def summary(sim: Simulator, b: Buyer, ticket: Mapping[str, Any], step_status: str) -> dict[str, Any]:
    order = sim.chain.order(b.address, ticket["orderId"])
    with sim.state.lock:
        rec = dict(sim.state.data["orders"].get(ticket["orderId"]) or {})
    return {
        "label": ticket["label"],
        "provider": ticket["provider"],
        "orderId": ticket["orderId"],
        "invoiceHash": ticket["invoiceHash"],
        "amount": str(ticket["amount"]),
        "kind": ticket["kind"],
        "orderNumber": rec.get("n"),
        "script": rec.get("mode"),
        "state": order.state.name,
        "outcome": order.outcome.name,
        "claimedAt": order.claimed_at,
        "deadline": order.deadline,
        "step": step_status,
    }


def file_report(
    sim: Simulator,
    reporter: Party,
    *,
    subject_agent_id: int,
    kind: str,
    order_id: str,
    vault: str,
    evidence: Mapping[str, Any],
) -> tuple[TxResult, str]:
    """Write the ``acb-report/1`` payload and send ``giveFeedback`` from the reporter's key (spec 4.1, 9.5)."""
    payload = report_payload(
        kind=kind,
        subject_agent_id=subject_agent_id,
        reporter_agent_id=reporter.agent_id,
        order_id=order_id,
        evidence=evidence,
    )
    feedback_hash = chash(payload)
    write_canonical(sim.out_dir / "reports" / f"{feedback_hash}.json", payload)
    fn = contracts(sim).reputation.give_feedback(
        subject_agent_id,
        1 if kind in POSITIVE_KINDS else -1,
        0,
        REPORT_TAG,
        kind,
        report_endpoint(order_id, reporter.agent_id, vault),
        evidence_uri(sim.evidence_base, "reports", feedback_hash),
        feedback_hash,
    )
    result = send(sim, reporter.account, fn)
    sim.emit(
        "report",
        {
            "message": f"{reporter.key} reported {kind}",
            "kind": kind,
            "reporter": {"agentId": str(reporter.agent_id), "name": reporter.name or reporter.key},
            "subjectAgentId": str(subject_agent_id),
            "orderId": order_id,
            "feedbackHash": feedback_hash,
        },
        tx_hash=result.hash,
    )
    return result, feedback_hash


# ---------------------------------------------------------------------------
# actions on every network
# ---------------------------------------------------------------------------


def slot_cost(sim: Simulator, slot: Slot) -> int:
    """The invoice amount of a slot (catalogue price, or the trade price of an attack slot)."""
    unit = slot.unit_price if slot.unit_price is not None else sim.catalogue(slot.provider)[slot.sku][0]
    return unit * slot.quantity


def slot_opened(sim: Simulator, b: Buyer, slot: Slot) -> bool:
    """True when the slot's order already exists in the buyer's vault."""
    rec = sim.state.data["labels"].get(slot.label)
    return rec is not None and sim.chain.order(b.address, rec["orderId"]).state != OrderState.NONE


def _warmup_slots(sim: Simulator) -> list[Slot]:
    slots = []
    for entry in sim.cfg.market["warmup"]:
        for i in range(1, int(entry["orders"]) + 1):
            slots.append(Slot(f"warmup:{entry['persona']}:{i}", entry["persona"], entry["sku"], int(entry["quantity"])))
    return slots


def warmup(sim: Simulator, args: dict[str, Any]) -> dict[str, Any]:
    """S4: mandate MB, then the 19 warm-up orders (escrow, claim, release) under it."""
    slots = _warmup_slots(sim)
    total = sum(slot_cost(sim, s) for s in slots)
    b = buyer(sim, sim.cfg.market.get("buyer", "beacon"))
    need = sum(slot_cost(sim, s) for s in slots if not slot_opened(sim, b, s))
    b, mandate_id, mandate_tx = beacon_mandate(sim, need=need)
    orders = drive_slots(sim, b, mandate_id, slots)
    return {
        "mandateId": mandate_id,
        "mandateTx": mandate_tx.hash if mandate_tx else None,
        "vault": b.address,
        "total": str(total),
        "orders": orders,
        "settled": sum(1 for o in orders if o["state"] == "RELEASED"),
    }


def ghost_default_round(sim: Simulator, args: dict[str, Any]) -> dict[str, Any]:
    """S11: Beacon escrows with Ghost, Ghost never claims, Beacon refunds (2400) after the deadline."""
    from acb.chain.clock import advance_to

    spec = sim.cfg.market["ghost_default_round"]
    phase = args.get("phase", "all")
    if phase not in ("open", "close", "all"):
        raise ValueError("phase must be open, close or all")
    persona = spec["persona"]
    out: dict[str, Any] = {"phase": phase}
    rounds = sim.state.data["rounds"].setdefault("ghost_default_round", [])
    b = buyer(sim, sim.cfg.market.get("buyer", "beacon"))
    if phase in ("open", "all"):
        number = len(rounds) + 1
        slots = [
            Slot(f"ghost_default:{number}:{i}", persona, spec["sku"], int(spec["quantity"]))
            for i in range(1, int(spec["orders"]) + 1)
        ]
        b, mandate_id, _ = beacon_mandate(sim, need=sum(slot_cost(sim, s) for s in slots))
        out["mandateId"] = mandate_id
        opened = drive_slots(sim, b, mandate_id, slots, wait_late=False)
        with sim.state.edit() as data:
            data["rounds"].setdefault("ghost_default_round", []).append([o["orderId"] for o in opened])
        out.update({"round": number, "opened": opened})
    if phase in ("close", "all"):
        rounds = sim.state.data["rounds"].get("ghost_default_round", [])
        if not rounds:
            raise ValueError("no ghost_default_round has been opened")
        index = int(args.get("round", len(rounds))) - 1
        order_ids = rounds[index]
        vault = contracts(sim).vault_at(b.address)
        open_orders = {oid: vault.get_order(oid) for oid in order_ids}
        refundable = {
            o: info for o, info in open_orders.items() if info.state == OrderState.ESCROWED and not info.claimed_at
        }
        if refundable:
            advance_to(sim.w3, max(i.deadline for i in refundable.values()) + 1, timeout_s=3600.0)
        refunds = {}
        for oid in refundable:
            refunds[oid] = send(sim, b.agent, vault.refund(oid, 2400)).hash
        mode = spec.get("report", "first")
        reported = list(refunds)[:1] if mode == "first" else list(refunds) if mode == "each" else []
        reporter = sim.party(sim.cfg.market.get("buyer", "beacon"))
        subject = sim.party(persona)
        reports = []
        for oid in reported:
            record = next(r for r in sim.state.data["labels"].values() if r["orderId"] == oid)
            _, feedback_hash = file_report(
                sim,
                reporter,
                subject_agent_id=subject.agent_id,
                kind="NON_DELIVERY",
                order_id=oid,
                vault=b.address,
                evidence={
                    "type": "escrow_refund",
                    "vault": b.address,
                    "orderId": oid,
                    "invoiceHash": record["invoiceHash"],
                    "refundTx": refunds[oid],
                    "reasonCode": 2400,
                },
            )
            reports.append(feedback_hash)
        out.update({"refunds": refunds, "reports": reports, "closedRound": index + 1})
    return out


def _unbacked_reports(sim: Simulator, spec: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
    subject = sim.party(args.get("subject", spec["subject"]))
    kind = str(args.get("kind", spec["kind"]))
    reporters = list(args.get("reporters", spec["reporters"]))
    out = []
    for key in reporters:
        reporter = sim.party(key)
        order_id = chash({"schema": "acb-sim-unbacked/1", "kind": kind, "subject": subject.key, "reporter": key})
        vault_entry = sim.dep.vaults.get(key)
        vault = vault_entry.address if vault_entry is not None else ZERO_ADDRESS
        tx, feedback_hash = file_report(
            sim,
            reporter,
            subject_agent_id=subject.agent_id,
            kind=kind,
            order_id=order_id,
            vault=vault,
            evidence={"type": "none", "note": "no order backs this report"},
        )
        out.append({"reporter": key, "tx": tx.hash, "feedbackHash": feedback_hash})
    return {"subject": subject.key, "kind": kind, "reports": out}


def boost_ghost(sim: Simulator, args: dict[str, Any]) -> dict[str, Any]:
    """S11/A34: Echo identities post POSITIVE feedback for Ghost (weight 0 in the scorecard)."""
    return _unbacked_reports(sim, sim.cfg.market["boost_ghost"], args)


def smear_atlas(sim: Simulator, args: dict[str, Any]) -> dict[str, Any]:
    """S11/A35: Echo identities post unbacked NON_DELIVERY reports on Atlas (weight 0)."""
    return _unbacked_reports(sim, sim.cfg.market["smear_atlas"], args)


def push_spam(sim: Simulator, args: dict[str, Any]) -> dict[str, Any]:
    """S19/A46: signed invoices with an empty (or reused) ``quoteRequestId`` pushed to the wallet inbox."""
    spec = sim.cfg.market["spam"]
    persona = str(args.get("persona", spec["persona"]))
    party = sim.party(persona)
    b = sim.dep.vaults.get(str(args.get("buyer", spec["buyer"])))
    if b is None:
        raise ValueError("unknown buyer vault")
    sku = str(args.get("sku", spec["sku"]))
    catalogue = sim.catalogue(persona)
    if sku not in catalogue:
        raise ValueError(f"{persona} has no sku {sku}")
    unit, description = catalogue[sku]
    quantity = int(args.get("quantity", spec["quantity"]))
    count = int(args.get("count", spec["count"]))
    if not 1 <= count <= 50:
        raise ValueError("count must be 1..50")
    qr = str(args.get("quoteRequestId", ""))
    memo = sim.persona(persona).memo if sim.persona(persona) else ""
    pushed = []
    for _ in range(count):
        signed, invoice_hash = sim.issue_invoice(
            party,
            buyer_vault=b.address,
            buyer_agent_id=int(b.agent_id),
            lines=[(sku, description, unit, quantity)],
            quote_request_id=qr,
            memo=memo,
            extra={"unsolicited": True},
        )
        answer = sim.inbox.post("/inbox/invoice", signed)
        with sim.state.edit() as data:
            data["spam"].append(invoice_hash)
        sim.emit("message", {"message": "unsolicited invoice pushed", "from": persona, "invoiceHash": invoice_hash})
        pushed.append({"invoiceHash": invoice_hash, "invoiceId": signed["invoiceId"], "inbox": answer})
    return {"persona": persona, "pushed": pushed}


def push_duplicate(sim: Simulator, args: dict[str, Any]) -> dict[str, Any]:
    """S19/A27: re-push one already pushed invoice unchanged (default: the first one)."""
    spam = sim.state.data["spam"]
    invoice_hash = args.get("invoiceHash")
    if invoice_hash is None:
        if not spam:
            raise ValueError("nothing pushed yet: run push_spam first")
        invoice_hash = spam[int(args.get("index", 0))]
    signed = sim.load_invoice(str(invoice_hash))
    answer = sim.inbox.post("/inbox/invoice", signed)
    sim.emit("message", {"message": "duplicate invoice pushed", "invoiceHash": invoice_hash})
    return {"invoiceHash": invoice_hash, "inbox": answer}


def push_message(sim: Simulator, args: dict[str, Any]) -> dict[str, Any]:
    """A04/A48-A50: a signed ``acb-message/1`` from a persona to the wallet inbox (owner display only)."""
    persona = str(args.get("persona", "orion"))
    text = str(args.get("text", ""))
    if not text or len(text) > 2000:
        raise ValueError("text must be 1..2000 characters")
    party = sim.party(persona)
    to = sim.dep.agents.get(str(args.get("to", "wallet")))
    if to is None:
        raise ValueError("unknown recipient")
    number = sim.state.bump(persona, "messages")
    message = build_message(
        party.account,
        message_id=f"msg-{persona}-{number:04d}",
        from_agent_id=party.agent_id,
        to_agent_id=int(to.agent_id),
        sent_at=sim.now(),
        text=text,
    )
    answer = sim.inbox.post("/inbox/message", message)
    sim.emit("message", {"message": "message pushed", "from": persona, "messageId": message["messageId"]})
    return {"messageId": message["messageId"], "inbox": answer}


def ghost_appeal(sim: Simulator, args: dict[str, Any]) -> dict[str, Any]:
    """S20: Ghost's owner files ``fileAppeal(ghostId, appealHash, uri)`` with a payload claiming delivery."""
    spec = sim.cfg.market["appeal"]
    party = sim.party(str(args.get("persona", spec["persona"])))
    order_ids = args.get("orderIds")
    if order_ids is None:
        order_ids = [o for r in sim.state.data["rounds"].get("ghost_default_round", []) for o in r]
    payload = appeal_payload(
        agent_id=party.agent_id, claim=str(args.get("claim", spec["claim"])), order_ids=order_ids, filed_at=sim.now()
    )
    appeal_hash = chash(payload)
    write_canonical(sim.out_dir / "appeals" / f"{appeal_hash}.json", payload)
    acb = contracts(sim).acb
    result = send(
        sim,
        party.account,
        acb.file_appeal(party.agent_id, appeal_hash, evidence_uri(sim.evidence_base, "appeals", appeal_hash)),
    )
    appeal_id = None
    for log in result.logs:
        if log.get("event") == "AppealFiled":
            appeal_id = int(log["args"]["appealId"])
    sim.emit(
        "info",
        {"message": "appeal filed", "agentId": str(party.agent_id), "appealId": appeal_id, "appealHash": appeal_hash},
        tx_hash=result.hash,
    )
    return {"appealId": appeal_id, "appealHash": appeal_hash, "tx": result.hash, "orderIds": payload["orderIds"]}


def set_override(sim: Simulator, args: dict[str, Any]) -> dict[str, Any]:
    """Override a persona's script (attack harness): ``mode`` is ``honest`` (always on time, never
    injects), ``default`` (never delivers or claims new orders), ``inject`` (every quote of an sku with a
    template carries it) or ``none`` (back to the script). Applies to orders first seen afterwards."""
    from acb.sim.personas import OVERRIDES

    persona = str(args.get("persona", ""))
    sim.party(persona)
    mode = args.get("mode")
    if mode not in (*OVERRIDES, "none", None):
        raise ValueError(f"mode must be one of {', '.join(OVERRIDES)} or none")
    with sim.state.edit() as data:
        if mode in ("none", None):
            data["overrides"].pop(persona, None)
        else:
            data["overrides"][persona] = mode
    sim.emit("info", {"message": "persona script overridden", "persona": persona, "mode": mode or "none"})
    return {"persona": persona, "override": None if mode in ("none", None) else mode}


def status(sim: Simulator, args: dict[str, Any]) -> dict[str, Any]:
    """Counters, overrides, spam and attack entities (no transaction)."""
    with sim.state.lock:
        data = sim.state.data
        return {
            "counters": data["counters"],
            "overrides": data["overrides"],
            "spam": list(data["spam"]),
            "rounds": data["rounds"],
            "entities": data["entities"],
            "vaults": data["vaults"],
            "mandates": data["mandates"],
        }


MARKET_ACTIONS: dict[str, Action] = {
    "warmup": warmup,
    "ghost_default_round": ghost_default_round,
    "boost_ghost": boost_ghost,
    "smear_atlas": smear_atlas,
    "push_spam": push_spam,
    "push_duplicate": push_duplicate,
    "push_message": push_message,
    "ghost_appeal": ghost_appeal,
    "set_override": set_override,
    "status": status,
}
"""Actions that run on every network (the attack actions are in :mod:`acb.sim.attacks`)."""


__all__ = [
    "MARKET_ACTIONS",
    "Buyer",
    "Slot",
    "beacon_mandate",
    "buyer",
    "drive_slots",
    "ensure_mandate",
    "file_report",
    "open_slot",
    "send",
    "settle",
    "slot_cost",
    "slot_opened",
]
