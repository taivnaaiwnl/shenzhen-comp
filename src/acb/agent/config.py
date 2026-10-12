"""Wallet agent configuration (``config/agent.yaml``, spec 13.3).

:class:`AgentConfig` is a frozen pydantic model read with ``utf-8-sig`` (BOM tolerant). Missing keys
take the spec's defaults, so a test can build one in code. ``ACB_AGENT_CONFIG`` points at another file.
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from acb.config import CONFIG_DIR, load_yaml

DEFAULT_AGENT_FILE = CONFIG_DIR / "agent.yaml"
AGENT_FILE_ENV = "ACB_AGENT_CONFIG"
PROMPT_VERSION = "acb-prompt-1.0.0"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class QuarantineConfig(_Frozen):
    mask_flagged_spans: bool = True
    max_chars: int = 500
    description_max_chars: int = 80


class DefaultsConfig(_Frozen):
    valid_hours: int = 24


class InboxConfig(_Frozen):
    per_issuer_per_minute: int = 10
    global_per_minute: int = 60
    max_bytes: int = 16384


class RedactorConfig(_Frozen):
    bip39_min_words: int = 12
    key_prefixes: tuple[str, ...] = ("sk-", "sk-ant-", "AKIA")


class LookalikeConfig(_Frozen):
    max_edit_distance: int = 2


class HttpConfig(_Frozen):
    provider_timeout_s: float = 10.0
    signer_timeout_s: float = 150.0


class AgentConfig(_Frozen):
    """Parsed ``config/agent.yaml``."""

    prompt_version: str = PROMPT_VERSION
    quarantine: QuarantineConfig = QuarantineConfig()
    defaults: DefaultsConfig = DefaultsConfig()
    history_turns: int = 6
    inbox: InboxConfig = InboxConfig()
    redactor: RedactorConfig = RedactorConfig()
    lookalike: LookalikeConfig = LookalikeConfig()
    http: HttpConfig = HttpConfig()
    delivery_watch_s: float = 2.0
    spam_report_min: int = Field(default=3, ge=1)
    messages_kept: int = 200

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> AgentConfig:
        """Read ``config/agent.yaml`` (or ``ACB_AGENT_CONFIG``, or ``path``)."""
        target = path or os.environ.get(AGENT_FILE_ENV) or DEFAULT_AGENT_FILE
        data: Any = load_yaml(target) or {}
        if not isinstance(data, dict):
            raise TypeError("agent.yaml must be a mapping")
        return cls.model_validate(data)


__all__ = ["AGENT_FILE_ENV", "DEFAULT_AGENT_FILE", "PROMPT_VERSION", "AgentConfig"]
