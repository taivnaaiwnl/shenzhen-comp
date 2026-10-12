"""Fixtures of the signer tests.

``env`` (marker ``chain``): the session deployment on a private Anvil (``tests/conftest.py``) with mandate M1 registered by the owner (Atlas, Slowpoke, Ghost, Orion, Nova; 1.00 per
tx, 4.00 total, 24 h), grades posted by the bureau (Atlas A, Slowpoke B, Ghost A, Orion A, Nova NEW,
upgrade delay waited) and an :class:`~acb.signer.executor.Executor` holding the agent signer's Anvil key,
with its state and output in ``tmp_path``. The set-up runs once per module (``signer_base``); every test
starts from an ``evm_snapshot`` of it and is reverted afterwards. Helpers are in ``signer_env.py``.
"""

from __future__ import annotations

import dataclasses

import pytest
from signer_env import SignerEnv, U, make_executor

from acb.canonical import chash
from acb.chain.contracts import bind
from acb.keys import KeyRing
from acb.policy import PolicyConfig
from acb.types import Grade


def _rpc(w3, method: str, params: list):
    response = w3.provider.make_request(method, params)
    if "error" in response:
        raise RuntimeError(f"{method} failed: {response['error']}")
    return response["result"]


@pytest.fixture(scope="module")
def signer_base(anvil, deployed, tmp_path_factory):
    """Mandate and grades set up once per test module on top of the session deployment; the chain is
    reverted to the pristine deployment when the module ends."""
    _, w3 = anvil
    pristine = _rpc(w3, "evm_snapshot", [])
    try:
        keys = KeyRing.anvil()
        c = bind(w3, deployed)
        tmp = tmp_path_factory.mktemp("signer")
        e = SignerEnv(
            w3=w3, dep=deployed, keys=keys, c=c, executor=None, cfg=PolicyConfig.load(), mandate_id="", tmp=tmp
        )
        names = ["atlas", "slowpoke", "ghost", "orion", "nova"]
        ids = [e.agent_id(n) for n in names]
        wallets = [c.identity.get_agent_wallet(i) for i in ids]
        mandate_id = chash({"test": "M1", "salt": tmp.name})
        e.send(
            "owner",
            e.vault.register_mandate(mandate_id, ids, wallets, 1 * U, 4 * U, e.now() + 86_400, False, chash({"p": 1})),
        )
        e.mandate_id = mandate_id
        for name, grade in (("atlas", Grade.A), ("slowpoke", Grade.B), ("ghost", Grade.A), ("orion", Grade.A)):
            e.post_grade(name, grade)
        e.post_grade("nova", Grade.NEW)
        e.warp(e.c.acb.upgrade_delay() + 1)
        yield e
    finally:
        _rpc(w3, "evm_revert", [pristine])


@pytest.fixture
def env(signer_base, tmp_path):
    """The module's set-up chain, reverted after each test, with a fresh executor in ``tmp_path``."""
    w3 = signer_base.w3
    snapshot = _rpc(w3, "evm_snapshot", [])
    e = dataclasses.replace(signer_base, tmp=tmp_path, senders={}, executor=None)
    e.executor = make_executor(e)
    try:
        yield e
    finally:
        _rpc(w3, "evm_revert", [snapshot])
