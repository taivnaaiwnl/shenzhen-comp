"""Personas of the simulated market and their behaviour scripts (spec 9.1, 9.2).

``config/personas.yaml`` is the single source: WP3's deploy reads the identity fields (name, category,
catalogue, anchor flag, description) and the simulator reads the rest (memos, behaviour scripts,
injection templates, the market plan and the attack parameters).

Everything in this module is pure: no chain, no clock, no I/O apart from reading the YAML file.
Order and quote numbers start at 1 and count every buyer (Beacon and the wallet alike).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from acb.config import CONFIG_DIR, load_yaml

PERSONAS_FILE = CONFIG_DIR / "personas.yaml"

AMOUNT_STEP = 10_000
"""Invoice amounts are multiples of 0.01 USDC (spec 0.1, 6.2 V03)."""
MAX_DELIVERY_S = 600
MAX_VALIDITY_S = 900
MAX_MEMO = 500
MAX_DESCRIPTION = 80
TEMPLATE_NAMES = ("injection_amount", "injection_drain")
ATTACKER_PLACEHOLDER = "{attackerId}"

DeliveryMode = Literal["on_time", "late", "never"]
Override = Literal["honest", "default", "inject"]
OVERRIDES: tuple[str, ...] = ("honest", "default", "inject")

_PRINTABLE = re.compile(r"^[\x20-\x7e]*$")
_SKU = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class PersonaConfigError(ValueError):
    """``config/personas.yaml`` does not follow the rules of this module."""


@dataclass(frozen=True)
class Sku:
    """One catalogue entry: unit price in base units and the line description of invoices."""

    sku: str
    unit_price: int
    description: str


@dataclass(frozen=True)
class Behaviour:
    """The scripted behaviour of one persona (deterministic in the order and quote numbers)."""

    delivery_s: int = 120
    validity_s: int = 600
    late_orders: str | tuple[int, ...] = ()
    late_after_s: int = 5
    never_from_order: int | None = None
    outage_orders: tuple[int, ...] = ()
    inject_from_quote: int | None = None
    inject_after_orders: int = 0
    injections: Mapping[str, str] = field(default_factory=dict)
    unsolicited: bool = False

    def is_late(self, n: int) -> bool:
        if self.late_orders == "odd":
            return n % 2 == 1
        return isinstance(self.late_orders, tuple) and n in self.late_orders

    def delivery_mode(self, n: int, override: Override | None = None) -> DeliveryMode:
        """How order number ``n`` (1-based) is handled: on time, late (after the deadline) or never.

        ``override`` (set by the attack harness, e.g. ``bust_out``) wins over the script.
        """
        if n < 1:
            raise ValueError("order numbers start at 1")
        if override == "default":
            return "never"
        if override == "honest":
            return "on_time"
        if self.never_from_order is not None and n >= self.never_from_order:
            return "never"
        if n in self.outage_orders:
            return "never"
        if self.is_late(n):
            return "late"
        return "on_time"

    def injection_for(
        self, quote_no: int, orders_before: int, sku: str, override: Override | None = None
    ) -> str | None:
        """Template name to put into the memo of quote number ``quote_no`` for ``sku``, or None.

        Injection starts at the first quote that is at least ``inject_from_quote`` and comes after at
        least ``inject_after_orders`` orders (Orion: honest for its first 5 orders, 4 Beacon + 1 wallet,
        then from its 6th quote on). An ``honest`` override switches it off; ``inject`` switches it on
        for every quote of an sku that has a template (attack harness).
        """
        if override == "honest":
            return None
        if override == "inject":
            return self.injections.get(sku)
        if self.inject_from_quote is None:
            return None
        if quote_no >= self.inject_from_quote and orders_before >= self.inject_after_orders:
            return self.injections.get(sku)
        return None


@dataclass(frozen=True)
class PersonaSpec:
    """One persona of ``config/personas.yaml``."""

    key: str
    name: str
    category: str
    description: str
    skus: Mapping[str, Sku]
    anchor: bool
    memo: str
    behaviour: Behaviour

    @property
    def is_provider(self) -> bool:
        return bool(self.skus)


@dataclass(frozen=True)
class SimConfig:
    """The parsed personas file."""

    personas: Mapping[str, PersonaSpec]
    templates: Mapping[str, str]
    market: Mapping[str, Any]
    attacks: Mapping[str, Any]

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> SimConfig:
        """Load and validate ``config/personas.yaml`` (or ``path``)."""
        return parse_config(load_yaml(path or PERSONAS_FILE))

    def persona(self, key: str) -> PersonaSpec:
        if key not in self.personas:
            raise KeyError(f"unknown persona {key!r}")
        return self.personas[key]

    def template(self, name: str, *, attacker_id: int | None = None) -> str:
        """The exact text of an injection template; ``{attackerId}`` becomes the attacker's agentId."""
        if name not in self.templates:
            raise KeyError(f"unknown injection template {name!r}")
        text = self.templates[name]
        if ATTACKER_PLACEHOLDER in text:
            if attacker_id is None:
                raise ValueError(f"template {name!r} needs the attacker's agentId")
            text = text.replace(ATTACKER_PLACEHOLDER, str(int(attacker_id)))
        return text


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def _int(value: Any, what: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or value is None:
        raise PersonaConfigError(f"{what} must be an integer")
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if not isinstance(value, int) or value < minimum:
        raise PersonaConfigError(f"{what} must be an integer >= {minimum}")
    return value


def parse_amount(value: Any, what: str) -> int:
    """A base-unit amount from YAML (a decimal string, or an int); never a float."""
    return _int(value, what)


def _text(value: Any, what: str, limit: int) -> str:
    text = "" if value is None else str(value)
    if len(text) > limit or not _PRINTABLE.fullmatch(text):
        raise PersonaConfigError(f"{what} must be printable ASCII of at most {limit} characters")
    return text


def _skus(key: str, raw: Any) -> dict[str, Sku]:
    if not raw:
        return {}
    items: list[tuple[str, Any]]
    if isinstance(raw, Mapping):
        items = [(str(k), v) for k, v in raw.items()]
    elif isinstance(raw, list):
        items = [(str(v.get("sku")), v) for v in raw if isinstance(v, Mapping)]
    else:
        raise PersonaConfigError(f"persona {key}: skus must be a mapping or a list")
    out: dict[str, Sku] = {}
    for sku, value in items:
        if not _SKU.fullmatch(sku):
            raise PersonaConfigError(f"persona {key}: bad sku name {sku!r}")
        if isinstance(value, Mapping):
            price = value.get("unit_price", value.get("unitPrice", value.get("price")))
            description = value.get("description") or sku
        else:
            price, description = value, sku
        unit = parse_amount(price, f"persona {key}: price of {sku}")
        if unit == 0 or unit % AMOUNT_STEP:
            raise PersonaConfigError(f"persona {key}: price of {sku} must be a positive multiple of {AMOUNT_STEP}")
        out[sku] = Sku(sku, unit, _text(description, f"persona {key}: description of {sku}", MAX_DESCRIPTION))
    return out


def _late(value: Any, key: str) -> str | tuple[int, ...]:
    if value in (None, "", "none"):
        return ()
    if value == "odd":
        return "odd"
    if isinstance(value, list):
        return tuple(_int(v, f"persona {key}: late_orders", minimum=1) for v in value)
    raise PersonaConfigError(f"persona {key}: late_orders must be 'odd', 'none' or a list")


def _behaviour(key: str, raw: Any, skus: Mapping[str, Sku], templates: Mapping[str, str]) -> Behaviour:
    raw = dict(raw or {})
    known = {
        "delivery_s",
        "validity_s",
        "late_orders",
        "late_after_s",
        "never_from_order",
        "outage_orders",
        "inject_from_quote",
        "inject_after_orders",
        "injections",
        "unsolicited",
    }
    unknown = set(raw) - known
    if unknown:
        raise PersonaConfigError(f"persona {key}: unknown behaviour fields {sorted(unknown)}")
    delivery_s = _int(raw.get("delivery_s", 120), f"persona {key}: delivery_s", minimum=1)
    validity_s = _int(raw.get("validity_s", 600), f"persona {key}: validity_s", minimum=1)
    if delivery_s > MAX_DELIVERY_S:
        raise PersonaConfigError(f"persona {key}: delivery_s must be <= {MAX_DELIVERY_S}")
    if validity_s > MAX_VALIDITY_S:
        raise PersonaConfigError(f"persona {key}: validity_s must be <= {MAX_VALIDITY_S}")
    injections = {str(k): str(v) for k, v in (raw.get("injections") or {}).items()}
    for sku, name in injections.items():
        if sku not in skus:
            raise PersonaConfigError(f"persona {key}: injection for unknown sku {sku!r}")
        if name not in templates:
            raise PersonaConfigError(f"persona {key}: unknown injection template {name!r}")
    never = raw.get("never_from_order")
    inject_from = raw.get("inject_from_quote")
    return Behaviour(
        delivery_s=delivery_s,
        validity_s=validity_s,
        late_orders=_late(raw.get("late_orders"), key),
        late_after_s=_int(raw.get("late_after_s", 5), f"persona {key}: late_after_s", minimum=1),
        never_from_order=None if never is None else _int(never, f"persona {key}: never_from_order", minimum=1),
        outage_orders=tuple(
            _int(v, f"persona {key}: outage_orders", minimum=1) for v in raw.get("outage_orders") or ()
        ),
        inject_from_quote=None
        if inject_from is None
        else _int(inject_from, f"persona {key}: inject_from_quote", minimum=1),
        inject_after_orders=_int(raw.get("inject_after_orders", 0), f"persona {key}: inject_after_orders"),
        injections=injections,
        unsolicited=bool(raw.get("unsolicited", False)),
    )


def parse_config(data: Any) -> SimConfig:
    """Validate the parsed YAML of ``config/personas.yaml``."""
    if not isinstance(data, Mapping) or not isinstance(data.get("personas"), Mapping):
        raise PersonaConfigError("personas.yaml must have a 'personas' mapping")
    templates = {str(k): " ".join(str(v).split()) for k, v in (data.get("injection_templates") or {}).items()}
    for name in TEMPLATE_NAMES:
        if name not in templates:
            raise PersonaConfigError(f"injection template {name!r} missing")
    for name, text in templates.items():
        _text(text, f"injection template {name}", MAX_MEMO)
    personas: dict[str, PersonaSpec] = {}
    for key, entry in data["personas"].items():
        key = str(key)
        if not isinstance(entry, Mapping):
            raise PersonaConfigError(f"persona {key} must be a mapping")
        name = entry.get("name") or entry.get("display_name")
        if not name:
            raise PersonaConfigError(f"persona {key} has no name")
        skus = _skus(key, entry.get("skus"))
        personas[key] = PersonaSpec(
            key=key,
            name=_text(name, f"persona {key}: name", 60),
            category=_text(entry.get("category") or "research", f"persona {key}: category", 40),
            description=_text(entry.get("description") or "", f"persona {key}: description", 200),
            skus=skus,
            anchor=bool(entry.get("anchor", False)),
            memo=_text(entry.get("memo") or "", f"persona {key}: memo", MAX_MEMO),
            behaviour=_behaviour(key, entry.get("behaviour"), skus, templates),
        )
    return SimConfig(
        personas=personas,
        templates=templates,
        market=dict(data.get("market") or {}),
        attacks=dict(data.get("attacks") or {}),
    )


__all__ = [
    "AMOUNT_STEP",
    "MAX_DELIVERY_S",
    "MAX_VALIDITY_S",
    "OVERRIDES",
    "PERSONAS_FILE",
    "TEMPLATE_NAMES",
    "Behaviour",
    "DeliveryMode",
    "Override",
    "PersonaConfigError",
    "PersonaSpec",
    "SimConfig",
    "Sku",
    "parse_amount",
    "parse_config",
]
