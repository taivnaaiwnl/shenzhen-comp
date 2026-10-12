"""deployments/<network>.json I/O (13.5, CR-1) and explorer links (10.4); no chain needed."""

from __future__ import annotations

import json

import pytest

from acb.chain import explorer
from acb.chain.deployments import (
    DEFAULT_DEPLOYMENTS_DIR,
    deployments_dir,
    deployments_path,
    dumps_deployments,
    load_deployments,
    save_deployments,
    try_load_deployments,
)
from acb.types import AgentEntry, ContractEntry, Deployments, NetworkProfile, VaultEntry

A = "0x" + "aa" * 20
H = "0x" + "bb" * 32


def _dep(network: str = "anvil") -> Deployments:
    return Deployments(
        network=network,
        chain_id=31337,
        deployed_at="2026-10-17T08:00:00Z",
        git_commit="uncommitted",
        start_block=0,
        roles={"deployer": A},
        contracts={
            "AgentCreditBureau": ContractEntry(address=A, tx_hash=H, block=4, extra={"upgradeDelay": 60}),
            "USDC": ContractEntry(address=A, extra={"decimals": 6, "mock": True}),
        },
        vault_code_hash=H,
        anchors=(7,),
        agents={
            "wallet": AgentEntry(
                agent_id=0, name="Research Wallet", identity_owner=A, register_tx=H, category="research", skus={}
            ),
            "atlas": AgentEntry(
                agent_id=1,
                name="Atlas Data",
                identity_owner=A,
                wallet=A,
                register_tx=H,
                category="fx-data",
                skus={"fx-live-15m": 200_000},
            ),
        },
        vaults={
            "wallet": VaultEntry(
                address=A, agent_id=0, owner=A, agent=A, autonomy=A, profile="wallet", deploy_tx=H, register_vault_tx=H
            )
        },
        abi_sha=H,
    )


def test_env_override_and_default(monkeypatch, tmp_path):
    monkeypatch.delenv("ACB_DEPLOYMENTS_DIR", raising=False)
    assert deployments_dir() == DEFAULT_DEPLOYMENTS_DIR
    monkeypatch.setenv("ACB_DEPLOYMENTS_DIR", str(tmp_path))
    assert deployments_path("anvil") == tmp_path / "anvil.json"
    with pytest.raises(ValueError):
        deployments_path("../evil")


def test_round_trip_layout_lf_no_bom(monkeypatch, tmp_path):
    monkeypatch.setenv("ACB_DEPLOYMENTS_DIR", str(tmp_path))
    dep = _dep()
    path = save_deployments(dep)
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r\n" not in raw and raw.endswith(b"\n")
    data = json.loads(raw)
    assert data["schema"] == "acb-deployments/1" and data["abi"] == {"sha": H}
    assert data["contracts"]["AgentCreditBureau"]["upgradeDelay"] == 60
    assert data["agents"]["atlas"]["agentId"] == "1" and data["agents"]["atlas"]["skus"] == {"fx-live-15m": "200000"}
    assert data["anchors"] == ["7"]
    assert load_deployments("anvil") == dep
    assert dumps_deployments(dep) == raw
    assert not list(tmp_path.glob("*.tmp")), "atomic write leaves no temp file"


def test_missing_wrong_network_and_invalid(monkeypatch, tmp_path):
    monkeypatch.setenv("ACB_DEPLOYMENTS_DIR", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        load_deployments("anvil")
    assert try_load_deployments("anvil") is None
    save_deployments(_dep("base_sepolia"), tmp_path / "anvil.json")
    with pytest.raises(ValueError, match="base_sepolia"):
        load_deployments("anvil")
    (tmp_path / "anvil.json").write_bytes(b'{"schema": "other"}')
    assert try_load_deployments("anvil") is None


def test_explorer_links():
    base = NetworkProfile.load("base_sepolia", {"BASE_SEPOLIA_RPC_URL": "http://127.0.0.1:1"})
    assert explorer.tx_url(base, H) == f"https://sepolia.basescan.org/tx/{H}"
    assert explorer.tx_url(base, H, "blockscout") == f"https://base-sepolia.blockscout.com/tx/{H}"
    assert explorer.address_url(base, A) == f"https://sepolia.basescan.org/address/{A}"
    assert explorer.tx_urls(base, H) == {
        "basescan": f"https://sepolia.basescan.org/tx/{H}",
        "blockscout": f"https://base-sepolia.blockscout.com/tx/{H}",
    }
    assert set(explorer.address_urls(base, A)) == {"basescan", "blockscout"}
    anvil = NetworkProfile.load("anvil", {"ANVIL_RPC_URL": "http://127.0.0.1:1"})
    assert explorer.tx_url(anvil, H) is None and explorer.tx_urls(anvil, H) == {}
    assert explorer.address_url(base, A, "nope") is None
