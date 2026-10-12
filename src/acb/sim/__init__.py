"""Counterparty simulator (WP8, process P6, spec 9): every other agent of the demo, each with its own key.

Modules: :mod:`~acb.sim.personas` (config and behaviour scripts), :mod:`~acb.sim.invoices` (signed
wire objects), :mod:`~acb.sim.state` (persisted counters), :mod:`~acb.sim.core` (the simulator and the
provider endpoints), :mod:`~acb.sim.market` and :mod:`~acb.sim.attacks` (scripted actions),
:mod:`~acb.sim.actions` (their registry), :mod:`~acb.sim.service` (FastAPI) and :mod:`~acb.sim.cli`.
"""
