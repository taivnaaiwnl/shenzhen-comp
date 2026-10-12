"""TxSender and NonceManager on Anvil (spec 7.4, 12.2): results, revert decoding by selector including a
mined revert replayed at block - 1, nonce resync after ``evm_revert``, after a receipt timeout and after
"nonce too low", fees and gas."""

from __future__ import annotations

import pytest
from chain_mocks import RpcError, mock_web3
from web3 import Web3

from acb.chain.contracts import OutflowParams
from acb.chain.tx import (
    EXPECTED_REVERT_GAS,
    NonceManager,
    TxSender,
    TxSimulationError,
    TxTimeout,
    fee_params,
    padded_gas,
    resync_all,
)
from acb.types import Decision, TxResult

pytestmark = pytest.mark.chain

H = "0x" + "11" * 32


def _rpc(w3, method, params=None):
    response = w3.provider.make_request(method, params or [])
    assert "error" not in response, response
    return response["result"]


def _is_hex32(value: str) -> bool:
    return isinstance(value, str) and value.startswith("0x") and len(value) == 66


def test_send_returns_txresult_with_decoded_logs(chain):
    deployer = chain.sender("deployer")
    usdc = chain.c.usdc
    target = chain.keys.address("beacon_owner")
    before = usdc.balance_of(target)
    nonce = deployer.nonces.chain_next()
    result = deployer.send(usdc.mint(target, 1_234))
    assert isinstance(result, TxResult)
    assert _is_hex32(result.hash), "Web3.to_hex, never HexBytes.hex()"
    assert result.status == 1 and result.expected_status == 1 and result.revert_error is None
    assert result.function == "mint" and result.to == usdc.address
    assert result.from_address == chain.keys.address("deployer")
    assert result.nonce == nonce and result.chain_id == 31337 and result.gas_used > 0
    transfer = [log for log in result.logs if log["event"] == "Transfer"]
    assert transfer and transfer[0]["args"]["to"] == target and transfer[0]["address"] == usdc.address
    assert _is_hex32(transfer[0]["transactionHash"])
    assert usdc.balance_of(target) == before + 1_234
    result.to_wire()  # JSON-ready


def test_simulation_revert_raises_and_sends_nothing(chain):
    signer = chain.sender("agent_signer")
    nonce = signer.nonces.chain_next()
    with pytest.raises(TxSimulationError) as info:
        signer.send(chain.c.usdc.transfer(chain.keys.address("atlas"), 10**12))
    assert info.value.revert_error == "InsufficientBalance()"
    assert info.value.function == "transfer"
    owner = chain.sender("owner")
    params = OutflowParams(H, 1, 1, 2**40, H, H, H, int(Decision.PAY), 2000)
    with pytest.raises(TxSimulationError, match=r"NotAgent\(\)"):
        owner.send(chain.c.vault("wallet").pay(params))
    assert signer.nonces.chain_next() == nonce
    assert signer.simulate(chain.c.usdc.transfer(chain.keys.address("atlas"), 1)).text == "InsufficientBalance()"
    assert chain.sender("deployer").simulate(chain.c.usdc.mint(chain.keys.address("atlas"), 1)) is None


def test_expected_revert_is_mined_and_decoded_with_arguments(chain):
    """Stolen-key style: no simulation, 500,000 gas, status 0, custom error with arguments."""
    signer = chain.sender("agent_signer")
    vault = chain.c.vault("wallet")
    ghost = chain.dep.agents["ghost"].agent_id
    mandate = "0x" + "22" * 32
    deadline = chain.w3.eth.get_block("latest")["timestamp"] + 600
    params = OutflowParams(H, ghost, 200_000, deadline, mandate, H, H, int(Decision.PAY), 2000)
    result = signer.send(vault.pay(params), expect_revert=True)
    assert result.status == 0 and result.expected_status == 0
    assert result.revert_error == f"MandateInactive({mandate})"
    tx = chain.w3.eth.get_transaction(result.hash)
    assert tx["gas"] == EXPECTED_REVERT_GAS
    # frozen vault: the modifier reports Frozen()
    chain.sender("owner").send(vault.freeze(3200, H))
    frozen = signer.send(vault.pay(params), expect_revert=True)
    assert frozen.status == 0 and frozen.revert_error == "Frozen()"


def test_mined_revert_is_replayed_at_block_minus_one(chain):
    signer = chain.sender("agent_signer")
    usdc = chain.c.usdc
    fn = usdc.transfer(chain.keys.address("atlas"), 5)
    result = signer.send(fn, expect_revert=True)
    assert result.status == 0 and result.revert_error == "InsufficientBalance()"
    # make the same call succeed at the latest block: the replay must still look at block - 1
    chain.sender("deployer").send(usdc.mint(chain.keys.address("agent_signer"), 10))
    assert signer.simulate(fn) is None
    call = {
        "from": signer.checksum_address,
        "to": Web3.to_checksum_address(usdc.address),
        "data": fn._encode_transaction_data(),
        "value": 0,
    }
    assert signer._replay_revert(call, EXPECTED_REVERT_GAS, result.block, result.gas_used) == "InsufficientBalance()"


def test_nonce_resync_after_evm_revert_is_automatic_on_anvil(chain):
    sender = chain.sender("beacon_owner")
    to = chain.keys.address("attacker")
    snap = _rpc(chain.w3, "evm_snapshot")
    first = sender.send_eth(to, 1)
    assert sender.nonces.peek() == first.nonce + 1
    assert _rpc(chain.w3, "evm_revert", [snap]) is True
    again = sender.send_eth(to, 1)
    assert again.status == 1 and again.nonce == first.nonce


def test_resync_all_after_evm_revert_without_per_send_check(chain):
    sender = chain.sender("beacon_owner")
    sender.nonces._verify_each = False  # behave like a public network: local counter only
    try:
        to = chain.keys.address("attacker")
        snap = _rpc(chain.w3, "evm_snapshot")
        first = sender.send_eth(to, 1)
        assert _rpc(chain.w3, "evm_revert", [snap]) is True
        assert sender.nonces.peek() == first.nonce + 1 > sender.nonces.chain_next()
        assert resync_all() >= 1
        assert sender.nonces.peek() == first.nonce
        again = sender.send_eth(to, 1)
        assert again.status == 1 and again.nonce == first.nonce
    finally:
        sender.nonces._verify_each = None


def test_nonce_too_low_resyncs_and_retries_once(chain):
    sender = chain.sender("beacon_owner")
    sender.nonces._verify_each = False
    try:
        to = chain.keys.address("attacker")
        sender.send_eth(to, 1)
        stale = sender.nonces.chain_next() - 1
        sender.nonces._next = stale
        result = sender.send_eth(to, 1)
        assert result.status == 1 and result.nonce == stale + 1
    finally:
        sender.nonces._verify_each = None


def test_receipt_timeout_resyncs(chain):
    w3 = chain.w3
    sender = TxSender(w3, chain.keys.account("beacon_owner"), receipt_timeout=1.0)
    to = chain.keys.address("attacker")
    expected_nonce = sender.nonces.chain_next()
    _rpc(w3, "evm_setAutomine", [False])
    try:
        with pytest.raises(TxTimeout) as info:
            sender.send_eth(to, 1)
        assert _is_hex32(info.value.tx_hash)
        assert sender.nonces.peek() is None, "a timeout forgets the local nonce"
        _rpc(w3, "evm_mine")
    finally:
        _rpc(w3, "evm_setAutomine", [True])
    assert w3.eth.get_transaction_receipt(info.value.tx_hash)["status"] == 1
    after = sender.send_eth(to, 1)
    assert after.nonce == expected_nonce + 1


def test_shared_nonce_manager_per_address(chain):
    a = chain.sender("nova")
    b = chain.sender("nova")
    assert a.nonces is b.nonces
    assert NonceManager.for_address(chain.w3, chain.keys.address("nova")) is a.nonces
    r1 = a.send_eth(chain.keys.address("attacker"), 1)
    r2 = b.send_eth(chain.keys.address("attacker"), 1)
    assert r2.nonce == r1.nonce + 1


def test_deploy_and_send_eth(chain):
    deployer = chain.sender("deployer")
    result = deployer.deploy("MockUSDC", chain.keys.checksum_address("deployer"))
    assert result.function == "deploy" and result.status == 1
    assert len(chain.w3.eth.get_code(Web3.to_checksum_address(result.to))) > 0
    eth = deployer.send_eth(chain.keys.address("attacker"), 7)
    assert eth.function == "ethTransfer" and eth.to == chain.keys.address("attacker")
    with pytest.raises(ValueError):
        deployer.send_eth(chain.keys.address("attacker"), 0)


def test_fee_params_on_anvil(chain):
    fees = fee_params(chain.w3)
    base = chain.w3.eth.get_block("latest")["baseFeePerGas"]
    prio = fees["maxPriorityFeePerGas"]
    assert fees["maxFeePerGas"] == 2 * base + prio


@pytest.mark.parametrize(("chain_id", "expected"), [(31337, 10**9), (84532, 10**6)])
def test_fee_fallbacks_without_max_priority_fee(chain_id, expected):
    def no_method(_params):
        raise RpcError(-32601, "method not found")

    w3 = mock_web3(
        {
            "eth_chainId": lambda p: hex(chain_id),
            "eth_maxPriorityFeePerGas": no_method,
            "eth_getBlockByNumber": lambda p: {
                "number": "0x1",
                "baseFeePerGas": hex(5_000_000),
                "hash": "0x" + "aa" * 32,
            },
        }
    )
    fees = fee_params(w3)
    assert fees == {"maxPriorityFeePerGas": expected, "maxFeePerGas": 2 * 5_000_000 + expected}


def test_padded_gas():
    assert padded_gas(100_000) == 135_000
    assert padded_gas(21_000) == 36_250
