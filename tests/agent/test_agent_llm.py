"""OpenAICompatClient and FallbackLLM (spec 8.2) against a fake OpenAI client (no network)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from acb.agent.fallback_parser import FallbackParser
from acb.agent.llm import FallbackLLM, OpenAICompatClient, build_llm, parse_json_tool
from acb.agent.mock_llm import MockLLM
from acb.agent.prompts import owner_messages
from acb.agent.tools import tool_specs
from acb.keys import PrivateKeyInEnvironment

REQ = httpx.Request("POST", "https://api.example.test/v1/chat/completions")


def bad_request(message: str) -> openai.BadRequestError:
    return openai.BadRequestError(message, response=httpx.Response(400, request=REQ), body=None)


def completion(*, tool: str | None = None, args: str | None = None, content: str | None = None):
    calls = None
    if tool is not None:
        calls = [SimpleNamespace(function=SimpleNamespace(name=tool, arguments=args or "{}"))]
    message = SimpleNamespace(tool_calls=calls, content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeCompletions:
    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def fake_client(script):
    comp = FakeCompletions(script)
    return SimpleNamespace(chat=SimpleNamespace(completions=comp)), comp


def client(script, **kw):
    fake, comp = fake_client(script)
    return OpenAICompatClient(model="m-1", api_key="k", client=fake, **kw), comp


MSGS = owner_messages("show my status", {"state": "IDLE"})
TOOLS = tool_specs("owner")


def test_native_tool_call_with_temperature_zero_and_extra_body():
    c, comp = client([completion(tool="show_status", args="{}")], extra_body={"thinking": {"type": "disabled"}})
    result = c.complete(MSGS, TOOLS)
    assert result.error is None and result.tool_calls[0].name == "show_status"
    sent = comp.calls[0]
    assert sent["temperature"] == 0
    assert sent["extra_body"] == {"thinking": {"type": "disabled"}}
    assert sent["tool_choice"] == "required"
    assert sent["tools"][0]["type"] == "function" and sent["tools"][0]["function"]["name"] == "reply_to_owner"
    assert "seed" not in sent


def test_400_on_required_retries_with_auto():
    c, comp = client([bad_request("tool_choice required is not supported"), completion(tool="show_status", args="{}")])
    result = c.complete(MSGS, TOOLS)
    assert result.tool_calls[0].name == "show_status"
    assert [k["tool_choice"] for k in comp.calls] == ["required", "auto"]


def test_then_json_mode_when_auto_fails_or_gives_no_call():
    answer = json.dumps({"tool": "show_status", "arguments": {}})
    c, comp = client([bad_request("no"), bad_request("no again"), completion(content=answer)])
    result = c.complete(MSGS, TOOLS)
    assert result.tool_calls[0].name == "show_status" and result.error is None
    assert comp.calls[-1]["response_format"] == {"type": "json_object"}
    assert "tools" not in comp.calls[-1]
    c2, _ = client([completion(content="I think you want the status"), completion(content=answer)])
    assert c2.complete(MSGS, TOOLS, tool_choice="auto").tool_calls[0].name == "show_status"


def test_json_tool_mode_from_the_start():
    answer = "```json\n" + json.dumps({"tool": "show_status", "arguments": {}}) + "\n```"
    c, comp = client([completion(content=answer)], tool_mode="json")
    result = c.complete(MSGS, TOOLS)
    assert result.tool_calls[0].name == "show_status"
    assert (
        comp.calls[0]["messages"][0]["role"] == "system"
        and "Tool calling is done in JSON" in comp.calls[0]["messages"][0]["content"]
    )


def test_temperature_rejected_is_retried_without_it():
    c, comp = client(
        [bad_request("Unsupported value: 'temperature' does not support 0"), completion(tool="show_status")]
    )
    assert c.complete(MSGS, TOOLS).tool_calls[0].name == "show_status"
    assert "temperature" in comp.calls[0] and "temperature" not in comp.calls[1]


def test_errors_are_returned_not_raised():
    c, _ = client([openai.APITimeoutError(request=REQ)])
    assert c.complete(MSGS, TOOLS).error == "timeout"
    status = openai.InternalServerError("boom", response=httpx.Response(500, request=REQ), body=None)
    c, _ = client([status])
    assert c.complete(MSGS, TOOLS).error == "http_500"
    c, _ = client([completion(tool="show_status", args="{not json")])
    assert c.complete(MSGS, TOOLS).error == "bad_json"
    c, _ = client([RuntimeError("socket")])
    assert c.complete(MSGS, TOOLS).error == "connection"
    c, _ = client([completion(content="no tool"), completion(content="still no json")])
    assert c.complete(MSGS, TOOLS).error == "bad_json"


def test_fallback_llm_uses_the_rule_based_parser_on_error():
    primary = MockLLM.from_rules([{"id": "down", "when": {"turn": "owner"}, "error": "http_503"}])
    llm = FallbackLLM(primary, FallbackParser())
    result = llm.complete(MSGS, TOOLS)
    assert llm.last_fallback and llm.last_error == "http_503"
    assert result.tool_calls[0].name == "show_status" and result.model == "rule-based"
    ok = FallbackLLM(MockLLM.from_rules([]), FallbackParser())
    ok.complete(MSGS, TOOLS)
    assert ok.last_fallback is False


@pytest.mark.parametrize("name", ["OWNER_PRIVATE_KEY", "acb_mnemonic", "Agent_Signer_Private_Key"])
def test_every_client_refuses_with_a_key_in_the_environment(monkeypatch, name):
    monkeypatch.setenv(name, "0x" + "11" * 32)
    c, comp = client([completion(tool="show_status")])
    with pytest.raises(PrivateKeyInEnvironment):
        c.complete(MSGS, TOOLS)
    assert comp.calls == []  # nothing was sent to the provider
    with pytest.raises(PrivateKeyInEnvironment):
        MockLLM.from_rules([]).complete(MSGS, TOOLS)
    with pytest.raises(PrivateKeyInEnvironment):
        FallbackLLM(MockLLM.from_rules([]), FallbackParser()).complete(MSGS, TOOLS)


def test_parse_json_tool():
    assert parse_json_tool('{"tool": "x", "arguments": {"a": 1}}').arguments == {"a": 1}
    assert parse_json_tool('noise {"tool": "x", "arguments": {}} tail').name == "x"
    assert parse_json_tool('{"tool": 3}') is None
    assert parse_json_tool('{"tool": "x", "arguments": [1]}') is None
    assert parse_json_tool("") is None


def test_build_llm_modes(tmp_path):
    llm, info = build_llm(
        mode="mock",
        model="x",
        api_key="",
        base_url="",
        timeout_s=5,
        tool_mode="native",
        extra_body={},
        mock_script="config/mock_llm/default.yaml",
        persona="naive",
    )
    assert isinstance(llm, MockLLM) and info == {"mode": "mock", "model": "mock-naive", "persona": "naive"}
    with pytest.raises(ValueError):
        build_llm(
            mode="openai",
            model="x",
            api_key="",
            base_url="",
            timeout_s=5,
            tool_mode="native",
            extra_body={},
            mock_script="",
            persona="careful",
        )
    fake, _ = fake_client([])
    llm, info = build_llm(
        mode="openai",
        model="gpt-x",
        api_key="k",
        base_url="https://api.openai.com/v1",
        timeout_s=5,
        tool_mode="native",
        extra_body={},
        mock_script="",
        persona="careful",
        client=fake,
    )
    assert isinstance(llm, FallbackLLM) and info["mode"] == "openai" and info["model"] == "gpt-x"
