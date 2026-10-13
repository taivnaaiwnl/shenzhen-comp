"""Web3 factory and chunked log queries (spec 0.4 items 4 and 6, 4.11).

- :func:`make_web3` builds a ``Web3`` for a :class:`~acb.types.NetworkProfile`: ``purpose="rpc"`` uses
  ``rpc_url``, ``purpose="logs"`` uses ``log_rpc_url`` (a second provider for indexing on Base
  Sepolia). The instance remembers the profile's ``logs_chunk_blocks``.
- :func:`get_logs_chunked` never asks for more than ``logs_chunk_blocks`` blocks per ``eth_getLogs``
  (the public Base Sepolia RPC refuses ranges above 200 blocks, error -32614). Each chunk is retried 3
  times with backoff 0.5 s, 1 s, 2 s; a chunk that the provider still refuses as "too large" is split
  in half and retried, so a provider with a smaller limit than configured also works.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from web3 import Web3
from web3.exceptions import Web3RPCError
from web3.middleware import Web3Middleware

from acb.types import NetworkProfile

DEFAULT_TIMEOUT_S = 30
DEFAULT_CHUNK_BLOCKS = 200
"""Used when neither the call nor the ``Web3`` instance names a chunk size (the most restrictive known limit)."""

RETRY_BACKOFF_S: tuple[float, ...] = (0.5, 1.0, 2.0)
"""Sleep before each retry of a failed ``eth_getLogs`` chunk (3 retries)."""

_CHUNK_ATTR = "_acb_logs_chunk_blocks"
_NETWORK_ATTR = "_acb_network"

_RANGE_ERROR_CODES = {-32614, -32005, -32602}
_RANGE_ERROR_WORDS = (
    "block range",
    "range is too large",
    "range too large",
    "too many blocks",
    "limited to",
    "query returned more than",
    "too large",
)


_CHAIN_ATTR = "_acb_chain_id"
ANVIL_CHAIN_ID = 31337

# state reads whose block parameter ("latest") ReadYourWrites pins; value = index of the block parameter
_READ_BLOCK_PARAM = {
    "eth_call": 1,
    "eth_estimateGas": 1,
    "eth_getCode": 1,
    "eth_getBalance": 1,
    "eth_getStorageAt": 2,
    "eth_getTransactionCount": 1,
}
_LAG_WORDS = ("header not found", "unknown block", "block not found", "missing trie node", "historical state")


def _is_lag_error(error: Any) -> bool:
    message = str(error.get("message", "") if isinstance(error, dict) else error).lower()
    return any(w in message for w in _LAG_WORDS)


class ReadYourWrites(Web3Middleware):
    """Read-your-writes on a load-balanced public RPC.

    ``sepolia.base.org`` is served by several nodes that can lag each other by a block or two, so a read
    sent right after a receipt may reach a node that has not seen that block (it then reports no code at
    a contract deployed one block earlier, an old balance, an old mandate). For state reads at "latest"
    this middleware asks instead for an explicit block: the highest of the newest block any local process
    has seen in a receipt (``acb.chain.tx.highest_receipt_block``) and the last ``eth_blockNumber``
    (cached for one second). A node that does not have that block yet answers "header not found"; the
    read is retried for up to ``WAIT_S``. Reads at an explicit block or at "pending" are left alone.
    Installed by :func:`make_web3` on public networks only.
    """

    WAIT_S = 30.0
    LATEST_CACHE_S = 1.0

    def __init__(self, w3: Any) -> None:
        super().__init__(w3)
        self._cache: tuple[float, int] = (0.0, -1)

    def _latest(self, make_request: Callable[..., Any]) -> int:
        now = time.monotonic()
        stamp, value = self._cache
        if now - stamp < self.LATEST_CACHE_S:
            return value
        response = make_request("eth_blockNumber", [])
        result = response.get("result") if isinstance(response, dict) else None
        value = max(value, int(result, 16)) if result else value
        self._cache = (now, value)
        return value

    def wrap_make_request(self, make_request: Callable[..., Any]) -> Callable[..., Any]:
        def middleware(method: str, params: Any) -> Any:
            pos = _READ_BLOCK_PARAM.get(method)
            if pos is None:
                return make_request(method, params)
            params = list(params or [])
            block = params[pos] if len(params) > pos else "latest"
            if block not in ("latest", None):
                return make_request(method, params)
            from acb.chain.tx import highest_receipt_block

            chain_id = getattr(self._w3, _CHAIN_ATTR, None)
            mark = highest_receipt_block(int(chain_id)) if chain_id else -1
            if mark < 0:
                return make_request(method, params)
            target = max(mark, self._latest(make_request))
            while len(params) <= pos:
                params.append("latest")
            params[pos] = hex(target)
            deadline = time.monotonic() + self.WAIT_S
            while True:
                response = make_request(method, params)
                error = response.get("error") if isinstance(response, dict) else None
                if not error or not _is_lag_error(error) or time.monotonic() >= deadline:
                    return response
                time.sleep(0.5)

        return middleware


def make_web3(net: NetworkProfile, purpose: str = "rpc", *, timeout: float = DEFAULT_TIMEOUT_S) -> Web3:
    """A ``Web3`` (HTTP) for ``net``. ``purpose`` is ``"rpc"`` (reads and writes) or ``"logs"`` (indexing).

    On public networks the instance reads its own writes (:class:`ReadYourWrites`)."""
    if purpose not in ("rpc", "logs"):
        raise ValueError("purpose must be 'rpc' or 'logs'")
    url = net.log_rpc_url if purpose == "logs" else net.rpc_url
    if not url:
        raise ValueError(f"no RPC URL configured for {net.name} ({purpose})")
    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": timeout}))
    setattr(w3, _CHUNK_ATTR, int(net.logs_chunk_blocks))
    setattr(w3, _NETWORK_ATTR, net.name)
    setattr(w3, _CHAIN_ATTR, int(net.chain_id))
    if int(net.chain_id) != ANVIL_CHAIN_ID:
        w3.middleware_onion.add(ReadYourWrites, name="acb_read_your_writes")
    return w3


def chunk_blocks_of(w3: Any, default: int = DEFAULT_CHUNK_BLOCKS) -> int:
    """The ``logs_chunk_blocks`` remembered by :func:`make_web3`, else ``default``."""
    value = getattr(w3, _CHUNK_ATTR, None)
    return int(value) if value else default


def set_chunk_blocks(w3: Any, blocks: int) -> None:
    """Attach a chunk size to a ``Web3`` created elsewhere (tests, fixtures)."""
    if blocks < 1:
        raise ValueError("chunk size must be >= 1")
    setattr(w3, _CHUNK_ATTR, int(blocks))


def check_chain_id(w3: Web3, net: NetworkProfile) -> int:
    """Raise ``RuntimeError`` unless the node reports ``net.chain_id``; returns the chain id."""
    chain_id = int(w3.eth.chain_id)
    if chain_id != net.chain_id:
        raise RuntimeError(f"RPC for {net.name} reports chain id {chain_id}, expected {net.chain_id}")
    return chain_id


def rpc(w3: Web3, method: str, params: list | None = None) -> Any:
    """Raw JSON-RPC call; raises ``RuntimeError`` on an error response."""
    response = w3.provider.make_request(method, params or [])
    if "error" in response:
        raise RuntimeError(f"{method} failed: {response['error']}")
    return response.get("result")


def is_range_error(exc: BaseException) -> bool:
    """True if a provider refused an ``eth_getLogs`` because the block range is too large."""
    payload: Any = None
    if isinstance(exc, Web3RPCError):
        payload = exc.rpc_response.get("error") if isinstance(exc.rpc_response, dict) else None
    if payload is None and exc.args and isinstance(exc.args[0], dict):
        payload = exc.args[0]
    code = payload.get("code") if isinstance(payload, dict) else None
    message = str(payload.get("message", "")) if isinstance(payload, dict) else str(exc)
    message = message.lower()
    if code == -32614:
        return True
    return (code in _RANGE_ERROR_CODES or code is None) and any(w in message for w in _RANGE_ERROR_WORDS)


def _resolve_block(w3: Any, block: int | str) -> int:
    if isinstance(block, int):
        return block
    if isinstance(block, str) and block.isdigit():
        return int(block)
    return int(w3.eth.get_block(block)["number"])


def _event_w3(event: Any) -> Any:
    w3 = getattr(event, "w3", None)
    if w3 is None:
        raise TypeError("event must be a web3 contract event (contract.events.Name)")
    return w3


def iter_chunks(from_block: int, to_block: int, size: int) -> Iterable[tuple[int, int]]:
    """Inclusive ``(start, end)`` ranges of at most ``size`` blocks covering ``from_block..to_block``."""
    if size < 1:
        raise ValueError("chunk size must be >= 1")
    start = from_block
    while start <= to_block:
        end = min(start + size - 1, to_block)
        yield start, end
        start = end + 1


def fetch_with_retry(
    fetch: Callable[[int, int], list],
    start: int,
    end: int,
    *,
    sleep: Callable[[float], None] = time.sleep,
    backoff: tuple[float, ...] = RETRY_BACKOFF_S,
) -> list:
    """Run ``fetch(start, end)`` with retries; split the range when the provider says it is too large."""
    attempt = 0
    while True:
        try:
            return list(fetch(start, end))
        except Exception as exc:  # provider errors come in many types
            if is_range_error(exc) and end > start:
                mid = (start + end) // 2
                left = fetch_with_retry(fetch, start, mid, sleep=sleep, backoff=backoff)
                right = fetch_with_retry(fetch, mid + 1, end, sleep=sleep, backoff=backoff)
                return left + right
            if attempt >= len(backoff):
                raise
            sleep(backoff[attempt])
            attempt += 1


def get_logs_chunked(
    event: Any,
    from_block: int | str,
    to_block: int | str,
    argument_filters: Mapping[str, Any] | None = None,
    *,
    chunk_blocks: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list:
    """Decoded logs of ``event`` (``contract.events.Name``) in ``from_block..to_block`` (inclusive).

    ``to_block`` may be ``"latest"`` (resolved once, at the start). Logs come back in chain order.
    ``chunk_blocks`` defaults to the ``logs_chunk_blocks`` of the ``Web3`` the contract is bound to
    (see :func:`make_web3`), else 200. Bind the contract to ``make_web3(net, "logs")`` for indexing.
    """
    w3 = _event_w3(event)
    start = _resolve_block(w3, from_block)
    end = _resolve_block(w3, to_block)
    if end < start:
        return []
    size = chunk_blocks or chunk_blocks_of(w3)
    filters = dict(argument_filters or {})

    def fetch(a: int, b: int) -> list:
        kwargs: dict[str, Any] = {"from_block": a, "to_block": b}
        if filters:
            kwargs["argument_filters"] = filters
        return list(event.get_logs(**kwargs))

    out: list = []
    for a, b in iter_chunks(start, end, size):
        out.extend(fetch_with_retry(fetch, a, b, sleep=sleep))
    return out


def get_raw_logs_chunked(
    w3: Web3,
    address: str | list[str] | None,
    topics: list | None,
    from_block: int | str,
    to_block: int | str,
    *,
    chunk_blocks: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list:
    """Undecoded ``eth_getLogs`` over a block range, chunked and retried like :func:`get_logs_chunked`."""
    start = _resolve_block(w3, from_block)
    end = _resolve_block(w3, to_block)
    if end < start:
        return []
    size = chunk_blocks or chunk_blocks_of(w3)
    base: dict[str, Any] = {}
    if address is not None:
        base["address"] = (
            [Web3.to_checksum_address(a) for a in address]
            if isinstance(address, list)
            else Web3.to_checksum_address(address)
        )
    if topics is not None:
        base["topics"] = topics

    def fetch(a: int, b: int) -> list:
        return list(w3.eth.get_logs({**base, "fromBlock": a, "toBlock": b}))

    out: list = []
    for a, b in iter_chunks(start, end, size):
        out.extend(fetch_with_retry(fetch, a, b, sleep=sleep))
    return out


__all__ = [
    "DEFAULT_CHUNK_BLOCKS",
    "RETRY_BACKOFF_S",
    "check_chain_id",
    "chunk_blocks_of",
    "fetch_with_retry",
    "get_logs_chunked",
    "get_raw_logs_chunked",
    "is_range_error",
    "iter_chunks",
    "make_web3",
    "rpc",
    "set_chunk_blocks",
]
