"""Fixtures of the owner console tests (WP7).

- ``console_env``: the owner console's environment (password, session secret, tokens) for one test.
- ``offline_console``: a console without a chain and with a :class:`~owner_helpers.FakeAgent`.
- ``chain_console``: an :class:`~acb.owner.actions.OwnerConsole` on the session's Anvil deployment
  (``fresh_deployed``), with the Anvil owner key and a :class:`~owner_helpers.FakeAgent`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from owner_helpers import PASSWORD, SECRET, FakeAgent


@pytest.fixture
def console_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    env = {
        "OWNER_UI_PASSWORD": PASSWORD,
        "SESSION_SECRET": SECRET,
        "AGENT_TOKEN": "agent-token-" + "a" * 32,
        "EVENTS_TOKEN": "events-token-" + "e" * 32,
        "ACB_OUT_DIR": str(tmp_path / "out"),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


@pytest.fixture
def offline_console(console_env, tmp_path):
    """A console with no chain (the factory fails) and a fake agent: for HTTP-level tests."""
    from acb.logbus import EventBus
    from acb.owner.actions import OwnerConsole

    def no_chain():
        raise FileNotFoundError("no deployment in this test")

    agent = FakeAgent()
    bus = EventBus("owner", out_dir=tmp_path / "bus")
    return OwnerConsole(chain_factory=no_chain, agent=agent, bus=bus)


@pytest.fixture
def chain_console(fresh_deployed, anvil, console_env, tmp_path):
    """An owner console on the Anvil deployment with the Anvil owner key and a fake agent."""
    from acb.chain.tx import resync_all
    from acb.keys import KeyRing
    from acb.logbus import EventBus
    from acb.owner import chain_views as cv
    from acb.owner.actions import OwnerConsole

    resync_all()
    agent = FakeAgent()
    bus = EventBus("owner", out_dir=tmp_path / "bus")
    console = OwnerConsole(
        chain_factory=lambda: cv.OwnerChain.load("anvil", dep=fresh_deployed),
        agent=agent,
        bus=bus,
        owner_account=KeyRing.anvil().account("owner"),
    )
    yield console
    resync_all()
