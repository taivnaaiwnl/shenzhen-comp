"""Persistent state of the wallet agent (spec 6.2, 6.6): ``data/<network>/agent/state.json``.

Quote requests, seen invoices, superseded invoices, orders (pending and settled), drafts, verified
mandates, the purchase plan, spam evidence counts, filed reports, the dialogue and the owner's
messages survive a restart. Every mutation rewrites the file atomically (temp file + ``os.replace``)
as canonical JSON, so no float and no partial file can ever be read back.

The folder is tied to one deployment on one chain instance: :func:`prepare_state_dir` archives an older
state (``archive/<n>/``) when the deployment fingerprint changes (a fresh Anvil, a new deployment), so
quote requests and orders of another chain are never mixed in. ``ACB_AGENT_STATE_DIR`` overrides the
folder (tests).
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from acb.canonical import write_canonical

STATE_FILE = "state.json"
FINGERPRINT_FILE = "fingerprint.txt"
STATE_DIR_ENV = "ACB_AGENT_STATE_DIR"

SECTIONS: dict[str, Any] = {
    "quoteRequests": {},
    "seenInvoices": {},
    "superseded": {},
    "orders": {},
    "drafts": {},
    "mandates": {},
    "purchases": {},
    "spam": {},
    "spamReported": {},
    "decisions": {},
    "reports": {},
    "messages": [],
    "dialogue": {},
}


def _empty() -> dict[str, Any]:
    return json.loads(json.dumps(SECTIONS))


def prepare_state_dir(base: str | os.PathLike[str], fingerprint: str) -> Path:
    """Use ``base`` for this deployment; archive what belongs to another deployment."""
    folder = Path(base)
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / FINGERPRINT_FILE
    current = marker.read_text(encoding="utf-8").strip() if marker.is_file() else None
    if current is not None and current != fingerprint:
        archive_root = folder / "archive"
        n = 1
        while (archive_root / str(n)).exists():
            n += 1
        target = archive_root / str(n)
        target.mkdir(parents=True)
        for item in folder.iterdir():
            if item.name == "archive":
                continue
            shutil.move(str(item), str(target / item.name))
    if current != fingerprint:
        marker.write_text(fingerprint + "\n", encoding="utf-8", newline="\n")
    return folder


class AgentStore:
    """File-backed state (one JSON document). Use :meth:`edit` for every change."""

    def __init__(self, folder: str | os.PathLike[str] | None):
        self.folder = Path(folder) if folder is not None else None
        self.lock = threading.RLock()
        self.data: dict[str, Any] = _empty()
        if self.folder is not None:
            self.folder.mkdir(parents=True, exist_ok=True)
            path = self.folder / STATE_FILE
            if path.is_file():
                loaded = json.loads(path.read_text(encoding="utf-8-sig"))
                for key, default in SECTIONS.items():
                    self.data[key] = loaded.get(key, json.loads(json.dumps(default)))

    @property
    def path(self) -> Path | None:
        return self.folder / STATE_FILE if self.folder is not None else None

    def save(self) -> None:
        with self.lock:
            if self.path is not None:
                write_canonical(self.path, self.data)

    @contextmanager
    def edit(self) -> Iterator[dict[str, Any]]:
        """Mutate ``data`` under the lock; the file is rewritten afterwards (also on error: never half)."""
        with self.lock:
            snapshot = json.loads(json.dumps(self.data))
            try:
                yield self.data
            except BaseException:
                self.data = snapshot
                raise
            self.save()

    def read(self, section: str) -> Any:
        """A deep copy of one section."""
        with self.lock:
            return json.loads(json.dumps(self.data[section]))

    # ------------------------------------------------------------------ orders

    def order(self, order_id: str) -> dict[str, Any] | None:
        with self.lock:
            rec = self.data["orders"].get(order_id.lower())
            return json.loads(json.dumps(rec)) if rec is not None else None

    def put_order(self, order_id: str, record: dict[str, Any]) -> None:
        with self.edit() as data:
            data["orders"][order_id.lower()] = record

    def update_order(self, order_id: str, **fields: Any) -> dict[str, Any] | None:
        with self.edit() as data:
            rec = data["orders"].get(order_id.lower())
            if rec is None:
                return None
            rec.update(fields)
            return json.loads(json.dumps(rec))

    def pending_order_ids(self, *, exclude: str | None = None) -> frozenset[str]:
        """Orders sent to the signer and not answered yet (C05)."""
        with self.lock:
            return frozenset(
                oid
                for oid, rec in self.data["orders"].items()
                if rec.get("status") == "in_flight" and oid != (exclude or "").lower()
            )

    def seen_key(self, issuer: int, invoice_id: str) -> str:
        return f"{int(issuer)}:{invoice_id}"


__all__ = ["FINGERPRINT_FILE", "SECTIONS", "STATE_DIR_ENV", "STATE_FILE", "AgentStore", "prepare_state_dir"]
