"""The one clock of the system (spec 0.1, 10.1): the timestamp of the latest block.

Every service uses :func:`now` for deadlines, expiry, cooldowns, grace periods and ages. Wall-clock
time appears only in logs. On Anvil, :func:`warp` moves the chain clock forward
(``evm_increaseTime`` + ``evm_mine``); on a public network :func:`advance_to` waits in real time.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

ANVIL_CHAIN_ID = 31337


def now(w3: Any) -> int:
    """Timestamp (seconds) of the latest block."""
    return int(w3.eth.get_block("latest")["timestamp"])


def block_timestamp(w3: Any, block: int | str) -> int:
    """Timestamp of ``block``."""
    return int(w3.eth.get_block(block)["timestamp"])


def is_anvil(w3: Any) -> bool:
    """True for a local Anvil chain (chain id 31337)."""
    return int(w3.eth.chain_id) == ANVIL_CHAIN_ID


def _rpc(w3: Any, method: str, params: list) -> Any:
    response = w3.provider.make_request(method, params)
    if "error" in response:
        raise RuntimeError(f"{method} failed: {response['error']}")
    return response.get("result")


def mine(w3: Any, blocks: int = 1) -> int:
    """Mine ``blocks`` empty blocks on Anvil; returns the new latest timestamp."""
    if not is_anvil(w3):
        raise RuntimeError("mine() is only available on Anvil")
    for _ in range(max(0, int(blocks))):
        _rpc(w3, "evm_mine", [])
    return now(w3)


def warp(w3: Any, seconds: int) -> int:
    """Advance the Anvil clock by ``seconds`` and mine one block; returns the new latest timestamp.

    Refuses on any chain other than Anvil (a public chain cannot be warped).
    """
    if not is_anvil(w3):
        raise RuntimeError("warp() is only available on Anvil (chain id 31337)")
    seconds = int(seconds)
    if seconds < 0:
        raise ValueError("cannot warp backwards")
    if seconds:
        _rpc(w3, "evm_increaseTime", [seconds])
    _rpc(w3, "evm_mine", [])
    return now(w3)


def advance_to(
    w3: Any,
    timestamp: int,
    *,
    poll_s: float = 2.0,
    timeout_s: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Make the latest block timestamp reach ``timestamp``: warp on Anvil, wait in real time elsewhere.

    Returns the latest timestamp once it is ``>= timestamp``; raises ``TimeoutError`` after ``timeout_s``.
    """
    current = now(w3)
    if current >= timestamp:
        return current
    if is_anvil(w3):
        return warp(w3, timestamp - current)
    started = time.monotonic()
    while current < timestamp:
        if timeout_s is not None and time.monotonic() - started > timeout_s:
            raise TimeoutError(f"chain clock at {current}, waiting for {timestamp}")
        sleep(min(poll_s, max(0.2, float(timestamp - current))))
        current = now(w3)
    return current


__all__ = ["ANVIL_CHAIN_ID", "advance_to", "block_timestamp", "is_anvil", "mine", "now", "warp"]
