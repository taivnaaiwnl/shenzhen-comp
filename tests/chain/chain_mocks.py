"""Mock JSON-RPC provider for the chain-layer tests (chunked logs, clock, fees) without a node."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from web3 import Web3
from web3.providers.base import BaseProvider


class MockProvider(BaseProvider):
    """A tiny JSON-RPC provider: answers from ``handlers`` (method -> callable(params)) and records calls.

    A handler may raise :class:`RpcError` to return a JSON-RPC error object.
    """

    def __init__(self, handlers: dict[str, Callable[[list], Any]]):
        super().__init__()
        self.handlers = handlers
        self.calls: list[tuple[str, list]] = []

    def make_request(self, method, params):
        self.calls.append((method, list(params)))
        handler = self.handlers.get(method)
        if handler is None:
            return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": f"no handler for {method}"}}
        try:
            return {"jsonrpc": "2.0", "id": 1, "result": handler(list(params))}
        except RpcError as exc:
            return {"jsonrpc": "2.0", "id": 1, "error": {"code": exc.code, "message": exc.message}}

    def is_connected(self, show_traceback: bool = False) -> bool:
        return True


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def mock_web3(handlers: dict[str, Callable[[list], Any]]) -> Web3:
    return Web3(MockProvider(handlers))
