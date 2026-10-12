"""The one clock: latest block timestamp, Anvil warps, real-time waits elsewhere (spec 0.1, 10.1)."""

from __future__ import annotations

import pytest
from chain_mocks import mock_web3

from acb.chain import clock


def _public_chain(timestamps: list[int]):
    state = {"i": 0}

    def block(_params):
        ts = timestamps[min(state["i"], len(timestamps) - 1)]
        state["i"] += 1
        return {"number": hex(100 + state["i"]), "timestamp": hex(ts), "hash": "0x" + "ab" * 32}

    return mock_web3({"eth_chainId": lambda p: hex(84532), "eth_getBlockByNumber": block})


@pytest.mark.chain
def test_now_is_latest_block_timestamp(anvil):
    _, w3 = anvil
    assert clock.now(w3) == w3.eth.get_block("latest")["timestamp"]
    assert clock.block_timestamp(w3, 0) <= clock.now(w3)
    assert clock.is_anvil(w3)


@pytest.mark.chain
def test_warp_and_mine_on_anvil(anvil):
    _, w3 = anvil
    before = clock.now(w3)
    block = w3.eth.block_number
    after = clock.warp(w3, 3600)
    assert after >= before + 3600
    assert w3.eth.block_number == block + 1
    clock.mine(w3, 2)
    assert w3.eth.block_number == block + 3
    with pytest.raises(ValueError):
        clock.warp(w3, -1)


@pytest.mark.chain
def test_advance_to_warps_on_anvil(anvil):
    _, w3 = anvil
    target = clock.now(w3) + 500
    assert clock.advance_to(w3, target) >= target
    assert clock.advance_to(w3, target - 1000) >= target  # already past: no change


def test_warp_refused_on_a_public_chain():
    w3 = _public_chain([1000])
    with pytest.raises(RuntimeError, match="Anvil"):
        clock.warp(w3, 10)
    with pytest.raises(RuntimeError):
        clock.mine(w3)


def test_advance_to_waits_in_real_time_elsewhere():
    w3 = _public_chain([1000, 1002, 1004, 1006])
    sleeps: list[float] = []
    assert clock.advance_to(w3, 1005, poll_s=2.0, sleep=sleeps.append) == 1006
    assert sleeps and all(s <= 2.0 for s in sleeps)


def test_advance_to_times_out():
    w3 = _public_chain([1000])
    with pytest.raises(TimeoutError):
        clock.advance_to(w3, 2000, timeout_s=0.0, sleep=lambda _s: None)
