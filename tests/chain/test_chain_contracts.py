"""Typed wrappers on the session deployment: reads return Appendix B views, writes are call builders."""

from __future__ import annotations

import pytest
from web3 import Web3

from acb.canonical import chash
from acb.chain import clock
from acb.chain.contracts import (
    DecisionParams,
    OutflowParams,
    b32,
    hex32,
    model_id_bytes,
    model_id_text,
    pack_reasons,
    unpack_reasons,
)
from acb.chain.deploy import load_tiers
from acb.types import (
    Decision,
    Grade,
    GradeView,
    MandateView,
    OrderState,
    Outcome,
    VaultState,
    grade_mask,
)

pytestmark = pytest.mark.chain

H = "0x" + "11" * 32


def test_vault_state_view_matches_tiers_yaml(chain):
    vault = chain.c.vault("wallet")
    state = vault.state()
    assert isinstance(state, VaultState)
    wallet = load_tiers("demo").profiles["wallet"]
    assert state.tier == wallet.initial_tier == 1
    assert state.config == wallet.tiers[1]
    assert state.config.pay_mask == grade_mask(Grade.A, Grade.B)
    assert state.agent_id == chain.dep.agents["wallet"].agent_id
    assert state.owner == chain.keys.address("owner") and state.agent == chain.keys.address("agent_signer")
    assert state.balance == 15_000_000 and state.reserve_floor == 3_000_000
    assert state.max_auto_tier == 2 and state.dispute_window == 120 and state.max_grade_age == 604800
    assert not state.frozen and state.window_spent == 0 and state.window_count == 0
    assert vault.free_balance() == 12_000_000
    assert vault.token() == chain.c.usdc.address and vault.bureau() == chain.c.acb.address
    assert vault.identity() == chain.c.identity.address
    assert vault.autonomy_operator() == chain.keys.address("autonomy")
    assert vault.window_seconds() == 86400 and vault.promotion_cooldown() == 60
    assert vault.ring_size() == 32 and vault.max_mandate_payees() == 6
    beacon = chain.c.vault("beacon").state()
    assert beacon.reserve_floor == 0 and beacon.config.pay_mask & (1 << Grade.NONE)


def test_frozen_vault_state_has_zero_config(chain):
    vault = chain.c.vault("wallet")
    chain.sender("owner").send(vault.freeze(3200, H))
    state = vault.state()
    assert state.frozen and state.tier == 255 and state.tier_before_freeze == 1
    assert state.config.per_tx == 0 and state.config.pay_mask == 0
    assert vault.is_frozen()


def test_mandate_escrow_claim_release_through_wrappers(chain):
    vault = chain.c.vault("wallet")
    atlas = chain.dep.agents["atlas"]
    now = clock.now(chain.w3)
    mandate_id = chash({"test": "mandate", "salt": 1})
    purpose = chash({"purpose": "fx data"})
    r = chain.sender("owner").send(
        vault.register_mandate(
            mandate_id, [atlas.agent_id], [atlas.wallet], 1_000_000, 4_000_000, now + 3600, False, purpose
        )
    )
    assert r.status == 1 and any(log["event"] == "MandateRegistered" for log in r.logs)
    view = vault.mandate_view(mandate_id)
    assert isinstance(view, MandateView)
    assert view.payee_agent_ids == (atlas.agent_id,) and view.payee_wallets == (atlas.wallet,)
    assert view.active and view.max_total == 4_000_000 and view.purpose_hash == purpose and view.register_tx == r.hash
    assert vault.mandate_payee(mandate_id, atlas.agent_id) == atlas.wallet
    assert vault.mandate_view("0x" + "77" * 32) is None

    order_id = chash({"order": 1})
    params = OutflowParams(order_id, atlas.agent_id, 200_000, now + 600, mandate_id, H, H, int(Decision.ESCROW), 2004)
    r = chain.sender("agent_signer").send(vault.escrow(params))
    assert r.status == 1
    order = vault.get_order(order_id)
    assert order.state == OrderState.ESCROWED and order.amount == 200_000 and order.payee == atlas.wallet
    assert order.mandate_id == mandate_id and order.decision == Decision.ESCROW
    assert vault.escrow_locked() == 200_000 and vault.get_mandate(mandate_id).spent == 200_000

    delivery = chash({"delivery": 1})
    chain.sender("atlas").send(vault.claim_delivered(order_id, delivery))
    chain.sender("agent_signer").send(vault.release(order_id))
    order = vault.get_order(order_id)
    assert order.state == OrderState.RELEASED and order.outcome == Outcome.ON_TIME and order.delivery_hash == delivery
    assert chain.c.usdc.balance_of(atlas.wallet) >= 200_000


def test_record_decision_and_approval_views(chain):
    vault = chain.c.vault("wallet")
    nova = chain.dep.agents["nova"]
    order_id = chash({"order": "refused"})
    params = DecisionParams(order_id, nova.agent_id, 400_000, int(Decision.REFUSE), 2100, H, H)
    r = chain.sender("agent_signer").send(vault.record_decision(params))
    assert r.status == 1 and r.logs[0]["event"] == "DecisionRecorded"
    assert vault.get_order(order_id).state == OrderState.REFUSED
    approval_id = chash({"order": "approved"})
    expiry = clock.now(chain.w3) + 900
    chain.sender("owner").send(vault.owner_approve(approval_id, nova.agent_id, nova.wallet, 400_000, expiry, False, 0))
    approval = vault.get_approval(approval_id)
    assert (
        approval.exists and approval.expected_payee == nova.wallet and not approval.used and approval.expiry == expiry
    )
    assert not vault.get_approval(chash({"order": "none"})).exists


def test_grade_post_and_grade_view(chain):
    acb = chain.c.acb
    atlas = chain.dep.agents["atlas"].agent_id
    assert acb.grade_view(atlas) is None
    assert acb.grade_of(atlas) == (Grade.NONE, 0)
    as_of = chain.w3.eth.block_number - 1
    evidence = chash({"bundle": "x"})
    r = chain.sender("bureau").send(
        acb.post_grade(atlas, Grade.A, 720, [1201, 1202], "acb-sc-1.0.0", H, evidence, as_of, "evidence/b.json")
    )
    assert r.status == 1
    record = acb.get_record(atlas)
    assert record.grade == Grade.A and record.prev_grade == Grade.NONE and record.score == 720
    assert record.reason_list == (1201, 1202) and record.model_id == "acb-sc-1.0.0" and record.evidence_hash == evidence
    assert acb.reasons_of(atlas) == (1201, 1202, 0, 0)
    view = acb.grade_view(atlas)
    assert isinstance(view, GradeView)
    assert view.tx_hash == r.hash and view.evidence_uri == "evidence/b.json" and view.grade == Grade.A
    assert view.effective_grade == Grade.NONE  # NONE -> A is an upgrade: hidden for upgradeDelay
    clock.warp(chain.w3, acb.upgrade_delay())
    assert acb.effective_grade(atlas, 0) == Grade.A == acb.grade_view(atlas).effective_grade
    assert acb.upgrade_delay() == 60 and acb.is_poster(chain.keys.address("bureau"))
    assert acb.vault_agent_id(chain.c.vault("wallet").address) == chain.dep.agents["wallet"].agent_id


def test_identity_and_reputation_wrappers(chain):
    identity = chain.c.identity
    rep = chain.c.reputation
    atlas = chain.dep.agents["atlas"]
    assert identity.owner_of(atlas.agent_id) == chain.keys.address("atlas")
    assert identity.try_owner_of(10_000) is None
    assert identity.get_agent_wallet(atlas.agent_id) == atlas.wallet
    assert identity.is_authorized_or_owner(chain.keys.address("atlas"), atlas.agent_id)
    assert identity.get_version() == "2.0.0-lite" and rep.get_version() == "2.0.0-lite"
    assert rep.get_identity_registry() == identity.address
    beacon = chain.keys.address("beacon")
    r = chain.sender("beacon").send(rep.give_feedback(atlas.agent_id, 1, 0, "acb-report", "POSITIVE", "acb:v1", "", H))
    assert r.status == 1 and r.logs[0]["event"] == "NewFeedback"
    assert rep.get_last_index(atlas.agent_id, beacon) == 1
    assert rep.get_summary(atlas.agent_id, [beacon], "acb-report") == (1, 1, 0)
    assert rep.read_feedback(atlas.agent_id, beacon, 1)["tag2"] == "POSITIVE"
    assert rep.get_clients(atlas.agent_id) == [beacon]
    assert len(rep.read_all_feedback(atlas.agent_id, [beacon])) == 1
    assert chain.c.usdc.is_blacklisted(beacon) is None
    assert chain.c.usdc.decimals() == 6 and chain.c.usdc.minter() == chain.keys.address("deployer")


def test_conversions():
    assert b32(H) == bytes.fromhex("11" * 32)
    assert b32(5) == (5).to_bytes(32, "big")
    with pytest.raises(ValueError):
        b32("0x1234")
    assert hex32(b"\x01" * 32) == "0x" + "01" * 32
    assert model_id_text(model_id_bytes("acb-sc-1.0.0")) == "acb-sc-1.0.0"
    assert model_id_text(b"\xff" * 32) == "0x" + "ff" * 32
    assert pack_reasons([2100]) == [2100, 0, 0, 0]
    with pytest.raises(ValueError):
        pack_reasons([1, 2, 3, 4, 5])
    packed = 1001 | (1002 << 16) | (1006 << 32)
    assert unpack_reasons(packed) == (1001, 1002, 1006, 0)
    assert Web3.is_address(H[:42])
