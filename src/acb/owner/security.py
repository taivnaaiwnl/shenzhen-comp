"""Login sessions, CSRF tokens and response headers of the owner console (spec 11.1).

- ``POST /login {password}`` is compared in constant time with ``OWNER_UI_PASSWORD``; success sets an
  ``HttpOnly; SameSite=Strict`` cookie holding a random session id signed (HMAC-SHA256) with
  ``SESSION_SECRET``. Sessions live in memory and expire after :data:`SESSION_TTL_S`.
- Every state-changing endpoint needs that cookie **and** the header ``X-ACB-CSRF`` equal to the
  session's CSRF token, which the page carries in ``<meta name="acb-csrf">``.
- Every response carries :data:`CSP` and ``X-Content-Type-Options: nosniff`` (plus a few more
  hardening headers that do not change the CSP).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass

CSP = "default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
"""The Content-Security-Policy of spec 11.1, verbatim."""

SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}

COOKIE_NAME = "acb_owner_session"
CSRF_HEADER = "X-ACB-CSRF"
SESSION_TTL_S = 12 * 3600
MIN_SECRET_CHARS = 16
MIN_PASSWORD_CHARS = 8


class ConfigError(RuntimeError):
    """The console is not configured safely enough to start."""


def check_config(password: str, session_secret: str) -> None:
    """Refuse to serve without a password or with a short session secret (most restrictive choice)."""
    if not password or len(password) < MIN_PASSWORD_CHARS:
        raise ConfigError(
            f"OWNER_UI_PASSWORD must be set (at least {MIN_PASSWORD_CHARS} characters); `acb keys init` fills it"
        )
    if not session_secret or len(session_secret) < MIN_SECRET_CHARS:
        raise ConfigError(
            f"SESSION_SECRET must be set (at least {MIN_SECRET_CHARS} characters); `acb keys init` fills it"
        )


def password_ok(given: str, expected: str) -> bool:
    """Constant-time comparison (digests first, so the length of the password does not leak)."""
    if not expected or not isinstance(given, str):
        return False
    a = hashlib.sha256(given.encode("utf-8")).digest()
    b = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(a, b)


@dataclass
class Session:
    sid: str
    csrf: str
    expires: float


class Sessions:
    """In-memory sessions; the cookie value is ``<sid>.<hmac(sid)>``."""

    def __init__(self, secret: str, *, ttl_s: float = SESSION_TTL_S, clock=time.time):
        self._key = hashlib.sha256(("acb-owner-session:" + secret).encode("utf-8")).digest()
        self.ttl_s = ttl_s
        self.clock = clock
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def _sign(self, sid: str) -> str:
        mac = hmac.new(self._key, sid.encode("ascii"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")

    def create(self) -> tuple[Session, str]:
        """A new session and its cookie value."""
        sid = secrets.token_urlsafe(32)
        session = Session(sid=sid, csrf=secrets.token_hex(32), expires=self.clock() + self.ttl_s)
        with self._lock:
            now = self.clock()
            for key in [k for k, s in self._sessions.items() if s.expires < now]:
                del self._sessions[key]
            self._sessions[sid] = session
        return session, f"{sid}.{self._sign(sid)}"

    def get(self, cookie: str | None) -> Session | None:
        """The live session of a cookie value, or None (bad signature, unknown or expired)."""
        if not cookie or "." not in cookie or len(cookie) > 200:
            return None
        sid, _, mac = cookie.rpartition(".")
        try:
            expected = self._sign(sid)
        except UnicodeEncodeError:
            return None
        if not hmac.compare_digest(mac.encode("ascii", "replace"), expected.encode("ascii")):
            return None
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                return None
            if session.expires < self.clock():
                del self._sessions[sid]
                return None
            return session

    def drop(self, cookie: str | None) -> None:
        session = self.get(cookie)
        if session is not None:
            with self._lock:
                self._sessions.pop(session.sid, None)

    @staticmethod
    def csrf_ok(session: Session | None, header: str | None) -> bool:
        if session is None or not header:
            return False
        return hmac.compare_digest(header.encode("utf-8", "replace"), session.csrf.encode("ascii"))


class LoginLimiter:
    """At most ``limit`` failed logins per ``window_s`` (all clients are local; one global bucket)."""

    def __init__(self, limit: int = 5, window_s: float = 60.0, clock=time.monotonic):
        self.limit = limit
        self.window_s = window_s
        self.clock = clock
        self._failures: deque[float] = deque()
        self._lock = threading.Lock()

    def blocked(self) -> bool:
        with self._lock:
            now = self.clock()
            while self._failures and self._failures[0] < now - self.window_s:
                self._failures.popleft()
            return len(self._failures) >= self.limit

    def failed(self) -> None:
        with self._lock:
            self._failures.append(self.clock())


__all__ = [
    "COOKIE_NAME",
    "CSP",
    "CSRF_HEADER",
    "SECURITY_HEADERS",
    "ConfigError",
    "LoginLimiter",
    "Session",
    "Sessions",
    "check_config",
    "password_ok",
]
