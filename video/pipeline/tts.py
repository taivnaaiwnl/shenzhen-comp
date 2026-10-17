"""Narration: one OpenAI text-to-speech clip per subtitle cue (video script, section 7, step 3).

Each clip is requested as raw PCM (24 kHz, 16-bit, mono) and cached under ``video/work/tts/`` by a hash of
(model, voice, instructions, spoken text), so a re-render only pays for cues whose words changed. The key
is read from ``.env`` (``OPENAI_API_KEY``) or the environment; it is never printed or written anywhere.

The subtitle keeps the written form ("0.80 USDC", "reason 2108"); ``spoken()`` gives the words the voice
reads for the few forms a voice reads badly. The two always say the same thing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CACHE = REPO / "video" / "work" / "tts"

# tts-1-hd at speed 1.0 reads at about the script's rate (112-150 words per minute per cue); gpt-4o-mini-tts
# with style instructions read about 110 words per minute and did not fit the cues. Instructions are sent
# only to the gpt-* models (the tts-1 models take none).
MODEL = "tts-1-hd"
VOICE = "sage"
INSTRUCTIONS = (
    "Narrate a technical product demo in clear, calm, neutral English. Steady pace, even tone, no excitement."
)
RATE = 24000  # Hz, 16-bit mono PCM

_DIGIT = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]


def _decimal_words(m: re.Match[str]) -> str:
    whole, frac = m.group(1), m.group(2)
    if len(whole) == 1:
        whole = _DIGIT[int(whole)]
    return f"{whole} point " + " ".join(_DIGIT[int(d)] for d in frac)


def spoken(text: str) -> str:
    """The words the voice reads for a subtitle line (same meaning, voice-friendly forms)."""
    t = " ".join(text.split())
    t = t.replace("ERC-8004", "E-R-C eight thousand four")
    t = re.sub(r"\breason 2108\b", "reason twenty-one oh eight", t)
    t = re.sub(r"\bM1\b", "M one", t)
    t = re.sub(r"\b(\d+)\.(\d+)\b", _decimal_words, t)
    return t


def _api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "")
    if key:
        return key
    env = REPO / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("OPENAI_API_KEY is not set (environment or .env): narration cannot be generated")


def clip(text: str, *, model: str = MODEL, voice: str = VOICE, instructions: str = INSTRUCTIONS) -> Path:
    """PCM file for ``text`` (cached)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    words = spoken(text)
    styled = model.startswith("gpt-")
    key = hashlib.sha256(json.dumps([model, voice, instructions if styled else "", words]).encode()).hexdigest()[:24]
    path = CACHE / f"{key}.pcm"
    if path.is_file() and path.stat().st_size > 0:
        return path
    req_body = {"model": model, "voice": voice, "input": words, "response_format": "pcm"}
    if styled:
        req_body["instructions"] = instructions
    body = json.dumps(req_body).encode()
    for attempt in range(4):
        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/speech",
            data=body,
            method="POST",
            headers={"Authorization": "Bearer " + _api_key(), "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            if exc.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise SystemExit(f"text-to-speech failed: HTTP {exc.code} {detail}") from None
        except urllib.error.URLError as exc:
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise SystemExit(f"text-to-speech failed: {exc.reason}") from None
    path.write_bytes(data)
    (CACHE / f"{key}.txt").write_text(words + "\n", encoding="utf-8")
    return path


def trim_silence(pcm: bytes, threshold: int = 350) -> bytes:
    """Drop leading and trailing near-silence (keeps 60 ms either side)."""
    import array

    a = array.array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    n = len(a)
    start = next((i for i in range(n) if abs(a[i]) > threshold), 0)
    end = next((i for i in range(n - 1, -1, -1) if abs(a[i]) > threshold), n - 1)
    pad = int(0.06 * RATE)
    start, end = max(0, start - pad), min(n, end + pad)
    return a[start:end].tobytes()


def seconds(pcm: bytes) -> float:
    return len(pcm) / 2 / RATE
