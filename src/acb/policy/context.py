"""Pure builders of the policy's input contexts (spec 6.1, Appendix B).

``ValidationContext`` (stage 1) and ``PolicyContext`` (stage 2) are frozen models that hold every chain
value and the time, so ``validate_invoice`` and ``decide`` stay pure. The functions here only assemble
and derive values (order ids, exposure, payment history) from data that was already read; the chain
reads themselves live in :mod:`acb.policy.chainview`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from acb.canonical import order_id as derive_order_id
from acb.policy.invoice import superseded_key
from acb.policy.mandate import pinned_wallet
from acb.types import (
    Deployments,
    Grade,
    MandateView,
    OrderState,
    PayeeView,
    PolicyContext,
    QuoteRequest,
    ValidatedInvoice,
    ValidationContext,
    VaultState,
)


def catalogue_from_deployments(dep: Deployments) -> dict[int, dict[str, int]]:
    """The pinned catalogue (13.5 ``agents[*].skus``) keyed by agentId: ``{agentId: {sku: unitPrice}}``."""
    return {int(a.agent_id): {sku: int(price) for sku, price in a.skus.items()} for a in dep.agents.values() if a.skus}


def display_name(dep: Deployments | None, agent_id: int) -> str:
    """The pinned directory name of ``agent_id`` (never a counterparty-supplied name)."""
    if dep is not None:
        for entry in dep.agents.values():
            if int(entry.agent_id) == int(agent_id):
                return entry.name
    return f"agent #{int(agent_id)}"


def pinned_wallets_for(quote_requests: Iterable[QuoteRequest], mandates: Mapping[str, MandateView]) -> dict[int, str]:
    """Issuer -> wallet pinned in the mandate of its quote request (V09). An issuer whose quote requests
    point at mandates that pin different wallets gets none (most restrictive)."""
    out: dict[int, str] = {}
    conflict: set[int] = set()
    for qr in quote_requests:
        m = mandates.get(qr.mandate_id.lower())
        wallet = pinned_wallet(m, int(qr.issuer_agent_id))
        if wallet is None:
            continue
        issuer = int(qr.issuer_agent_id)
        if issuer in out and out[issuer] != wallet:
            conflict.add(issuer)
        out[issuer] = wallet
    return {k: v for k, v in out.items() if k not in conflict}


def build_validation_context(
    *,
    now: int,
    chain_id: int,
    usdc: str,
    vault_address: str,
    vault_agent_id: int,
    catalogue: Mapping[int, Mapping[str, int]],
    quote_requests: Iterable[QuoteRequest],
    pinned_wallets: Mapping[int, str],
    live_wallets: Mapping[int, str | None],
    superseded: Mapping[tuple[int, str], int] | Mapping[str, int] | None = None,
) -> ValidationContext:
    """Assemble a :class:`~acb.types.ValidationContext`.

    ``superseded`` maps ``(issuerAgentId, invoiceId)`` (or the string key of
    :func:`acb.policy.invoice.superseded_key`) to the re-quote quantity q' computed by code (6.5).
    Live wallets that are unset (``None`` or the zero address) are left out.
    """
    sup: dict[str, int] = {}
    for key, q in (superseded or {}).items():
        sup[superseded_key(*key) if isinstance(key, tuple) else str(key)] = int(q)
    zero = "0x" + "00" * 20
    return ValidationContext(
        now=int(now),
        chain_id=int(chain_id),
        usdc=usdc,
        vault_address=vault_address,
        vault_agent_id=int(vault_agent_id),
        catalogue={int(k): {s: int(p) for s, p in v.items()} for k, v in catalogue.items()},
        quote_requests=tuple(quote_requests),
        superseded=sup,
        pinned_wallets={int(k): v.lower() for k, v in pinned_wallets.items()},
        live_wallets={int(k): v.lower() for k, v in live_wallets.items() if v and v.lower() != zero},
    )


def unsecured_exposure(paid: Iterable[Mapping[str, Any]], delivered_order_ids: Iterable[str]) -> int:
    """Sum of ``Paid`` amounts to one payee whose order has no delivery recorded (6.5 C14).

    ``paid`` holds ``{"orderId", "amount"}`` items (from ``Paid`` logs filtered by payee);
    ``delivered_order_ids`` the order ids settled by a delivery (``DeliveryRecorded`` with outcome ON_TIME
    or LATE; a FAILED order stays exposed, see :meth:`acb.policy.chainview.ChainView.payee_logs`).
    """
    delivered = {o.lower() for o in delivered_order_ids}
    return sum(int(p["amount"]) for p in paid if str(p["orderId"]).lower() not in delivered)


def payment_history(paid: Iterable[Mapping[str, Any]]) -> tuple[int, ...]:
    """Amounts of previous direct payments to one payee, oldest first (C17)."""
    return tuple(int(p["amount"]) for p in paid)


def build_payee_view(
    *,
    agent_id: int,
    grade: Grade,
    live_wallet: str | None,
    pinned_wallet: str | None,
    name: str,
    score: int | None = None,
    posted_at: int | None = None,
    registered_at: int | None = None,
    unsecured_exposure: int = 0,
    payment_history: Iterable[int] = (),
) -> PayeeView:
    zero = "0x" + "00" * 20
    return PayeeView(
        agent_id=int(agent_id),
        grade=Grade(grade),
        score=score,
        posted_at=posted_at,
        live_wallet=None if not live_wallet or live_wallet.lower() == zero else live_wallet.lower(),
        pinned_wallet=pinned_wallet.lower() if pinned_wallet else None,
        display_name=name,
        registered_at=registered_at,
        unsecured_exposure=int(unsecured_exposure),
        payment_history=tuple(int(a) for a in payment_history),
    )


def build_policy_context(
    *,
    now: int,
    chain_id: int,
    usdc: str,
    vault: VaultState,
    payee: PayeeView,
    invoice: ValidatedInvoice | None,
    invoice_sig_valid: bool,
    mandate: MandateView | None,
    order_state: OrderState = OrderState.NONE,
    pending_order_ids: Iterable[str] = (),
    seen_invoice_conflict: bool = False,
    denylisted: bool = False,
    order_id: str | None = None,
) -> PolicyContext:
    """Assemble a :class:`~acb.types.PolicyContext`; the order id is derived from the invoice (6.6)
    unless given (an inconsistent given id makes C04 fail)."""
    if order_id is None:
        if invoice is None:
            raise ValueError("order_id is required without an invoice")
        inv = invoice.signed.invoice
        order_id = derive_order_id(chain_id, vault.address, int(inv.issuer_agent_id), inv.invoice_id)
    return PolicyContext(
        now=int(now),
        chain_id=int(chain_id),
        usdc=usdc,
        vault=vault,
        payee=payee,
        invoice=invoice,
        invoice_sig_valid=bool(invoice_sig_valid),
        mandate=mandate,
        order_id=order_id,
        order_state=OrderState(order_state),
        pending_order_ids=frozenset(o.lower() for o in pending_order_ids),
        seen_invoice_conflict=bool(seen_invoice_conflict),
        denylisted=bool(denylisted),
    )


__all__ = [
    "build_payee_view",
    "build_policy_context",
    "build_validation_context",
    "catalogue_from_deployments",
    "display_name",
    "payment_history",
    "pinned_wallets_for",
    "unsecured_exposure",
]
