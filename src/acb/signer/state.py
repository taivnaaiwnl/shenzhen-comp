"""Persistent state of the signer (``data/<network>/signer/``) and its in-process rate limiter.

Everything the signer must remember across restarts is written atomically as canonical JSON:

- ``requests/<request_id>.json``: the stored :class:`~acb.types.ExecuteResponse` of every request
  (idempotency: a repeated ``request_id`` returns the first result and never re-sends). A request that
  is still running, or that died mid-way, is marked ``in_progress`` and is never executed again.
- ``state.json``: quote requests the signer has seen used (quoteRequestId -> invoiceHash), invoices
  seen ((issuer, invoiceId) -> invoiceHash), chain timestamps of recorded violations (repeated-violation
  rule), wall-clock times of on-chain refusal records (hourly recording budget) and the reports filed
  (one per subject, kind and order).

The outflow rate limit (6 per minute) uses a monotonic clock and lives in memory only (spec 7.3).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from acb.canonical import atomic_write_bytes, canonical_bytes
from acb.types import ExecuteResponse

_REQUEST_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
STATE_SCHEMA = "acb-signer-state/1"
IN_PROGRESS = "in_progress"


def valid_request_id(request_id: str) -> bool:
    """A lowercase UUID4 string (spec 7.2)."""
    return isinstance(request_id, str) and _REQUEST_ID.fullmatch(request_id) is not None


class SignerState:
    """File-backed state; every mutation is written at once (atomic replace)."""

    def __init__(self, folder: str | os.PathLike[str]):
        self.folder = Path(folder)
        self.requests_dir = self.folder / "requests"
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "state.json"
        self._lock = threading.RLock()
        data: dict[str, Any] = {}
        if self.path.is_file():
            data = json.loads(self.path.read_text(encoding="utf-8-sig") or "{}")
        self.used_quote_requests: dict[str, str] = dict(data.get("usedQuoteRequests", {}))
        self.seen_invoices: dict[str, str] = dict(data.get("seenInvoices", {}))
        self.violation_times: list[int] = [int(t) for t in data.get("violationTimes", [])]
        self.recorded_decision_times: list[int] = [int(t) for t in data.get("recordedDecisionTimes", [])]
        self.reports: dict[str, str] = dict(data.get("reports", {}))

    # ---- persistence ----
    def _save(self) -> None:
        body = {
            "schema": STATE_SCHEMA,
            "usedQuoteRequests": self.used_quote_requests,
            "seenInvoices": self.seen_invoices,
            "violationTimes": self.violation_times[-200:],
            "recordedDecisionTimes": self.recorded_decision_times[-500:],
            "reports": self.reports,
        }
        atomic_write_bytes(self.path, canonical_bytes(body))

    # ---- requests (idempotency) ----
    def _request_path(self, request_id: str) -> Path:
        if not valid_request_id(request_id):
            raise ValueError("request_id must be a lowercase UUID4")
        return self.requests_dir / f"{request_id}.json"

    def stored(self, request_id: str) -> ExecuteResponse | str | None:
        """The stored response, ``IN_PROGRESS`` for a request that never finished, or None."""
        path = self._request_path(request_id)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("status") == IN_PROGRESS:
            return IN_PROGRESS
        return ExecuteResponse.model_validate(data)

    def begin(self, request_id: str) -> None:
        atomic_write_bytes(
            self._request_path(request_id), canonical_bytes({"requestId": request_id, "status": IN_PROGRESS})
        )

    def finish(self, response: ExecuteResponse) -> None:
        atomic_write_bytes(self._request_path(response.request_id), canonical_bytes(response.to_wire()))

    # ---- invoices and quote requests ----
    @staticmethod
    def invoice_key(issuer_agent_id: int, invoice_id: str) -> str:
        return f"{int(issuer_agent_id)}:{invoice_id}"

    def quote_request_used_by(self, quote_request_id: str) -> str | None:
        with self._lock:
            return self.used_quote_requests.get(quote_request_id)

    def seen_conflict(self, issuer_agent_id: int, invoice_id: str, invoice_hash: str) -> bool:
        """True when this (issuer, invoiceId) was seen before with different content (6.6)."""
        with self._lock:
            seen = self.seen_invoices.get(self.invoice_key(issuer_agent_id, invoice_id))
            return seen is not None and seen != invoice_hash

    def mark_invoice(self, quote_request_id: str, issuer_agent_id: int, invoice_id: str, invoice_hash: str) -> None:
        """The invoice was decided on-chain or executed: its quote request is used, its key is seen."""
        with self._lock:
            if quote_request_id:
                self.used_quote_requests.setdefault(quote_request_id, invoice_hash)
            self.seen_invoices.setdefault(self.invoice_key(issuer_agent_id, invoice_id), invoice_hash)
            self._save()

    # ---- violations ----
    def violations_in_window(self, now: int, window_s: int) -> list[int]:
        with self._lock:
            return [t for t in self.violation_times if now - t < window_s]

    def add_violation(self, now: int) -> None:
        with self._lock:
            self.violation_times.append(int(now))
            self._save()

    # ---- on-chain recording budget ----
    def recording_allowed(self, wall_now: float, per_hour: int) -> bool:
        with self._lock:
            recent = [t for t in self.recorded_decision_times if wall_now - t < 3600]
            return len(recent) < per_hour

    def add_recorded(self, wall_now: float) -> None:
        with self._lock:
            self.recorded_decision_times.append(int(wall_now))
            self._save()

    # ---- reports ----
    @staticmethod
    def report_key(subject_agent_id: int, kind: str, order_id: str) -> str:
        return f"{int(subject_agent_id)}:{kind}:{order_id}"

    def report_filed(self, key: str) -> str | None:
        with self._lock:
            return self.reports.get(key)

    def add_report(self, key: str, feedback_hash: str) -> None:
        with self._lock:
            self.reports[key] = feedback_hash
            self._save()


def prepare_state_dir(folder: str | os.PathLike[str], fingerprint: str) -> Path:
    """Bind ``folder`` to one deployment. State left by another deployment (Anvil reuses the same
    addresses on every fresh chain) is moved to ``archive/<old fingerprint>/`` so that old request ids,
    seen invoices and violation times never leak into a new chain."""
    root = Path(folder)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / "deployment.json"
    if marker.is_file():
        old = json.loads(marker.read_text(encoding="utf-8-sig") or "{}").get("fingerprint")
        if old == fingerprint:
            return root
        target = root / "archive" / str(old or "unknown")[:18]
        target.mkdir(parents=True, exist_ok=True)
        for item in root.iterdir():
            if item.name != "archive":
                shutil.move(str(item), str(target / item.name))
    elif any(item.name != "archive" for item in root.iterdir()):
        target = root / "archive" / "unbound"
        target.mkdir(parents=True, exist_ok=True)
        for item in root.iterdir():
            if item.name != "archive":
                shutil.move(str(item), str(target / item.name))
    atomic_write_bytes(marker, canonical_bytes({"fingerprint": fingerprint}))
    return root


class RateLimiter:
    """At most ``limit`` events per ``period`` seconds of a monotonic clock (in memory)."""

    def __init__(self, limit: int, period: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.limit = int(limit)
        self.period = float(period)
        self.clock = clock
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def _trim(self, now: float) -> None:
        while self._times and now - self._times[0] >= self.period:
            self._times.popleft()

    def allowed(self) -> bool:
        with self._lock:
            self._trim(self.clock())
            return len(self._times) < self.limit

    def record(self) -> None:
        with self._lock:
            now = self.clock()
            self._trim(now)
            self._times.append(now)


__all__ = ["IN_PROGRESS", "RateLimiter", "SignerState", "prepare_state_dir", "valid_request_id"]
