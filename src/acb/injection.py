"""Injection detector ``acb-inj-1.0.0`` (spec 6.4), shared by the policy (C10), the signer, the agent's
quarantine (8.5) and the bureau's report verification (4.2).

``detect(fields)`` runs deterministic regex rules over every string field of an invoice or message:

======  ============  ===========================================================================
Rule    Class         Detects
======  ============  ===========================================================================
R1      instruction   "ignore/disregard/forget/override ... previous/all/your ... instruction/rule/limit/policy"
R2      instruction   "system/admin/developer/owner/security team" followed by override/message/prompt/notice/":"
R3      instruction   "ai/assistant/agent/wallet/llm/bot ... must/should/please/now ... pay/send/transfer/approve/sign"
R11     instruction   "note/message/instruction to/for (the) ai/assistant/agent/llm/bot/model"
R4      action        "raise/increase/set/upgrade/unlock/lift ... tier/limit/cap/permission/allowance/autonomy"
R5      action        "transfer/send/move/withdraw ... all/entire/everything/full balance/remaining/max"
R6      action        an address (0x + 40 hex) or "agent #N" within 60 characters of a pay verb
R7      action        "correct/actual/updated/new/real total/amount/price/invoice"
R8      hidden        zero-width, bidi and tag characters (the list of 6.4 plus U+061C, U+2061-U+2064 and
                      U+FFF9-U+FFFB)
R9      hidden        a base64 run of >= 40 characters decoding to printable ASCII with a pay verb or the
                      text of an instruction/action rule (not R7, not R10); Morse-like runs
R10     sensitive     private key, seed phrase, mnemonic, api key, password, secret key, .env
======  ============  ===========================================================================

Normalisation: NFKC then casefold, for every rule except R8 (raw string) and the base64 part of R9
(NFKC only, since casefolding would corrupt base64). Normalisation is applied per character with an
index map, so every hit's ``start``/``end`` index the **input** string, which is what the quarantine
needs to mask spans. Before the rules run, the views also drop:

- the R8 characters (R8 reports them) and the other *ignorable* invisible characters (every other
  format character such as the soft hyphen U+00AD, the combining grapheme joiner U+034F, variation
  selectors, Hangul fillers). Ignorable characters are not a hit by themselves, so honest text that
  contains them is not adverse, but they cannot split a keyword to hide it;
- combining marks (each character of the rule view is decomposed with NFKD and its marks removed, so
  "ignore" with an accent and "IGNORE" with a dotted capital I (U+0130) read as plain letters).

Cyrillic and Greek look-alike letters inside a word that also has a Latin letter are read as their
Latin twins ("transfer" spelled with a Cyrillic a, U+0430). Words written entirely in Cyrillic (Mongolian text)
are left alone.

``quarantine`` = any hit (high recall). ``adverse`` = an R8 or R9 hit, or an instruction rule and an
action rule hit in the **same field** (high precision). Only ``adverse`` refuses (C10) or backs an
INJECTION_ATTEMPT report. R10 alone and R7 alone are never adverse.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from acb.canonical import chash
from acb.types import DetectorHit, DetectorResult

DETECTOR_ID = "acb-inj-1.0.0"

INSTRUCTION_RULES = frozenset({"R1", "R2", "R3", "R11"})
ACTION_RULES = frozenset({"R4", "R5", "R6", "R7"})
HIDDEN_RULES = frozenset({"R8", "R9"})
SENSITIVE_RULES = frozenset({"R10"})

RULE_CLASS: dict[str, str] = {
    **{r: "instruction" for r in INSTRUCTION_RULES},
    **{r: "action" for r in ACTION_RULES},
    **{r: "hidden" for r in HIDDEN_RULES},
    **{r: "sensitive" for r in SENSITIVE_RULES},
}

_F = re.DOTALL  # an instruction split over lines is still one instruction

PAY_VERB = re.compile(
    r"\b(pay|pays|paid|paying|payment|send|sends|sending|sent|transfer|transfers|transferring|transferred"
    r"|wire|remit|withdraw|move|approve|sign)\b",
    _F,
)
"""Pay verbs used by R6 and R9 (verbs of R3 and R5 plus their usual forms)."""

# Rules evaluated on the casefolded view (the patterns of spec 6.4).
_TEXT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "R1",
        re.compile(
            r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|all|your)\b.{0,20}"
            r"\b(instruction|rule|limit|polic)",
            _F,
        ),
    ),
    (
        "R2",
        re.compile(
            r"\b(system|admin(istrator)?|developer|owner|security team)\b\s*(override|message|prompt|notice|:)", _F
        ),
    ),
    (
        "R3",
        re.compile(
            r"\b(ai|assistant|agent|wallet|llm|bot)\b.{0,30}\b(must|should|please|now|immediately)\b.{0,40}"
            r"\b(pay|send|transfer|approve|sign)",
            _F,
        ),
    ),
    (
        "R11",
        re.compile(r"\b(note|message|instruction)s?\s+(to|for)\s+(the\s+)?(ai|assistant|agent|llm|bot|model)\b", _F),
    ),
    (
        "R4",
        re.compile(
            r"\b(raise|increase|set|upgrade|unlock|lift)\b.{0,30}\b(tier|limit|cap|permission|allowance|autonomy)", _F
        ),
    ),
    (
        "R5",
        re.compile(
            r"\b(transfer|send|move|withdraw)\b.{0,20}\b(all|entire|everything|full balance|remaining|max)\b", _F
        ),
    ),
    ("R7", re.compile(r"\b(correct|actual|updated|new|real)\s+(total|amount|price|invoice)\b", _F)),
    (
        "R10",
        re.compile(r"\b(private key|seed phrase|mnemonic|api key|password|secret key)\b|(?<![\w.])\.env\b", _F),
    ),
)

# R6 targets: an address (not the first 40 hex digits of a longer hash) or "agent #N".
_R6_TARGET = re.compile(r"(?<![0-9a-z])0x[0-9a-f]{40}(?![0-9a-z])|\bagent\s*#?\s*\d+", _F)
_R6_WINDOW = 60

_R9_DECODED_RULES = (INSTRUCTION_RULES | ACTION_RULES) - {"R7"}
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/_-]{40,}={0,2}")
_MORSE_RUN = re.compile(r"(?:[.\-]{1,5}(?:[ \t]*/[ \t]*|[ \t]+)){8,}")

# R8: the characters listed in 6.4, plus other invisible controls that only hide or reorder text.
_R8_RANGES = (
    (0x200B, 0x200F),  # zero-width space/non-joiner/joiner, LRM, RLM
    (0x2060, 0x2064),  # word joiner, invisible function application/times/separator/plus
    (0xFEFF, 0xFEFF),  # zero-width no-break space (BOM)
    (0x202A, 0x202E),  # bidi embeddings and overrides
    (0x2066, 0x2069),  # bidi isolates
    (0x061C, 0x061C),  # Arabic letter mark (bidi)
    (0xFFF9, 0xFFFB),  # interlinear annotation controls
    (0xE0000, 0xE007F),  # tag characters
)

# Invisible characters that are dropped from the rule views but are not a hit on their own
# (Unicode Default_Ignorable code points outside R8, plus every other format character).
_IGNORABLE_RANGES = (
    (0x00AD, 0x00AD),  # soft hyphen
    (0x034F, 0x034F),  # combining grapheme joiner
    (0x115F, 0x1160),  # Hangul choseong/jungseong fillers
    (0x17B4, 0x17B5),  # Khmer inherent vowels
    (0x180B, 0x180F),  # Mongolian free variation selectors and vowel separator
    (0x3164, 0x3164),  # Hangul filler
    (0xFE00, 0xFE0F),  # variation selectors
    (0xFFA0, 0xFFA0),  # halfwidth Hangul filler
    (0xE0100, 0xE01EF),  # variation selectors supplement
)

# Blank-looking characters that the rules read as a space.
_SPACE_LIKE = frozenset({0x2800})  # Braille pattern blank

# Cyrillic and Greek letters that look like Latin letters (after casefolding).
_CONFUSABLES: dict[str, str] = {
    "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c", "\u0443": "y",
    "\u0445": "x", "\u0456": "i", "\u0458": "j", "\u0455": "s", "\u0501": "d", "\u04bb": "h",
    "\u04cf": "l", "\u051b": "q", "\u051d": "w", "\u043a": "k", "\u0475": "v", "\u0261": "g",
    "\u03b1": "a", "\u03bf": "o", "\u03c1": "p", "\u03b9": "i", "\u03ba": "k", "\u03bd": "v",
    "\u03c5": "u", "\u03b5": "e", "\u03c4": "t",
}  # fmt: skip
_WORD = re.compile(r"[^\W\d_]+")


def _in_ranges(cp: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(low <= cp <= high for low, high in ranges)


def is_hidden_char(ch: str) -> bool:
    """True for characters rule R8 flags (zero-width, bidi controls, tag characters); always adverse."""
    return _in_ranges(ord(ch), _R8_RANGES)


def is_ignorable_char(ch: str) -> bool:
    """True for other invisible characters: removed from the rule views without being a hit."""
    if is_hidden_char(ch):
        return False
    return _in_ranges(ord(ch), _IGNORABLE_RANGES) or unicodedata.category(ch) == "Cf"


def strip_hidden(text: str) -> str:
    """``text`` without the characters of rule R8 and the other ignorable invisible characters."""
    return "".join(ch for ch in text if not (is_hidden_char(ch) or is_ignorable_char(ch)))


def _fold_char(ch: str) -> str:
    """One input character in the rule view: NFKC, casefold, NFKD, combining marks removed."""
    folded = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", ch).casefold())
    return "".join(c for c in folded if not unicodedata.combining(c))


def _unconfuse(chars: list[str]) -> None:
    """In place: read look-alike letters as Latin inside words that also contain a Latin letter."""
    text = "".join(chars)
    for m in _WORD.finditer(text):
        word = m.group(0)
        if any("a" <= c <= "z" for c in word) and any(c in _CONFUSABLES for c in word):
            for k in range(m.start(), m.end()):
                chars[k] = _CONFUSABLES.get(chars[k], chars[k])


def _views(text: str) -> tuple[str, list[int], str, list[int]]:
    """NFKC view and rule (casefold) view of ``text`` without invisible characters, with index maps."""
    nfkc_chars: list[str] = []
    nfkc_map: list[int] = []
    fold_chars: list[str] = []
    fold_map: list[int] = []
    for i, ch in enumerate(text):
        if is_hidden_char(ch) or is_ignorable_char(ch):
            continue
        if ord(ch) in _SPACE_LIKE:
            ch = " "
        for c in unicodedata.normalize("NFKC", ch):
            nfkc_chars.append(c)
            nfkc_map.append(i)
        for c in _fold_char(ch):
            fold_chars.append(c)
            fold_map.append(i)
    _unconfuse(fold_chars)  # one entry per character, so the index map stays aligned
    return "".join(nfkc_chars), nfkc_map, "".join(fold_chars), fold_map


def normalize(text: str) -> str:
    """The view most rules run on: hidden characters removed, NFKC, casefold."""
    return _views(text)[2]


def _raw_span(index_map: list[int], start: int, end: int) -> tuple[int, int]:
    if end <= start:
        return index_map[start], index_map[start]
    return index_map[start], index_map[end - 1] + 1


def _decodes_to_instruction(run: str) -> bool:
    """R9 base64 check: decodes to printable ASCII with a pay verb or an instruction/action rule's text.

    R7 ("new price") and R10 ("api key") are left out: alone they are never adverse in plain text,
    so they must not become adverse just because they were base64-encoded.
    """
    body = run.rstrip("=")
    has_std = bool(re.search(r"[+/]", body))
    has_url = bool(re.search(r"[-_]", body))
    if has_std and has_url:
        return False
    decoder = base64.urlsafe_b64decode if has_url else base64.b64decode
    for cut in range(4):
        chunk = body[: len(body) - cut] if cut else body
        if len(chunk) < 40:
            break
        try:
            raw = decoder(chunk + "=" * (-len(chunk) % 4))
        except (binascii.Error, ValueError):
            continue
        if not raw or any(not (0x20 <= b <= 0x7E or b in (9, 10, 13)) for b in raw):
            continue
        decoded = raw.decode("ascii").casefold()
        if PAY_VERB.search(decoded):
            return True
        return any(rule in _R9_DECODED_RULES and pattern.search(decoded) for rule, pattern in _TEXT_RULES)
    return False


def _field_hits(field: str, text: str) -> list[DetectorHit]:
    hits: set[tuple[str, int, int]] = set()

    # R8 on the raw string: one hit per run of hidden characters
    i = 0
    while i < len(text):
        if is_hidden_char(text[i]):
            j = i
            while j < len(text) and is_hidden_char(text[j]):
                j += 1
            hits.add(("R8", i, j))
            i = j
        else:
            i += 1

    nfkc, nfkc_map, fold, fold_map = _views(text)
    if fold:
        for rule, pattern in _TEXT_RULES:
            for m in pattern.finditer(fold):
                if m.end() > m.start():
                    end = m.end()
                    while end < len(fold) and fold[end].isalnum():  # "instruction|polic" -> whole word
                        end += 1
                    hits.add((rule, *_raw_span(fold_map, m.start(), end)))

        verbs = [(m.start(), m.end()) for m in PAY_VERB.finditer(fold)]
        for m in _R6_TARGET.finditer(fold):
            s, e = m.start(), m.end()
            best: tuple[int, int, int] | None = None
            for vs, ve in verbs:
                gap = s - ve if ve <= s else (vs - e if vs >= e else 0)
                if gap <= _R6_WINDOW and (best is None or gap < best[0]):
                    best = (gap, vs, ve)
            if best is not None:
                hits.add(("R6", *_raw_span(fold_map, min(s, best[1]), max(e, best[2]))))

        for m in _MORSE_RUN.finditer(fold):
            hits.add(("R9", *_raw_span(fold_map, m.start(), m.end())))

    if nfkc:
        for m in _BASE64_RUN.finditer(nfkc):
            if _decodes_to_instruction(m.group(0)):
                hits.add(("R9", *_raw_span(nfkc_map, m.start(), m.end())))

    return [
        DetectorHit(field=field, rule=r, start=s, end=e) for r, s, e in sorted(hits, key=lambda h: (h[1], h[0], h[2]))
    ]


def detect(fields: Mapping[str, str]) -> DetectorResult:
    """Run every rule over every field. ``fields`` maps a field name (for example ``memo`` or
    ``items[0].description``) to its text; all values must be strings."""
    all_hits: list[DetectorHit] = []
    adverse = False
    normalized: dict[str, str] = {}
    for field, text in fields.items():
        if not isinstance(field, str) or not isinstance(text, str):
            raise TypeError("detect() takes a mapping of str field names to str values")
        field_hits = _field_hits(field, text)
        all_hits.extend(field_hits)
        normalized[field] = normalize(text)
        rules = {h.rule for h in field_hits}
        if rules & HIDDEN_RULES or (rules & INSTRUCTION_RULES and rules & ACTION_RULES):
            adverse = True
    return DetectorResult(
        quarantine=bool(all_hits),
        adverse=adverse,
        hits=tuple(all_hits),
        normalized_sha=chash(normalized),
    )


def invoice_fields(invoice: Mapping[str, Any]) -> dict[str, str]:
    """Every string field the detector reads from a wire invoice (spec 6.4): ``invoiceId``,
    ``items[i].sku``, ``items[i].description`` and ``memo``. Non-string values are skipped
    (validation rejects them separately)."""
    out: dict[str, str] = {}
    if isinstance(invoice.get("invoiceId"), str):
        out["invoiceId"] = invoice["invoiceId"]
    items = invoice.get("items")
    if isinstance(items, (list, tuple)):
        for i, item in enumerate(items):
            if not isinstance(item, Mapping):
                continue
            for key in ("sku", "description"):
                if isinstance(item.get(key), str):
                    out[f"items[{i}].{key}"] = item[key]
    if isinstance(invoice.get("memo"), str):
        out["memo"] = invoice["memo"]
    return out


__all__ = [
    "ACTION_RULES",
    "DETECTOR_ID",
    "HIDDEN_RULES",
    "INSTRUCTION_RULES",
    "PAY_VERB",
    "RULE_CLASS",
    "SENSITIVE_RULES",
    "detect",
    "invoice_fields",
    "is_hidden_char",
    "is_ignorable_char",
    "normalize",
    "strip_hidden",
]
