"""The pinned directory and payee resolution (spec 8.3 "Directory and payee resolution", 8.6).

Payee names are matched **only** against the agents pinned in ``deployments/<network>.json`` (their
display names, categories and catalogues, 13.5): never the simulator's endpoints and never global
ERC-8004 names (anyone can register "Atlas Data" there). Payees are the pinned agents with a
catalogue, other than the wallet itself.

Matching is a case-insensitive token match plus category tags: every word of the owner's phrase
(after dropping filler words) must be a token of the agent's name, its category (``fx-data`` gives
``fx``, ``data`` and ``fx-data``) or its skus. A phrase that names one agent's full display name, its
key or ``#<agentId>`` resolves to that agent only. 0 matches and more than one match force a
clarification in code (even if the LLM did not ask).

Every candidate shown to the owner carries its agentId, display name, grade, registration age and payout
wallet (from chain) plus a **lookalike warning** when its name is within ``max_edit_distance`` (2) of
another known name or contains confusable characters.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from acb.types import AgentEntry, Deployments, Grade, PayeeCandidate

STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "for",
        "from",
        "to",
        "with",
        "by",
        "my",
        "our",
        "provider",
        "providers",
        "vendor",
        "vendors",
        "agent",
        "agents",
        "service",
        "services",
        "company",
        "supplier",
        "suppliers",
        "source",
        "sources",
        "feed",
        "feeds",
        "some",
        "any",
        "buy",
        "purchase",
        "get",
        "market",
    }
)
SYNONYMS = {
    "forex": "fx",
    "currency": "fx",
    "currencies": "fx",
    "exchange": "fx",
    "api": "api",
    "apis": "api",
    "call": "calls",
    "calls": "calls",
    "sample": "sample",
    "samples": "sample",
    "ad": "ads",
    "ads": "ads",
    "advertising": "ads",
    "historical": "history",
    "datum": "data",
}
_CONFUSABLE_SKELETON = str.maketrans({"0": "o", "1": "l", "5": "s", "3": "e", "|": "l", "$": "s", "@": "a"})


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def tokens(text: str) -> list[str]:
    """Lowercase word tokens of a phrase, filler words dropped, synonyms mapped."""
    out = []
    for word in re.findall(r"[0-9a-z]+", _fold(text)):
        if word in STOPWORDS:
            continue
        out.append(SYNONYMS.get(word, word))
    return out


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance."""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def has_confusables(name: str) -> bool:
    """Non-ASCII letters (homoglyph candidates) or invisible characters in a display name."""
    for ch in name:
        if ord(ch) > 0x7E:
            return True
        if unicodedata.category(ch) in ("Cf", "Cc"):
            return True
    return False


def skeleton(name: str) -> str:
    """A crude confusable skeleton: casefold, NFKC, digits that look like letters, ``rn`` -> ``m``."""
    s = _fold(name).translate(_CONFUSABLE_SKELETON)
    s = s.replace("rn", "m").replace("vv", "w")
    return re.sub(r"[^0-9a-z]", "", s)


@dataclass(frozen=True)
class DirectoryEntry:
    """One pinned agent."""

    key: str
    agent_id: int
    name: str
    category: str
    skus: dict[str, int]
    wallet: str | None
    tokens: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_provider(self) -> bool:
        return bool(self.skus)


@dataclass(frozen=True)
class Resolution:
    """Result of resolving one phrase: ``matches`` are directory entries."""

    phrase: str
    matches: tuple[DirectoryEntry, ...]

    @property
    def unique(self) -> DirectoryEntry | None:
        return self.matches[0] if len(self.matches) == 1 else None


class Directory:
    """The pinned directory of one deployment."""

    def __init__(
        self, entries: list[DirectoryEntry], *, wallet_agent_id: int | None = None, max_edit_distance: int = 2
    ):
        self.entries = entries
        self.wallet_agent_id = wallet_agent_id
        self.max_edit_distance = max_edit_distance
        self._by_id = {e.agent_id: e for e in entries}
        self._lookalikes = self._compute_lookalikes()

    @classmethod
    def from_deployments(cls, dep: Deployments, *, vault_name: str = "wallet", max_edit_distance: int = 2) -> Directory:
        wallet_id = int(dep.vaults[vault_name].agent_id) if vault_name in dep.vaults else None
        entries = [_entry(key, a) for key, a in sorted(dep.agents.items(), key=lambda kv: int(kv[1].agent_id))]
        return cls(entries, wallet_agent_id=wallet_id, max_edit_distance=max_edit_distance)

    # ------------------------------------------------------------------ lookups

    def get(self, agent_id: int) -> DirectoryEntry | None:
        return self._by_id.get(int(agent_id))

    def by_key(self, key: str) -> DirectoryEntry | None:
        for e in self.entries:
            if e.key == key:
                return e
        return None

    def name_of(self, agent_id: int) -> str:
        e = self.get(agent_id)
        return e.name if e is not None else f"agent #{int(agent_id)}"

    @property
    def providers(self) -> list[DirectoryEntry]:
        """Agents that can be payees: a catalogue, and not the wallet itself."""
        return [e for e in self.entries if e.is_provider and e.agent_id != self.wallet_agent_id]

    def catalogue(self) -> dict[int, dict[str, int]]:
        return {e.agent_id: dict(e.skus) for e in self.entries if e.skus}

    def skus_of(self, agent_ids: list[int] | tuple[int, ...]) -> list[str]:
        out: set[str] = set()
        for aid in agent_ids:
            e = self.get(aid)
            if e is not None:
                out.update(e.skus)
        return sorted(out)

    def lookalike_of(self, agent_id: int) -> str | None:
        """The known name this agent's name could be confused with (or a confusables note)."""
        return self._lookalikes.get(int(agent_id))

    def _compute_lookalikes(self) -> dict[int, str]:
        out: dict[int, str] = {}
        for e in self.entries:
            if has_confusables(e.name):
                out[e.agent_id] = f"{e.name} (contains confusable characters)"
                continue
            mine = _fold(e.name)
            for other in self.entries:
                if other.agent_id == e.agent_id:
                    continue
                theirs = _fold(other.name)
                if edit_distance(mine, theirs) <= self.max_edit_distance or (
                    skeleton(e.name) and skeleton(e.name) == skeleton(other.name)
                ):
                    out[e.agent_id] = other.name
                    break
        return out

    # ------------------------------------------------------------------ resolution

    def resolve(self, phrase: str) -> Resolution:
        """Resolve one owner phrase against the payees of the pinned directory."""
        providers = self.providers
        folded = _fold(phrase).strip()
        m = re.fullmatch(r"(?:agent\s*)?#\s*(\d+)", folded)
        if m:
            e = self.get(int(m.group(1)))
            return Resolution(phrase, (e,) if e is not None and e in providers else ())
        exact = [e for e in providers if _fold(e.name) == folded or e.key == folded]
        if exact:
            return Resolution(phrase, tuple(exact))
        words = tokens(phrase)
        if not words:
            return Resolution(phrase, ())
        found = [e for e in providers if all(w in e.tokens for w in words)]
        return Resolution(phrase, tuple(found))

    def resolve_all(self, phrases: list[str]) -> list[Resolution]:
        return [self.resolve(p) for p in phrases]

    def by_category(self) -> dict[str, list[DirectoryEntry]]:
        out: dict[str, list[DirectoryEntry]] = {}
        for e in self.providers:
            out.setdefault(e.category, []).append(e)
        return out

    def find_names_in(self, text: str) -> list[DirectoryEntry]:
        """Providers whose display name, first name word or key appears in free owner text."""
        folded = _fold(text)
        out = []
        for e in self.providers:
            first = _fold(e.name).split()[0] if e.name.strip() else e.key
            if (
                _fold(e.name) in folded
                or re.search(rf"\b{re.escape(first)}\b", folded)
                or re.search(rf"\b{re.escape(e.key)}\b", folded)
            ):
                out.append(e)
        return out


def _entry(key: str, a: AgentEntry) -> DirectoryEntry:
    toks: set[str] = set(tokens(a.name)) | {key.lower()}
    cat = a.category.lower()
    toks.add(cat)
    toks.update(SYNONYMS.get(t, t) for t in re.findall(r"[0-9a-z]+", cat))
    for sku in a.skus:
        toks.update(SYNONYMS.get(t, t) for t in re.findall(r"[a-z]+", sku.lower()) if t not in STOPWORDS)
    return DirectoryEntry(
        key=key,
        agent_id=int(a.agent_id),
        name=a.name,
        category=a.category,
        skus={s: int(p) for s, p in a.skus.items()},
        wallet=a.wallet,
        tokens=frozenset(toks),
    )


def candidate(
    entry: DirectoryEntry,
    *,
    grade: Grade,
    score: int | None,
    live_wallet: str | None,
    registered_at: int | None,
    lookalike_of: str | None,
) -> PayeeCandidate:
    """A :class:`~acb.types.PayeeCandidate` (names from the directory, everything else from chain)."""
    return PayeeCandidate(
        agent_id=entry.agent_id,
        name=entry.name,
        grade=grade,
        score=score,
        live_wallet=live_wallet,
        registered_at=registered_at,
        lookalike_of=lookalike_of,
    )


def age_text(registered_at: int | None, now: int) -> str:
    """``registered 2 h ago`` style text from the chain clock."""
    if registered_at is None:
        return "registration time unknown"
    age = max(0, int(now) - int(registered_at))
    if age < 120:
        return f"registered {age} s ago"
    if age < 7200:
        return f"registered {age // 60} min ago"
    if age < 172_800:
        return f"registered {age // 3600} h ago"
    return f"registered {age // 86_400} days ago"


def describe_candidate(c: PayeeCandidate, now: int) -> str:
    """One line: ``Atlas Data (#7, grade A, registered 2 h ago, wallet 0x1234...abcd)``."""
    wallet = c.live_wallet or "no wallet"
    if c.live_wallet:
        wallet = f"wallet {c.live_wallet[:6]}...{c.live_wallet[-4:]}"
    line = f"{c.name} (#{c.agent_id}, grade {Grade(c.grade).name}, {age_text(c.registered_at, now)}, {wallet})"
    if c.lookalike_of:
        line += f" - WARNING: looks like {c.lookalike_of}"
    return line


__all__ = [
    "Directory",
    "DirectoryEntry",
    "Resolution",
    "age_text",
    "candidate",
    "describe_candidate",
    "edit_distance",
    "has_confusables",
    "skeleton",
    "tokens",
]
