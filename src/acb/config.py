"""Configuration and environment loading (spec 2.1, 13).

- :func:`load_env_file` is the **only** place in the code base that imports ``dotenv``. It is called by
  the launcher and by one-shot commands acting as one declared role (``deploy``, ``keys``,
  ``owner <cmd>``, ``scenario``, ``attacks``). No ``serve`` command calls it: services read
  ``os.environ`` only, as filtered by the launcher (``config/processes.yaml``).
- YAML is read with ``encoding="utf-8-sig"`` so a BOM written by an editor does no harm.
- Runtime output goes under :func:`out_dir` (``ACB_OUT_DIR``), runtime state under :func:`data_dir`.
"""

from __future__ import annotations

import io
import json
import os
import re
import time
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from acb.types import NetworkProfile

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_ENV_FILE = REPO_ROOT / ".env"

DEFAULT_RPC_URLS = {
    "ANVIL_RPC_URL": "http://127.0.0.1:8545",
    "BASE_SEPOLIA_RPC_URL": "https://sepolia.base.org",
}

OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL = "gpt-5.4-mini"


# ---------------------------------------------------------------------------
# .env files
# ---------------------------------------------------------------------------


class EnvSyntaxError(ValueError):
    """A line of an env file that python-dotenv or Foundry would not load."""


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_env_text(text: str, *, strict: bool = True) -> dict[str, str | None]:
    """Parse env-file text (never touches ``os.environ``).

    With ``strict`` every non-blank, non-comment line must be a valid ``NAME=value`` binding and an
    unquoted value must not contain whitespace (Foundry fails on such lines). Raises
    :class:`EnvSyntaxError` naming the line number, never the value.
    """
    from dotenv.parser import parse_stream

    text = text.removeprefix("\ufeff")
    values: dict[str, str | None] = {}
    for binding in parse_stream(io.StringIO(text)):
        line_no = binding.original.line
        raw = binding.original.string
        if binding.error:
            if strict:
                raise EnvSyntaxError(f"line {line_no}: not a valid NAME=value binding")
            continue
        if binding.key is None:  # blank line or comment
            continue
        if strict:
            if not _ENV_NAME.fullmatch(binding.key):
                raise EnvSyntaxError(f"line {line_no}: invalid variable name")
            value_part = raw.split("=", 1)[1] if "=" in raw else ""
            value_part = value_part.strip()
            if value_part.startswith("#"):
                # python-dotenv reads "NAME=   # note" as the value "# note": put notes on their own line
                raise EnvSyntaxError(f"line {line_no}: empty value followed by a comment")
            if value_part and value_part[0] not in "\"'":
                unquoted = re.split(r"\s+#", value_part, maxsplit=1)[0].rstrip()
                if re.search(r"\s", unquoted):
                    raise EnvSyntaxError(f"line {line_no}: value with spaces must be double-quoted")
        values[binding.key] = binding.value
    return values


def parse_env_file(path: str | os.PathLike[str], *, strict: bool = True) -> dict[str, str | None]:
    """Parse an env file (UTF-8, BOM tolerated) without loading it into the environment."""
    return parse_env_text(Path(path).read_text(encoding="utf-8-sig"), strict=strict)


def load_env_file(path: str | os.PathLike[str] | None = None, *, override: bool = False) -> list[str]:
    """Load ``.env`` (default: the repository's) into ``os.environ`` and return the names it set.

    Existing variables win unless ``override``. Afterwards the LLM defaults are applied
    (:func:`apply_llm_env_defaults`). A missing file is not an error (Anvil needs no ``.env``).
    """
    from dotenv import dotenv_values

    env_path = Path(path) if path is not None else DEFAULT_ENV_FILE
    loaded: list[str] = []
    if env_path.is_file():
        for key, value in dotenv_values(env_path, encoding="utf-8-sig").items():
            if value is None:
                continue
            if override or key not in os.environ:
                os.environ[key] = value
                loaded.append(key)
    apply_llm_env_defaults(os.environ)
    return loaded


# ---------------------------------------------------------------------------
# LLM settings
# ---------------------------------------------------------------------------


def _is_openai_url(url: str) -> bool:
    """True for an empty URL (the OpenAI default) or an ``https`` URL whose host is exactly ``api.openai.com``.

    The host is parsed, never substring-matched, so ``https://api.openai.com.evil.example/v1`` or a
    URL with ``api.openai.com`` in its path never receives ``OPENAI_API_KEY``.
    """
    if not url:
        return True
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
        port = parts.port
    except ValueError:
        return False
    return parts.scheme == "https" and host == "api.openai.com" and port in (None, 443) and not parts.username


def apply_llm_env_defaults(env: MutableMapping[str, str]) -> None:
    """Fill ``LLM_API_KEY`` from ``OPENAI_API_KEY`` when it is empty and the provider is OpenAI.

    In that case an empty ``LLM_BASE_URL`` becomes the OpenAI URL and an empty ``LLM_MODEL`` the team
    default (``gpt-5.4-mini``). A non-OpenAI ``LLM_BASE_URL`` (for example DeepSeek) never receives
    the OpenAI key. Idempotent; never prints values.
    """
    if env.get("LLM_API_KEY"):
        if not env.get("LLM_MODEL") and _is_openai_url(env.get("LLM_BASE_URL", "")):
            env["LLM_MODEL"] = DEFAULT_LLM_MODEL
        return
    openai_key = env.get("OPENAI_API_KEY", "")
    if not openai_key or not _is_openai_url(env.get("LLM_BASE_URL", "")):
        return
    env["LLM_API_KEY"] = openai_key
    if not env.get("LLM_BASE_URL"):
        env["LLM_BASE_URL"] = OPENAI_BASE_URL
    if not env.get("LLM_MODEL"):
        env["LLM_MODEL"] = DEFAULT_LLM_MODEL


@dataclass(frozen=True)
class LLMSettings:
    """Resolved LLM configuration (spec 8.2). ``repr`` never shows the key."""

    mode: str
    base_url: str
    model: str
    api_key: str
    timeout_s: float
    tool_mode: str
    extra_body: dict
    mock_script: str
    mock_persona: str
    allow_switch: bool

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    @property
    def effective_mode(self) -> str:
        """``mock`` or ``openai``: ``auto`` means openai when a key is set."""
        if self.mode == "auto":
            return "openai" if self.api_key else "mock"
        return self.mode

    def __repr__(self) -> str:
        return (
            f"LLMSettings(mode={self.mode!r}, base_url={self.base_url!r}, model={self.model!r}, "
            f"api_key={'<set>' if self.api_key else '<empty>'}, tool_mode={self.tool_mode!r})"
        )


def llm_settings(
    environ: Mapping[str, str] | None = None, *, env_file: str | os.PathLike[str] | None = None
) -> LLMSettings:
    """LLM settings from the environment (default ``os.environ``), with the OpenAI fallback applied.

    ``env_file`` (tests, one-shot tools) adds the ``LLM_*``, ``MOCK_LLM_*`` and ``OPENAI_API_KEY``
    values of that file under the environment, without loading anything else.
    """
    env: dict[str, str] = {}
    if env_file is not None and Path(env_file).is_file():
        for key, value in parse_env_file(env_file, strict=False).items():
            if value is not None and (key.startswith(("LLM_", "MOCK_LLM_")) or key == "OPENAI_API_KEY"):
                env[key] = value
    env.update({k: v for k, v in (os.environ if environ is None else environ).items()})
    apply_llm_env_defaults(env)
    extra_raw = env.get("LLM_EXTRA_BODY", "") or "{}"
    try:
        extra = json.loads(extra_raw)
    except json.JSONDecodeError as exc:
        raise ValueError("LLM_EXTRA_BODY is not valid JSON") from exc
    if not isinstance(extra, dict):
        raise TypeError("LLM_EXTRA_BODY must be a JSON object")
    mode = (env.get("LLM_MODE") or "auto").strip().lower()
    if mode not in ("mock", "openai", "auto"):
        raise ValueError("LLM_MODE must be mock, openai or auto")
    return LLMSettings(
        mode=mode,
        base_url=env.get("LLM_BASE_URL") or OPENAI_BASE_URL,
        model=env.get("LLM_MODEL") or DEFAULT_LLM_MODEL,
        api_key=env.get("LLM_API_KEY", ""),
        timeout_s=float(env.get("LLM_TIMEOUT_S") or 20),
        tool_mode=(env.get("LLM_TOOL_MODE") or "native").strip().lower(),
        extra_body=extra,
        mock_script=env.get("MOCK_LLM_SCRIPT") or "config/mock_llm/default.yaml",
        mock_persona=(env.get("MOCK_LLM_PERSONA") or "careful").strip().lower(),
        allow_switch=(env.get("ACB_ALLOW_LLM_SWITCH") or "0").strip() == "1",
    )


# ---------------------------------------------------------------------------
# YAML and network profiles
# ---------------------------------------------------------------------------


def resolve_path(path: str | os.PathLike[str]) -> Path:
    """A relative path is taken relative to the repository root."""
    p = Path(path)
    return p if p.is_absolute() else REPO_ROOT / p


def load_yaml(path: str | os.PathLike[str]) -> Any:
    """``yaml.safe_load`` of a file read with ``utf-8-sig`` (relative paths: repository root)."""
    with open(resolve_path(path), encoding="utf-8-sig") as fh:
        return yaml.safe_load(fh)


def load_network(network: str, environ: Mapping[str, str] | None = None) -> NetworkProfile:
    """The :class:`NetworkProfile` of ``config/networks.yaml`` entry ``network``.

    ``rpc_url`` is read from the variable named by ``rpc_env`` (default for Anvil
    ``http://127.0.0.1:8545``, for Base Sepolia ``https://sepolia.base.org``); ``log_rpc_url`` from
    ``log_rpc_env`` and falls back to ``rpc_url`` when empty.
    """
    env = os.environ if environ is None else environ
    data = load_yaml(CONFIG_DIR / "networks.yaml") or {}
    if network not in data:
        raise KeyError(f"unknown network {network!r}; known: {', '.join(sorted(data))}")
    entry = dict(data[network])
    rpc_env = entry.pop("rpc_env")
    log_rpc_env = entry.pop("log_rpc_env", rpc_env)
    rpc_url = env.get(rpc_env) or DEFAULT_RPC_URLS.get(rpc_env, "")
    if not rpc_url:
        raise KeyError(f"{rpc_env} is not set")
    log_rpc_url = env.get(log_rpc_env) or rpc_url
    return NetworkProfile(
        name=network,
        rpc_url=rpc_url,
        log_rpc_url=log_rpc_url,
        explorers=tuple(entry.pop("explorers", None) or ()),
        **entry,
    )


def network_name(environ: Mapping[str, str] | None = None) -> str:
    """``NETWORK`` from the environment, default ``anvil``."""
    env = os.environ if environ is None else environ
    return (env.get("NETWORK") or "anvil").strip()


def processes_config() -> dict[str, list[str]]:
    """``config/processes.yaml``: environment allow-list per process role plus ``base``."""
    data = load_yaml(CONFIG_DIR / "processes.yaml") or {}
    return {str(k): [str(x) for x in (v or [])] for k, v in data.items()}


# ---------------------------------------------------------------------------
# run ids, output and state directories, tools
# ---------------------------------------------------------------------------

_RUN_ID: str | None = None


def run_id(environ: Mapping[str, str] | None = None) -> str:
    """``ACB_RUN_ID`` or, once per process, ``r-YYYYMMDD-HHMM`` (UTC)."""
    global _RUN_ID
    env = os.environ if environ is None else environ
    if env.get("ACB_RUN_ID"):
        return env["ACB_RUN_ID"]
    if _RUN_ID is None:
        _RUN_ID = time.strftime("r-%Y%m%d-%H%M", time.gmtime())
    return _RUN_ID


def out_dir(*parts: str, environ: Mapping[str, str] | None = None, create: bool = True) -> Path:
    """Runtime output directory: ``ACB_OUT_DIR`` or ``runs/<run_id>/out/<network>/`` (spec 10.4).

    Extra ``parts`` are joined below it. The directory is created unless ``create=False``.
    """
    env = os.environ if environ is None else environ
    base = env.get("ACB_OUT_DIR")
    root = Path(base) if base else REPO_ROOT / "runs" / run_id(env) / "out" / network_name(env)
    path = root.joinpath(*parts)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir(network: str, *parts: str, create: bool = True) -> Path:
    """Runtime state directory ``data/<network>/...`` (git-ignored), e.g. ``data_dir("anvil", "agent")``."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", network):
        raise ValueError("invalid network name")
    path = REPO_ROOT.joinpath("data", network, *parts)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def foundry_bin(environ: Mapping[str, str] | None = None) -> Path:
    """Directory of forge/anvil/cast: ``ACB_FOUNDRY_BIN`` or ``%USERPROFILE%/.foundry/bin``."""
    env = os.environ if environ is None else environ
    if env.get("ACB_FOUNDRY_BIN"):
        return Path(env["ACB_FOUNDRY_BIN"])
    home = env.get("USERPROFILE") or env.get("HOME") or str(Path.home())
    return Path(home) / ".foundry" / "bin"


def foundry_tool(name: str, environ: Mapping[str, str] | None = None) -> Path | None:
    """Path of a Foundry executable (``anvil``, ``forge``, ``cast``), or None if not installed."""
    import shutil

    folder = foundry_bin(environ)
    for candidate in (folder / f"{name}.exe", folder / name):
        if candidate.is_file():
            return candidate
    found = shutil.which(name)
    return Path(found) if found else None


__all__ = [
    "CONFIG_DIR",
    "DEFAULT_ENV_FILE",
    "DEFAULT_LLM_MODEL",
    "OPENAI_BASE_URL",
    "REPO_ROOT",
    "EnvSyntaxError",
    "LLMSettings",
    "NetworkProfile",
    "apply_llm_env_defaults",
    "data_dir",
    "foundry_bin",
    "foundry_tool",
    "llm_settings",
    "load_env_file",
    "load_network",
    "load_yaml",
    "network_name",
    "out_dir",
    "parse_env_file",
    "parse_env_text",
    "processes_config",
    "resolve_path",
    "run_id",
]
