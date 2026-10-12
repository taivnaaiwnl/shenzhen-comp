"""Owner dialogue of the wallet agent (spec 8.1, 8.3, 8.6).

One owner turn = one authenticated owner message, one LLM call with the owner-turn tool set, one tool.
Code then validates the call, re-derives everything security-relevant and answers with a reply and
cards (Appendix B). State machine (persisted)::

    IDLE -> CLARIFYING -> DRAFT -> AWAITING_REGISTRATION -> ACTIVE      (corrections return to DRAFT)

- **Intent** (``draft_mandate``): payee names are resolved against the pinned directory only; 0 or more
  than one match forces a clarification (``field="payee"``) even if the LLM did not ask, listing every
  candidate with agentId, grade, registration age, payout wallet and lookalike warnings. The total budget
  must be a number the owner wrote (echo-and-verify for owner turns); otherwise code asks for it
  (``field="budget"``). Defaults: ``max_per_tx`` = the current tier's perTx (when the owner gave none),
  ``valid_hours`` 24, ``recurring`` false.
- **Correction** (``amend_mandate``): before registration the draft card is updated with a diff; after
  registration a correction creates a new draft and the owner revokes the old mandate on-chain.
- **Registration**: the owner console registers the mandate with the owner key and calls
  ``/mandates/activated``; :meth:`Dialogue.activated` verifies it on-chain (MandateRegistered in our vault,
  payees, live wallets, caps, validity and recurring flag equal the draft) before it is ever used.
- ``request_quotes`` records the purchase plan (code-built from catalogue enums); ``explain`` renders
  reason-code texts verbatim, the failing checks, grade reasons from chain and tx links;
  ``show_status`` returns a status card; ``freeze_wallet`` asks the signer for ``SELF_FREEZE``.

Drafts change only in owner turns. Turns from the first intent to the registered mandate are logged per
conversation (target median <= 4).
"""

from __future__ import annotations

import re
import secrets
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from acb import reason_codes
from acb.agent.directory import Resolution, age_text, describe_candidate, tokens
from acb.agent.fallback_parser import numbers_in
from acb.agent.prompts import owner_messages
from acb.agent.redact import strongest
from acb.agent.signer_client import new_request_id
from acb.agent.tools import (
    AmendArgs,
    AskArgs,
    DraftArgs,
    ExplainArgs,
    FreezeArgs,
    ReplyArgs,
    RequestQuotesArgs,
    parse_call,
    tool_specs,
    usdc_text,
    usdc_units,
)
from acb.agent.wallet import TIER_NAMES, jsonable
from acb.types import (
    ChatResponse,
    Clarification,
    Decision,
    DecisionCard,
    ExecuteRequest,
    Grade,
    MandateDraft,
    PayeeCandidate,
    PayeeView,
    Proposal,
    StatusCard,
    VaultState,
)

if TYPE_CHECKING:
    from acb.agent.wallet import WalletAgent

STATES = ("IDLE", "CLARIFYING", "DRAFT", "AWAITING_REGISTRATION", "ACTIVE")
ALL_WORDS = re.compile(r"\ball\b|\bevery\b|\bboth\b|\u5168\u90e8|\u90fd")
PER_TX_WORDS = re.compile(
    r"per[ -]?(?:tx|transaction|payment|purchase|order|invoice)|\beach\b|\bsingle\b|at most|\bmax(?:imum)?\b"
    r"|\blimit\b|\u6bcf\u7b14|\u5355\u7b14|\u6700\u591a"
)


def _mentions(entry: Any, folded_text: str) -> bool:
    """The owner's text names this directory entry: full name, first name word, key or ``#agentId``."""
    name = entry.name.casefold()
    first = name.split()[0] if name.split() else entry.key
    words = (first, entry.key.casefold())
    if name and name in folded_text:
        return True
    if any(re.search(rf"(?<![0-9a-z]){re.escape(w)}(?![0-9a-z])", folded_text) for w in words if w):
        return True
    return re.search(rf"#\s*{int(entry.agent_id)}(?![0-9])", folded_text) is not None


NO_TOOL_REPLY = "I could not choose an action for that. Could you rephrase?"
NOT_A_TOOL_REPLY = (
    "I cannot do that: it is not one of my tools. I cannot change tiers, limits or keys, unfreeze the wallet or "
    "withdraw funds; only you can, with the owner key. The attempt was reported to the signer."
)
MAX_MESSAGE = 4000
VALIDITY_SLACK_S = 600


class Dialogue:
    """Owner turns of one :class:`~acb.agent.wallet.WalletAgent`."""

    def __init__(self, agent: WalletAgent):
        self.a = agent
        with agent.store.edit() as data:
            d = data["dialogue"]
            d.setdefault("state", "IDLE")
            d.setdefault("turns", [])
            d.setdefault("ownerTurns", 0)
            d.setdefault("intentTurn", None)
            d.setdefault("currentDraft", None)
            d.setdefault("pending", None)
            d.setdefault("metrics", [])

    # ------------------------------------------------------------------ state

    def state(self) -> str:
        return self.a.store.read("dialogue")["state"]

    def _set(self, **fields: Any) -> None:
        with self.a.store.edit() as data:
            data["dialogue"].update(fields)

    def _owner_texts(self) -> list[str]:
        """The owner's messages of this conversation: since the first intent, or only the latest message."""
        d = self.a.store.read("dialogue")
        start = d.get("intentTurn")
        owner = [t for t in d["turns"] if t.get("role") == "owner"]
        if start is None:
            return [owner[-1].get("text", "")] if owner else []
        return [t.get("text", "") for t in owner if int(t.get("n", 0)) >= int(start)]

    def _grounded(self, units: int, extra: str = "") -> bool:
        """The amount is a number the owner wrote in this conversation (echo-and-verify for owner turns)."""
        value = Decimal(int(units)) / Decimal(10**6)
        texts = self._owner_texts() + ([extra] if extra else [])
        return any(n == value for t in texts for n in numbers_in(t))

    # ------------------------------------------------------------------ context

    def _draft_summary(self) -> dict[str, Any] | None:
        d = self.a.store.read("dialogue")
        draft_id = d.get("currentDraft")
        if not draft_id:
            return None
        rec = self.a.store.read("drafts").get(draft_id)
        if rec is None:
            return None
        return {
            "draft_id": draft_id,
            "purpose": rec["purpose"],
            "payees": [{"agentId": aid, "name": self.a.directory.name_of(aid)} for aid in rec["payeeIds"]],
            "max_per_tx_usdc": usdc_text(int(rec["maxPerTx"])),
            "max_total_usdc": usdc_text(int(rec["maxTotal"])),
            "valid_hours": int(rec["validHours"]),
            "recurring": bool(rec["recurring"]),
            "status": rec["status"],
            "mandateId": rec.get("mandateId"),
        }

    def context(self, vault: VaultState, block: int | None) -> dict[str, Any]:
        a = self.a
        d = a.store.read("dialogue")
        history = d["turns"][-int(a.cfg.history_turns) :] if a.cfg.history_turns > 0 else []
        return {
            "state": d["state"],
            "status": a.status_context(vault),
            "mandates": [a.mandate_context(m, block) for m in a.verified_mandates(block)],
            "draft": self._draft_summary(),
            "pending_clarification": d.get("pending"),
            "dialogue": [{"role": t["role"], "text": t["text"]} for t in history],
            "directory": a.directory_context(),
        }

    def sku_enum(self, block: int | None) -> list[str]:
        a = self.a
        ids: list[int] = []
        for m in a.verified_mandates(block):
            ids.extend(int(x) for x in m.payee_agent_ids)
        return a.directory.skus_of(ids)

    # ------------------------------------------------------------------ one owner turn

    def owner_turn(self, message: str) -> ChatResponse:
        a = self.a
        message = (message or "")[:MAX_MESSAGE]
        with a.lock:
            with a.store.edit() as data:
                d = data["dialogue"]
                d["ownerTurns"] = int(d["ownerTurns"]) + 1
                d["turns"].append({"role": "owner", "text": message, "n": d["ownerTurns"]})
                d["turns"] = d["turns"][-50:]
            block = a.chain.block()
            vault = a.chain.vault_state(block)
            ctx = self.context(vault, block)
            skus = self.sku_enum(block)
            result, fallback = a.complete(owner_messages(message, ctx), tool_specs("owner", skus))
            tool = result.tool_calls[0].name if result.tool_calls else None
            reply, cards = self._handle(result, fallback, skus, vault, block, message)
            final = a.redactor.scan(reply)
            reply = final.text
            with a.store.edit() as data:
                d = data["dialogue"]
                d["turns"].append({"role": "agent", "text": reply[:1200], "n": d["ownerTurns"]})
                d["turns"] = d["turns"][-50:]
            a.bus.emit(
                "chat",
                {
                    "message": message[:500],
                    "reply": reply[:1200],
                    "tool": tool,
                    "state": self.state(),
                    "llm": a.llm_view(fallback),
                    "cards": [c.type for c in cards],
                },
            )
            for card in cards:
                a.bus.emit("card", card.to_wire())
            return ChatResponse(reply=reply, cards=tuple(cards), llm=a.llm_view(fallback))

    def _handle(
        self, result: Any, fallback: bool, skus: list[str], vault: VaultState, block: int, message: str
    ) -> tuple[str, list[Any]]:
        a = self.a
        if not result.tool_calls:
            return NO_TOOL_REPLY, []
        call = result.tool_calls[0]
        # exactly one tool per turn (8.1): every further call outside the owner set is reported, and
        # if there was one, nothing of this turn is executed
        extra_bad = a.report_extra_calls(result.tool_calls[1:], "owner", sku_enum=skus)
        args, kinds = a.redactor.scan_value(call.arguments)
        text_r = a.redactor.scan(result.text)
        secret = strongest(tuple(kinds) + text_r.kinds)
        parsed = parse_call("owner", call.model_copy(update={"arguments": args}), sku_enum=skus)
        if secret is not None or parsed.status == "not_allowed":
            a.report_violation(
                tool_name=call.name,
                raw_args=args,
                raw_text=text_r.text if result.text is not None else None,
                secret_match=secret,
                turn="owner",
            )
        if parsed.status == "not_allowed" or extra_bad:
            return NOT_A_TOOL_REPLY, []
        if parsed.status == "invalid":
            return f"I could not use that request ({parsed.error}). Could you rephrase?", []
        handler = getattr(self, f"_tool_{parsed.name}")
        reply, cards = handler(parsed.args, vault=vault, block=block, message=message, result=result, fallback=fallback)
        if secret is not None:
            reply += " (Part of my answer was removed because it looked like a secret.)"
        return reply, cards

    # ------------------------------------------------------------------ simple tools

    def _tool_reply_to_owner(self, args: ReplyArgs, **_: Any) -> tuple[str, list[Any]]:
        return args.text, []

    def _tool_ask_clarification(self, args: AskArgs, *, block: int, **_: Any) -> tuple[str, list[Any]]:
        a = self.a
        d = a.store.read("dialogue")
        candidates: list[PayeeCandidate] = []
        if args.field == "payee":
            candidates = [a.candidate(e, block) for e in a.directory.providers]
        previous = d.get("pending") or {}
        pending = {
            "field": args.field,
            "question": args.question,
            "options": list(args.options),
            "args": previous.get("args"),
            "keep": previous.get("keep", []),
        }
        self._start_intent()
        self._set(state="CLARIFYING", pending=pending)
        card = Clarification(
            question=args.question, field=args.field, options=tuple(args.options), candidates=tuple(candidates)
        )
        return args.question, [card]

    def _start_intent(self) -> None:
        with self.a.store.edit() as data:
            d = data["dialogue"]
            if d.get("intentTurn") is None:
                d["intentTurn"] = d["ownerTurns"]

    # ------------------------------------------------------------------ drafts

    def _tool_draft_mandate(self, args: DraftArgs, *, vault: VaultState, block: int, message: str, **_: Any):
        a = self.a
        self._start_intent()
        resolutions = a.directory.resolve_all(list(args.payee_names))
        unknown = [r for r in resolutions if not r.matches]
        ambiguous = [r for r in resolutions if len(r.matches) > 1]
        grounded = self._grounded_payees(resolutions)
        unnamed = [r for r in resolutions if r.unique is not None and r.unique.agent_id not in grounded]
        keep = [r.unique.name for r in resolutions if r.unique is not None and r.unique.agent_id in grounded]
        if unknown or ambiguous or unnamed:
            return self._payee_clarification(args.model_dump(), unknown, ambiguous, keep, block, unnamed=unnamed)
        total = usdc_units(args.max_total_usdc)
        if total == 0 or not self._grounded(total, message):
            return self._budget_clarification(args.model_dump(), keep)
        per_tx, notes = self._per_tx(usdc_units(args.max_per_tx_usdc), total, vault, message)
        hours = int(args.valid_hours)
        default_hours = int(a.cfg.defaults.valid_hours)
        if hours != default_hours and not self._hours_grounded(hours):
            notes.append(f"Validity set to {default_hours} h (the default; you did not give a period).")
            hours = default_hours
        ids: list[int] = []
        for r in resolutions:
            if r.unique is not None and r.unique.agent_id not in ids:
                ids.append(r.unique.agent_id)
        draft_id = "d-" + secrets.token_hex(6)
        record = {
            "draftId": draft_id,
            "purpose": args.purpose,
            "payeeIds": ids[:6],
            "maxPerTx": str(per_tx),
            "maxTotal": str(total),
            "validHours": hours,
            "recurring": bool(args.recurring),
            "status": "draft",
            "mandateId": None,
            "version": 1,
        }
        with a.store.edit() as data:
            data["drafts"][draft_id] = record
            data["dialogue"].update(state="DRAFT", currentDraft=draft_id, pending=None)
        card = self._draft_card(record, block, diff=None)
        return self._draft_reply(card, notes, block), [card]

    # ---- echo-and-verify for owner turns: every value of a draft comes from the owner's own words

    def _offered_names(self) -> set[str]:
        """Names the owner accepted as a group: the open payee question's options when the owner's latest
        message says "all" / "both" / "every", and the payees already resolved before that question."""
        d = self.a.store.read("dialogue")
        pending = d.get("pending") or {}
        out = set(pending.get("keep") or [])
        texts = self._owner_texts()
        if pending.get("field") == "payee" and texts and ALL_WORDS.search(texts[-1].lower()):
            out |= {str(o) for o in pending.get("options") or []}
        return out

    def _grounded_payees(self, resolutions: list[Resolution]) -> set[int]:
        """AgentIds the owner named: by name, first name word, key or ``#id``; by a phrase made of the owner's
        own words that matches only that provider; or by the category of the only provider in it."""
        directory = self.a.directory
        joined = " ".join(self._owner_texts()).casefold()
        owner_tokens = set(tokens(joined))
        offered = self._offered_names()
        out: set[int] = set()
        for r in resolutions:
            e = r.unique
            if e is None:
                continue
            if e.name in offered or _mentions(e, joined) or set(tokens(r.phrase)) <= owner_tokens:
                out.add(e.agent_id)
            else:
                category = set(tokens(e.category.replace("-", " ")))
                alone = [p for p in directory.providers if p.category == e.category] == [e]
                if category and category <= owner_tokens and alone:
                    out.add(e.agent_id)
        return out

    def _hours_grounded(self, hours: int) -> bool:
        joined = " ".join(self._owner_texts()).lower()
        if hours == 24 and re.search(
            r"today|tonight|24 ?h|one day|a day|\u4eca\u5929|\u4eca\u65e5|\u4e00\u5929", joined
        ):
            return True
        if hours == 168 and re.search(
            r"\bweek\b|7 days|seven days|\u4e00\u5468|\u4e00\u661f\u671f|\u4e03\u5929", joined
        ):
            return True
        for text in self._owner_texts():
            low = text.lower()
            for n in numbers_in(text):
                if n == hours and re.search(r"hour|\bh\b|\d\s*h\b|hrs?\b|\u5c0f\u65f6", low):
                    return True
                if n * 24 == hours and re.search(r"\bdays?\b|\u5929", low):
                    return True
        return False

    def _per_tx(self, per_tx: int, total: int, vault: VaultState, message: str) -> tuple[int, list[str]]:
        notes: list[str] = []
        tier_cap = 0 if vault.frozen else int(vault.config.per_tx)
        default = min(tier_cap, total) if tier_cap > 0 else total
        said_per_tx = bool(PER_TX_WORDS.search(" ".join(self._owner_texts()).lower()))
        if per_tx == 0 or not self._grounded(per_tx, message) or (per_tx == total and not said_per_tx):
            if per_tx != default:
                notes.append(f"Max per payment set to {usdc_text(default)} USDC (the current tier's limit).")
            per_tx = default
        if per_tx > total:
            notes.append("Max per payment lowered to the total budget.")
            per_tx = total
        return per_tx, notes

    def _payee_clarification(
        self,
        args: dict[str, Any],
        unknown: list[Resolution],
        ambiguous: list[Resolution],
        keep: list[str],
        block: int,
        *,
        unnamed: list[Resolution] | None = None,
    ) -> tuple[str, list[Any]]:
        a = self.a
        now = a.chain.now(block)
        parts: list[str] = []
        candidates: list[PayeeCandidate] = []
        options: list[str] = []
        if unnamed:
            # the LLM picked providers the owner did not name: code asks instead of letting it choose
            entries = []
            for r in unnamed:
                for e in a.directory.providers:
                    if e.category == r.unique.category and e not in entries:  # type: ignore[union-attr]
                        entries.append(e)
            cands = [a.candidate(e, block) for e in entries]
            parts.append(
                "You did not name a provider, and I will not choose one for you. Matching providers: "
                + "; ".join(describe_candidate(c, now) for c in cands)
                + "."
            )
            for c in cands:
                candidates.append(c)
                options.append(c.name)
        for r in ambiguous:
            cands = [a.candidate(e, block) for e in r.matches]
            parts.append(
                f"{len(cands)} providers match '{r.phrase}': "
                + "; ".join(describe_candidate(c, now) for c in cands)
                + "."
            )
            for c in cands:
                if c.agent_id not in {x.agent_id for x in candidates}:
                    candidates.append(c)
                    options.append(c.name)
        if unknown:
            phrases = ", ".join(f"'{r.phrase}'" for r in unknown)
            parts.append(f"I could not find {phrases} in the pinned directory of registered providers.")
            by_cat = a.directory.by_category()
            listing = "; ".join(
                f"{cat}: " + ", ".join(e.name for e in entries) for cat, entries in sorted(by_cat.items())
            )
            parts.append(f"Known providers by category: {listing}.")
            for e in a.directory.providers:
                if e.agent_id not in {x.agent_id for x in candidates}:
                    candidates.append(a.candidate(e, block))
                    options.append(e.name)
        if keep:
            parts.append("Already resolved: " + ", ".join(keep) + ".")
        if (ambiguous or unnamed) and len(options) > 1:
            options.append("All of them")
        parts.append("Which should I use?")
        question = " ".join(parts)
        pending = {"field": "payee", "question": question[:600], "options": options, "args": args, "keep": keep}
        self._set(state="CLARIFYING", pending=pending)
        card = Clarification(question=question, field="payee", options=tuple(options), candidates=tuple(candidates))
        return question, [card]

    def _budget_clarification(self, args: dict[str, Any], keep: list[str]) -> tuple[str, list[Any]]:
        question = "What total budget should this mandate have, in USDC? I will not guess an amount."
        pending = {"field": "budget", "question": question, "options": [], "args": args, "keep": keep}
        self._set(state="CLARIFYING", pending=pending)
        return question, [Clarification(question=question, field="budget", options=(), candidates=())]

    def _draft_card(self, record: dict[str, Any], block: int | None, diff: dict | None) -> MandateDraft:
        a = self.a
        payees = []
        for aid in record["payeeIds"]:
            entry = a.directory.get(aid)
            payees.append(a.candidate(entry, block) if entry is not None else a.payee_candidate(aid, block))
        return MandateDraft(
            draft_id=record["draftId"],
            purpose=record["purpose"],
            payees=tuple(payees),
            max_per_tx=int(record["maxPerTx"]),
            max_total=int(record["maxTotal"]),
            valid_hours=int(record["validHours"]),
            recurring=bool(record["recurring"]),
            diff=diff,
        )

    def _draft_reply(self, card: MandateDraft, notes: list[str], block: int | None) -> str:
        now = self.a.chain.now(block)
        payees = "; ".join(describe_candidate(c, now) for c in card.payees)
        warnings = [f"Warning: {c.name} looks like {c.lookalike_of}." for c in card.payees if c.lookalike_of]
        unrated = [c.name for c in card.payees if Grade(c.grade) in (Grade.NEW, Grade.NONE)]
        text = (
            f"Mandate draft {card.draft_id}: {card.purpose}. Payees: {payees}. "
            f"Max per payment {usdc_text(card.max_per_tx)} USDC, total {usdc_text(card.max_total)} USDC, "
            f"valid {card.valid_hours} h, {'recurring' if card.recurring else 'not recurring'}."
        )
        if unrated:
            text += f" {', '.join(unrated)} {'is' if len(unrated) == 1 else 'are'} new and unrated: paid by escrow."
        if warnings:
            text += " " + " ".join(warnings)
        if notes:
            text += " " + " ".join(notes)
        text += " Please confirm it in the owner console; it re-reads every payee from the chain before you sign."
        return text

    def _tool_amend_mandate(self, args: AmendArgs, *, vault: VaultState, block: int, message: str, **_: Any):
        a = self.a
        drafts = a.store.read("drafts")
        current = a.store.read("dialogue").get("currentDraft")
        draft_id = current if args.draft_id in ("current", "latest") else args.draft_id
        rec = drafts.get(draft_id)
        if rec is None:
            hint = f" The current draft is {current}." if current else " There is no draft yet."
            return f"I do not have a draft with id {args.draft_id}.{hint}", []
        field, value = args.field, args.value
        old = self._display(rec, field)
        new_rec = dict(rec)
        notes: list[str] = []
        if field == "purpose":
            if not isinstance(value, str):
                return "The purpose must be text.", []
            new_rec["purpose"] = value
        elif field == "payee_names":
            names = [value] if isinstance(value, str) else value
            if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
                return "Payee names must be a list of names.", []
            resolutions = a.directory.resolve_all(names)
            unknown = [r for r in resolutions if not r.matches]
            ambiguous = [r for r in resolutions if len(r.matches) > 1]
            grounded = self._grounded_payees(resolutions) | {int(x) for x in rec["payeeIds"]}
            unnamed = [r for r in resolutions if r.unique is not None and r.unique.agent_id not in grounded]
            if unknown or ambiguous or unnamed:
                base = self._draft_args(rec)
                base["payee_names"] = names
                keep = [r.unique.name for r in resolutions if r.unique is not None and r.unique.agent_id in grounded]
                return self._payee_clarification(base, unknown, ambiguous, keep, block, unnamed=unnamed)
            ids: list[int] = []
            for r in resolutions:
                if r.unique.agent_id not in ids:  # type: ignore[union-attr]
                    ids.append(r.unique.agent_id)  # type: ignore[union-attr]
            new_rec["payeeIds"] = ids[:6]
        elif field in ("max_per_tx_usdc", "max_total_usdc"):
            try:
                units = usdc_units(value) if isinstance(value, str) else None
            except ValueError:
                units = None
            if units is None or units == 0:
                return "Amounts are USDC strings like 4.00.", []
            if not self._grounded(units, message):
                return self._budget_clarification(self._draft_args(rec), [])
            key = "maxPerTx" if field == "max_per_tx_usdc" else "maxTotal"
            new_rec[key] = str(units)
            if int(new_rec["maxPerTx"]) > int(new_rec["maxTotal"]):
                new_rec["maxPerTx"] = new_rec["maxTotal"]
                notes.append("Max per payment lowered to the total budget.")
        elif field == "valid_hours":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 168:
                return "Validity must be 1 to 168 hours.", []
            if not self._hours_grounded(value):
                return "How long should the mandate be valid (for example 24 hours or 2 days)?", []
            new_rec["validHours"] = value
        elif field == "recurring":
            if not isinstance(value, bool):
                return "Recurring must be true or false.", []
            new_rec["recurring"] = value
        diff = {field: {"from": old, "to": self._display(new_rec, field)}}
        registered = rec.get("status") == "registered"
        if registered:
            new_id = "d-" + secrets.token_hex(6)
            new_rec.update(draftId=new_id, status="draft", mandateId=None, version=1, replaces=rec.get("mandateId"))
            notes.append(
                f"The registered mandate {rec.get('mandateId')} stays active until you revoke it on-chain in the console."
            )
        else:
            new_rec["version"] = int(rec.get("version", 1)) + 1
        with a.store.edit() as data:
            data["drafts"][new_rec["draftId"]] = new_rec
            data["dialogue"].update(state="DRAFT", currentDraft=new_rec["draftId"], pending=None)
        card = self._draft_card(new_rec, block, diff=diff)
        change = f"Changed {field.replace('_usdc', '').replace('_', ' ')} from {old} to {diff[field]['to']}."
        return change + " " + self._draft_reply(card, notes, block), [card]

    def _draft_args(self, rec: dict[str, Any]) -> dict[str, Any]:
        return {
            "purpose": rec["purpose"],
            "payee_names": [self.a.directory.name_of(aid) for aid in rec["payeeIds"]],
            "max_per_tx_usdc": usdc_text(int(rec["maxPerTx"])),
            "max_total_usdc": usdc_text(int(rec["maxTotal"])),
            "valid_hours": int(rec["validHours"]),
            "recurring": bool(rec["recurring"]),
        }

    def _display(self, rec: dict[str, Any], field: str) -> Any:
        if field == "purpose":
            return rec["purpose"]
        if field == "payee_names":
            return [self.a.directory.name_of(aid) for aid in rec["payeeIds"]]
        if field == "max_per_tx_usdc":
            return usdc_text(int(rec["maxPerTx"]))
        if field == "max_total_usdc":
            return usdc_text(int(rec["maxTotal"]))
        if field == "valid_hours":
            return int(rec["validHours"])
        return bool(rec["recurring"])

    # ------------------------------------------------------------------ registration

    def activated(
        self, mandate_id: str, tx_hash: str, draft_id: str, *, attempts: int = 5, wait_s: float = 1.0
    ) -> dict:
        """``POST /mandates/activated``: verify the registered mandate on-chain before using it."""
        import time

        a = self.a
        mid = mandate_id.lower()
        with a.lock:
            rec = a.store.read("drafts").get(draft_id)
            if rec is None:
                return {"status": "error", "error": "unknown draft"}
            self._set(state="AWAITING_REGISTRATION")
            view = None
            for i in range(max(1, attempts)):
                view = a.chain.mandate(mid)
                if view is not None:
                    break
                if i + 1 < attempts:
                    time.sleep(wait_s)
            if view is None:
                return {"status": "pending", "error": "MandateRegistered not found in our vault yet"}
            problems = self._compare(view, rec, tx_hash)
            existing = a.store.read("mandates").get(mid)
            if problems and existing is not None and existing.get("status") == "active":
                # a later mismatching notification never disables a mandate already verified on-chain
                a.bus.emit(
                    "error",
                    {"message": "activation does not match an active mandate", "mandateId": mid, "problems": problems},
                )
                return {"status": "mismatch", "mandateId": mid, "problems": problems}
            if problems:
                with a.store.edit() as data:
                    data["mandates"][mid] = {"status": "rejected", "draftId": draft_id, "problems": problems}
                    data["dialogue"]["state"] = "DRAFT"
                a.bus.emit(
                    "error", {"message": "mandate does not match the draft", "mandateId": mid, "problems": problems}
                )
                return {"status": "mismatch", "mandateId": mid, "problems": problems}
            d = a.store.read("dialogue")
            intent = d.get("intentTurn")
            turns = int(d["ownerTurns"]) - int(intent) + 1 if intent is not None else None
            with a.store.edit() as data:
                data["mandates"][mid] = {
                    "status": "active",
                    "draftId": draft_id,
                    "txHash": tx_hash.lower(),
                    "payeeIds": [int(x) for x in view.payee_agent_ids],
                    "verifiedAt": a.chain.now(),
                }
                data["drafts"][draft_id].update(status="registered", mandateId=mid)
                dd = data["dialogue"]
                dd.update(state="ACTIVE", pending=None, intentTurn=None)
                dd["metrics"].append({"draftId": draft_id, "mandateId": mid, "turnsToMandate": turns})
            a.bus.emit(
                "mandate",
                {
                    "mandateId": mid,
                    "draftId": draft_id,
                    "payees": [self.a.directory.name_of(x) for x in view.payee_agent_ids],
                    "maxPerTx": str(int(view.max_per_tx)),
                    "maxTotal": str(int(view.max_total)),
                    "validUntil": int(view.valid_until),
                    "turnsToMandate": turns,
                    "status": "active",
                },
                tx_hash=tx_hash.lower(),
            )
            return {"status": "active", "mandateId": mid, "turnsToMandate": turns}

    def _compare(self, view: Any, rec: dict[str, Any], tx_hash: str) -> list[str]:
        a = self.a
        problems = []
        if view.vault.lower() != a.vault_address:
            problems.append("mandate is not registered in our vault")
        if (view.register_tx or "").lower() != tx_hash.lower():
            problems.append("txHash is not the MandateRegistered transaction")
        if not view.active:
            problems.append("mandate is not active")
        now = a.chain.now()
        if now > view.valid_until:
            problems.append("mandate expired")
        if sorted(int(x) for x in view.payee_agent_ids) != sorted(int(x) for x in rec["payeeIds"]):
            problems.append("payees differ from the draft")
        for aid, wallet in zip(view.payee_agent_ids, view.payee_wallets, strict=False):
            live = a.chain.live_wallet(int(aid))
            if live is None or live.lower() != wallet.lower():
                problems.append(f"pinned wallet of #{int(aid)} differs from its live agent wallet")
        if int(view.max_per_tx) != int(rec["maxPerTx"]):
            problems.append("max per tx differs from the draft")
        if int(view.max_total) != int(rec["maxTotal"]):
            problems.append("max total differs from the draft")
        if bool(view.recurring) != bool(rec["recurring"]):
            problems.append("recurring flag differs from the draft")
        if int(view.valid_until) > now + int(rec["validHours"]) * 3600 + VALIDITY_SLACK_S:
            problems.append("validity is longer than the draft")
        return problems

    # ------------------------------------------------------------------ purchase plan

    def _tool_request_quotes(self, args: RequestQuotesArgs, *, block: int, **_: Any):
        a = self.a
        items = [{"payee_agent_id": i.payee_agent_id, "sku": i.sku, "quantity": i.quantity} for i in args.items]
        check = a.runtime.validate_items(args.mandate_id, items)
        if isinstance(check, str):
            return f"I cannot request those quotes: {check}.", []
        with a.store.edit() as data:
            data["purchases"][args.mandate_id.lower()] = check
        lines = []
        total = 0
        for item in check:
            entry = a.directory.get(item["payee_agent_id"])
            unit = int(entry.skus[item["sku"]]) if entry is not None else 0
            total += unit * item["quantity"]
            lines.append(
                f"{item['quantity']} x {item['sku']} from {a.directory.name_of(item['payee_agent_id'])} "
                f"(#{item['payee_agent_id']}) at {usdc_text(unit)} USDC"
            )
        return (
            f"Quote plan saved for mandate {args.mandate_id[:10]}...: "
            + "; ".join(lines)
            + f" (catalogue total {usdc_text(total)} USDC). Each quote gets a single-use quote request; the purchase "
            "starts when you press Buy in the console. Every invoice is checked by the policy and the signer.",
            [],
        )

    # ------------------------------------------------------------------ explain, status, freeze

    def _tool_explain(self, args: ExplainArgs, *, vault: VaultState, block: int, **_: Any):
        handler = {
            "decision": self._explain_decision,
            "grade": self._explain_grade,
            "tier": self._explain_tier,
            "mandate": self._explain_mandate,
        }[args.subject]
        return handler(args.id.strip(), vault, block)

    def _find_decision(self, ident: str) -> dict[str, Any] | None:
        decisions = list(self.a.store.read("decisions").values())
        if not decisions:
            return None
        key = ident.lower()
        if key in ("", "latest", "last", "current"):
            return decisions[-1]
        for rec in reversed(decisions):
            payee = rec.get("payee") or {}
            if key in (
                str(rec.get("orderId", "")).lower(),
                str(rec.get("decisionHash", "")).lower(),
                str(rec.get("invoiceHash", "")).lower(),
                str(rec.get("invoiceId", "")).lower(),
            ):
                return rec
            name = str(payee.get("name", "")).lower()
            if name and (key in name or name.split()[0] in key):
                return rec
        return None

    def _explain_decision(self, ident: str, vault: VaultState, block: int):
        a = self.a
        rec = self._find_decision(ident)
        if rec is None:
            return f"I found no decision for '{ident}'.", []
        payee = rec.get("payee") or {}
        reasons = [int(r) for r in rec.get("reasons") or []]
        head = (
            f"{rec['outcome']} {payee.get('name', '?')} #{payee.get('agentId', '?')} "
            f"{usdc_text(int(rec.get('amount') or 0))} USDC:"
        )
        lines = [head]
        lines += [t for t in a.reason_texts(reasons)]
        failing = [c for c in rec.get("checks") or [] if not c.get("passed")]
        if failing:
            lines.append(
                "Failed checks: " + ", ".join(f"{c['id']} {c['name']} ({c.get('detail', '')})" for c in failing)
            )
        grade = a.chain.grade_info(int(rec["issuer"]), block)
        if grade.reasons:
            lines.append(
                f"{payee.get('name', 'Payee')} grade {Grade(grade.grade).name}: "
                + ", ".join(a.reason_texts(grade.reasons))
            )
        if rec.get("violation"):
            v = rec["violation"]
            lines.append(f"Violation recorded: {v['kind']} (severity {v['severity']}).")
        for h in rec.get("txHashes") or []:
            lines.append(f"tx {a.explorer(h)}")
        if not rec.get("txHashes"):
            lines.append("off-chain (no transaction)")
        card = DecisionCard(
            order_id=rec["orderId"],
            payee=PayeeCandidate.model_validate(payee),
            amount=int(rec.get("amount") or 0),
            outcome=Decision[rec["outcome"]],
            reasons=tuple(reasons[:6]),
            reason_texts=a.reason_texts(reasons[:6]),
            tx_hash=(rec.get("txHashes") or [None])[-1],
            offchain=not rec.get("txHashes"),
        )
        return " ".join(lines), [card]

    def _explain_grade(self, ident: str, vault: VaultState, block: int):
        a = self.a
        aid: int | None = None
        m = re.fullmatch(r"#?\s*(\d+)", ident)
        if m:
            aid = int(m.group(1))
        else:
            res = a.directory.resolve(ident)
            if res.unique is not None:
                aid = res.unique.agent_id
            else:
                found = a.directory.find_names_in(ident)
                aid = found[0].agent_id if len(found) == 1 else None
        if aid is None:
            return f"Which agent? I could not match '{ident}' to one agent of the pinned directory.", []
        info = a.chain.grade_info(aid, block)
        name = a.directory.name_of(aid)
        if info.score is None:
            return f"{name} (#{aid}) has no posted grade yet (NONE): unrated counterparties are paid by escrow.", []
        reasons = ", ".join(a.reason_texts(info.reasons)) or "no adverse factors"
        return f"{name} (#{aid}): effective grade {Grade(info.grade).name}, score {info.score}. Reasons: {reasons}.", []

    def _explain_tier(self, ident: str, vault: VaultState, block: int):
        a = self.a
        tier = int(vault.tier)
        cfg = vault.config
        text = f"The wallet is at tier {tier} ({TIER_NAMES.get(tier, '?')})"
        if vault.frozen:
            text += ": frozen. Nothing moves out until you unfreeze it with the owner key (3300)."
        else:
            text += (
                f": per payment {usdc_text(int(cfg.per_tx))} USDC, rolling 24 h window {usdc_text(int(cfg.window_cap))} "
                f"USDC, at most {int(cfg.max_tx)} payments, new payees {usdc_text(int(cfg.new_payee_cap))} USDC."
            )
        text += (
            " Tiers are earned on-chain: the autonomy operator promotes one tier after a clean record "
            f"({int(vault.clean_since_change)} clean orders so far, {int(vault.violation_points)} violation points) and a "
            f"cooldown, never above the ceiling you set (maxAutoTier {int(vault.max_auto_tier)}). I cannot raise it; "
            "you can lower the ceiling or freeze the wallet."
        )
        changes = a.chain.tier_changes(block)
        if changes:
            last = changes[-1]
            code = int(last["reasonCode"])
            try:
                why = reason_codes.text(code)
            except KeyError:
                why = "?"
            text += f" Last change: tier {last['fromTier']} -> {last['toTier']} ({code}: {why}) tx {a.explorer(last['txHash'])}."
        return text, []

    def _explain_mandate(self, ident: str, vault: VaultState, block: int):
        a = self.a
        mid = ident.lower() if re.fullmatch(r"0x[0-9a-fA-F]{64}", ident) else None
        if mid is None:
            mandates = a.verified_mandates(block)
            if not mandates:
                return "There is no active mandate yet.", []
            view = mandates[-1]
        else:
            view = a.chain.mandate(mid, block)
            if view is None:
                return f"Mandate {ident} is not registered in our vault.", []
        payees = ", ".join(f"{a.directory.name_of(x)} (#{int(x)})" for x in view.payee_agent_ids)
        text = (
            f"Mandate {view.mandate_id}: payees {payees}; max per payment {usdc_text(int(view.max_per_tx))} USDC; "
            f"spent {usdc_text(int(view.spent))} of {usdc_text(int(view.max_total))} USDC; valid until chain time "
            f"{int(view.valid_until)}; {'active' if view.active else 'revoked'}"
            f"{'; recurring' if view.recurring else ''}. Registered by the owner in tx {a.explorer(view.register_tx)}."
        )
        return text, []

    def _tool_show_status(self, _args: Any, *, vault: VaultState, block: int, **_: Any):
        a = self.a
        mandates = a.verified_mandates(block)
        free = max(0, int(vault.balance) - int(vault.escrow_locked) - int(vault.reserve_floor))
        text = (
            f"Tier {int(vault.tier)} ({TIER_NAMES.get(int(vault.tier), '?')}){' FROZEN' if vault.frozen else ''}. "
            f"Balance {usdc_text(int(vault.balance))} USDC (reserve {usdc_text(int(vault.reserve_floor))}, escrow "
            f"{usdc_text(int(vault.escrow_locked))}, free {usdc_text(free)}). Window {usdc_text(int(vault.window_spent))} of "
            f"{usdc_text(int(vault.config.window_cap))} USDC ({int(vault.window_count)} of {int(vault.config.max_tx)} payments)."
        )
        for m in mandates:
            text += f" Mandate {m.mandate_id[:10]}...: {usdc_text(int(m.spent))} of {usdc_text(int(m.max_total))} USDC spent."
        recent = list(a.store.read("decisions").values())[-3:]
        if recent:
            text += (
                " Recent decisions: "
                + "; ".join(
                    f"{r['outcome']} {(r.get('payee') or {}).get('name', '?')} {usdc_text(int(r.get('amount') or 0))} "
                    f"[{','.join(str(x) for x in r.get('reasons') or [])}]"
                    for r in recent
                )
                + "."
            )
        return text, [StatusCard(vault=vault, mandates=tuple(mandates))]

    def _tool_freeze_wallet(self, args: FreezeArgs, *, result: Any, fallback: bool, **_: Any):
        a = self.a
        proposal = Proposal(
            source="fallback" if fallback else "llm",
            turn="owner",
            tool="freeze_wallet",
            action=None,
            invoice_id=None,
            payee_agent_id=None,
            amount=None,
            raw_args=jsonable(args.model_dump()),
            raw_text=None,
            llm_model=getattr(result, "model", None),
        )
        resp = a.execute(ExecuteRequest(request_id=new_request_id(), kind="SELF_FREEZE", proposal=proposal))
        if resp is not None and resp.status == "executed":
            tx = resp.tx[-1].hash if resp.tx else None
            a.bus.emit("tier", {"message": "self-freeze", "reason": args.reason[:200]}, reason_codes=[3203], tx_hash=tx)
            text = (
                f"The wallet is frozen (3203: {reason_codes.text(3203)}). Only you can unfreeze it with the owner key. "
                f"tx {a.explorer(tx)}"
            )
            return text, []
        status = resp.status if resp is not None else "signer unavailable"
        codes = ",".join(str(c) for c in (resp.reason_codes if resp is not None else ()))
        return f"The freeze was not executed ({status} {codes}). You can freeze the wallet from the console.", []


def payee_line(view: PayeeView, now: int) -> str:
    """One line about a payee view (used by reports and the CLI)."""
    return (
        f"{view.display_name} (#{view.agent_id}, grade {Grade(view.grade).name}, {age_text(view.registered_at, now)})"
    )


__all__ = ["STATES", "Dialogue", "payee_line"]
