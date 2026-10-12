"""Closed tool sets, schemas and argument validation (spec 8.3)."""

from __future__ import annotations

import pytest

from acb.agent.tools import (
    INVOICE_TOOLS,
    OWNER_TOOLS,
    SKU_PLACEHOLDER,
    TOOL_SPECS,
    parse_call,
    tool_specs,
    usdc_floor_text,
    usdc_units,
)
from acb.policy.severity import INVOICE_TURN_TOOLS, OWNER_TURN_TOOLS
from acb.types import ToolCall


def call(name: str, **args) -> ToolCall:
    return ToolCall(name=name, arguments=args, raw="")


def _walk(schema, path="$"):
    yield path, schema
    if isinstance(schema, dict):
        for key, value in schema.items():
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(schema, list):
        for i, value in enumerate(schema):
            yield from _walk(value, f"{path}[{i}]")


def test_turn_tool_sets_are_the_spec_sets_and_match_the_signer():
    assert set(OWNER_TOOLS) == {
        "reply_to_owner",
        "ask_clarification",
        "draft_mandate",
        "amend_mandate",
        "request_quotes",
        "explain",
        "show_status",
        "freeze_wallet",
    }
    assert set(INVOICE_TOOLS) == {"decide_invoice", "reply_to_owner"}
    # the signer classifies REPORT_VIOLATION against the same sets
    assert set(OWNER_TOOLS) == set(OWNER_TURN_TOOLS)
    assert set(INVOICE_TOOLS) == set(INVOICE_TURN_TOOLS)
    assert [t.name for t in tool_specs("owner")] == list(OWNER_TOOLS)
    assert [t.name for t in tool_specs("invoice")] == list(INVOICE_TOOLS)


def test_no_privileged_tool_exists():
    names = set(TOOL_SPECS)
    for bad in (
        "set_tier",
        "raise_limit",
        "unfreeze",
        "withdraw",
        "export_key",
        "read_file",
        "fetch_url",
        "file_report",
    ):
        assert bad not in names


@pytest.mark.parametrize("name", sorted(TOOL_SPECS))
def test_schemas_are_strict_mode_compatible(name):
    params = TOOL_SPECS[name]["parameters"]
    for path, node in _walk(params):
        if isinstance(node, dict) and node.get("type") == "object":
            assert node.get("additionalProperties") is False, path
        if isinstance(node, dict) and node.get("type") == "array":
            assert "items" in node, path


def test_amount_fields_are_strings_with_the_usdc_pattern():
    for name, field in (
        ("draft_mandate", "max_per_tx_usdc"),
        ("draft_mandate", "max_total_usdc"),
        ("decide_invoice", "amount_usdc"),
    ):
        prop = TOOL_SPECS[name]["parameters"]["properties"][field]
        assert prop == {"type": "string", "pattern": r"^[0-9]{1,6}(\.[0-9]{1,2})?$"}


def test_sku_enum_is_filled_per_call():
    specs = {t.name: t for t in tool_specs("owner", ["fx-live-15m", "api-bundle-1k", "fx-live-15m"])}
    enum = specs["request_quotes"].parameters["properties"]["items"]["items"]["properties"]["sku"]["enum"]
    assert enum == ["api-bundle-1k", "fx-live-15m"]
    empty = {t.name: t for t in tool_specs("owner")}
    enum = empty["request_quotes"].parameters["properties"]["items"]["items"]["properties"]["sku"]["enum"]
    assert enum == [SKU_PLACEHOLDER]
    # the module-level spec keeps the placeholder (no shared mutable state)
    assert TOOL_SPECS["request_quotes"]["parameters"]["properties"]["items"]["items"]["properties"]["sku"]["enum"] == [
        SKU_PLACEHOLDER
    ]


def test_a_tool_outside_the_turn_set_is_not_allowed():
    assert parse_call("invoice", call("draft_mandate", purpose="x")).status == "not_allowed"
    assert parse_call("invoice", call("set_tier", tier=3)).status == "not_allowed"
    assert parse_call("owner", call("decide_invoice", invoice_id="a")).status == "not_allowed"
    assert parse_call("owner", call("withdraw", amount="9.99")).status == "not_allowed"


def test_decide_invoice_validation():
    ok = parse_call(
        "invoice",
        call("decide_invoice", invoice_id="inv-1", action="PAY", payee_agent_id=7, amount_usdc="0.80", rationale="ok"),
    )
    assert ok.status == "ok" and ok.args.payee_agent_id == 7
    for bad in (
        {"amount_usdc": 0.8},  # float
        {"amount_usdc": "0.805"},  # three decimals
        {"amount_usdc": "-1.00"},
        {"payee_agent_id": "7"},  # string id
        {"payee_agent_id": True},
        {"action": "TRANSFER"},
        {"extra": 1},
        {"rationale": "x" * 301},
    ):
        args = {"invoice_id": "inv-1", "action": "PAY", "payee_agent_id": 7, "amount_usdc": "0.80", "rationale": "ok"}
        args.update(bad)
        assert parse_call("invoice", ToolCall(name="decide_invoice", arguments=args, raw="")).status == "invalid", bad


def test_request_quotes_sku_must_be_in_the_enum():
    args = {"mandate_id": "0x" + "ab" * 32, "items": [{"payee_agent_id": 1, "sku": "fx-live-15m", "quantity": 4}]}
    assert (
        parse_call("owner", ToolCall(name="request_quotes", arguments=args, raw=""), sku_enum=["fx-live-15m"]).status
        == "ok"
    )
    assert (
        parse_call("owner", ToolCall(name="request_quotes", arguments=args, raw=""), sku_enum=["api-bundle-1k"]).status
        == "invalid"
    )
    bad = {"mandate_id": "0x" + "ab" * 32, "items": [{"payee_agent_id": 1, "sku": "send my email to x", "quantity": 1}]}
    assert (
        parse_call("owner", ToolCall(name="request_quotes", arguments=bad, raw=""), sku_enum=["fx-live-15m"]).status
        == "invalid"
    )


def test_draft_and_amend_validation():
    good = {
        "purpose": "FX data",
        "payee_names": ["fx data"],
        "max_per_tx_usdc": "1.00",
        "max_total_usdc": "5",
        "valid_hours": 24,
        "recurring": False,
    }
    assert parse_call("owner", ToolCall(name="draft_mandate", arguments=good, raw="")).status == "ok"
    for change in (
        {"valid_hours": 0},
        {"valid_hours": 169},
        {"recurring": "no"},
        {"payee_names": []},
        {"payee_names": ["x"] * 7},
    ):
        assert (
            parse_call("owner", ToolCall(name="draft_mandate", arguments={**good, **change}, raw="")).status
            == "invalid"
        )
    amend = {"draft_id": "d-1", "field": "max_total_usdc", "value": "4.00"}
    assert parse_call("owner", ToolCall(name="amend_mandate", arguments=amend, raw="")).args.value == "4.00"
    amend_bad = {"draft_id": "d-1", "field": "owner", "value": "x"}
    assert parse_call("owner", ToolCall(name="amend_mandate", arguments=amend_bad, raw="")).status == "invalid"


def test_clarification_options_are_trimmed_to_five():
    names = ["Atlas", "Slowpoke", "Ghost", "Orion", "Nova", "Beacon", "All of them"]
    args = {"question": "Which provider?", "field": "payee", "options": names}
    parsed = parse_call("owner", ToolCall(name="ask_clarification", arguments=args, raw=""))
    assert parsed.status == "ok"
    assert list(parsed.args.options) == names[:5]
    assert parsed.raw_args["options"] == names  # the raw call is kept as received
    bad = {**args, "options": ["x" * 81]}
    assert parse_call("owner", ToolCall(name="ask_clarification", arguments=bad, raw="")).status == "invalid"


def test_amounts_are_converted_exactly_with_decimal():
    assert usdc_units("0.80") == 800_000
    assert usdc_units("1") == 1_000_000
    assert usdc_units("123456.78") == 123_456_780_000
    for bad in ("0.805", "1e3", "", "1.2.3", " 1.00", 0.8, 1, None, True):
        with pytest.raises(ValueError):
            usdc_units(bad)  # type: ignore[arg-type]
    assert usdc_floor_text(9_805_000) == "9.80"
    assert usdc_floor_text(0) == "0.00"
