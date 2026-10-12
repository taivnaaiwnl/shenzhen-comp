"""``config/autonomy.yaml`` (spec 13.3): parameters of the autonomy operator's off-chain rules (5.4)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from acb.config import CONFIG_DIR, load_yaml
from acb.types import Grade

AUTONOMY_PATH = CONFIG_DIR / "autonomy.yaml"


@dataclass(frozen=True)
class AutonomyConfig:
    loss_limit: tuple[int, int, int, int]
    own_grade_promote: frozenset[Grade]
    min_distinct_payees: int
    poll_s: int
    evaluate_after_rescore: bool
    signer_unreachable_alert_after: int
    decision_tx_grace_s: int = 300


def load_autonomy_config(path: str | os.PathLike[str] | None = None) -> AutonomyConfig:
    data = load_yaml(path or AUTONOMY_PATH) or {}
    limits = [int(x) for x in data.get("loss_limit") or []]
    if len(limits) != 4:
        raise ValueError("loss_limit needs one value per tier 0-3")
    return AutonomyConfig(
        loss_limit=(limits[0], limits[1], limits[2], limits[3]),
        own_grade_promote=frozenset(Grade[str(g)] for g in data.get("own_grade_promote") or []),
        min_distinct_payees=int(data.get("min_distinct_payees", 2)),
        poll_s=int(data.get("poll_s", 30)),
        evaluate_after_rescore=bool(data.get("evaluate_after_rescore", True)),
        signer_unreachable_alert_after=int(data.get("signer_unreachable_alert_after", 3)),
        decision_tx_grace_s=int(data.get("decision_tx_grace_s", 300)),
    )


__all__ = ["AUTONOMY_PATH", "AutonomyConfig", "load_autonomy_config"]
