"""Fixtures of the chain-layer tests (WP3).

The shared ``anvil``, ``deployed``, ``fresh_deployed`` and ``warp`` fixtures come from
``tests/conftest.py`` (WP1). This file adds a small bundle of bound contracts for the Anvil tests;
the mock JSON-RPC provider lives in ``chain_mocks.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from acb.keys import KeyRing


@pytest.fixture
def ring() -> KeyRing:
    return KeyRing.anvil()


@dataclass
class Chain:
    """The session deployment, bound for one test."""

    w3: Any
    net: Any
    dep: Any
    keys: KeyRing
    c: Any  # acb.chain.contracts.ChainContracts

    def sender(self, role: str):
        from acb.chain.tx import TxSender

        return TxSender(self.w3, self.keys.account(role))


@pytest.fixture
def chain(anvil, anvil_profile, fresh_deployed) -> Chain:
    """Bound contracts of the session deployment; state is reverted after the test."""
    from acb.chain.contracts import bind

    _, w3 = anvil
    return Chain(w3=w3, net=anvil_profile, dep=fresh_deployed, keys=KeyRing.anvil(), c=bind(w3, fresh_deployed))
