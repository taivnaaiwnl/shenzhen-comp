"""Deployment phases on Anvil (spec 3.7, 9.1, 13.1, 13.5): shape of the result, idempotency, partial
re-runs, personas and tiers, extra vaults for the attack harness, funding and the official-registry
preflight."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from acb.chain import abi
from acb.chain.contracts import AcbContract, VaultContract
from acb.chain.deploy import (
    AGENT_ORDER,
    DEFAULT_PERSONAS,
    FUNDING_USDC_BPS_ENV,
    SIMULATED_LABEL,
    DeploymentError,
    agent_uri,
    check_official_erc8004,
    default_funding_plan,
    deploy_all,
    deploy_contracts,
    deploy_extra_vault,
    fund,
    load_personas,
    load_tiers,
    parse_personas,
    register_agents,
    register_extra_agent,
    registration_json,
)
from acb.chain.deployments import deployments_path, load_deployments, save_deployments
from acb.types import ContractEntry, FundingPlan, Grade, NetworkProfile, grade_mask

FIXTURES = Path(__file__).parent / "fixtures"
EIP1967 = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"


def _copy_deployments(tmp_path, monkeypatch, dep):
    """Point ACB_DEPLOYMENTS_DIR at a private copy so the session file stays untouched."""
    monkeypatch.setenv("ACB_DEPLOYMENTS_DIR", str(tmp_path))
    save_deployments(dep)
    return deployments_path(dep.network)


# ---------------------------------------------------------------------------
# pure parts
# ---------------------------------------------------------------------------


def test_registration_json_is_labelled_and_canonical():
    persona = DEFAULT_PERSONAS["atlas"]
    reg = registration_json("atlas", persona)
    assert reg["type"] == "https://eips.ethereum.org/EIPS/eip-8004#registration-v1"
    assert reg["description"].startswith(SIMULATED_LABEL)
    assert reg["services"] == [{"name": "acb-quote", "endpoint": "http://127.0.0.1:8704/p/atlas"}]
    assert reg["x-acb"] == {"category": "fx-data", "skus": ["fx-live-15m"]}
    uri = agent_uri("atlas", persona)
    assert uri.startswith("data:application/json;base64,")
    assert json.loads(base64.b64decode(uri.split(",", 1)[1])) == reg


def test_personas_fixture_parses_both_sku_layouts():
    personas = load_personas(FIXTURES / "personas.yaml")
    assert personas["slowpoke"].skus == {"fx-history-day": 200_000, "fx-history-day-promo": 100_000}
    assert personas["nova"].name == "Nova Labs Fixture"
    assert personas["beacon"].anchor and not personas["atlas"].anchor
    assert set(AGENT_ORDER) - {"wallet"} <= set(personas)
    with pytest.raises(ValueError):
        parse_personas({"personas": {"x": {"category": "ads"}}})
    with pytest.raises(ValueError):
        parse_personas({"x": {"name": "X", "skus": {"a": 0.5}}})
    assert load_personas(FIXTURES / "missing.yaml") == DEFAULT_PERSONAS


def test_tiers_yaml_resolves_profiles_and_masks():
    demo = load_tiers("demo")
    production = load_tiers("production")
    wallet = demo.profiles["wallet"]
    assert wallet.promotion_cooldown == 60 and production.profiles["wallet"].promotion_cooldown == 86400
    assert wallet.dispute_window == 120 and production.profiles["wallet"].dispute_window == 259200
    assert wallet.tiers[0].per_tx == 0 and wallet.tiers[0].pay_mask == 0
    assert wallet.tiers[1].pay_mask == grade_mask(Grade.A, Grade.B) == 0x30
    assert wallet.tiers[1].escrow_mask == 0x3B
    assert wallet.tiers[3].pay_mask == grade_mask(Grade.A, Grade.B, Grade.NEW)
    assert demo.profiles["market"].tiers[1].pay_mask == 0x33


def test_default_funding_plan(monkeypatch):
    base = NetworkProfile.load("base_sepolia", {"BASE_SEPOLIA_RPC_URL": "http://127.0.0.1:1"})
    monkeypatch.delenv(FUNDING_USDC_BPS_ENV, raising=False)
    plan = default_funding_plan(base)
    # config/networks.yaml scales Base Sepolia to the funded deployer: usdc_bps 9500, topup 0.00004 ETH
    assert plan.usdc_to_vaults == {"wallet": 14_250_000, "beacon": 5_700_000}
    assert sum(plan.usdc_to_vaults.values()) <= 20_000_000
    assert set(plan.eth) >= {"owner", "admin", "agent_signer", "bureau", "autonomy", "beacon_owner", "atlas"}
    assert "deployer" not in plan.eth and all(v == 40_000_000_000_000 for v in plan.eth.values())
    assert sum(plan.eth.values()) <= 800_000_000_000_000  # leaves room for the deployments in 0.0012 ETH
    monkeypatch.setenv(FUNDING_USDC_BPS_ENV, "9000")
    scaled = default_funding_plan(base)
    assert scaled.usdc_to_vaults == {"wallet": 13_500_000, "beacon": 5_400_000}
    assert sum(scaled.usdc_to_vaults.values()) <= 20_000_000
    anvil = NetworkProfile.load("anvil", {"ANVIL_RPC_URL": "http://127.0.0.1:1"})
    assert default_funding_plan(anvil).eth == {}


# ---------------------------------------------------------------------------
# the session deployment
# ---------------------------------------------------------------------------

pytest_chain = pytest.mark.chain


@pytest_chain
def test_deployment_shape(chain):
    dep, keys, c = chain.dep, chain.keys, chain.c
    assert dep.network == "anvil" and dep.chain_id == 31337
    assert dep.vault_code_hash == abi.codehash_of("TierVault") == c.acb.vault_code_hash()
    assert dep.abi_sha == abi.abi_sha()
    assert dep.roles["agentSigner"] == keys.address("agent_signer") and dep.roles["admin"] == keys.address("admin")
    assert set(dep.contracts) == {"AgentCreditBureau", "IdentityRegistry", "ReputationRegistry", "USDC"}
    assert dep.contracts["USDC"].extra == {"decimals": 6, "mock": True}
    assert dep.contracts["AgentCreditBureau"].extra == {"upgradeDelay": 60}
    assert c.acb.admin() == keys.address("admin") and c.acb.is_poster(keys.address("bureau"))
    assert c.acb.token() == c.usdc.address and c.acb.identity() == c.identity.address
    # identities: 12 registrations, each from its own key, wallet's by the owner
    assert list(dep.agents) == list(AGENT_ORDER)
    assert len({a.agent_id for a in dep.agents.values()}) == 12
    assert dep.agents["wallet"].identity_owner == keys.address("owner") and dep.agents["wallet"].wallet is None
    assert dep.agents["atlas"].identity_owner == keys.address("atlas") == dep.agents["atlas"].wallet
    assert dep.agents["atlas"].skus == {"fx-live-15m": 200_000}
    assert dep.anchors == (dep.agents["beacon"].agent_id,)
    for key in AGENT_ORDER:
        entry = dep.agents[key]
        persona = DEFAULT_PERSONAS.get(key)
        assert c.identity.owner_of(entry.agent_id) == entry.identity_owner
        if persona is not None:
            assert c.identity.token_uri(entry.agent_id) == agent_uri(key, persona)
    # vaults
    assert list(dep.vaults) == ["wallet", "beacon", "echo1", "echo2", "echo3"]
    for name, entry in dep.vaults.items():
        vault = VaultContract(chain.w3, entry.address)
        assert c.acb.is_vault(entry.address) and entry.register_vault_tx and entry.deploy_tx
        assert vault.codehash() == dep.vault_code_hash
        assert vault.agent_id() == entry.agent_id == dep.agents[name if name != "wallet" else "wallet"].agent_id
        assert vault.owner() == entry.owner and vault.agent() == entry.agent
    assert dep.vaults["wallet"].owner == keys.address("owner")
    assert dep.vaults["wallet"].agent == keys.address("agent_signer")
    assert dep.vaults["beacon"].owner == keys.address("beacon_owner") and dep.vaults["beacon"].agent == keys.address(
        "beacon"
    )
    assert len({dep.vaults[f"echo{i}"].owner for i in (1, 2, 3)}) == 3
    # funding
    assert c.usdc.balance_of(dep.vaults["wallet"].address) == 15_000_000
    assert c.usdc.balance_of(dep.vaults["beacon"].address) == 6_000_000
    # the file in ACB_DEPLOYMENTS_DIR round-trips
    assert load_deployments("anvil") == dep


@pytest_chain
def test_second_run_skips_every_phase(chain):
    path = deployments_path("anvil")
    before = path.read_bytes()
    messages: list[str] = []
    dep, txs = deploy_all(chain.net, chain.keys, log=messages.append)
    assert txs == []
    assert dep == chain.dep
    assert [m for m in messages if m.startswith("skip")] == [m for m in messages if not m.startswith("note")]
    assert path.read_bytes() == before


@pytest_chain
def test_partial_rerun_redeploys_only_what_is_missing(chain, tmp_path, monkeypatch):
    vaults = {k: v for k, v in chain.dep.vaults.items() if k != "echo3"}
    _copy_deployments(tmp_path, monkeypatch, chain.dep.model_copy(update={"vaults": vaults}))
    dep, txs = deploy_all(chain.net, chain.keys)
    assert [t.function for t in txs] == ["deploy", "registerVault"]
    assert dep.vaults["echo3"].address != chain.dep.vaults["echo3"].address
    assert load_deployments("anvil").vaults["echo3"].address == dep.vaults["echo3"].address


@pytest_chain
def test_missing_bureau_code_redeploys_bureau_and_vaults(chain, tmp_path, monkeypatch):
    contracts = dict(chain.dep.contracts)
    contracts["AgentCreditBureau"] = ContractEntry(address="0x" + "de" * 20)
    _copy_deployments(tmp_path, monkeypatch, chain.dep.model_copy(update={"contracts": contracts}))
    dep, txs = deploy_contracts(chain.net, chain.keys, existing=load_deployments("anvil"))
    assert [t.function for t in txs] == ["deploy", "setPoster"]
    assert dep.contracts["USDC"] == chain.dep.contracts["USDC"]
    assert dep.agents == chain.dep.agents and dep.vaults == {}
    assert AcbContract(chain.w3, dep.contracts["AgentCreditBureau"].address).is_poster(chain.keys.address("bureau"))


@pytest_chain
def test_personas_refresh_the_pinned_directory_without_transactions(chain, tmp_path, monkeypatch):
    _copy_deployments(tmp_path, monkeypatch, chain.dep)
    dep, txs = register_agents(chain.net, chain.keys, chain.dep, personas=load_personas(FIXTURES / "personas.yaml"))
    assert txs == []
    assert dep.agents["nova"].name == "Nova Labs Fixture"
    assert load_deployments("anvil").agents["nova"].name == "Nova Labs Fixture"


@pytest_chain
def test_extra_vaults_and_agents_for_the_attack_harness(chain, tmp_path, monkeypatch):
    _copy_deployments(tmp_path, monkeypatch, chain.dep)
    dep = chain.dep
    # wash ring A36a: three vaults with ONE owner
    for i in (1, 2, 3):
        dep, txs = deploy_extra_vault(
            chain.net, chain.keys, dep, f"ringA{i}", agent=f"echo{i}", owner_role="echo1_vault_owner"
        )
        assert [t.function for t in txs] == ["deploy", "registerVault"]
    owners = {dep.vaults[f"ringA{i}"].owner for i in (1, 2, 3)}
    assert owners == {chain.keys.address("echo1_vault_owner")}
    assert all(chain.c.acb.is_vault(dep.vaults[f"ringA{i}"].address) for i in (1, 2, 3))
    # idempotent
    again, txs = deploy_extra_vault(chain.net, chain.keys, dep, "ringA1", agent="echo1", owner_role="echo1_vault_owner")
    assert txs == [] and again.vaults["ringA1"] == dep.vaults["ringA1"]
    # a sybil identity and its unregistered vault
    dep, txs = register_extra_agent(chain.net, chain.keys, dep, "sybil1", "sybil1")
    assert len(txs) == 1 and dep.agents["sybil1"].identity_owner == chain.keys.address("sybil1")
    dep, txs = deploy_extra_vault(
        chain.net, chain.keys, dep, "sybil1", agent="sybil1", owner_role="sybil2", register=False
    )
    assert [t.function for t in txs] == ["deploy"]
    assert not chain.c.acb.is_vault(dep.vaults["sybil1"].address)
    assert load_deployments("anvil").vaults.keys() >= {"ringA1", "ringA2", "ringA3", "sybil1"}


@pytest_chain
def test_fund_sends_only_the_difference(chain, tmp_path, monkeypatch):
    _copy_deployments(tmp_path, monkeypatch, chain.dep)
    plan = FundingPlan(eth={"sybil3": 10_001 * 10**18}, usdc_to_vaults={"wallet": 16_000_000, "beacon": 6_000_000})
    txs = fund(chain.net, chain.keys, chain.dep, plan)
    assert [t.function for t in txs] == ["ethTransfer", "mint"]
    assert chain.c.usdc.balance_of(chain.dep.vaults["wallet"].address) == 16_000_000
    assert fund(chain.net, chain.keys, chain.dep, plan) == []
    with pytest.raises(DeploymentError):
        fund(chain.net, chain.keys, chain.dep, FundingPlan(eth={}, usdc_to_vaults={"nope": 1}))


@pytest_chain
def test_fund_with_a_real_token_checks_the_deployer_balance_first(chain):
    contracts = dict(chain.dep.contracts)
    contracts["USDC"] = contracts["USDC"].model_copy(update={"extra": {"decimals": 6, "mock": False}})
    dep = chain.dep.model_copy(update={"contracts": contracts})
    deployer = chain.sender("deployer")
    nonce = deployer.nonces.chain_next()
    plan = FundingPlan(eth={}, usdc_to_vaults={"wallet": 100_000_000})
    with pytest.raises(DeploymentError, match=FUNDING_USDC_BPS_ENV):
        fund(chain.net, chain.keys, dep, plan)
    assert deployer.nonces.chain_next() == nonce, "nothing sent"
    chain.sender("deployer").send(chain.c.usdc.mint(chain.keys.address("deployer"), 85_000_000))
    txs = fund(chain.net, chain.keys, dep, plan)
    assert [t.function for t in txs] == ["transfer"]
    assert chain.c.usdc.balance_of(dep.vaults["wallet"].address) == 100_000_000


@pytest_chain
def test_official_registry_preflight(chain):
    w3 = chain.w3
    identity, reputation = chain.c.identity.address, chain.c.reputation.address
    impl_i, impl_r = "0x" + "a1" * 20, "0x" + "b2" * 20
    for proxy, impl in ((identity, impl_i), (reputation, impl_r)):
        value = "0x" + "00" * 12 + impl[2:]
        assert w3.provider.make_request("anvil_setStorageAt", [proxy, EIP1967, value]).get("result") is True
    cfg = {
        "mode": "official",
        "identity": identity,
        "reputation": reputation,
        "identity_impl": impl_i,
        "reputation_impl": impl_r,
        "expected_version": "2.0.0-lite",
    }
    net = chain.net.model_copy(update={"erc8004": cfg})
    entries = check_official_erc8004(w3, net)
    assert entries["IdentityRegistry"].official and entries["IdentityRegistry"].implementation == impl_i
    assert entries["ReputationRegistry"].version == "2.0.0-lite"
    with pytest.raises(DeploymentError, match="getVersion"):
        check_official_erc8004(w3, chain.net.model_copy(update={"erc8004": {**cfg, "expected_version": "2.0.0"}}))
    with pytest.raises(DeploymentError, match="implementation"):
        check_official_erc8004(w3, chain.net.model_copy(update={"erc8004": {**cfg, "identity_impl": impl_r}}))


@pytest_chain
def test_wrong_chain_is_refused(chain):
    wrong = chain.net.model_copy(update={"chain_id": 84532})
    with pytest.raises(RuntimeError, match="chain id"):
        deploy_all(wrong, chain.keys)


@pytest_chain
def test_deploy_contracts_alone_reads_the_file_and_skips(chain):
    dep, txs = deploy_contracts(chain.net, chain.keys)
    assert txs == [] and dep == chain.dep
