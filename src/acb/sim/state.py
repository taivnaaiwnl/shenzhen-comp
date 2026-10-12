"""Persistent state of the simulator: counters, issued invoices, orders, mandates, attack entities.

Kept in ``$ACB_OUT_DIR/sim/state.json`` (written atomically after every change), so a restarted
simulator continues the same scripts: Slowpoke's odd orders, Ghost's fifth order and Orion's sixth
quote are counted across restarts, and invoice ids are never reused for the same issuer.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from acb.canonical import atomic_write_bytes

STATE_SCHEMA = "acb-sim-state/1"


def _empty() -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "counters": {},  # persona -> {"invoices", "quotes", "orders", "messages"}
        "invoices": {},  # "<persona>:<invoiceId>" -> summary of an issued invoice
        "orders": {},  # orderId -> {"persona", "n", "invoiceId", "vault", "mode", "delivered", ...}
        "labels": {},  # scripted order slot (e.g. "warmup:atlas:1") -> {"invoiceId", "orderId", "vault", ...}
        "mandates": {},  # "<vault name>:<label>" -> {"mandateId", "payees", "tx"}
        "overrides": {},  # persona -> "honest" | "default"
        "spam": [],  # invoice hashes pushed as unsolicited invoices, in push order
        "rounds": {},  # action -> list of rounds (lists of order ids)
        "entities": {},  # attack identities: label -> {"agentId", "address", "keyIndex", ...}
        "vaults": {},  # attack vaults: name -> {"address", "agentId", "owner", "agent", ...}
    }


class SimState:
    """Thread-safe JSON state. Use :meth:`edit` for read-modify-write blocks."""

    def __init__(self, path: str | os.PathLike[str] | None):
        self.path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self.data = _empty()
        if self.path is not None and self.path.is_file():
            loaded = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if loaded.get("schema") != STATE_SCHEMA:
                raise ValueError(f"{self.path}: not a simulator state file")
            for key, value in _empty().items():
                loaded.setdefault(key, value)
            self.data = loaded

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            text = json.dumps(self.data, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False)
            atomic_write_bytes(self.path, (text + "\n").encode("utf-8"))

    @contextmanager
    def edit(self) -> Iterator[dict[str, Any]]:
        """Lock, yield the data for changes, then save."""
        with self._lock:
            yield self.data
            self.save()

    # ---- counters ----
    def counter(self, persona: str, name: str) -> int:
        with self._lock:
            return int(self.data["counters"].get(persona, {}).get(name, 0))

    def bump(self, persona: str, name: str, *, at_least: int = 0) -> int:
        """Increment a counter (to at least ``at_least``) and return the new value."""
        with self.edit() as data:
            counters = data["counters"].setdefault(persona, {})
            value = max(int(counters.get(name, 0)) + 1, at_least)
            counters[name] = value
            return value

    def override(self, persona: str) -> str | None:
        with self._lock:
            return self.data["overrides"].get(persona)


__all__ = ["STATE_SCHEMA", "SimState"]
