"""Policy configuration (``config/policy.yaml``, spec 13.3).

:class:`PolicyConfig` is a frozen pydantic model. ``PolicyConfig.load()`` reads the YAML file (BOM
tolerant) and keeps the parsed mapping, whose ``chash`` is the ``config_hash`` every
:class:`~acb.types.PolicyDecision` carries. Tier caps and grade masks are deliberately absent: the
policy reads them from the vault (spec 6.5).
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from acb.canonical import chash
from acb.config import CONFIG_DIR, load_yaml
from acb.types import Grade

DEFAULT_POLICY_FILE = CONFIG_DIR / "policy.yaml"
POLICY_FILE_ENV = "ACB_POLICY_FILE"


def _to_int(value: Any) -> int:
    """Amounts are decimal strings in YAML; plain ints are accepted too (never floats or bools)."""
    if isinstance(value, (bool, float)):
        raise TypeError("amounts must be integers or decimal strings")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise ValueError(f"not an amount: {value!r}")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SpikeConfig(_Frozen):
    multiplier: int = 5
    min_history: int = 3


class InvoiceConfig(_Frozen):
    max_bytes: int = 16384
    max_memo: int = 500
    max_description: int = 80
    max_delivery_s: int = 600
    max_validity_s: int = 900
    max_future_s: int = 30
    min_amount: int = 100_000
    amount_step: int = 10_000

    @field_validator("min_amount", "amount_step", mode="before")
    @classmethod
    def _amounts(cls, value: Any) -> int:
        return _to_int(value)


class QuoteConfig(_Frozen):
    close_after_s: int = 60


class DenylistConfig(_Frozen):
    agent_ids: frozenset[int] = frozenset()
    addresses: frozenset[str] = frozenset()

    @field_validator("addresses", mode="before")
    @classmethod
    def _lower(cls, value: Any) -> Any:
        return frozenset(str(a).lower() for a in (value or ()))


class RepeatedViolationsConfig(_Frozen):
    count: int = 3
    window_s: int = 600


class SignerConfig(_Frozen):
    max_outflows_per_minute: int = 6
    repeated_violations: RepeatedViolationsConfig = RepeatedViolationsConfig()
    max_recorded_decisions_per_hour: int = 30
    receipt_timeout_s: int = 90


class DeliveryConfig(_Frozen):
    grace_s: int = 30
    poll_s: int = 2


class PolicyConfig(_Frozen):
    """Parsed ``config/policy.yaml``. ``raw`` is the parsed mapping that ``config_hash`` covers."""

    policy_version: str = "acb-pol-1.0.0"
    b_cap_bps: int = 5000
    exposure_limit: dict[str, int] = Field(
        default_factory=lambda: {"A": 1_000_000, "B": 500_000, "NEW": 500_000, "NONE": 500_000, "C": 0}
    )
    spike: SpikeConfig = SpikeConfig()
    invoice: InvoiceConfig = InvoiceConfig()
    quote: QuoteConfig = QuoteConfig()
    denylist: DenylistConfig = DenylistConfig()
    recurring_min_tier: int = 2
    signer: SignerConfig = SignerConfig()
    delivery: DeliveryConfig = DeliveryConfig()
    drain_fraction_bps: int = 5000
    raw: dict = Field(default_factory=dict, exclude=True)

    @field_validator("exposure_limit", mode="before")
    @classmethod
    def _exposure(cls, value: Any) -> Any:
        out = {str(k): _to_int(v) for k, v in dict(value or {}).items()}
        unknown = set(out) - {g.name for g in Grade}
        if unknown:
            raise ValueError(f"unknown grades in exposure_limit: {sorted(unknown)}")
        return out

    @property
    def config_hash(self) -> str:
        """``chash`` of the parsed YAML mapping (or of the model's own JSON when built in code)."""
        source = self.raw or self.model_dump(mode="json")
        return chash(_canonicalisable(source))

    def exposure_limit_for(self, grade: Grade) -> int:
        """Exposure limit of a grade; a grade missing from the file has limit 0 (most restrictive)."""
        return int(self.exposure_limit.get(Grade(grade).name, 0))

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> PolicyConfig:
        return cls.model_validate({**data, "raw": data})

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> PolicyConfig:
        """Read ``config/policy.yaml`` (or ``ACB_POLICY_FILE``, or ``path``)."""
        target = path or os.environ.get(POLICY_FILE_ENV) or DEFAULT_POLICY_FILE
        data = load_yaml(target) or {}
        if not isinstance(data, dict):
            raise TypeError("policy.yaml must be a mapping")
        return cls.from_mapping(data)


def _canonicalisable(obj: Any) -> Any:
    """Sets become sorted lists so the mapping can be hashed canonically."""
    if isinstance(obj, dict):
        return {str(k): _canonicalisable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_canonicalisable(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted(_canonicalisable(v) for v in obj)
    return obj


__all__ = ["DEFAULT_POLICY_FILE", "POLICY_FILE_ENV", "PolicyConfig"]
