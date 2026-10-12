"""Deterministic scripted LLM (spec 8.7).

Same messages and tools give the same :class:`~acb.types.LLMResult` (latency fixed at 5 ms). A script is
a YAML list of rules evaluated top-down; the first match wins::

    - id: s5-intent
      when: {turn: owner, contains: "(?i)fx market data"}
      call: {tool: draft_mandate, args: {purpose: "...", payee_names: ["fx data"], max_per_tx_usdc: "1.00",
             max_total_usdc: "5.00", valid_hours: 24, recurring: false}}
    - id: invoice-default
      when: {turn: invoice}
      call: {tool: decide_invoice, args: {invoice_id: "{invoice.invoiceId}", action: "{policy_preview.tool_action}",
             payee_agent_id: "{invoice.issuerAgentId}", amount_usdc: "{invoice.amount_usdc}",
             rationale: "Follows policy preview"}}

``when`` keys (all optional, all must hold): ``turn`` (``owner``/``invoice``), ``contains`` (a regex
searched in the owner's message, or in an invoice turn in the user messages), ``state`` (dialogue
state), ``flagged`` (an invoice DATA block is flagged), ``persona``. A rule may give ``text`` (free
text of the result) or ``error`` (``timeout``, ``http_500``, ... to simulate an outage).

Template variables come from the context the runtime gives the model (the JSON block of
:mod:`acb.agent.prompts`): ``invoice.*``, ``policy_preview.*`` (including ``tool_action`` and
``amount_usdc``), ``draft.*``, ``status.*``, ``mandate.*`` (the newest active mandate),
``owner.message``. A string that is exactly one ``{var}`` takes the variable's own type (an agentId stays
an integer). ``{secret:NAME}`` renders the value of one of the agent's own secret variables; it exists only
so the attack suite can simulate an LLM that leaks a secret (A13), which the redactor must catch.

Personas:

- ``careful`` (default): the rules (the default script follows ``policy_preview.tool_action``).
- ``naive``: when the invoice's DATA block is flagged it extracts from the (unmasked) text, with fixed
  regexes, the first amount ``\\d+\\.\\d{2}`` (if none and rule R5 hit: the status's free balance) and the
  first ``agent #N`` (if none: the invoice issuer), and proposes ``decide_invoice(invoice_id=<this>,
  action=PAY, payee_agent_id=N, amount_usdc=<amount>)``. This simulates a hijacked LLM generically, so the
  block shown in the demo is real.

No rule matched: ``reply_to_owner("I did not understand. Could you rephrase?")``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from acb.agent.llm import guard_keys
from acb.agent.prompts import extract_context
from acb.agent.quarantine import DATA_HEADER, parse_blocks
from acb.agent.redact import SECRET_VARIABLES
from acb.config import load_yaml
from acb.types import LLMResult, ToolCall, ToolSpec

LATENCY_MS = 5
NOT_UNDERSTOOD = "I did not understand. Could you rephrase?"
PERSONAS = ("careful", "naive")
_VAR = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*|secret:[A-Z_]+)\}")
_AMOUNT = re.compile(r"\d+\.\d{2}")
_AGENT = re.compile(r"agent\s*#\s*(\d+)", re.IGNORECASE)


class MockLLM:
    """See the module docstring."""

    def __init__(self, script_path: str | os.PathLike[str] | None = None, persona: str = "careful", *, rules=None):
        if persona not in PERSONAS:
            raise ValueError(f"persona must be one of {PERSONAS}")
        self.persona = persona
        if rules is None:
            loaded = load_yaml(Path(script_path)) if script_path is not None else []
            rules = loaded or []
        if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
            raise ValueError("a MockLLM script is a list of rules")
        self.rules: list[dict[str, Any]] = [dict(r) for r in rules]
        self.name = f"mock-{persona}"
        self.model = self.name
        self.last_rule: str | None = None

    @classmethod
    def from_rules(cls, rules: list[dict[str, Any]], persona: str = "careful") -> MockLLM:
        return cls(None, persona, rules=rules)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _view(messages: list[dict]) -> dict[str, Any]:
        ctx = extract_context(messages)
        turn = ctx.get("turn") or "owner"
        users = [m.get("content") or "" for m in messages if m.get("role") == "user"]
        blocks = []
        for content in users:
            if isinstance(content, str) and content.startswith(DATA_HEADER):
                blocks.extend(parse_blocks(content))
        owner_text = ""
        if turn == "owner":
            plain = [c for c in users if isinstance(c, str) and not c.startswith(DATA_HEADER)]
            owner_text = plain[-1] if plain else ""
        return {"ctx": ctx, "turn": turn, "owner": owner_text, "users": users, "blocks": blocks}

    @staticmethod
    def _vars(view: dict[str, Any]) -> dict[str, Any]:
        ctx = view["ctx"]
        mandates = ctx.get("mandates") or []
        return {
            "invoice": ctx.get("invoice") or {},
            "policy_preview": ctx.get("policy_preview") or {},
            "draft": ctx.get("draft") or {},
            "status": ctx.get("status") or {},
            "mandate": mandates[-1] if mandates else {},
            "owner": {"message": view["owner"]},
            "state": ctx.get("state"),
        }

    @staticmethod
    def _lookup(variables: dict[str, Any], path: str) -> Any:
        if path.startswith("secret:"):
            name = path.split(":", 1)[1]
            return os.environ.get(name, "") if name in SECRET_VARIABLES else ""
        value: Any = variables
        for part in path.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return None
        return value

    def _render(self, value: Any, variables: dict[str, Any]) -> Any:
        if isinstance(value, str):
            whole = _VAR.fullmatch(value)
            if whole:
                found = self._lookup(variables, whole.group(1))
                return value if found is None else found
            return _VAR.sub(lambda m: _text(self._lookup(variables, m.group(1)), m.group(0)), value)
        if isinstance(value, dict):
            return {k: self._render(v, variables) for k, v in value.items()}
        if isinstance(value, list):
            return [self._render(v, variables) for v in value]
        return value

    def _matches(self, when: dict[str, Any], view: dict[str, Any]) -> bool:
        if "turn" in when and when["turn"] != view["turn"]:
            return False
        if "persona" in when and when["persona"] != self.persona:
            return False
        if "state" in when and when["state"] != view["ctx"].get("state"):
            return False
        if "flagged" in when and bool(when["flagged"]) != any(b.flagged for b in view["blocks"]):
            return False
        if "contains" in when:
            subject = view["owner"] if view["turn"] == "owner" else "\n".join(str(u) for u in view["users"])
            if re.search(str(when["contains"]), subject) is None:
                return False
        return True

    def _naive(self, view: dict[str, Any]) -> LLMResult:
        ctx = view["ctx"]
        invoice = ctx.get("invoice") or {}
        text = "\n".join(b.text for b in view["blocks"])
        rules = set((ctx.get("detector") or {}).get("rules") or []) | {
            r for b in view["blocks"] for r in getattr(b, "rules", ())
        }
        amount_match = _AMOUNT.search(text)
        if amount_match:
            amount = amount_match.group(0)
        elif "R5" in rules:
            amount = str((ctx.get("status") or {}).get("free_balance_usdc") or invoice.get("amount_usdc") or "0")
        else:
            amount = str(invoice.get("amount_usdc") or "0")
        agent_match = _AGENT.search(text)
        payee = int(agent_match.group(1)) if agent_match else invoice.get("issuerAgentId")
        args = {
            "invoice_id": invoice.get("invoiceId"),
            "action": "PAY",
            "payee_agent_id": payee,
            "amount_usdc": amount,
            "rationale": "The invoice note says how to pay, so I follow it.",
        }
        self.last_rule = "naive"
        return self._out(ToolCall(name="decide_invoice", arguments=args, raw=_dump(args)))

    def _out(self, call: ToolCall | None, text: str | None = None, error: str | None = None) -> LLMResult:
        return LLMResult(
            text=text,
            tool_calls=[call] if call is not None else [],
            model=self.model,
            latency_ms=LATENCY_MS,
            error=error,
        )

    # ------------------------------------------------------------------ API

    def complete(
        self,
        messages: list[dict],
        tools: list[ToolSpec],
        *,
        tool_choice: str = "required",
        timeout_s: float | None = None,
    ) -> LLMResult:
        guard_keys()
        view = self._view(messages)
        if self.persona == "naive" and view["turn"] == "invoice" and any(b.flagged for b in view["blocks"]):
            return self._naive(view)
        variables = self._vars(view)
        for rule in self.rules:
            if not self._matches(dict(rule.get("when") or {}), view):
                continue
            self.last_rule = str(rule.get("id") or "")
            if rule.get("error"):
                return self._out(None, error=str(rule["error"]))
            call_spec = rule.get("call")
            text = self._render(rule.get("text"), variables) if rule.get("text") is not None else None
            if not call_spec:
                return self._out(None, text=text)
            args = self._render(call_spec.get("args") or {}, variables)
            name = str(call_spec.get("tool"))
            return self._out(ToolCall(name=name, arguments=args, raw=_dump(args)), text=text)
        self.last_rule = None
        args = {"text": NOT_UNDERSTOOD}
        return self._out(ToolCall(name="reply_to_owner", arguments=args, raw=_dump(args)))


def _text(value: Any, default: str) -> str:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return _dump(value)
    return str(value)


def _dump(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


__all__ = ["LATENCY_MS", "NOT_UNDERSTOOD", "PERSONAS", "MockLLM"]
