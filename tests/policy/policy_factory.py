"""Hand-built contexts for the pure policy tests (no chain). Values follow spec 5.1, 9.1 and 9.2.

Everything here is test data: deterministic keys derived from fixed labels, one vault, a mandate with
the six demo payees and the wallet tier profile of 5.1 written out by hand (not read from the chain).
"""

from __future__ import annotations

from typing import Any

from eth_account import Account
from web3 import Web3

from acb.canonical import invoice_hash, order_id, sign_invoice
from acb.policy import PolicyConfig, validate_invoice
from acb.policy.context import build_payee_view, build_policy_context, build_validation_context
from acb.policy.invoice import new_quote_request
from acb.types import (
    Grade,
    MandateView,
    OrderState,
    Proposal,
    QuoteRequest,
    TierConfig,
    ToolAction,
    ValidatedInvoice,
    VaultState,
    grade_mask,
)

CHAIN_ID = 31337
USDC = "0x" + "a1" * 20
VAULT = "0x" + "b2" * 20
OWNER = "0x" + "c3" * 20
AGENT = "0x" + "d4" * 20
WALLET_AGENT_ID = 0
NOW = 1_800_000_000
MANDATE_ID = "0x" + "11" * 32
PURPOSE = "0x" + "22" * 32
U = 1_000_000  # 1.00 USDC


def key_of(label: str) -> Any:
    return Account.from_key(Web3.keccak(text=f"acb-test-{label}"))


PERSONAS: dict[str, tuple[int, str, dict[str, int]]] = {
    # key: (agentId, name, catalogue)
    "atlas": (5, "Atlas Data", {"fx-live-15m": 200_000}),
    "slowpoke": (6, "Slowpoke Analytics", {"fx-history-day": 200_000, "fx-history-day-promo": 100_000}),
    "ghost": (7, "Ghost Feeds", {"fx-backup-15m": 200_000}),
    "blaster": (8, "Blaster Ads", {"ad-slot": 500_000}),
    "orion": (9, "Orion API", {"api-bundle-1k": 200_000, "api-priority-1k": 300_000}),
    "nova": (10, "Nova Labs", {"fx-sample": 400_000}),
    "attacker": (16, "Treasury Helper", {}),
    "test": (20, "Test Provider", {"unit": 10_000, "unit20": 200_000}),
}
KEYS = {name: key_of(name) for name in PERSONAS}
WALLETS = {PERSONAS[name][0]: KEYS[name].address.lower() for name in PERSONAS}
AGENT_IDS = {name: PERSONAS[name][0] for name in PERSONAS}
CATALOGUE = {aid: cat for aid, _, cat in PERSONAS.values() if cat}

A, B, C, NEW, NONE, D = Grade.A, Grade.B, Grade.C, Grade.NEW, Grade.NONE, Grade.D

TIERS: dict[int, TierConfig] = {
    0: TierConfig(per_tx=0, window_cap=0, new_payee_cap=0, max_tx=0, pay_mask=0, escrow_mask=0, min_clean_to_promote=3),
    1: TierConfig(
        per_tx=1 * U,
        window_cap=5 * U,
        new_payee_cap=U // 2,
        max_tx=10,
        pay_mask=grade_mask(A, B),
        escrow_mask=grade_mask(A, B, C, NEW, NONE),
        min_clean_to_promote=3,
    ),
    2: TierConfig(
        per_tx=2 * U,
        window_cap=10 * U,
        new_payee_cap=1 * U,
        max_tx=20,
        pay_mask=grade_mask(A, B),
        escrow_mask=grade_mask(A, B, C, NEW, NONE),
        min_clean_to_promote=6,
    ),
    3: TierConfig(
        per_tx=4 * U,
        window_cap=20 * U,
        new_payee_cap=1 * U,
        max_tx=32,
        pay_mask=grade_mask(A, B, NEW),
        escrow_mask=grade_mask(A, B, C, NEW, NONE),
        min_clean_to_promote=0,
    ),
}
ZERO_TIER = TierConfig(
    per_tx=0, window_cap=0, new_payee_cap=0, max_tx=0, pay_mask=0, escrow_mask=0, min_clean_to_promote=0
)

CFG = PolicyConfig.load()


def vault_state(
    *,
    tier: int = 1,
    frozen: bool = False,
    balance: int = 15 * U,
    escrow_locked: int = 0,
    reserve_floor: int = 3 * U,
    window_spent: int = 0,
    window_count: int = 0,
    config: TierConfig | None = None,
) -> VaultState:
    if frozen:
        tier_value, cfg = 255, ZERO_TIER
    else:
        tier_value, cfg = tier, config or TIERS[tier]
    return VaultState(
        address=VAULT,
        agent_id=WALLET_AGENT_ID,
        owner=OWNER,
        agent=AGENT,
        tier=tier_value,
        frozen=frozen,
        tier_before_freeze=tier if frozen else 0,
        max_auto_tier=2,
        config=cfg,
        balance=balance,
        escrow_locked=escrow_locked,
        reserve_floor=reserve_floor,
        window_spent=window_spent,
        window_count=window_count,
        clean_since_change=0,
        violation_points=0,
        last_tier_change_at=NOW - 1000,
        max_grade_age=604800,
        dispute_window=120,
    )


def mandate(
    *,
    payees: tuple[str, ...] = ("atlas", "slowpoke", "ghost", "orion", "nova", "test"),
    max_per_tx: int = 1 * U,
    max_total: int = 4 * U,
    spent: int = 0,
    valid_until: int = NOW + 86_400,
    recurring: bool = False,
    active: bool = True,
    mandate_id: str = MANDATE_ID,
    vault: str = VAULT,
    wallets: dict[int, str] | None = None,
) -> MandateView:
    ids = tuple(AGENT_IDS[p] for p in payees)
    wl = wallets or WALLETS
    return MandateView(
        mandate_id=mandate_id,
        vault=vault,
        payee_agent_ids=ids,
        payee_wallets=tuple(wl[i] for i in ids),
        max_per_tx=max_per_tx,
        max_total=max_total,
        spent=spent,
        valid_until=valid_until,
        recurring=recurring,
        active=active,
        purpose_hash=PURPOSE,
        register_tx=None,
    )


def quote_request(
    issuer: str = "atlas",
    *,
    sku: str | None = None,
    quantity: int = 4,
    cap_of: str = "",
    mandate_id: str = MANDATE_ID,
    qr_id: str | None = None,
    created_at: int = NOW - 10,
    used_by: str | None = None,
) -> QuoteRequest:
    aid = AGENT_IDS[issuer]
    qr = new_quote_request(
        mandate_id=mandate_id,
        issuer_agent_id=aid,
        sku=sku or next(iter(CATALOGUE[aid])),
        quantity=quantity,
        now=created_at,
        cfg=CFG,
        cap_of=cap_of,
        quote_request_id=qr_id or "qr-" + Web3.keccak(text=f"{issuer}-{sku}-{quantity}-{cap_of}").hex()[:32],
    )
    return qr.model_copy(update={"used_by": used_by}) if used_by else qr


def invoice(
    issuer: str = "atlas",
    *,
    qr: QuoteRequest | None = None,
    sku: str | None = None,
    quantity: int | None = None,
    unit_price: int | None = None,
    invoice_id: str | None = None,
    memo: str = "Thanks for your order.",
    description: str = "Data slice",
    issued_at: int = NOW - 5,
    validity: int = 600,
    delivery: int = 60,
    signer: str | None = None,
    sign: bool = True,
    **overrides: Any,
) -> dict[str, Any]:
    """A signed wire invoice (spec 9.2) answering ``qr``; ``overrides`` replace wire fields before signing."""
    aid = AGENT_IDS[issuer]
    qr = qr or quote_request(issuer, sku=sku, quantity=quantity or 4)
    sku = sku or qr.sku
    quantity = quantity if quantity is not None else qr.quantity
    unit = unit_price if unit_price is not None else CATALOGUE[aid][sku]
    body = {
        "schema": "acb-invoice/1",
        "invoiceId": invoice_id or f"inv-{issuer}-{quantity:04d}-{sku[:6]}",
        "quoteRequestId": qr.quote_request_id,
        "capOf": qr.cap_of,
        "issuerAgentId": str(aid),
        "buyerAgentId": str(WALLET_AGENT_ID),
        "buyerVault": VAULT,
        "chainId": CHAIN_ID,
        "token": USDC,
        "items": [{"sku": sku, "description": description, "unitPrice": str(unit), "quantity": quantity}],
        "amount": str(unit * quantity),
        "issuedAt": issued_at,
        "expiresAt": issued_at + validity,
        "deliveryDeadline": issued_at + delivery,
        "memo": memo,
    }
    body.update(overrides)
    if not sign:
        return {**body, "signature": "0x" + "00" * 65}
    return sign_invoice(KEYS[signer or issuer].key, body)


def vctx(
    *quote_requests: QuoteRequest,
    now: int = NOW,
    superseded: dict | None = None,
    live_wallets: dict[int, str] | None = None,
    pinned_wallets: dict[int, str] | None = None,
    catalogue: dict | None = None,
):
    return build_validation_context(
        now=now,
        chain_id=CHAIN_ID,
        usdc=USDC,
        vault_address=VAULT,
        vault_agent_id=WALLET_AGENT_ID,
        catalogue=catalogue if catalogue is not None else CATALOGUE,
        quote_requests=quote_requests,
        pinned_wallets=pinned_wallets if pinned_wallets is not None else WALLETS,
        live_wallets=live_wallets if live_wallets is not None else WALLETS,
        superseded=superseded or {},
    )


def validated(raw: dict[str, Any], qr: QuoteRequest, *, check_signature: bool = True, **kw: Any) -> ValidatedInvoice:
    result = validate_invoice(raw, vctx(qr, **kw), CFG, check_signature=check_signature)
    assert isinstance(result, ValidatedInvoice), result
    return result


def proposal(
    raw: dict[str, Any] | None,
    action: ToolAction | str | None = ToolAction.PAY,
    *,
    payee: int | None = None,
    amount: int | None = None,
    source: str = "llm",
    tool: str = "decide_invoice",
    turn: str = "invoice",
    raw_args: dict | None = None,
) -> Proposal:
    payee = payee if payee is not None else (int(raw["issuerAgentId"]) if raw else None)
    amount = amount if amount is not None else (int(raw["amount"]) if raw else None)
    action = ToolAction(action) if action is not None else None
    if raw_args is None:
        raw_args = {
            "invoice_id": raw["invoiceId"] if raw else "x",
            "action": action.value if action else None,
            "payee_agent_id": payee,
            "amount_usdc": units_to_usdc_str(amount) if amount is not None else None,
            "rationale": "test",
        }
    return Proposal(
        source=source,
        turn=turn,
        tool=tool,
        action=action,
        invoice_id=raw["invoiceId"] if raw else None,
        payee_agent_id=payee,
        amount=amount,
        raw_args=raw_args,
        raw_text=None,
        llm_model="mock",
    )


def units_to_usdc_str(units: int) -> str:
    whole, frac = divmod(int(units), U)
    return f"{whole}.{frac // 10_000:02d}"


def context(
    raw: dict[str, Any],
    qr: QuoteRequest,
    *,
    grade: Grade = A,
    vault: VaultState | None = None,
    mandate_view: MandateView | None | str = "default",
    sig_valid: bool = True,
    order_state: OrderState = OrderState.NONE,
    pending: tuple[str, ...] = (),
    seen_conflict: bool = False,
    denylisted: bool = False,
    exposure: int = 0,
    history: tuple[int, ...] = (),
    live_wallet: str | None = "default",
    now: int = NOW,
    superseded: dict | None = None,
    order: str | None = None,
    check_signature: bool = True,
):
    """A PolicyContext for ``raw``. ``check_signature=False`` skips the (slow, pure-Python) signature
    recovery for table-driven tests whose invoices are built unsigned; ``sig_valid`` then stands in."""
    issuer = int(raw["issuerAgentId"])
    m = mandate() if mandate_view == "default" else mandate_view
    vi = validated(raw, qr, superseded=superseded, check_signature=check_signature)
    payee = build_payee_view(
        agent_id=issuer,
        grade=grade,
        live_wallet=WALLETS.get(issuer) if live_wallet == "default" else live_wallet,
        pinned_wallet=WALLETS.get(issuer),
        name="payee",
        unsecured_exposure=exposure,
        payment_history=history,
    )
    return build_policy_context(
        now=now,
        chain_id=CHAIN_ID,
        usdc=USDC,
        vault=vault or vault_state(),
        payee=payee,
        invoice=vi,
        invoice_sig_valid=sig_valid,
        mandate=m,
        order_state=order_state,
        pending_order_ids=pending,
        seen_invoice_conflict=seen_conflict,
        denylisted=denylisted,
        order_id=order,
    )


def order_of(raw: dict[str, Any]) -> str:
    return order_id(CHAIN_ID, VAULT, int(raw["issuerAgentId"]), raw["invoiceId"])


def hash_of(raw: dict[str, Any]) -> str:
    return invoice_hash(raw)
