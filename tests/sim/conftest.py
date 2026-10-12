"""Fixtures of the simulator tests (WP8).

- ``fake_sim``: a :class:`~acb.sim.core.Simulator` over :class:`sim_helpers.FakeChain` (no node), with a
  captured inbox; output in ``tmp_path``.
- ``anvil_sim``: a simulator on the session Anvil (WP1's ``fresh_deployed``: the chain is reverted after
  the test), Anvil keys, a captured inbox; output in ``tmp_path``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from sim_helpers import FakeInbox, FakeSim, make_fake_sim

from acb.keys import KeyRing


@pytest.fixture
def fake_sim(tmp_path) -> FakeSim:
    return make_fake_sim(tmp_path)


@dataclass
class AnvilSim:
    sim: Any
    w3: Any
    dep: Any
    keys: KeyRing
    inbox: FakeInbox


@pytest.fixture
def anvil_sim(anvil, anvil_profile, fresh_deployed, tmp_path) -> AnvilSim:
    from acb.sim.core import Simulator

    _, w3 = anvil
    keys = KeyRing.anvil()
    inbox = FakeInbox()
    sim = Simulator(
        anvil_profile, fresh_deployed, keys, w3=w3, out_dir=tmp_path, inbox=inbox, evidence_base="evidence/anvil/"
    )
    return AnvilSim(sim, w3, fresh_deployed, keys, inbox)
