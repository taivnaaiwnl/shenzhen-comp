"""The shared chain fixtures of tests/conftest.py (spec 12.2), on a real Anvil."""

from __future__ import annotations

import pytest

from acb.keys import KeyRing

pytestmark = pytest.mark.chain


def test_anvil_fixture(anvil, anvil_profile):
    url, w3 = anvil
    assert url.startswith("http://127.0.0.1:") and not url.endswith(":8545")
    assert w3.eth.chain_id == 31337
    accounts = [a.lower() for a in w3.eth.accounts]
    assert len(accounts) == 30
    ring = KeyRing.anvil()
    for role in ("deployer", "owner", "admin", "agent_signer_next", "sybil6"):
        assert ring.address(role) in accounts
    assert anvil_profile.rpc_url == url and anvil_profile.chain_id == 31337


def test_automine_and_warp(anvil, warp):
    _, w3 = anvil
    ring = KeyRing.anvil()
    before = w3.eth.block_number
    tx = w3.eth.send_transaction(
        {"from": ring.checksum_address("deployer"), "to": ring.checksum_address("nova"), "value": 1}
    )
    assert w3.eth.wait_for_transaction_receipt(tx, timeout=10)["status"] == 1
    assert w3.eth.block_number == before + 1, "automine: one block per transaction"
    t0 = w3.eth.get_block("latest")["timestamp"]
    t1 = warp(3600)
    assert t1 >= t0 + 3600


def test_out_dir_fixture(out_dir):
    import os

    assert os.environ["ACB_OUT_DIR"] == str(out_dir)


def test_fresh_deployed_reverts_state(anvil, fresh_deployed):
    """Skipped until WP3's deploy lands."""
    _, w3 = anvil
    assert fresh_deployed.chain_id == 31337
    assert w3.eth.get_code(w3.to_checksum_address(fresh_deployed.contracts["AgentCreditBureau"].address))


def test_snapshot_revert_mechanism_used_by_fresh_deployed(anvil):
    _, w3 = anvil
    ring = KeyRing.anvil()
    target = ring.checksum_address("attacker")
    before = w3.eth.get_balance(target)
    snap = w3.provider.make_request("evm_snapshot", [])["result"]
    tx = w3.eth.send_transaction({"from": ring.checksum_address("deployer"), "to": target, "value": 12345})
    w3.eth.wait_for_transaction_receipt(tx, timeout=10)
    assert w3.eth.get_balance(target) == before + 12345
    assert w3.provider.make_request("evm_revert", [snap])["result"] is True
    assert w3.eth.get_balance(target) == before
