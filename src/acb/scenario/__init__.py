"""Scenario runner, attack and benign suites, evidence export (package WP9, spec 10, 12.3, 12.4).

- :mod:`acb.scenario.env`: one running system (chain, deployment, the six services, clients).
- :mod:`acb.scenario.recorder`: every transaction of a run, attributed to a step, with receipts.
- :mod:`acb.scenario.steps` and :mod:`acb.scenario.runner`: the recorded run S0-S21 (10.2).
- :mod:`acb.scenario.attacks`: the counted attack suite A01-A63 (12.3).
- :mod:`acb.scenario.benign`: the benign suite B01-B14 (12.4).
- :mod:`acb.scenario.evidence` and :mod:`acb.scenario.headline`: ``evidence/<network>/`` (10.4).
- :mod:`acb.scenario.cli`: ``acb scenario run|reset``, ``acb attacks run``, ``acb evidence export|verify``.
"""

from __future__ import annotations

__all__: list[str] = []
