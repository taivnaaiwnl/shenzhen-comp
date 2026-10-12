"""Fixtures of the wallet agent tests (WP6). Fakes and builders live in ``agent_helpers.py``."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _keys_raise_not_exit():
    """In tests a visible key raises ``PrivateKeyInEnvironment`` instead of ending the test process."""
    from acb.agent.llm import set_exit_on_keys

    set_exit_on_keys(False)
    yield
    set_exit_on_keys(False)
