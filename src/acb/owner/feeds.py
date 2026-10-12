"""The merged event stream of the owner console (spec 11.1 ``GET /stream``, 10.3).

The console polls every service's ``GET /events?after=<seq>`` every 500 ms with the read-only
``EVENTS_TOKEN`` (agent, signer, bureau, autonomy, simulator), tails JSONL files (the runner's
``$ACB_OUT_DIR/runner.jsonl``; recorded event files for a replay) and reads its own event bus. Every
envelope gets a console-local number so the browser can resume a stream with ``Last-Event-ID``.

Event data is untrusted: the browser renders it with ``textContent`` only.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

POLL_S = 0.5
OFFLINE_POLL_S = 2.0
MAX_EVENTS = 5000
MAX_EVENT_BYTES = 64 * 1024
DEFAULT_PORTS = {"agent": 8701, "signer": 8702, "bureau": 8703, "sim": 8704, "autonomy": 8705}
PORT_VARS = {
    "agent": "AGENT_PORT",
    "signer": "SIGNER_PORT",
    "bureau": "BUREAU_PORT",
    "sim": "SIM_PORT",
    "autonomy": "AUTONOMY_PORT",
}


def _envelope(raw: Any, fallback_service: str) -> dict[str, Any] | None:
    """A 10.3 envelope with only JSON-safe fields, or None for something that is not one."""
    if not isinstance(raw, Mapping):
        return None
    seq = raw.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        return None
    data = raw.get("data")
    codes = raw.get("reason_codes") or []
    try:
        encoded = json.dumps(raw, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return None
    if len(encoded) > MAX_EVENT_BYTES:
        data = {"message": "event too large to show"}
    return {
        "seq": seq,
        "ts": str(raw.get("ts") or "")[:40],
        "run_id": str(raw.get("run_id") or "")[:80],
        "service": str(raw.get("service") or fallback_service)[:40],
        "type": str(raw.get("type") or "info")[:40],
        "step": (str(raw["step"])[:20] if raw.get("step") is not None else None),
        "chain_id": raw.get("chain_id") if isinstance(raw.get("chain_id"), int) else None,
        "data": data if isinstance(data, Mapping) else {},
        "reason_codes": [int(c) for c in codes if isinstance(c, int) and not isinstance(c, bool)][:8]
        if isinstance(codes, list)
        else [],
        "tx_hash": str(raw["tx_hash"])[:80] if raw.get("tx_hash") else None,
        "explorer": str(raw["explorer"])[:300] if raw.get("explorer") else None,
    }


class Feed:
    """One source of envelopes. ``poll()`` returns the new ones (raw dicts)."""

    name: str = ""
    online: bool = False
    error: str | None = None

    def poll(self) -> list[dict[str, Any]]:  # pragma: no cover - interface
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "online": self.online, "error": self.error}


class HttpFeed(Feed):
    """``GET {base}/events?after=<seq>`` with ``Authorization: Bearer <EVENTS_TOKEN>``."""

    def __init__(self, name: str, base_url: str, token: str, *, transport: Any = None, timeout: float = 2.0):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.after = 0
        self.transport = transport
        self.timeout = timeout
        self._client: httpx.Client | None = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(transport=self.transport, timeout=self.timeout)
        return self._client

    def poll(self) -> list[dict[str, Any]]:
        try:
            response = self._http().get(
                f"{self.base_url}/events",
                params={"after": self.after, "limit": 500},
                headers={"Authorization": f"Bearer {self.token}"},
            )
        except httpx.HTTPError as exc:
            if self.online:
                self.after = 0  # the service may restart with a new sequence: re-read, duplicates are dropped
            self.online, self.error = False, type(exc).__name__
            return []
        if response.status_code != 200:
            self.online, self.error = False, f"HTTP {response.status_code}"
            return []
        try:
            items = response.json()
        except ValueError:
            self.online, self.error = False, "bad JSON"
            return []
        self.online, self.error = True, None
        if not isinstance(items, list):
            return []
        out = [i for i in items if isinstance(i, dict)]
        for item in out:
            seq = item.get("seq")
            if isinstance(seq, int) and not isinstance(seq, bool):
                self.after = max(self.after, seq)
        return out

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


class FileFeed(Feed):
    """Tails a JSONL events file (the runner's ``runner.jsonl``, or a recorded file for a replay)."""

    def __init__(self, name: str, path: str | os.PathLike[str]):
        self.name = name
        self.path = Path(path)
        self.offset = 0
        self._partial = b""

    def poll(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            self.online, self.error = False, "no file"
            return []
        try:
            size = self.path.stat().st_size
            if size < self.offset:
                self.offset, self._partial = 0, b""
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read(4 * 1024 * 1024)
        except OSError as exc:
            self.online, self.error = False, type(exc).__name__
            return []
        self.offset += len(chunk)
        self.online, self.error = True, None
        data = self._partial + chunk
        lines = data.split(b"\n")
        self._partial = lines.pop()
        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line.decode("utf-8-sig"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(item, dict):
                out.append(item)
        return out


class BusFeed(Feed):
    """The console's own :class:`~acb.logbus.EventBus` (owner transactions and notifications)."""

    def __init__(self, bus: Any):
        self.name = bus.service
        self.bus = bus
        self.after = 0
        self.online = True

    def poll(self) -> list[dict[str, Any]]:
        events = self.bus.since(self.after, 1000)
        if events:
            self.after = events[-1].seq
        return [e.model_dump(mode="json") for e in events]


class FeedMerger:
    """Merges every feed into one numbered list (``id`` is local to this console)."""

    def __init__(self, feeds: list[Feed], *, poll_s: float = POLL_S, maxlen: int = MAX_EVENTS):
        self.feeds = list(feeds)
        self.poll_s = poll_s
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._seen: deque[tuple] = deque(maxlen=maxlen * 4)
        self._seen_set: set[tuple] = set()
        self._next_id = 0
        self._lock = threading.Condition()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.listeners: list[Callable[[dict[str, Any]], None]] = []

    # ---- merging -----------------------------------------------------------------------------------

    def _remember(self, key: tuple) -> bool:
        if key in self._seen_set:
            return False
        if len(self._seen) == self._seen.maxlen:
            old = self._seen.popleft()
            self._seen_set.discard(old)
        self._seen.append(key)
        self._seen_set.add(key)
        return True

    def add(self, feed_name: str, raw: Mapping[str, Any]) -> dict[str, Any] | None:
        envelope = _envelope(raw, feed_name)
        if envelope is None:
            return None
        key = (feed_name, envelope["service"], envelope["run_id"], envelope["seq"])
        with self._lock:
            if not self._remember(key):
                return None
            self._next_id += 1
            item = {"id": self._next_id, "feed": feed_name, "event": envelope}
            self._events.append(item)
            self._lock.notify_all()
        for listener in list(self.listeners):
            try:
                listener(item)
            except Exception:  # noqa: BLE001, S110 - a listener must not stop the merge
                pass
        return item

    def poll_once(self) -> int:
        """Poll every feed once; returns how many new events were merged."""
        added = 0
        for feed in self.feeds:
            try:
                items = feed.poll()
            except Exception as exc:  # noqa: BLE001 - a broken feed must not stop the others
                feed.online, feed.error = False, type(exc).__name__
                continue
            for raw in items:
                if self.add(feed.name, raw) is not None:
                    added += 1
        return added

    # ---- reading -----------------------------------------------------------------------------------

    @property
    def last_id(self) -> int:
        with self._lock:
            return self._next_id

    def since(self, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            out = [e for e in self._events if e["id"] > after]
        return out[: max(0, limit)]

    def recent(self, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._events)
        return items[-max(0, limit) :]

    def wait(self, after: int, timeout: float) -> list[dict[str, Any]]:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            while self._next_id <= after:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._lock.wait(remaining)
        return self.since(after)

    def status(self) -> list[dict[str, Any]]:
        return [f.status() for f in self.feeds]

    # ---- background polling ------------------------------------------------------------------------

    def start(self) -> FeedMerger:
        """One polling thread per feed, so a feed that is down (a refused connection can take seconds on
        Windows) never delays the others."""
        if not self._threads:
            for feed in self.feeds:
                thread = threading.Thread(target=self._run, args=(feed,), name=f"owner-feed-{feed.name}", daemon=True)
                self._threads.append(thread)
                thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        for feed in self.feeds:
            close = getattr(feed, "close", None)
            if close is not None:
                close()

    def _run(self, feed: Feed) -> None:
        while not self._stop.is_set():
            try:
                items = feed.poll()
            except Exception as exc:  # noqa: BLE001 - a broken feed must not stop its thread
                feed.online, feed.error = False, type(exc).__name__
                items = []
            for raw in items:
                self.add(feed.name, raw)
            self._stop.wait(self.poll_s if feed.online else max(self.poll_s, OFFLINE_POLL_S))


def default_feeds(environ: Mapping[str, str] | None = None, *, out_dir: str | os.PathLike[str] | None = None) -> list:
    """HTTP feeds of P2-P6 on 127.0.0.1 (ports from the environment) and the runner's JSONL file."""
    env = os.environ if environ is None else environ
    token = env.get("EVENTS_TOKEN", "")
    feeds: list[Feed] = []
    for name in ("agent", "signer", "bureau", "autonomy", "sim"):
        port = int(env.get(PORT_VARS[name]) or DEFAULT_PORTS[name])
        feeds.append(HttpFeed(name, f"http://127.0.0.1:{port}", token))
    if out_dir is not None:
        feeds.append(FileFeed("runner", Path(out_dir) / "runner.jsonl"))
    return feeds


__all__ = ["BusFeed", "Feed", "FeedMerger", "FileFeed", "HttpFeed", "default_feeds"]
