"""LLM clients of the wallet agent (spec 8.2).

``LLMClient.complete(messages, tools, *, tool_choice="required", timeout_s=None) -> LLMResult``. Errors
are returned in ``LLMResult.error`` (``"timeout"``, ``"http_<code>"``, ``"bad_json"``, ``"connection"``),
never raised. Implementations:

- :class:`OpenAICompatClient`: any OpenAI-compatible provider (OpenAI, DeepSeek, ...). ``temperature=0``,
  ``extra_body=LLM_EXTRA_BODY`` (e.g. to turn a thinking mode off), ``max_retries=1``, no reliance on
  ``seed``. Tool-choice fallback: on HTTP 400 with ``tool_choice="required"`` it retries once with
  ``"auto"``; if that fails or returns no tool call it uses JSON mode (``LLM_TOOL_MODE=json`` uses JSON
  mode from the start): the tools are described in the system prompt and the model must answer
  ``{"tool": name, "arguments": {...}}`` with ``response_format={"type": "json_object"}``. A model that
  rejects ``temperature`` (HTTP 400 naming it) is called again without it.
- :class:`~acb.agent.mock_llm.MockLLM`: deterministic scripted model (8.7).
- :class:`FallbackLLM` ``(primary, secondary)``: on a primary ``error`` it uses the secondary (the
  rule-based parser) and sets ``last_fallback`` (reason 2300 on decisions, banner in the UI).

Every client calls :func:`guard_keys` first: the agent refuses any LLM call while a ``*_PRIVATE_KEY`` or
``*_MNEMONIC`` variable is visible (spec 2.1, 7.6).
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Protocol

from acb.agent.tools import tool_specs_json
from acb.keys import PrivateKeyInEnvironment, assert_no_private_keys
from acb.types import LLMResult, ToolCall, ToolSpec

_EXIT_ON_KEYS = False


def set_exit_on_keys(value: bool) -> None:
    """In the service process a visible key ends the process (exit 78); elsewhere it raises."""
    global _EXIT_ON_KEYS
    _EXIT_ON_KEYS = bool(value)


def guard_keys() -> None:
    """Refuse the LLM call if a key is visible: :class:`acb.keys.PrivateKeyInEnvironment` in tests and
    tools; in the service the whole process ends with exit code 78 (``os._exit``: a ``SystemExit`` raised in
    a worker thread would end only that thread)."""
    try:
        assert_no_private_keys(exit_process=False)
    except PrivateKeyInEnvironment as exc:
        if _EXIT_ON_KEYS:
            sys.stderr.write(f"{exc}\n")
            sys.stderr.flush()
            os._exit(78)
        raise


class LLMClient(Protocol):
    """Spec 8.2."""

    name: str

    def complete(
        self,
        messages: list[dict],
        tools: list[ToolSpec],
        *,
        tool_choice: str = "required",
        timeout_s: float | None = None,
    ) -> LLMResult: ...


def _result(
    *,
    model: str,
    started: float,
    text: str | None = None,
    calls: list[ToolCall] | None = None,
    error: str | None = None,
) -> LLMResult:
    return LLMResult(
        text=text,
        tool_calls=list(calls or []),
        model=model,
        latency_ms=int((time.monotonic() - started) * 1000),
        error=error,
    )


def json_mode_instructions(tools: list[ToolSpec]) -> str:
    """The system text of JSON mode: the tool list and the answer format."""
    specs = json.dumps([t.model_dump() for t in tools], ensure_ascii=True, separators=(",", ":"))
    return (
        "Tool calling is done in JSON. Answer with one JSON object and nothing else: "
        '{"tool": "<tool name>", "arguments": {...}}. The arguments must follow the tool\'s JSON schema. '
        f"Tools: {specs}"
    )


def parse_json_tool(content: str | None) -> ToolCall | None:
    """A JSON-mode answer ``{"tool", "arguments"}`` as a :class:`ToolCall`, or None."""
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") :] if "{" in text else text
    try:
        value = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(text[start : end + 1])
        except ValueError:
            return None
    if not isinstance(value, dict) or not isinstance(value.get("tool"), str):
        return None
    args = value.get("arguments", {})
    if not isinstance(args, dict):
        return None
    return ToolCall(name=value["tool"], arguments=args, raw=content)


class OpenAICompatClient:
    """An OpenAI-compatible chat completions client (see the module docstring)."""

    name = "openai"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        timeout_s: float = 20.0,
        tool_mode: str = "native",
        extra_body: dict | None = None,
        client: Any = None,
    ):
        self.model = model
        self.timeout_s = float(timeout_s)
        self.tool_mode = tool_mode if tool_mode in ("native", "json") else "native"
        self.extra_body = dict(extra_body or {})
        self._omit_temperature = False
        if client is None:
            import openai

            client = openai.OpenAI(api_key=api_key, base_url=base_url or None, timeout=self.timeout_s, max_retries=1)
        self._client = client

    # ------------------------------------------------------------------ plumbing

    def _create(self, timeout_s: float, **kwargs: Any) -> Any:
        import openai

        params: dict[str, Any] = {"model": self.model, "timeout": timeout_s, **kwargs}
        if self.extra_body:
            params["extra_body"] = self.extra_body
        if not self._omit_temperature:
            params["temperature"] = 0
        try:
            return self._client.chat.completions.create(**params)
        except openai.BadRequestError as exc:
            if not self._omit_temperature and "temperature" in str(exc).lower():
                self._omit_temperature = True
                params.pop("temperature", None)
                return self._client.chat.completions.create(**params)
            raise

    @staticmethod
    def _error(exc: Exception) -> str:
        import openai

        if isinstance(exc, openai.APITimeoutError):
            return "timeout"
        if isinstance(exc, openai.APIStatusError):
            return f"http_{exc.status_code}"
        if isinstance(exc, openai.APIConnectionError):
            return "connection"
        return "connection"

    @staticmethod
    def _calls(response: Any) -> tuple[list[ToolCall], str | None, bool]:
        """``(tool calls, text, bad_json)`` of a chat completion."""
        choice = response.choices[0]
        message = choice.message
        calls: list[ToolCall] = []
        bad = False
        for tc in getattr(message, "tool_calls", None) or []:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            raw = fn.arguments or "{}"
            try:
                args = json.loads(raw)
            except ValueError:
                bad = True
                continue
            if not isinstance(args, dict):
                bad = True
                continue
            calls.append(ToolCall(name=fn.name, arguments=args, raw=raw))
        return calls, getattr(message, "content", None), bad

    def _json_mode(self, messages: list[dict], tools: list[ToolSpec], timeout_s: float, started: float) -> LLMResult:
        import openai

        msgs = [{"role": "system", "content": json_mode_instructions(tools)}, *messages]
        try:
            response = self._create(timeout_s, messages=msgs, response_format={"type": "json_object"})
        except openai.APIError as exc:
            return _result(model=self.model, started=started, error=self._error(exc))
        except Exception:  # noqa: BLE001 - errors are returned, never raised (8.2)
            return _result(model=self.model, started=started, error="connection")
        content = getattr(response.choices[0].message, "content", None)
        call = parse_json_tool(content)
        if call is None:
            return _result(model=self.model, started=started, text=content, error="bad_json")
        return _result(model=self.model, started=started, text=None, calls=[call])

    # ------------------------------------------------------------------ API

    def complete(
        self,
        messages: list[dict],
        tools: list[ToolSpec],
        *,
        tool_choice: str = "required",
        timeout_s: float | None = None,
    ) -> LLMResult:
        import openai

        guard_keys()
        started = time.monotonic()
        timeout = float(timeout_s or self.timeout_s)
        if self.tool_mode == "json" or not tools:
            return self._json_mode(messages, tools, timeout, started)
        choice = tool_choice
        for attempt in range(2):
            try:
                response = self._create(timeout, messages=messages, tools=tool_specs_json(tools), tool_choice=choice)
            except openai.BadRequestError as exc:
                if attempt == 0 and choice == "required":
                    choice = "auto"
                    continue
                if exc.status_code == 400:
                    return self._json_mode(messages, tools, timeout, started)
                return _result(model=self.model, started=started, error=self._error(exc))
            except openai.APIError as exc:
                return _result(model=self.model, started=started, error=self._error(exc))
            except Exception:  # noqa: BLE001 - errors are returned, never raised (8.2)
                return _result(model=self.model, started=started, error="connection")
            calls, text, bad = self._calls(response)
            if calls:
                return _result(model=self.model, started=started, text=text, calls=calls)
            if bad:
                return _result(model=self.model, started=started, text=text, error="bad_json")
            # no tool call (allowed with "auto"): last resort is JSON mode
            return self._json_mode(messages, tools, timeout, started)
        return _result(model=self.model, started=started, error="http_400")  # pragma: no cover


class FallbackLLM:
    """``primary`` first; on its ``error`` the ``secondary`` (rule-based parser) answers (spec 8.2)."""

    def __init__(self, primary: Any, secondary: Any):
        self.primary = primary
        self.secondary = secondary
        self.name = getattr(primary, "name", "llm")
        self.last_fallback = False
        self.last_error: str | None = None

    @property
    def model(self) -> str:
        return getattr(self.primary, "model", self.name)

    def complete(
        self,
        messages: list[dict],
        tools: list[ToolSpec],
        *,
        tool_choice: str = "required",
        timeout_s: float | None = None,
    ) -> LLMResult:
        guard_keys()
        result = self.primary.complete(messages, tools, tool_choice=tool_choice, timeout_s=timeout_s)
        self.last_error = result.error
        if result.error is None:
            self.last_fallback = False
            return result
        self.last_fallback = True
        return self.secondary.complete(messages, tools, tool_choice=tool_choice, timeout_s=timeout_s)


def used_fallback(llm: Any) -> bool:
    """True when the last call of ``llm`` was answered by the rule-based fallback."""
    return bool(getattr(llm, "last_fallback", False))


def build_llm(
    *,
    mode: str,
    model: str,
    api_key: str,
    base_url: str,
    timeout_s: float,
    tool_mode: str,
    extra_body: dict,
    mock_script: str,
    persona: str,
    client: Any = None,
) -> tuple[Any, dict[str, Any]]:
    """The client for an effective mode (``mock`` or ``openai``) and its description for ``ChatResponse.llm``."""
    from acb.agent.fallback_parser import FallbackParser
    from acb.agent.mock_llm import MockLLM
    from acb.config import resolve_path

    if mode == "mock":
        llm = MockLLM(resolve_path(mock_script), persona=persona)
        return llm, {"mode": "mock", "model": llm.model, "persona": persona}
    if mode != "openai":
        raise ValueError("mode must be mock or openai")
    if not api_key:
        raise ValueError("LLM mode openai needs LLM_API_KEY")
    primary = OpenAICompatClient(
        model=model,
        api_key=api_key,
        base_url=base_url,
        timeout_s=timeout_s,
        tool_mode=tool_mode,
        extra_body=extra_body,
        client=client,
    )
    return FallbackLLM(primary, FallbackParser()), {"mode": "openai", "model": model, "persona": None}


__all__ = [
    "FallbackLLM",
    "LLMClient",
    "OpenAICompatClient",
    "build_llm",
    "guard_keys",
    "json_mode_instructions",
    "parse_json_tool",
    "set_exit_on_keys",
    "used_fallback",
]
