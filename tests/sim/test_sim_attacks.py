"""Anvil-only attack actions (spec 9.4, 12.3 A36-A43, A59), marker ``chain``.

These tests check that each action produces the on-chain record the attack needs (the harness of
WP9 measures the effect on grades)."""

from __future__ import annotations

import pytest
from web3 import Web3

from acb.chain.contracts import bind
from acb.types import OrderState, Outcome

pytestmark = pytest.mark.chain


def _h(value) -> str:
    return value.lower() if isinstance(value, str) else Web3.to_hex(value)


def test_attack_identities_are_extra_keys_with_distinct_owners(anvil_sim):
    from acb.sim.attacks import sybil

    sim = anvil_sim.sim
    a, va = sybil(sim, "probe-a")
    b, vb = sybil(sim, "probe-b")
    roles = set(sim.keys.addresses().values())
    owners = {va.owner.address.lower(), vb.owner.address.lower()}
    ids = {a.address, b.address}
    assert len(owners) == 2 and not owners & ids and not (owners | ids) & roles
    c = bind(sim.w3, sim.dep)
    assert c.identity.owner_of(a.agent_id) == a.address and c.identity.get_agent_wallet(a.agent_id) == a.address
    assert c.acb.is_vault(va.address) and c.acb.vault_agent_id(va.address) == a.agent_id
    assert c.usdc.balance_of(va.address) == 5_000_000
    # a restarted simulator knows the entities again
    from acb.sim.core import Simulator

    again = Simulator(sim.net, sim.dep, sim.keys, w3=sim.w3, out_dir=sim.out_dir, inbox=anvil_sim.inbox)
    assert again.party("probe-a").address == a.address


def test_wash_round_distinct_owners(anvil_sim):
    sim = anvil_sim.sim
    out = sim.run_action("wash_round_distinct", {})["result"]
    orders = out["orders"]
    assert len(orders) == 3 and {o["state"] for o in orders} == {"RELEASED"}
    payers = [o["label"].split(":")[2] for o in orders]
    assert payers == ["0", "1", "2"]
    assert [o["provider"] for o in orders] == ["echo2", "echo3", "echo1"]
    owners = {sim.dep.vaults[k].owner for k in ("echo1", "echo2", "echo3")}
    assert len(owners) == 3


@pytest.mark.slow
def test_wash_round_same_owner(anvil_sim):
    sim = anvil_sim.sim
    out = sim.run_action("wash_round_same_owner", {})["result"]
    assert len(out["orders"]) == 3 and {o["state"] for o in out["orders"]} == {"RELEASED"}
    vaults = sim.state.data["vaults"]
    owners = {vaults[n]["owner"] for n in out["vaults"]}
    assert owners == {sim.keys.address("echo1_vault_owner")}
    second = sim.run_action("wash_round_same_owner", {})["result"]
    assert second["round"] == 2 and len(second["orders"]) == 3


@pytest.mark.slow
def test_dust_ring(anvil_sim):
    sim = anvil_sim.sim
    out = sim.run_action("dust_ring", {"payers": 2, "orders": 2})["result"]
    assert len(out["orders"]) == 4
    assert {o["state"] for o in out["orders"]} == {"PAID"}
    c = bind(sim.w3, sim.dep)
    for o in out["orders"]:
        vault = sim.state.data["labels"][o["label"]]["vault"]
        order = c.vault_at(sim.state.data["vaults"][vault]["address"]).get_order(o["orderId"])
        assert order.outcome == Outcome.ON_TIME and order.amount == 100_000
        assert order.payee_agent_id == int(out["subject"]["agentId"])


def test_sybil_failed_orders(anvil_sim):
    sim = anvil_sim.sim
    out = sim.run_action("sybil_failed_orders", {})["result"]
    assert len(out["orders"]) == 2
    for o in out["orders"]:
        assert o["providerDelivered"] and o["outcome"] == "FAILED"
    atlas = sim.dep.agents["atlas"].agent_id
    owners = {sim.state.data["vaults"][o["sybil"]]["owner"] for o in out["orders"]}
    assert len(owners) == 2
    # Atlas really delivered both orders
    assert sim.state.counter("atlas", "orders") == 2
    assert all(rec["delivered"] for rec in sim.state.data["orders"].values() if rec["persona"] == "atlas")
    assert atlas >= 0


def test_dust_smear(anvil_sim):
    sim = anvil_sim.sim
    out = sim.run_action("dust_smear", {})["result"]
    assert out["dust"]["amount"] == "10000"
    assert out["dust"]["earlyRefund"]["status"] == 0 and out["dust"]["earlyRefund"]["error"] == "EscrowNotExpired"
    assert out["dust"]["outcome"] == "FAILED"
    real = out["real"]
    assert real["earlyRefund"]["error"] == "EscrowNotExpired"
    assert real["delivery"] == "delivered" and real["claimTx"]
    assert real["refundAfterClaim"]["status"] == 0 and real["refundAfterClaim"]["error"] == "DeliveryClaimed"
    assert real["state"] == "ESCROWED"


def test_sybil_reports_with_replay(anvil_sim):
    sim = anvil_sim.sim
    out = sim.run_action("sybil_reports", {"sybils": 1, "replay": True})["result"]
    reports = out["reports"]
    assert {(r["subject"], r["kind"]) for r in reports} == {
        ("atlas", "INJECTION_ATTEMPT"), ("atlas", "SPAM"), ("wallet", "INJECTION_ATTEMPT"), ("wallet", "SPAM"),
    }  # fmt: skip
    for r in reports:
        assert len(r["reportTxs"]) == 2
        assert (r["decisionTx"] is not None) == (r["kind"] == "INJECTION_ATTEMPT")
    c = bind(sim.w3, sim.dep)
    vault = c.vault_at(sim.state.data["vaults"]["accuser-1"]["address"])
    refused = [r for r in reports if r["kind"] == "INJECTION_ATTEMPT"]
    assert all(vault.get_order(r["orderId"]).state == OrderState.REFUSED for r in refused)
    accuser = sim.party("accuser-1")
    atlas = sim.dep.agents["atlas"].agent_id
    assert c.reputation.get_last_index(atlas, accuser.address) == 4


def test_fake_vaults_are_refused(anvil_sim):
    sim = anvil_sim.sim
    out = sim.run_action("fake_vault", {})["result"]
    assert out["bytecode"]["error"] == "BadVaultCode" and out["bytecode"]["status"] == 0
    for variant in ("token", "bureau", "identity"):
        assert out[variant]["error"] == "BadVaultConfig", (variant, out[variant])
        assert not out[variant]["registered"]
    c = bind(sim.w3, sim.dep)
    # the same-codehash vaults really share the TierVault code hash
    assert c.vault_at(out["token"]["vault"]).codehash() == sim.dep.vault_code_hash


@pytest.mark.slow
def test_bust_out_builds_then_defaults(anvil_sim):
    sim = anvil_sim.sim
    built = sim.run_action("bust_out", {"phase": "build", "orders": 3})["result"]
    assert built["persona"] == "nova" and [o["outcome"] for o in built["orders"]] == ["ON_TIME"] * 3
    assert [o["orderNumber"] for o in built["orders"]] == [1, 2, 3]  # order 2 delivered despite Nova's outage script
    sim.run_action("bust_out", {"phase": "default"})
    from acb.sim.market import Slot, buyer, drive_slots, ensure_mandate

    b = buyer(sim, "beacon")
    mandate_id, _ = ensure_mandate(
        sim,
        b,
        "after-bust",
        [sim.party("nova")],
        max_per_tx=1_000_000,
        max_total=1_000_000,
        valid_s=3600 * 24,
        purpose="x",
    )
    after = drive_slots(sim, b, mandate_id, [Slot("bust:after:1", "nova", "fx-sample", 1)], wait_late=False)
    assert after[0]["script"] == "never" and after[0]["state"] == "ESCROWED" and after[0]["claimedAt"] == 0
    sim.run_action("bust_out", {"phase": "reset"})
    assert sim.state.override("nova") is None
    assert _h(b.address) == sim.dep.vaults["beacon"].address
