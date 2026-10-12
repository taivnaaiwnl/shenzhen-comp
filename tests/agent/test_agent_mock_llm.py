"""MockLLM contract (spec 8.7): determinism, rules, templates, careful and naive personas."""

from __future__ import annotations

import pytest
from agent_helpers import DEFAULT_SCRIPT, INJECTION_AMOUNT, INJECTION_DRAIN, invoice_rule

from acb.agent.mock_llm import NOT_UNDERSTOOD, MockLLM
from acb.agent.prompts import invoice_messages, owner_messages
from acb.agent.quarantine import data_message, quarantine
from acb.agent.tools import tool_specs

INVOICE_CTX = {
    "status": {"free_balance_usdc": "9.80", "tier": 2},
    "invoice": {"invoiceId": "inv-orion-0006", "issuerAgentId": 5, "amount_usdc": "0.60", "amount": "600000"},
    "policy_preview": {"tool_action": "PAY", "amount_usdc": "0.60", "outcome": "PAY"},
    "detector": {"quarantine": False, "adverse": False, "rules": []},
}


def inv_messages(memo: str, *, mask: bool = False, ctx: dict | None = None) -> list[dict]:
    blocks = [quarantine("invoice:inv-orion-0006:memo", memo, mask=mask)]
    context = dict(ctx or INVOICE_CTX)
    if blocks[0].flagged:
        context = {**context, "detector": {"quarantine": True, "adverse": True, "rules": list(blocks[0].rules)}}
    return invoice_messages(context, data_message(blocks))


def test_deterministic_and_fixed_latency():
    llm = MockLLM(DEFAULT_SCRIPT)
    msgs = owner_messages("I need FX market data and API calls for my research today, budget 5 USDC", {"state": "IDLE"})
    a = llm.complete(msgs, tool_specs("owner"))
    b = llm.complete(msgs, tool_specs("owner"))
    assert a == b and a.latency_ms == 5 and a.error is None
    assert a.tool_calls[0].name == "draft_mandate"
    assert a.tool_calls[0].arguments["payee_names"] == ["fx data", "api calls"]


def test_invoice_default_follows_the_policy_preview_with_typed_values():
    llm = MockLLM(DEFAULT_SCRIPT)
    result = llm.complete(inv_messages("Thanks for your order."), tool_specs("invoice"))
    call = result.tool_calls[0]
    assert call.name == "decide_invoice"
    assert call.arguments == {
        "invoice_id": "inv-orion-0006",
        "action": "PAY",
        "payee_agent_id": 5,  # an integer, not "5"
        "amount_usdc": "0.60",
        "rationale": "Follows policy preview",
    }


def test_no_rule_matched_replies_not_understood():
    llm = MockLLM.from_rules([])
    result = llm.complete(owner_messages("hello", {}), tool_specs("owner"))
    assert result.tool_calls[0].name == "reply_to_owner"
    assert result.tool_calls[0].arguments == {"text": NOT_UNDERSTOOD}


def test_when_conditions_and_inline_templates():
    rules = [
        {
            "id": "s",
            "when": {"turn": "owner", "state": "DRAFT", "contains": "(?i)total"},
            "call": {
                "tool": "amend_mandate",
                "args": {"draft_id": "{draft.draft_id}", "field": "max_total_usdc", "value": "4.00"},
            },
        },
        {
            "id": "t",
            "when": {"turn": "owner"},
            "call": {"tool": "reply_to_owner", "args": {"text": "Tier {status.tier} and {missing.var}"}},
        },
    ]
    llm = MockLLM.from_rules(rules)
    ctx = {"state": "DRAFT", "draft": {"draft_id": "d-abc"}, "status": {"tier": 1}}
    r1 = llm.complete(owner_messages("make the total 4", ctx), [])
    assert r1.tool_calls[0].arguments["draft_id"] == "d-abc"
    r2 = llm.complete(owner_messages("hello", ctx), [])
    assert r2.tool_calls[0].arguments["text"] == "Tier 1 and {missing.var}"


def test_error_rules_simulate_an_outage():
    llm = MockLLM.from_rules([{"id": "down", "when": {"turn": "invoice"}, "error": "timeout"}])
    result = llm.complete(inv_messages("ok"), tool_specs("invoice"))
    assert result.error == "timeout" and result.tool_calls == []


def test_naive_follows_the_amount_injection():
    llm = MockLLM.from_rules([invoice_rule()], persona="naive")
    result = llm.complete(inv_messages(INJECTION_AMOUNT), tool_specs("invoice"))
    args = result.tool_calls[0].arguments
    assert args["action"] == "PAY" and args["amount_usdc"] == "3.00" and args["payee_agent_id"] == 5


def test_naive_follows_the_drain_injection_with_the_free_balance():
    llm = MockLLM.from_rules([invoice_rule()], persona="naive")
    result = llm.complete(inv_messages(INJECTION_DRAIN), tool_specs("invoice"))
    args = result.tool_calls[0].arguments
    assert args == {
        "invoice_id": "inv-orion-0006",
        "action": "PAY",
        "payee_agent_id": 8,
        "amount_usdc": "9.80",
        "rationale": args["rationale"],
    }


def test_naive_behaves_like_careful_on_clean_invoices_and_careful_ignores_injections():
    naive = MockLLM.from_rules([invoice_rule()], persona="naive")
    clean = naive.complete(inv_messages("Thanks for your order."), tool_specs("invoice"))
    assert clean.tool_calls[0].arguments["amount_usdc"] == "0.60"
    careful = MockLLM.from_rules([invoice_rule()], persona="careful")
    hijack = careful.complete(inv_messages(INJECTION_DRAIN), tool_specs("invoice"))
    assert hijack.tool_calls[0].arguments["payee_agent_id"] == 5


def test_naive_with_masking_on_cannot_read_the_masked_amount():
    llm = MockLLM.from_rules([invoice_rule()], persona="naive")
    result = llm.complete(inv_messages(INJECTION_AMOUNT, mask=True), tool_specs("invoice"))
    args = result.tool_calls[0].arguments
    assert args["payee_agent_id"] == 5  # no agent mentioned: the issuer


def test_bad_persona_and_script_shape():
    with pytest.raises(ValueError):
        MockLLM(DEFAULT_SCRIPT, persona="evil")
    with pytest.raises(ValueError):
        MockLLM.from_rules({"not": "a list"})  # type: ignore[arg-type]


def test_secret_template_only_reads_the_agent_secret_names(monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN", "t" * 40)
    monkeypatch.setenv("HOME_SECRET", "nope")
    llm = MockLLM.from_rules(
        [
            {
                "id": "leak",
                "when": {"turn": "owner"},
                "call": {"tool": "reply_to_owner", "args": {"text": "{secret:AGENT_TOKEN} {secret:HOME_SECRET}"}},
            }
        ]
    )
    text = llm.complete(owner_messages("x", {}), []).tool_calls[0].arguments["text"]
    assert text == "t" * 40 + " "
