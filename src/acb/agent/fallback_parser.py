"""Rule-based parser used when the LLM is unavailable (spec 8.2 ``FallbackLLM``, 8.6 "Fallback").

It is wrapped as an :class:`~acb.agent.llm.LLMClient` and reads the same messages as a real model (the
context block of :mod:`acb.agent.prompts` plus the owner's message), so every call it makes goes through
the same validators and policy as an LLM's. It handles:

- owner turns: a buy/pay intent with names and amounts (``draft_mandate``), corrections of the current
  draft (``amend_mandate``), answers to a payee or budget clarification, "buy N <sku> from <name>" under an
  active mandate (``request_quotes``), "status", "explain <id>" / "why ...", "freeze", "approve" (a
  pointer to the owner console: approvals are owner transactions); English and simple Chinese;
- invoice turns: ``decide_invoice`` with the policy preview's action (the policy outcome directly).

Anything else gets a short help text. Numbers are read with :func:`numbers_in` (digits, English number
words up to twenty, Chinese numerals up to ninety-nine).
"""

from __future__ import annotations

import re
import time
from decimal import Decimal
from typing import Any

from acb.agent.prompts import extract_context
from acb.types import LLMResult, ToolCall, ToolSpec

HELP = (
    "I am in rule-based mode. I can draft a mandate (for example: buy FX data from Atlas, budget 4 USDC), "
    "change the draft (make the total 3 USDC), show the status, explain a decision or freeze the wallet."
)
NO_SECRETS = (
    "I do not hold any keys, tokens or passwords. The wallet's signing key lives in a separate signer process "
    "that I cannot read, and I have no tool that reads files or the environment."
)
APPROVAL_HELP = (
    "Approvals are owner transactions: confirm the approval card in the owner console (or with "
    "`acb owner approve <orderId>`). I cannot approve payments myself."
)

_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "fifteen": 15,
    "twenty": 20,
}
_CN_DIGITS = {
    "\u96f6": 0,
    "\u3007": 0,
    "\u4e00": 1,
    "\u4e8c": 2,
    "\u4e24": 2,
    "\u4e09": 3,
    "\u56db": 4,
    "\u4e94": 5,
    "\u516d": 6,
    "\u4e03": 7,
    "\u516b": 8,
    "\u4e5d": 9,
}
_NUM = re.compile(r"(?<![0-9A-Za-z#.])(\d+(?:\.\d+)?)(?![0-9A-Za-z])")
_CN_NUM = re.compile(
    r"[\u96f6\u3007\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]+(?:\u70b9[\u96f6\u3007\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d]+)?"
)


def _cn_value(token: str) -> Decimal | None:
    whole, _, frac = token.partition("\u70b9")
    value = 0
    if "\u5341" in whole:
        tens, _, ones = whole.partition("\u5341")
        value = (_CN_DIGITS.get(tens, 1) if tens else 1) * 10 + (_CN_DIGITS.get(ones, 0) if ones else 0)
    elif whole:
        if any(c not in _CN_DIGITS for c in whole):
            return None
        for c in whole:
            value = value * 10 + _CN_DIGITS[c]
    text = str(value)
    if frac:
        if any(c not in _CN_DIGITS for c in frac):
            return None
        text += "." + "".join(str(_CN_DIGITS[c]) for c in frac)
    return Decimal(text)


def numbers_in(text: str) -> list[Decimal]:
    """Every number written in ``text``: digits (not inside ``#7`` or ``0x..``), number words, Chinese."""
    out: list[Decimal] = []
    for m in _NUM.finditer(text or ""):
        out.append(Decimal(m.group(1)))
    for word in re.findall(r"[A-Za-z]+", text or ""):
        if word.lower() in _WORDS:
            out.append(Decimal(_WORDS[word.lower()]))
    for m in _CN_NUM.finditer(text or ""):
        v = _cn_value(m.group(0))
        if v is not None:
            out.append(v)
    return out


def usdc_string(value: Decimal) -> str | None:
    """A tool amount string (two decimals) for a number, or None if it does not fit the pattern."""
    q = value.quantize(Decimal("0.01"))
    if q != value or q < 0 or q >= Decimal(1_000_000):
        return None
    return f"{q:.2f}"


def _number_after(text: str, keywords: str) -> Decimal | None:
    m = re.search(
        rf"(?:{keywords})[^0-9\u96f6\u3007\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]{{0,24}}(\d+(?:\.\d+)?|[\u96f6\u3007\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]+)",
        text,
    )
    if not m:
        return None
    token = m.group(1)
    if token[0].isdigit():
        return Decimal(token)
    return _cn_value(token)


def _number_before(text: str, keywords: str) -> Decimal | None:
    m = re.search(rf"(\d+(?:\.\d+)?)\s*{keywords}", text)
    return Decimal(m.group(1)) if m else None


_CATEGORY_PHRASES: tuple[tuple[str, str], ...] = (
    (r"histor", "historical fx data"),
    (r"\bfx\b|forex|exchange rate|currency|\u6c47\u7387|\u5916\u6c47", "fx data"),
    (r"\bapi\b|api calls|\u63a5\u53e3|\u8c03\u7528", "api calls"),
    (r"sample|\u6837\u672c|\u6837\u4f8b", "sample data"),
    (r"\bads?\b|advertis|\u5e7f\u544a", "ads"),
)


class FallbackParser:
    """The rule-based parser as an LLM client (see the module docstring)."""

    name = "fallback"
    model = "rule-based"

    def complete(
        self,
        messages: list[dict],
        tools: list[ToolSpec],
        *,
        tool_choice: str = "required",
        timeout_s: float | None = None,
    ) -> LLMResult:
        started = time.monotonic()
        ctx = extract_context(messages)
        turn = ctx.get("turn") or "owner"
        if turn == "invoice":
            call = self._invoice(ctx)
        else:
            owner = ""
            for m in messages:
                if m.get("role") == "user" and isinstance(m.get("content"), str):
                    owner = m["content"]
            call = self._owner(owner, ctx)
        return LLMResult(
            text=None,
            tool_calls=[call],
            model=self.model,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=None,
        )

    # ------------------------------------------------------------------ invoice turns

    @staticmethod
    def _invoice(ctx: dict[str, Any]) -> ToolCall:
        invoice = ctx.get("invoice") or {}
        preview = ctx.get("policy_preview") or {}
        args = {
            "invoice_id": invoice.get("invoiceId", ""),
            "action": preview.get("tool_action") or "REQUEST_OWNER_APPROVAL",
            "payee_agent_id": invoice.get("issuerAgentId"),
            "amount_usdc": invoice.get("amount_usdc", "0"),
            "rationale": "Rule-based mode: the policy outcome decides.",
        }
        return _call("decide_invoice", args)

    # ------------------------------------------------------------------ owner turns

    def _owner(self, text: str, ctx: dict[str, Any]) -> ToolCall:
        low = text.lower()
        draft = ctx.get("draft") or None
        state = ctx.get("state") or "IDLE"
        pending = ctx.get("pending_clarification") or None

        if re.search(r"\bfreeze\b|\u51bb\u7ed3", low) and not re.search(r"unfreeze|\u89e3\u51bb", low):
            return _call("freeze_wallet", {"reason": "Owner asked to freeze the wallet (rule-based mode)."})
        if re.search(r"\bapprov|\u6279\u51c6", low):
            return _call("reply_to_owner", {"text": APPROVAL_HELP})
        explain = self._explain(text, low, ctx)
        if explain is not None:
            return explain
        if re.search(r"private key|mnemonic|seed phrase|\.env\b|api key|password|\btokens?\b", low):
            return _call("reply_to_owner", {"text": NO_SECRETS})
        if pending is not None and state == "CLARIFYING":
            answer = self._clarification_answer(text, low, ctx, pending)
            if answer is not None:
                return answer
        if draft and state in ("DRAFT", "AWAITING_REGISTRATION", "CLARIFYING", "ACTIVE"):
            amend = self._amend(text, low, ctx, draft)
            if amend is not None:
                return amend
        if re.search(r"\bstatus\b|\bbalance\b|how much|\blimits?\b|\u4f59\u989d|\u72b6\u6001", low) and not re.search(
            r"\bbuy\b|\bpurchase\b|\u8d2d\u4e70|\u91c7\u8d2d", low
        ):
            return _call("show_status", {})
        quotes = self._request_quotes(text, low, ctx)
        if quotes is not None:
            return quotes
        if re.search(
            r"\b(buy|purchase|pay|need|want|get|order|budget|mandate|allow|authori[sz]e|set up|create)\b"
            r"|\u8d2d\u4e70|\u91c7\u8d2d|\u9700\u8981|\u9884\u7b97|\u4e70",
            low,
        ) or (_directory_names_in(text, ctx) and numbers_in(text)):
            return self._draft(text, low, ctx)
        return _call("reply_to_owner", {"text": HELP})

    @staticmethod
    def _explain(text: str, low: str, ctx: dict[str, Any]) -> ToolCall | None:
        if not re.search(r"\bexplain\b|\bwhy\b|\u4e3a\u4ec0\u4e48|\u89e3\u91ca", low):
            return None
        hex_id = re.search(r"0x[0-9a-fA-F]{64}", text)
        if re.search(r"\btier\b|\u7b49\u7ea7|\u5c42\u7ea7", low):
            return _call("explain", {"subject": "tier", "id": "current"})
        if re.search(r"\bgrade[ds]?\b|\u8bc4\u7ea7|\u4fe1\u7528", low):
            agent = re.search(r"#\s*(\d+)", text)
            name = _first_directory_name(text, ctx)
            ident = agent.group(1) if agent else (name or "unknown")
            return _call("explain", {"subject": "grade", "id": str(ident)[:80]})
        if re.search(r"\bmandate\b|\u6388\u6743", low):
            return _call("explain", {"subject": "mandate", "id": hex_id.group(0).lower() if hex_id else "current"})
        if hex_id:
            return _call("explain", {"subject": "decision", "id": hex_id.group(0).lower()})
        name = _first_directory_name(text, ctx)
        return _call("explain", {"subject": "decision", "id": (name or "latest")[:80]})

    @staticmethod
    def _clarification_answer(text: str, low: str, ctx: dict[str, Any], pending: dict[str, Any]) -> ToolCall | None:
        args = dict(pending.get("args") or {})
        field = pending.get("field")
        if not args:
            return None
        if field == "payee":
            options = [str(o) for o in pending.get("options") or []]
            if re.search(r"\ball\b|every|both|\u5168\u90e8|\u90fd", low):
                names = [o for o in options if not o.lower().startswith("all")] or list(args.get("payee_names", []))
            else:
                names = [o for o in options if o.lower() in low]
            names += [n for n in _directory_names_in(text, ctx) if n not in names]
            if not names:
                return None
            # the phrases that already resolved to one provider stay in the mandate
            names = [n for n in pending.get("keep") or [] if n not in names] + names
            args["payee_names"] = names[:6]
            return _call("draft_mandate", args)
        if field == "budget":
            nums = numbers_in(text)
            if not nums:
                return None
            total = usdc_string(nums[0])
            if total is None:
                return None
            args["max_total_usdc"] = total
            return _call("draft_mandate", args)
        return None

    @staticmethod
    def _amend(text: str, low: str, ctx: dict[str, Any], draft: dict[str, Any]) -> ToolCall | None:
        draft_id = str(draft.get("draft_id") or draft.get("draftId") or "")
        if not draft_id:
            return None
        correction = re.search(
            r"\b(actually|instead|change|make|not|correct|set|only|rather|add|remove|drop|sorry)\b"
            r"|\u6539|\u4e0d\u662f|\u6539\u6210|\u52a0\u4e0a|\u53bb\u6389",
            low,
        )
        if not correction:
            return None
        if re.search(r"per[ -]?(tx|transaction|payment|order)|each|\u6bcf\u7b14|\u5355\u7b14", low):
            value = _number_after(low, r"per[ -]?(?:tx|transaction|payment|order)|each|\u6bcf\u7b14|\u5355\u7b14")
            if value is not None and usdc_string(value):
                return _call(
                    "amend_mandate", {"draft_id": draft_id, "field": "max_per_tx_usdc", "value": usdc_string(value)}
                )
        if re.search(r"total|budget|\u603b|\u9884\u7b97", low):
            value = _number_after(low, r"total|budget|\u603b\u989d|\u603b|\u9884\u7b97")
            if value is None:
                nums = numbers_in(text)
                value = nums[0] if nums else None
            if value is not None and usdc_string(value):
                return _call(
                    "amend_mandate", {"draft_id": draft_id, "field": "max_total_usdc", "value": usdc_string(value)}
                )
        hours = _hours(low)
        if hours is not None and re.search(r"hour|day|week|valid|\u5c0f\u65f6|\u5929|\u5468", low):
            return _call("amend_mandate", {"draft_id": draft_id, "field": "valid_hours", "value": hours})
        payees = _amended_payees(text, low, ctx, draft)
        if payees is not None:
            return _call("amend_mandate", {"draft_id": draft_id, "field": "payee_names", "value": payees[:6]})
        nums = numbers_in(text)
        if nums and usdc_string(nums[0]):
            return _call(
                "amend_mandate", {"draft_id": draft_id, "field": "max_total_usdc", "value": usdc_string(nums[0])}
            )
        return None

    @staticmethod
    def _request_quotes(text: str, low: str, ctx: dict[str, Any]) -> ToolCall | None:
        mandates = ctx.get("mandates") or []
        if not mandates or not re.search(
            r"\b(quote|quotes|order|buy|purchase)\b|\u62a5\u4ef7|\u4e0b\u5355|\u8d2d\u4e70", low
        ):
            return None
        mandate = mandates[-1]
        payees = {int(p.get("agentId")): p for p in mandate.get("payees") or [] if p.get("agentId") is not None}
        directory = ctx.get("directory") or []
        items = []
        for entry in directory:
            aid = int(entry.get("agentId"))
            if aid not in payees:
                continue
            skus = list(entry.get("skus") or [])
            named = [s for s in skus if s.lower() in low]
            first = str(entry.get("name", "")).split()[0].lower() if entry.get("name") else ""
            if not named and not (first and re.search(rf"\b{re.escape(first)}\b", low)):
                continue
            sku = named[0] if named else (skus[0] if skus else None)
            if sku is None:
                continue
            qty_m = re.search(r"(\d+)\s*(?:x|units?|pcs|of|\u4efd|\u4e2a)", low)
            qty = int(qty_m.group(1)) if qty_m else 1
            items.append({"payee_agent_id": aid, "sku": sku, "quantity": max(1, min(1000, qty))})
        if not items:
            return None
        return _call("request_quotes", {"mandate_id": mandate.get("mandateId"), "items": items[:6]})

    @staticmethod
    def _draft(text: str, low: str, ctx: dict[str, Any]) -> ToolCall:
        names = _directory_names_in(text, ctx)
        if not names:
            for pattern, phrase in _CATEGORY_PHRASES:
                if re.search(pattern, low) and phrase not in names:
                    names.append(phrase)
            if "historical fx data" in names and "fx data" in names:
                names.remove("fx data")
        if not names:
            return _call(
                "ask_clarification",
                {
                    "question": "Which providers or kind of data should this mandate pay for?",
                    "field": "payee",
                    "options": ["fx data", "api calls", "sample data"],
                },
            )
        total = _number_before(low, r"(?:usdc|usd|dollars?)?\s*(?:in\s+)?total")
        if total is None:
            total = _number_after(low, r"budget|total|\u9884\u7b97|\u603b\u5171|\u603b\u989d")
        if total is None:
            total = _number_after(low, r"up to|at most|max(?:imum)?|\u6700\u591a")
        if total is None:
            usdc = re.search(r"(\d+(?:\.\d+)?)\s*(?:usdc|usd|dollars?|\$|\u7f8e\u5143|\u5200)", low)
            total = Decimal(usdc.group(1)) if usdc else None
        if total is None:
            cn = re.search(
                r"([\u96f6\u3007\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]+)\s*(?:usdc|\u7f8e\u5143|\u5200)",
                low,
            )
            total = _cn_value(cn.group(1)) if cn else None
        per_tx = _number_after(low, r"per[ -]?(?:tx|transaction|payment|order)|each|\u6bcf\u7b14|\u5355\u7b14")
        if per_tx is None:
            per_tx = _number_before(
                low, r"(?:usdc\s*|usd\s*|dollars?\s*)?(?:per|each|a)\s*(?:tx|transaction|payment|order)"
            )
        recurring = bool(re.search(r"every day|daily|recurring|each day|\u6bcf\u5929|\u6bcf\u65e5", low))
        hours = _hours(low) or 24
        args = {
            "purpose": _purpose(text),
            "payee_names": names[:6],
            "max_per_tx_usdc": (usdc_string(per_tx) if per_tx is not None else None) or "0",
            "max_total_usdc": (usdc_string(total) if total is not None else None) or "0",
            "valid_hours": hours,
            "recurring": recurring,
        }
        return _call("draft_mandate", args)


def _amended_payees(text: str, low: str, ctx: dict[str, Any], draft: dict[str, Any]) -> list[str] | None:
    """The new payee list of a correction ("only X", "add X", "remove X"), or None if it is not about payees."""
    mentioned = _directory_names_in(text, ctx)
    if not mentioned:
        return None
    current = [str(p.get("name")) for p in draft.get("payees") or [] if p.get("name")]
    removed = []
    for name in mentioned:
        first = re.escape(name.split()[0].lower())
        if re.search(rf"(?:remove|drop|without|except|exclude|no more|not)\s+(?:the\s+)?{first}", low) or re.search(
            rf"\u53bb\u6389\s*{first}", low
        ):
            removed.append(name)
    if re.search(r"\bonly\b|\binstead\b|\u53ea|\u6539\u6210", low):
        return [n for n in mentioned if n not in removed] or None
    if re.search(r"\b(add|include|plus|also)\b|\u52a0\u4e0a|\u589e\u52a0", low):
        return current + [n for n in mentioned if n not in current and n not in removed]
    if removed:
        return [n for n in current if n not in removed] or None
    return None


def _hours(low: str) -> int | None:
    m = re.search(r"(\d+)\s*(?:hours?|hrs?|h\b|\u5c0f\u65f6)", low)
    if m:
        return max(1, min(168, int(m.group(1))))
    m = re.search(r"(\d+)\s*(?:days?|\u5929)", low)
    if m:
        return max(1, min(168, int(m.group(1)) * 24))
    if re.search(r"\bweek\b|\u4e00\u5468|\u4e00\u661f\u671f", low):
        return 168
    if re.search(r"today|tonight|24 ?h|\u4eca\u5929|\u4eca\u65e5", low):
        return 24
    return None


def _purpose(text: str) -> str:
    clean = " ".join(text.split())
    return (clean[:197] + "...") if len(clean) > 200 else clean


def _first_directory_name(text: str, ctx: dict[str, Any]) -> str | None:
    names = _directory_names_in(text, ctx)
    return names[0] if names else None


def _directory_names_in(text: str, ctx: dict[str, Any]) -> list[str]:
    low = text.lower()
    out: list[str] = []
    for entry in ctx.get("directory") or []:
        name = str(entry.get("name") or "")
        key = str(entry.get("key") or "")
        first = name.split()[0].lower() if name else ""
        named = (name and name.lower() in low) or (first and re.search(rf"\b{re.escape(first)}\b", low))
        keyed = key and re.search(rf"\b{re.escape(key.lower())}\b", low)
        if (named or keyed) and name not in out:
            out.append(name)
    return out


def _call(name: str, args: dict[str, Any]) -> ToolCall:
    import json

    return ToolCall(name=name, arguments=args, raw=json.dumps(args, sort_keys=True, ensure_ascii=True))


__all__ = ["APPROVAL_HELP", "HELP", "FallbackParser", "numbers_in", "usdc_string"]
