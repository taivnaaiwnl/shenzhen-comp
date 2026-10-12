"""Event bus of every service (spec 10.3): one envelope, a JSONL file and an in-memory feed.

Each service creates one :class:`EventBus`. ``emit()`` numbers the event (``seq`` continues across
restarts that reuse the same output directory), appends it as one JSON line to
``$ACB_OUT_DIR/<service>.jsonl`` and keeps it in memory for ``GET /events?after=<seq>``
(:func:`events_router`, which requires ``EVENTS_TOKEN``). Envelope keys stay snake_case.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import threading
import time
from collections import deque
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from acb.types import EVENT_TYPES, Event


def utc_now_iso() -> str:
    """Wall-clock UTC time for logs only, e.g. ``2026-10-17T09:12:03Z``."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def format_usdc(amount: int | str) -> str:
    """Base units to a two-decimal USDC string (``400000`` -> ``0.40``); exact, never a float."""
    units = int(amount)
    sign = "-" if units < 0 else ""
    units = abs(units)
    whole, frac = divmod(units, 10**6)
    cents = frac // 10**4
    rest = frac % 10**4
    text = f"{sign}{whole}.{cents:02d}"
    return text if rest == 0 else f"{sign}{whole}.{frac:06d}"


def format_log_line(event: Event) -> str:
    """Human log line of spec 10.3, e.g.
    ``2026-10-17T09:12:03Z S12 REFUSE Ghost Feeds #53 0.40 USDC grade=D tier=2 reasons=2100 tx=0xabc...``.
    """
    data = event.data or {}
    parts = [event.ts, event.step or "-"]
    parts.append(str(data.get("outcome") or data.get("decision") or event.type.upper()))
    payee = data.get("payee")
    if isinstance(payee, Mapping):
        if payee.get("name"):
            parts.append(str(payee["name"]))
        if payee.get("agentId") is not None:
            parts.append(f"#{payee['agentId']}")
    if data.get("amount") is not None:
        try:
            parts.append(f"{format_usdc(data['amount'])} USDC")
        except (TypeError, ValueError):
            parts.append(str(data["amount"]))
    grade = payee.get("grade") if isinstance(payee, Mapping) else data.get("grade")
    if grade is not None:
        parts.append(f"grade={grade}")
    if data.get("tier") is not None:
        parts.append(f"tier={data['tier']}")
    if event.reason_codes:
        parts.append("reasons=" + ",".join(str(c) for c in event.reason_codes))
    if event.tx_hash:
        parts.append(f"tx={event.tx_hash[:10]}...")
    if data.get("message") and not isinstance(payee, Mapping):
        parts.append(str(data["message"]))
    return " ".join(parts)


def _last_seq(path: Path) -> int:
    """Highest ``seq`` already written to ``path`` (0 if none or unreadable)."""
    if not path.is_file():
        return 0
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return 0
    best = 0
    for line in tail.splitlines():
        try:
            best = max(best, int(json.loads(line).get("seq", 0)))
        except (ValueError, AttributeError, TypeError):
            continue
    return best


class EventBus:
    """Numbered events of one service: JSONL file plus an in-memory ring buffer.

    ``out_dir=None`` uses :func:`acb.config.out_dir`; ``jsonl=False`` keeps events in memory only.
    ``explorer_tx`` is a URL template with ``{hash}`` used to fill ``explorer`` for events with a
    transaction hash (for example ``NetworkProfile.explorers[0]["tx"]``).
    """

    def __init__(
        self,
        service: str,
        *,
        run_id: str | None = None,
        chain_id: int | None = None,
        out_dir: str | os.PathLike[str] | None = None,
        jsonl: bool = True,
        maxlen: int = 10_000,
        explorer_tx: str | None = None,
        echo: bool = False,
    ):
        from acb import config

        if not service or not service.replace("_", "").replace("-", "").isalnum():
            raise ValueError("service must be a short alphanumeric name")
        self.service = service
        self.run_id = run_id or config.run_id()
        self.chain_id = chain_id
        self.explorer_tx = explorer_tx
        self.echo = echo
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._events: deque[Event] = deque(maxlen=maxlen)
        self.path: Path | None = None
        if jsonl:
            folder = Path(out_dir) if out_dir is not None else config.out_dir()
            folder.mkdir(parents=True, exist_ok=True)
            self.path = folder / f"{service}.jsonl"
        self._seq = _last_seq(self.path) if self.path else 0

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def emit(
        self,
        type: str,
        data: Mapping[str, Any] | None = None,
        *,
        step: str | None = None,
        reason_codes: Iterable[int] = (),
        tx_hash: str | None = None,
        explorer: str | None = None,
        chain_id: int | None = None,
    ) -> Event:
        """Record one event and return it. ``type`` must be one of :data:`acb.types.EVENT_TYPES`."""
        if type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {type!r}")
        if tx_hash and explorer is None and self.explorer_tx:
            explorer = self.explorer_tx.format(hash=tx_hash)
        payload = dict(data or {})
        with self._cond:
            self._seq += 1
            event = Event(
                seq=self._seq,
                ts=utc_now_iso(),
                run_id=self.run_id,
                service=self.service,
                type=type,
                step=step,
                chain_id=chain_id if chain_id is not None else self.chain_id,
                data=payload,
                reason_codes=tuple(int(c) for c in reason_codes),
                tx_hash=tx_hash,
                explorer=explorer,
            )
            line = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
            if self.path is not None:
                with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line + "\n")
            self._events.append(event)
            self._cond.notify_all()
        if self.echo:
            print(format_log_line(event), flush=True)
        return event

    def since(self, after: int = 0, limit: int = 500) -> list[Event]:
        """Events with ``seq > after`` still in memory, oldest first, at most ``limit``."""
        with self._lock:
            out = [e for e in self._events if e.seq > after]
        return out[: max(0, limit)]

    def wait(self, after: int, timeout: float) -> list[Event]:
        """Like :meth:`since`, but blocks up to ``timeout`` seconds for a new event."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while self._seq <= after:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
        return self.since(after)


def _check_token(authorization: str | None, token_env: str) -> None:
    from fastapi import HTTPException

    expected = os.environ.get(token_env, "")
    if not expected:
        raise HTTPException(status_code=503, detail=f"{token_env} is not configured")
    given = ""
    if authorization and authorization.lower().startswith("bearer "):
        given = authorization[7:].strip()
    if not given or not hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="invalid or missing token")


def events_router(bus: EventBus, token_env: str = "EVENTS_TOKEN"):
    """FastAPI router with ``GET /events?after=<seq>&limit=<n>&wait=<s>``.

    Requires ``Authorization: Bearer <value of token_env>`` (constant-time comparison; 503 when the
    variable is not set, 401 on a wrong token). Returns a JSON list of envelopes (snake_case).
    ``wait`` (0-25 s) long-polls until an event newer than ``after`` exists.
    """
    from fastapi import APIRouter, Header, Query

    router = APIRouter()

    @router.get("/events")
    async def get_events(
        after: int = Query(0, ge=0),
        limit: int = Query(500, ge=1, le=5000),
        wait: float = Query(0.0, ge=0.0, le=25.0),
        authorization: str | None = Header(default=None),
    ) -> list[dict[str, Any]]:
        _check_token(authorization, token_env)
        events = bus.since(after, limit)
        if not events and wait > 0:
            deadline = time.monotonic() + wait
            while not events and time.monotonic() < deadline:
                await asyncio.sleep(0.2)
                events = bus.since(after, limit)
        return [e.model_dump(mode="json") for e in events]

    return router


def read_jsonl(path: str | os.PathLike[str]) -> list[Event]:
    """Read an events file written by :class:`EventBus` (skips unreadable lines)."""
    out: list[Event] = []
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(Event.model_validate(json.loads(line)))
            except ValueError:
                continue
    return out


__all__ = ["EventBus", "events_router", "format_log_line", "format_usdc", "read_jsonl", "utc_now_iso"]
