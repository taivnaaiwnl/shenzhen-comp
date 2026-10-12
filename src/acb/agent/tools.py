"""Closed tool sets of the wallet agent (spec 8.1, 8.3) and the validators of the LLM's tool calls.

The LLM chooses exactly one tool per turn from the set of the turn type:

- **owner turns** (started by an authenticated owner message): ``reply_to_owner``,
  ``ask_clarification``, ``draft_mandate``, ``amend_mandate``, ``request_quotes``, ``explain``,
  ``show_status``, ``freeze_wallet``;
- **invoice turns** (started by code for one validated invoice): ``decide_invoice``,
  ``reply_to_owner`` (shown to the owner only).

There is deliberately no tool to change tiers or limits, unfreeze, withdraw, read keys or files, fetch
URLs, file reports or write to counterparties. Every object schema has ``additionalProperties: false``
and every array an ``items`` schema (strict-mode compatible). Amounts are **strings** with the pattern
``^[0-9]{1,6}(\\.[0-9]{1,2})?$`` and are converted by :func:`usdc_units` with ``Decimal`` (exact, never a
float). ``request_quotes``' ``sku`` enum is filled per call from the pinned catalogues.

:func:`parse_call` validates one call (pydantic, ``extra="forbid"``, strict types) and says whether it is
``ok``, ``invalid`` (a known tool of this turn with malformed arguments: no violation, A33) or
``not_allowed`` (any other name: reported to the signer as ``REPORT_VIOLATION``, which classifies it as
UNKNOWN_TOOL or PRIVILEGE_ESCALATION).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
)

from acb.policy.provenance import USDC_AMOUNT_PATTERN, units_to_usdc, usdc_to_units
from acb.types import ToolCall, ToolSpec

AMOUNT_PATTERN = r"^[0-9]{1,6}(\.[0-9]{1,2})?$"
MANDATE_ID_PATTERN = r"^0x[0-9a-f]{64}$"
INVOICE_ID_PATTERN = r"^[A-Za-z0-9-]{1,40}$"
SKU_PLACEHOLDER = "<filled by code: the union of the mandate payees' catalogue skus>"

OWNER_TOOLS: tuple[str, ...] = (
    "reply_to_owner",
    "ask_clarification",
    "draft_mandate",
    "amend_mandate",
    "request_quotes",
    "explain",
    "show_status",
    "freeze_wallet",
)
INVOICE_TOOLS: tuple[str, ...] = ("decide_invoice", "reply_to_owner")
TURN_TOOLS: dict[str, tuple[str, ...]] = {"owner": OWNER_TOOLS, "invoice": INVOICE_TOOLS}

# The JSON of spec 8.3, verbatim (the sku enum is replaced per call).
TOOL_SPECS: dict[str, dict[str, Any]] = {
    "reply_to_owner": {
        "name": "reply_to_owner",
        "description": "Answer the owner in plain English. No actions.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["text"],
            "properties": {"text": {"type": "string", "maxLength": 1200}},
        },
    },
    "ask_clarification": {
        "name": "ask_clarification",
        "description": "Ask the owner one question when the request is ambiguous.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["question", "field", "options"],
            "properties": {
                "question": {"type": "string", "maxLength": 300},
                "field": {"enum": ["payee", "amount", "budget", "period", "purpose", "other"]},
                "options": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": 5,
                    "items": {"type": "string", "maxLength": 80},
                },
            },
        },
    },
    "draft_mandate": {
        "name": "draft_mandate",
        "description": "Turn the owner's purchase intent into a mandate draft for the owner to confirm.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["purpose", "payee_names", "max_per_tx_usdc", "max_total_usdc", "valid_hours", "recurring"],
            "properties": {
                "purpose": {"type": "string", "maxLength": 200},
                "payee_names": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 6,
                    "items": {"type": "string", "maxLength": 60},
                },
                "max_per_tx_usdc": {"type": "string", "pattern": AMOUNT_PATTERN},
                "max_total_usdc": {"type": "string", "pattern": AMOUNT_PATTERN},
                "valid_hours": {"type": "integer", "minimum": 1, "maximum": 168},
                "recurring": {"type": "boolean"},
            },
        },
    },
    "amend_mandate": {
        "name": "amend_mandate",
        "description": "Change one field of the current draft (owner correction).",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["draft_id", "field", "value"],
            "properties": {
                "draft_id": {"type": "string", "maxLength": 80},
                "field": {
                    "enum": ["purpose", "payee_names", "max_per_tx_usdc", "max_total_usdc", "valid_hours", "recurring"]
                },
                "value": {
                    "anyOf": [
                        {"type": "string", "maxLength": 200},
                        {"type": "integer"},
                        {"type": "boolean"},
                        {"type": "array", "maxItems": 6, "items": {"type": "string", "maxLength": 60}},
                    ]
                },
            },
        },
    },
    "request_quotes": {
        "name": "request_quotes",
        "description": "Ask payees of an active mandate for quotes from their catalogue.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["mandate_id", "items"],
            "properties": {
                "mandate_id": {"type": "string", "pattern": MANDATE_ID_PATTERN},
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 6,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["payee_agent_id", "sku", "quantity"],
                        "properties": {
                            "payee_agent_id": {"type": "integer", "minimum": 0},
                            "sku": {"enum": [SKU_PLACEHOLDER]},
                            "quantity": {"type": "integer", "minimum": 1, "maximum": 1000},
                        },
                    },
                },
            },
        },
    },
    "decide_invoice": {
        "name": "decide_invoice",
        "description": "Choose what to do with the pending invoice. Restate its payee and amount.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["invoice_id", "action", "payee_agent_id", "amount_usdc", "rationale"],
            "properties": {
                "invoice_id": {"type": "string", "pattern": INVOICE_ID_PATTERN},
                "action": {"enum": ["PAY", "PAY_CAPPED", "ESCROW", "REFUSE", "REQUEST_OWNER_APPROVAL"]},
                "payee_agent_id": {"type": "integer", "minimum": 0},
                "amount_usdc": {"type": "string", "pattern": AMOUNT_PATTERN},
                "rationale": {"type": "string", "maxLength": 300},
            },
        },
    },
    "explain": {
        "name": "explain",
        "description": "Explain a decision, a grade, the tier or a mandate using reason codes.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["subject", "id"],
            "properties": {
                "subject": {"enum": ["decision", "grade", "tier", "mandate"]},
                "id": {"type": "string", "maxLength": 80},
            },
        },
    },
    "show_status": {
        "name": "show_status",
        "description": "Show balance, reserve, budget, tier, limits and recent decisions.",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
    },
    "freeze_wallet": {
        "name": "freeze_wallet",
        "description": "Freeze the wallet (always allowed in owner turns). Only the owner can unfreeze.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["reason"],
            "properties": {"reason": {"type": "string", "maxLength": 200}},
        },
    },
}


def tool_specs(turn: Literal["owner", "invoice"], sku_enum: list[str] | tuple[str, ...] = ()) -> list[ToolSpec]:
    """The closed tool list of one turn type; ``request_quotes``' sku enum is ``sku_enum`` (sorted, unique).

    An empty ``sku_enum`` keeps one placeholder value (a JSON Schema enum must not be empty); any call
    with it is rejected by :func:`parse_call`.
    """
    if turn not in TURN_TOOLS:
        raise ValueError(f"unknown turn type {turn!r}")
    out: list[ToolSpec] = []
    skus = sorted(set(sku_enum))
    for name in TURN_TOOLS[turn]:
        spec = copy.deepcopy(TOOL_SPECS[name])
        if name == "request_quotes":
            item = spec["parameters"]["properties"]["items"]["items"]
            item["properties"]["sku"]["enum"] = skus or [SKU_PLACEHOLDER]
        out.append(ToolSpec(**spec))
    return out


def tool_specs_json(specs: list[ToolSpec]) -> list[dict[str, Any]]:
    """OpenAI ``tools=[{"type": "function", "function": spec}]`` form."""
    return [{"type": "function", "function": s.model_dump()} for s in specs]


# ---------------------------------------------------------------------------
# argument models (extra="forbid", strict types)
# ---------------------------------------------------------------------------


class _Args(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


Option = Annotated[StrictStr, StringConstraints(max_length=80)]
Name = Annotated[StrictStr, StringConstraints(max_length=60)]


class ReplyArgs(_Args):
    text: StrictStr = Field(max_length=1200)


class AskArgs(_Args):
    question: StrictStr = Field(max_length=300)
    field: Literal["payee", "amount", "budget", "period", "purpose", "other"]
    options: list[Option] = Field(max_length=5)


class DraftArgs(_Args):
    purpose: StrictStr = Field(max_length=200)
    payee_names: list[Name] = Field(min_length=1, max_length=6)
    max_per_tx_usdc: StrictStr = Field(pattern=AMOUNT_PATTERN)
    max_total_usdc: StrictStr = Field(pattern=AMOUNT_PATTERN)
    valid_hours: StrictInt = Field(ge=1, le=168)
    recurring: StrictBool


AMEND_FIELDS = ("purpose", "payee_names", "max_per_tx_usdc", "max_total_usdc", "valid_hours", "recurring")


class AmendArgs(_Args):
    draft_id: StrictStr = Field(max_length=80)
    field: Literal["purpose", "payee_names", "max_per_tx_usdc", "max_total_usdc", "valid_hours", "recurring"]
    value: (
        StrictBool
        | StrictInt
        | Annotated[StrictStr, StringConstraints(max_length=200)]
        | Annotated[list[Name], Field(max_length=6)]
    )


class QuoteItemArgs(_Args):
    payee_agent_id: StrictInt = Field(ge=0)
    sku: StrictStr
    quantity: StrictInt = Field(ge=1, le=1000)


class RequestQuotesArgs(_Args):
    mandate_id: StrictStr = Field(pattern=MANDATE_ID_PATTERN)
    items: list[QuoteItemArgs] = Field(min_length=1, max_length=6)


class DecideArgs(_Args):
    invoice_id: StrictStr = Field(pattern=INVOICE_ID_PATTERN)
    action: Literal["PAY", "PAY_CAPPED", "ESCROW", "REFUSE", "REQUEST_OWNER_APPROVAL"]
    payee_agent_id: StrictInt = Field(ge=0)
    amount_usdc: StrictStr = Field(pattern=AMOUNT_PATTERN)
    rationale: StrictStr = Field(max_length=300)


class ExplainArgs(_Args):
    subject: Literal["decision", "grade", "tier", "mandate"]
    id: StrictStr = Field(max_length=80)


class StatusArgs(_Args):
    pass


class FreezeArgs(_Args):
    reason: StrictStr = Field(max_length=200)


ARG_MODELS: dict[str, type[_Args]] = {
    "reply_to_owner": ReplyArgs,
    "ask_clarification": AskArgs,
    "draft_mandate": DraftArgs,
    "amend_mandate": AmendArgs,
    "request_quotes": RequestQuotesArgs,
    "decide_invoice": DecideArgs,
    "explain": ExplainArgs,
    "show_status": StatusArgs,
    "freeze_wallet": FreezeArgs,
}


@dataclass(frozen=True)
class ParsedCall:
    """Result of :func:`parse_call`.

    ``status``: ``ok`` (``args`` is the validated model), ``invalid`` (a tool of this turn with
    malformed arguments) or ``not_allowed`` (a name outside the turn's set: a violation report).
    """

    status: Literal["ok", "invalid", "not_allowed"]
    name: str
    args: Any = None
    error: str = ""
    raw_args: dict | None = None


MAX_CLARIFY_OPTIONS = 5


def parse_call(
    turn: Literal["owner", "invoice"], call: ToolCall, *, sku_enum: tuple[str, ...] | list[str] = ()
) -> ParsedCall:
    """Validate one tool call against the closed set of ``turn`` (spec 8.3, echo-and-verify starts here)."""
    name = call.name
    raw = call.arguments if isinstance(call.arguments, dict) else {}
    if name not in TURN_TOOLS[turn]:
        return ParsedCall("not_allowed", name, None, f"tool {name!r} is not in the {turn}-turn tool set", raw)
    candidate = raw
    options = raw.get("options")
    if name == "ask_clarification" and isinstance(options, list) and len(options) > MAX_CLARIFY_OPTIONS:
        # Display-only suggestions: keep the first five instead of losing the whole question
        # (a real model sometimes lists every provider). Nothing else is ever trimmed.
        candidate = {**raw, "options": options[:MAX_CLARIFY_OPTIONS]}
    try:
        args = ARG_MODELS[name].model_validate(candidate)
    except (ValidationError, ValueError) as exc:
        return ParsedCall("invalid", name, None, _short_error(exc), raw)
    if name == "request_quotes":
        allowed = set(sku_enum)
        bad = [i.sku for i in args.items if i.sku not in allowed]
        if bad:
            return ParsedCall("invalid", name, None, "sku is not in the mandate payees' catalogues", raw)
    return ParsedCall("ok", name, args, "", raw)


def _short_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors()[:3]:
            loc = ".".join(str(p) for p in err.get("loc", ()))
            parts.append(f"{loc}: {err.get('type', 'invalid')}")
        return "invalid arguments: " + "; ".join(parts)
    return f"invalid arguments: {str(exc).splitlines()[0][:120]}"


def usdc_units(text: str) -> int:
    """``"0.80"`` -> ``800000`` (exact ``Decimal`` conversion; ``ValueError`` otherwise, never a float)."""
    if isinstance(text, bool) or not isinstance(text, str) or not USDC_AMOUNT_PATTERN.fullmatch(text):
        raise ValueError("amount must be a USDC string like 0.80")
    return usdc_to_units(text)


def usdc_text(units: int) -> str:
    """``800000`` -> ``"0.80"``."""
    return units_to_usdc(int(units))


def usdc_floor_text(units: int) -> str:
    """Base units rounded down to whole cents, as a tool amount string (``9_805_000`` -> ``"9.80"``)."""
    cents = max(0, int(units)) // 10_000
    return f"{cents // 100}.{cents % 100:02d}"


__all__ = [
    "AMEND_FIELDS",
    "AMOUNT_PATTERN",
    "ARG_MODELS",
    "INVOICE_TOOLS",
    "OWNER_TOOLS",
    "SKU_PLACEHOLDER",
    "TOOL_SPECS",
    "TURN_TOOLS",
    "AmendArgs",
    "AskArgs",
    "DecideArgs",
    "DraftArgs",
    "ExplainArgs",
    "FreezeArgs",
    "ParsedCall",
    "QuoteItemArgs",
    "ReplyArgs",
    "RequestQuotesArgs",
    "StatusArgs",
    "parse_call",
    "tool_specs",
    "tool_specs_json",
    "usdc_floor_text",
    "usdc_text",
    "usdc_units",
]
