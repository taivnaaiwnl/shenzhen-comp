"""Output redactor of the wallet agent (spec 8.6 "Data requests", Appendix D R-7).

Every string the LLM produces passes through :meth:`Redactor.scan` before it reaches the owner, a log or
another process. Three detectors, none of which looks at the *shape* of 32-byte hashes, so tx hashes,
orderIds and mandateIds pass untouched (benign test B13):

1. **exact** - exact values of the secrets this process holds (``AGENT_TOKEN``, ``AGENT_ADMIN_TOKEN``,
   ``SIGNER_TOKEN``, ``EVENTS_TOKEN``, ``LLM_API_KEY``, and ``OPENAI_API_KEY``, the fallback of the LLM
   key). Only their keccak hashes are kept; every
   candidate token of the text is hashed and compared, also with surrounding quotes or punctuation
   removed and split at ``=``, ``:`` and ``/``. A secret that appears inside a longer token is found by a
   substring scan of the token at the secrets' own lengths.
2. **bip39** - runs of ``bip39_min_words`` (12) or more words of the BIP-39 English list (the list that
   ships with eth-account), separated only by spaces or punctuation.
3. **prefix** - provider key prefixes (``sk-``, ``sk-ant-``, ``AKIA``) followed by a key-like body.

A match is replaced by ``[REDACTED]`` and reported (``secret_match`` = the strongest kind: exact >
bip39 > prefix); the caller files ``REPORT_VIOLATION`` (SECRET_EXFILTRATION) with the signer.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from web3 import Web3

SECRET_VARIABLES: tuple[str, ...] = ("AGENT_TOKEN", "AGENT_ADMIN_TOKEN", "SIGNER_TOKEN", "EVENTS_TOKEN", "LLM_API_KEY")
FALLBACK_SECRET_VARIABLES: tuple[str, ...] = ("OPENAI_API_KEY",)
"""Also hashed: the variable :func:`acb.config.llm_settings` falls back to when ``LLM_API_KEY`` is unset."""
REDACTED = "[REDACTED]"
MIN_SECRET_LEN = 8
"""Shorter values are ignored (an empty or trivial token would match ordinary words)."""

_STRENGTH = {"exact": 3, "bip39": 2, "prefix": 1}
_TOKEN = re.compile(r"[^\s\"'`<>()\[\]{},;|]+")
_SPLIT = re.compile(r"[=:/@?&#]")
_WORD = re.compile(r"[A-Za-z]+")


@lru_cache(maxsize=1)
def bip39_words() -> frozenset[str]:
    """The BIP-39 English word list bundled with eth-account."""
    import eth_account

    path = Path(eth_account.__file__).parent / "hdaccount" / "wordlist" / "english.txt"
    return frozenset(w.strip() for w in path.read_text(encoding="utf-8").split() if w.strip())


def _keccak(value: str) -> bytes:
    return bytes(Web3.keccak(text=value))


@dataclass(frozen=True)
class RedactResult:
    """Redacted text and what matched (``kinds`` in order of discovery, without values)."""

    text: str
    kinds: tuple[str, ...] = ()

    @property
    def matched(self) -> bool:
        return bool(self.kinds)

    @property
    def secret_match(self) -> Literal["exact", "bip39", "prefix"] | None:
        if not self.kinds:
            return None
        return max(self.kinds, key=lambda k: _STRENGTH[k])  # type: ignore[return-value]


@dataclass
class Redactor:
    """Exact-value, BIP-39 and key-prefix redactor (see the module docstring)."""

    secret_hashes: frozenset[bytes] = frozenset()
    secret_lengths: frozenset[int] = frozenset()
    bip39_min_words: int = 12
    key_prefixes: tuple[str, ...] = ("sk-", "sk-ant-", "AKIA")
    _prefix_re: re.Pattern[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        parts = []
        for prefix in sorted(set(self.key_prefixes), key=len, reverse=True):
            if prefix == "AKIA":
                parts.append(r"AKIA[0-9A-Z]{16}")
            else:
                parts.append(re.escape(prefix) + r"[A-Za-z0-9_\-]{16,}")
        body = "|".join(parts) if parts else r"(?!x)x"
        self._prefix_re = re.compile(rf"(?<![A-Za-z0-9_\-])(?:{body})")

    @classmethod
    def from_values(cls, values: Iterable[str], **kwargs: Any) -> Redactor:
        """A redactor for these secret values (values shorter than :data:`MIN_SECRET_LEN` are ignored)."""
        kept = [v for v in values if isinstance(v, str) and len(v) >= MIN_SECRET_LEN]
        return cls(
            secret_hashes=frozenset(_keccak(v) for v in kept),
            secret_lengths=frozenset(len(v) for v in kept),
            **kwargs,
        )

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        names: Iterable[str] = SECRET_VARIABLES + FALLBACK_SECRET_VARIABLES,
        **kwargs: Any,
    ) -> Redactor:
        """A redactor for the secrets this process holds (by variable name; values are hashed at once).

        The default names are the five of spec 8.6 plus ``OPENAI_API_KEY``, the API key the LLM client
        uses when ``LLM_API_KEY`` is unset.
        """
        env = os.environ if environ is None else environ
        return cls.from_values((env.get(n, "") for n in names), **kwargs)

    # ------------------------------------------------------------------ detection

    def _is_secret(self, candidate: str) -> bool:
        return bool(candidate) and len(candidate) in self.secret_lengths and _keccak(candidate) in self.secret_hashes

    def _exact_spans(self, text: str) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        if not self.secret_hashes:
            return spans
        for m in _TOKEN.finditer(text):
            token, base = m.group(0), m.start()
            found = False
            candidates: list[tuple[int, int]] = [(0, len(token))]
            stripped = token.strip(".,!?;:*_~")
            if stripped and stripped != token:
                off = token.find(stripped)
                candidates.append((off, off + len(stripped)))
            pos = 0
            for piece in _SPLIT.split(token):
                if piece:
                    off = token.find(piece, pos)
                    candidates.append((off, off + len(piece)))
                    pos = off + len(piece)
            for s, e in candidates:
                if self._is_secret(token[s:e]):
                    spans.append((base + s, base + e))
                    found = True
                    break
            if found:
                continue
            # a secret glued inside a longer token: scan windows of the secrets' lengths
            for length in self.secret_lengths:
                if length >= len(token):
                    continue
                for s in range(len(token) - length + 1):
                    if self._is_secret(token[s : s + length]):
                        spans.append((base + s, base + s + length))
                        found = True
                        break
                if found:
                    break
        return spans

    def _bip39_spans(self, text: str) -> list[tuple[int, int]]:
        words = bip39_words()
        spans: list[tuple[int, int]] = []
        run: list[re.Match[str]] = []
        last_end = 0
        for m in _WORD.finditer(text):
            gap = text[last_end : m.start()]
            joined = not run or re.fullmatch(r"[\s,.;:\-]*", gap) is not None
            if m.group(0).lower() in words and joined:
                run.append(m)
            else:
                if len(run) >= self.bip39_min_words:
                    spans.append((run[0].start(), run[-1].end()))
                run = [m] if m.group(0).lower() in words else []
            last_end = m.end()
        if len(run) >= self.bip39_min_words:
            spans.append((run[0].start(), run[-1].end()))
        return spans

    def _prefix_spans(self, text: str) -> list[tuple[int, int]]:
        return [(m.start(), m.end()) for m in self._prefix_re.finditer(text)]

    def scan(self, text: str | None) -> RedactResult:
        """Redact ``text``; ``RedactResult.kinds`` lists the detectors that matched."""
        if not text:
            return RedactResult(text or "")
        found: list[tuple[int, int, str]] = []
        for kind, spans in (
            ("exact", self._exact_spans(text)),
            ("bip39", self._bip39_spans(text)),
            ("prefix", self._prefix_spans(text)),
        ):
            found.extend((s, e, kind) for s, e in spans)
        if not found:
            return RedactResult(text)
        found.sort()
        merged: list[list[int]] = []
        for s, e, _ in found:
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        out, pos = [], 0
        for s, e in merged:
            out.append(text[pos:s])
            out.append(REDACTED)
            pos = e
        out.append(text[pos:])
        kinds = tuple(dict.fromkeys(k for _, _, k in found))
        return RedactResult("".join(out), kinds)

    def scan_value(self, value: Any) -> tuple[Any, tuple[str, ...]]:
        """Redact every string inside a JSON-like value (tool arguments); returns ``(value, kinds)``."""
        kinds: list[str] = []

        def walk(v: Any) -> Any:
            if isinstance(v, str):
                r = self.scan(v)
                kinds.extend(r.kinds)
                return r.text
            if isinstance(v, Mapping):
                return {k: walk(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return [walk(x) for x in v]
            return v

        out = walk(value)
        return out, tuple(dict.fromkeys(kinds))


def strongest(kinds: Iterable[str]) -> Literal["exact", "bip39", "prefix"] | None:
    """The strongest match kind of ``kinds`` (exact > bip39 > prefix), or None."""
    items = [k for k in kinds if k in _STRENGTH]
    if not items:
        return None
    return max(items, key=lambda k: _STRENGTH[k])  # type: ignore[return-value]


__all__ = ["MIN_SECRET_LEN", "REDACTED", "SECRET_VARIABLES", "RedactResult", "Redactor", "bip39_words", "strongest"]
