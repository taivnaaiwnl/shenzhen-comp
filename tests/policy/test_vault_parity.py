"""Policy/vault parity on Anvil (spec 6.7, A57).

For every grade x tier x amount boundary: a PAY or ESCROW outcome of ``decide`` passes ``eth_call`` of
the vault function from the agent key, and every amount the policy sends to owner approval because it is
above a cap the vault enforces (tier 0, per-tx cap, new-payee cap, grade masks) reverts in the vault.

Tiers 2 and 3 are emulated by the owner writing their configs into tier 1 (``setTierConfig``): the matrix
is generic over the config read from the vault and uses the tier number only for "tier 0". Tier 0 is the
real tier 0 (``setTier`` by the autonomy operator).

Cap re-quotes (capOf set) of the B payee are included: their executable PAY_CAPPED is a direct payment,
so the exposure limit (C14) applies and turns a re-quote above the B limit into ESCROW 2009.

Known, intended gap (policy stricter than the vault): the B-grade cap (half the per-tx cap) exists only
in the policy, so a B amount between the B cap and the per-tx cap goes to approval or a capped re-quote
although the vault would accept it.
"""

from __future__ import annotations

import pytest
from policy_factory import CFG, TIERS, U

from acb.canonical import chash
from acb.chain import clock
from acb.chain.contracts import OutflowParams, bind
from acb.chain.tx import TxSender
from acb.keys import KeyRing
from acb.policy import decide
from acb.policy.context import build_payee_view, build_policy_context, build_validation_context
from acb.policy.invoice import new_quote_request, superseded_key, validate_invoice
from acb.policy.matrix import grade_cap
from acb.types import Decision, Grade, ValidatedInvoice, mask_has

pytestmark = pytest.mark.chain

H = "0x" + "11" * 32
GRADES = {
    "atlas": Grade.A,
    "slowpoke": Grade.B,
    "orion": Grade.C,
    "nova": Grade.NEW,
    "blaster": Grade.NONE,
    "ghost": Grade.D,
}
SCORES = {Grade.A: 760, Grade.B: 680, Grade.C: 600, Grade.NEW: 600, Grade.D: 420}
UNIT = 10_000


def _send(w3, keys, role, fn):
    result = TxSender(w3, keys.account(role)).send(fn)
    assert result.status == 1, result.revert_error
    return result


def _amounts(grade: Grade, tier: int) -> list[int]:
    cfg = TIERS[tier]
    cap = grade_cap(grade, cfg, CFG.b_cap_bps)
    candidates = (100_000, 200_000, cfg.per_tx + UNIT, cap - UNIT, cap, cap + UNIT, cfg.new_payee_cap + UNIT)
    return sorted({a for a in candidates if a >= 100_000})


@pytest.fixture
def chain(anvil, fresh_deployed):
    _, w3 = anvil
    keys = KeyRing.anvil()
    dep = fresh_deployed
    c = bind(w3, dep)
    vault = c.vault("wallet")
    ids = [int(dep.agents[n].agent_id) for n in GRADES]
    wallets = [c.identity.get_agent_wallet(i) for i in ids]
    mandate_id = chash({"parity": "mandate"})
    now = clock.now(w3)
    _send(w3, keys, "owner", vault.register_mandate(mandate_id, ids, wallets, 10 * U, 50 * U, now + 86_400, False, H))
    for name, grade in GRADES.items():
        if grade == Grade.NONE:
            continue
        aid = int(dep.agents[name].agent_id)
        as_of = w3.eth.block_number - 1
        evidence = chash({"parity": name})
        _send(
            w3,
            keys,
            "bureau",
            c.acb.post_grade(aid, int(grade), SCORES[grade], [1100], "acb-sc-1.0.0", H, evidence, as_of, "e.json"),
        )
    clock.warp(w3, c.acb.upgrade_delay() + 1)
    return w3, keys, dep, c, vault, mandate_id


def _context(w3, dep, c, vault, mandate_id, name: str, amount: int, row: int, state, mview, cap_of: str = ""):
    aid = int(dep.agents[name].agent_id)
    now = clock.now(w3)
    qr = new_quote_request(
        mandate_id=mandate_id,
        issuer_agent_id=aid,
        sku="unit",
        quantity=amount // UNIT,
        now=now,
        cfg=CFG,
        cap_of=cap_of,
    )
    raw = {
        "schema": "acb-invoice/1",
        "invoiceId": f"inv-parity-{row}",
        "quoteRequestId": qr.quote_request_id,
        "capOf": cap_of,
        "issuerAgentId": str(aid),
        "buyerAgentId": str(int(dep.vaults["wallet"].agent_id)),
        "buyerVault": vault.address,
        "chainId": int(dep.chain_id),
        "token": dep.contracts["USDC"].address,
        "items": [{"sku": "unit", "description": "parity", "unitPrice": str(UNIT), "quantity": amount // UNIT}],
        "amount": str(amount),
        "issuedAt": now,
        "expiresAt": now + 600,
        "deliveryDeadline": now + 300,
        "memo": "",
        "signature": "0x" + "00" * 65,
    }
    live = c.identity.get_agent_wallet(aid)
    vctx = build_validation_context(
        now=now,
        chain_id=dep.chain_id,
        usdc=dep.contracts["USDC"].address,
        vault_address=vault.address,
        vault_agent_id=dep.vaults["wallet"].agent_id,
        catalogue={aid: {"unit": UNIT}},
        quote_requests=(qr,),
        pinned_wallets={aid: live},
        live_wallets={aid: live},
        superseded={superseded_key(aid, cap_of): amount // UNIT} if cap_of else {},
    )
    vi = validate_invoice(raw, vctx, CFG, check_signature=False)
    assert isinstance(vi, ValidatedInvoice), vi
    grade = c.acb.effective_grade(aid, state.max_grade_age)
    payee = build_payee_view(agent_id=aid, grade=grade, live_wallet=live, pinned_wallet=live, name=name)
    ctx = build_policy_context(
        now=now,
        chain_id=dep.chain_id,
        usdc=dep.contracts["USDC"].address,
        vault=state,
        payee=payee,
        invoice=vi,
        invoice_sig_valid=True,
        mandate=mview,
    )
    return ctx, grade


def test_policy_outcomes_are_accepted_or_reverted_by_the_vault(chain):
    w3, keys, dep, c, vault, mandate_id = chain
    agent = TxSender(w3, keys.account("agent_signer"))
    checked = {"accepted": 0, "reverted": 0, "policy_stricter": 0, "requote": 0, "requote_escrow": 0}
    row = 0
    for tier in (1, 2, 3, 0):
        if tier == 0:
            _send(w3, keys, "owner", vault.set_tier_config(1, TIERS[1]))
            _send(w3, keys, "autonomy", vault.set_tier(0, 3105, H))
        else:
            _send(w3, keys, "owner", vault.set_tier_config(1, TIERS[tier]))
        state, mview = vault.state(), vault.mandate_view(mandate_id)
        # every grade's plain invoices, plus B cap re-quotes (capOf set): their executable PAY_CAPPED is
        # a direct payment, and above the B exposure limit C14 turns it into ESCROW 2009
        cases = [(name, g, a, "") for name, g in GRADES.items() for a in _amounts(g, tier)]
        cases += [("slowpoke", Grade.B, a, "inv-parity-orig") for a in _amounts(Grade.B, tier)]
        for name, expected_grade, amount, cap_of in cases:
            row += 1
            ctx, grade = _context(w3, dep, c, vault, mandate_id, name, amount, row, state, mview, cap_of)
            assert grade == expected_grade
            d = decide(None, ctx, CFG)
            cfg = ctx.vault.config
            pay_amount = d.amount
            params = OutflowParams(
                ctx.order_id,
                ctx.payee.agent_id,
                pay_amount,
                clock.now(w3) + 300,
                mandate_id,
                H,
                d.decision_hash,
                int(d.outcome) if d.outcome in (Decision.PAY, Decision.PAY_CAPPED, Decision.ESCROW) else 1,
                d.reasons[0],
            )
            label = f"tier {tier} {grade.name} {amount}: {d.outcome.name} {d.reasons}"
            if cap_of and d.outcome in (Decision.PAY_CAPPED, Decision.ESCROW):
                checked["requote"] += 1
                checked["requote_escrow"] += int(d.outcome == Decision.ESCROW and d.reasons[0] == 2009)
            if d.outcome in (Decision.PAY, Decision.PAY_CAPPED):
                # PAY_CAPPED is the preview (the re-quoted q' units are paid with pay()) or, with capOf,
                # the executable cap re-quote
                assert agent.simulate(vault.pay(params)) is None, label
                checked["accepted"] += 1
            elif d.outcome == Decision.ESCROW:
                escrow = OutflowParams(
                    ctx.order_id,
                    ctx.payee.agent_id,
                    pay_amount,
                    clock.now(w3) + 300,
                    mandate_id,
                    H,
                    d.decision_hash,
                    int(Decision.ESCROW),
                    d.reasons[0],
                )
                assert agent.simulate(vault.escrow(escrow)) is None, label
                checked["accepted"] += 1
            else:
                vault_cap_exceeded = (
                    tier == 0
                    or grade == Grade.D
                    or amount > cfg.per_tx
                    or (grade in (Grade.NEW, Grade.NONE) and amount > cfg.new_payee_cap)
                    or not (mask_has(cfg.pay_mask, grade) or mask_has(cfg.escrow_mask, grade))
                )
                if vault_cap_exceeded:
                    pay_p = OutflowParams(
                        ctx.order_id, ctx.payee.agent_id, amount, clock.now(w3) + 300, mandate_id, H, H, 1, 2000
                    )
                    esc_p = OutflowParams(
                        ctx.order_id, ctx.payee.agent_id, amount, clock.now(w3) + 300, mandate_id, H, H, 3, 2004
                    )
                    assert agent.simulate(vault.pay(pay_p)) is not None, label
                    assert agent.simulate(vault.escrow(esc_p)) is not None, label
                    checked["reverted"] += 1
                else:
                    assert grade == Grade.B, label  # the only policy-only cap
                    checked["policy_stricter"] += 1
    assert checked["accepted"] >= 40 and checked["reverted"] >= 30 and checked["requote"] >= 6, checked
    assert checked["requote_escrow"] >= 2, checked  # C14 on cap re-quotes (tiers 2 and 3)
