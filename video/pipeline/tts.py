"""Narration: one text-to-speech clip per subtitle cue (video script, section 7, step 3).

The voice is ElevenLabs (``eleven_multilingual_v2``, premade voice Matilda); ``ACB_TTS=openai`` switches to
OpenAI text-to-speech (``tts-1-hd``, voice ``sage``). Each clip is requested as raw PCM (24 kHz, 16-bit, mono)
and cached under ``video/work/tts/`` by a hash of (provider, model, voice, settings, spoken text), so a
re-render only pays for cues whose words changed. The key (``ELEVENLABS_API_KEY`` or ``OPENAI_API_KEY``) is
read from the environment or ``.env``; it is never printed or written anywhere.

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

PROVIDER = os.environ.get("ACB_TTS", "elevenlabs")  # "elevenlabs" (default) or "openai"

# ElevenLabs: the highest-quality model and a premade voice made for informative narration
EL_MODEL = "eleven_multilingual_v2"
EL_VOICE_ID = "XrExE9yKIg1WjnnlVkGX"
EL_VOICE_NAME = "Matilda"
# speed 1.08: at 1.0 Matilda reads about 8% slower than the cue timings (written for the OpenAI voice) allow
EL_SETTINGS = {"stability": 0.6, "similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": True, "speed": 1.08}

# OpenAI: tts-1-hd at speed 1.0 reads at about the script's rate (112-150 words per minute per cue);
# gpt-4o-mini-tts with style instructions read about 110 words per minute and did not fit the cues.
# Instructions are sent only to the gpt-* models (the tts-1 models take none).
MODEL = "tts-1-hd"
VOICE = "sage"
INSTRUCTIONS = (
    "Narrate a technical product demo in clear, calm, neutral English. Steady pace, even tone, no excitement."
)
RATE = 24000  # Hz, 16-bit mono PCM (ElevenLabs output_format pcm_24000 is the same)


def describe() -> dict[str, str]:
    """Provider, model and voice of the narration (for the report and the on-screen note)."""
    if PROVIDER == "elevenlabs":
        return {"provider": "ElevenLabs", "model": EL_MODEL, "voice": EL_VOICE_NAME}
    return {"provider": "OpenAI", "model": MODEL, "voice": VOICE}


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


def _api_key(name: str = "OPENAI_API_KEY") -> str:
    key = os.environ.get(name, "")
    if key:
        return key
    env = REPO / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(f"{name} is not set (environment or .env): narration cannot be generated")


def _post(url: str, body: dict, headers: dict[str, str]) -> bytes:
    """POST with retries on rate limits, server errors and network errors; returns the response bytes."""
    data = json.dumps(body).encode()
    for attempt in range(4):
        req = urllib.request.Request(
            url, data=data, method="POST", headers={**headers, "Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read()
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
    raise SystemExit("text-to-speech failed")


def clip(text: str, *, model: str = MODEL, voice: str = VOICE, instructions: str = INSTRUCTIONS) -> Path:
    """PCM file for ``text`` (cached). ``model``, ``voice`` and ``instructions`` apply to the OpenAI provider."""
    CACHE.mkdir(parents=True, exist_ok=True)
    words = spoken(text)
    if PROVIDER == "elevenlabs":
        ident: list = ["elevenlabs", EL_MODEL, EL_VOICE_ID, EL_SETTINGS, words]
    else:
        styled = model.startswith("gpt-")
        ident = [model, voice, instructions if styled else "", words]  # unchanged: keeps the OpenAI cache valid
    key = hashlib.sha256(json.dumps(ident, sort_keys=True).encode()).hexdigest()[:24]
    path = CACHE / f"{key}.pcm"
    if path.is_file() and path.stat().st_size > 0:
        return path
    if PROVIDER == "elevenlabs":
        data = _post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{EL_VOICE_ID}?output_format=pcm_24000",
            {"text": words, "model_id": EL_MODEL, "voice_settings": EL_SETTINGS},
            {"xi-api-key": _api_key("ELEVENLABS_API_KEY")},
        )
    else:
        req_body = {"model": model, "voice": voice, "input": words, "response_format": "pcm"}
        if model.startswith("gpt-"):
            req_body["instructions"] = instructions
        data = _post(
            "https://api.openai.com/v1/audio/speech",
            req_body,
            {"Authorization": "Bearer " + _api_key("OPENAI_API_KEY")},
        )
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
