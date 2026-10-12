"""The owner's actions (spec 11.1, 11.3): every owner transaction and every notification to the agent.

:class:`OwnerConsole` is shared by the web app and the CLI, so both use the same verification code
(:mod:`acb.owner.chain_views`):

- **mandate**: ``prepare_mandate(draft_id)`` rebuilds the card from chain reads and stores the exact
  values; ``confirm_mandate(draft_id, prepare_id)`` re-checks the chain, sends ``registerMandate`` with
  those stored values (never values sent back by the browser or the agent) and then tells the agent
  (``POST /mandates/activated``), which verifies the mandate on-chain itself;
- **approval**: ``prepare_approval(order_id)`` verifies the agent's signed invoice itself;
  ``approve(order_id, prepare_id)`` re-verifies, sends ``ownerApprove`` with the stored values and asks
  the agent to execute (``POST /approvals/{orderId}/executed-request``); ``reject`` only tells the agent;
- ``revoke_mandate``, ``freeze`` (3202), ``unfreeze(to_tier, rotate_to)`` (3300, optional key rotation in
  the same transaction) and ``bad_data`` (the owner signs the report payload hash, the signer files it).

Signing modes: ``server`` (default) signs with the owner key P1 holds; ``browser`` (stretch goal 11.4)
returns the exact transaction for MetaMask (``eth_sendTransaction``) and accepts it only when the mined
transaction is exactly that call from the vault owner.
"""

from __future__ import annotations

import concurrent.futures
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from acb.owner import chain_views as cv
from acb.owner.agent_client import AgentClient, AgentUnavailable

PREPARE_TTL_S = 600
"""A reviewed card must be confirmed within this many seconds (wall clock); then review it again."""
BROWSER_TX_TTL_S = 900
MAX_CHAT_CHARS = 2000
AGENT_VIEW_WAIT_S = 3.0
"""How long a snapshot waits for the agent's state; afterwards it shows the previous read."""
FREEZE_REASON = 3202
UNFREEZE_REASON = 3300


class ActionError(Exception):
    """An owner action that cannot be done; ``status`` is the HTTP status, the message is safe to show."""

    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def owner_account_for(network: str, environ: Mapping[str, str] | None = None) -> Any:
    """The owner key: ``OWNER_PRIVATE_KEY``, or on Anvil only the public Anvil test key of the owner role."""
    from acb.keys import KeyRing, load_role_key

    try:
        return load_role_key("owner", environ)
    except KeyError:
        if network == "anvil":
            return KeyRing.anvil().account("owner")
        raise


class OwnerConsole:
    """State and actions of one owner console."""

    def __init__(
        self,
        *,
        chain_factory: Callable[[], cv.OwnerChain],
        agent: AgentClient | None,
        bus: Any,
        owner_account: Any = None,
        signing: str = "server",
        merger: Any = None,
        clock: Callable[[], float] = time.time,
    ):
        if signing not in ("server", "browser"):
            raise ValueError("signing must be 'server' or 'browser'")
        self._chain_factory = chain_factory
        self._chain: cv.OwnerChain | None = None
        self.agent = agent
        self.bus = bus
        self.owner_account = owner_account
        self.signing = signing
        self.merger = merger
        self.clock = clock
        self._prepared: dict[str, dict[str, Any]] = {}
        self._pending_tx: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._tx_lock = threading.Lock()
        self._sender: Any = None
        self._snapshot: tuple[float, dict[str, Any]] | None = None
        self._agent_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="owner-agent")
        self._agent_future: concurrent.futures.Future | None = None
        self._agent_cache: tuple[dict[str, Any], list[str]] = ({"online": False}, ["the agent was not read yet"])

    # ------------------------------------------------------------------ basics

    @property
    def chain(self) -> cv.OwnerChain:
        with self._lock:
            if self._chain is None:
                try:
                    self._chain = self._chain_factory()
                except (FileNotFoundError, KeyError, ValueError, OSError) as exc:
                    raise ActionError(503, f"the chain or the deployment is not available: {exc}") from None
            return self._chain

    def _agent(self) -> AgentClient:
        if self.agent is None:
            raise ActionError(503, "no wallet agent is configured")
        return self.agent

    def owner_address(self) -> str | None:
        if self.owner_account is not None:
            return self.owner_account.address.lower()
        if self.signing == "browser":
            try:
                return self.chain.vault.owner().lower()
            except Exception:  # noqa: BLE001 - unknown
                return None
        return None

    def owner_check(self) -> dict[str, Any]:
        """Is the console's owner key the vault's owner? (shown in the banner; actions refuse otherwise)."""
        address = self.owner_address()
        try:
            vault_owner = self.chain.vault.owner().lower()
        except ActionError:
            raise
        except Exception as exc:  # noqa: BLE001 - chain unreachable
            raise ActionError(503, f"cannot read the vault owner: {type(exc).__name__}") from None
        return {"address": address, "vaultOwner": vault_owner, "isVaultOwner": address == vault_owner}

    def _emit(self, type_: str, data: dict[str, Any], **kw: Any) -> None:
        if self.bus is not None:
            try:
                self.bus.emit(type_, data, **kw)
            except Exception:  # noqa: BLE001, S110 - logging must never break an owner action
                pass

    # ------------------------------------------------------------------ sending

    def _sender_for(self) -> Any:
        from acb.chain.tx import TxSender

        if self.owner_account is None:
            raise ActionError(503, "the owner key is not available to this console")
        if self._sender is None:
            self._sender = TxSender(self.chain.w3, self.owner_account)
        return self._sender

    def _transact(self, fn: Any, label: str, after: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
        """Send ``fn`` from the owner (or hand it to the browser), then run ``after(tx_hash)``."""
        check = self.owner_check()
        if not check["isVaultOwner"]:
            raise ActionError(403, "this console's owner key is not the owner of the wallet vault")
        if self.signing == "browser":
            action_id = "tx-" + secrets.token_hex(12)
            tx = {
                "from": check["vaultOwner"],
                "to": self.chain.vault.address,
                "data": fn._encode_transaction_data(),
                "value": "0x0",
                "chainId": hex(int(self.chain.dep.chain_id)),
            }
            with self._lock:
                self._pending_tx[action_id] = {
                    "kind": "tx",
                    "label": label,
                    "tx": tx,
                    "after": after,
                    "created": self.clock(),
                }
            return {"status": "sign_in_browser", "actionId": action_id, "label": label, "tx": tx}
        from acb.chain.tx import TxError, TxSimulationError

        with self._tx_lock:
            sender = self._sender_for()
            try:
                result = sender.send(fn)
            except TxSimulationError as exc:
                raise ActionError(409, f"{label}: the vault would revert with {exc.revert_error}") from None
            except TxError as exc:
                raise ActionError(502, f"{label}: the transaction failed ({type(exc).__name__})") from None
        if result.status != 1:
            raise ActionError(409, f"{label} reverted: {result.revert_error}", txHash=result.hash)
        return after(result.hash)

    def submitted(self, action_id: str, tx_hash: str | None = None, signature: str | None = None) -> dict[str, Any]:
        """Browser signing: the browser sent the transaction (or signed the digest); verify and continue."""
        with self._lock:
            pending = self._pending_tx.pop(action_id, None)
        if pending is None or self.clock() - pending["created"] > BROWSER_TX_TTL_S:
            raise ActionError(404, "unknown or expired browser action: review it again")
        if pending["kind"] == "sign":
            from acb.canonical import recover_eip191

            try:
                signer = recover_eip191(pending["digest"], signature or "")
            except (TypeError, ValueError):
                raise ActionError(400, "the signature is malformed") from None
            if signer != self.chain.vault.owner().lower():
                raise ActionError(403, "the signature is not from the vault owner")
            return pending["after"](signature)
        if not cv.is_hex32(tx_hash):
            raise ActionError(400, "txHash must be 0x + 64 hex")
        w3 = self.chain.w3
        try:
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            tx = w3.eth.get_transaction(tx_hash)
        except Exception as exc:  # noqa: BLE001 - not mined or not found
            raise ActionError(504, f"the transaction was not found or not mined ({type(exc).__name__})") from None
        want = pending["tx"]
        sent_data = tx.get("input")
        sent_data = sent_data if isinstance(sent_data, str) else "0x" + bytes(sent_data).hex()
        if (
            str(tx.get("from", "")).lower() != want["from"]
            or str(tx.get("to", "")).lower() != want["to"]
            or sent_data.lower() != want["data"].lower()
            or int(tx.get("value", 0)) != 0
        ):
            raise ActionError(409, "the mined transaction is not the reviewed call from the vault owner")
        if int(receipt["status"]) != 1:
            raise ActionError(409, f"{pending['label']} reverted on-chain")
        return pending["after"](tx_hash.lower())

    # ------------------------------------------------------------------ prepared cards

    def _store(self, prepared: dict[str, Any], **extra: Any) -> dict[str, Any]:
        prepare_id = "p-" + secrets.token_hex(12)
        with self._lock:
            now = self.clock()
            for key in [k for k, v in self._prepared.items() if now - v["created"] > PREPARE_TTL_S]:
                del self._prepared[key]
            self._prepared[prepare_id] = {"prepared": prepared, "created": now, **extra}
        public = {k: v for k, v in prepared.items() if k != "salt"}
        public["prepareId"] = prepare_id
        public["signing"] = self.signing
        public["expiresInS"] = PREPARE_TTL_S
        return public

    def _take(self, prepare_id: str, kind: str, key: str, value: str) -> dict[str, Any]:
        with self._lock:
            entry = self._prepared.pop(prepare_id, None) if isinstance(prepare_id, str) else None
        if entry is None or self.clock() - entry["created"] > PREPARE_TTL_S:
            raise ActionError(409, "this card was not reviewed or its review expired: review it again")
        prepared = entry["prepared"]
        if prepared["kind"] != kind or prepared.get(key) != value:
            raise ActionError(409, "the reviewed card does not belong to this request")
        return entry

    def discard(self, prepare_id: str) -> None:
        """Forget a reviewed card (the owner cancelled)."""
        with self._lock:
            self._prepared.pop(prepare_id, None)

    # ------------------------------------------------------------------ chat

    def chat(self, message: str) -> dict[str, Any]:
        """Proxy one owner message to the agent; every card is rebuilt by P1 before it is shown."""
        if not isinstance(message, str) or not message.strip():
            raise ActionError(422, "the message is empty")
        if len(message) > MAX_CHAT_CHARS:
            raise ActionError(422, f"the message is longer than {MAX_CHAT_CHARS} characters")
        try:
            answer = self._agent().chat(message)
        except AgentUnavailable as exc:
            raise ActionError(502, str(exc)) from None
        reply = answer.get("reply")
        llm = answer.get("llm") if isinstance(answer.get("llm"), dict) else {}
        cards = answer.get("cards") if isinstance(answer.get("cards"), list) else []
        out_cards = [self.verified_card(card) for card in cards[:8] if isinstance(card, dict)]
        return {
            "reply": reply if isinstance(reply, str) else "",
            "cards": out_cards,
            "llm": {
                "mode": str(llm.get("mode") or "")[:40],
                "model": str(llm.get("model") or "")[:80],
                "persona": str(llm.get("persona") or "")[:40],
                "fallback": bool(llm.get("fallback")),
            },
        }

    def verified_card(self, card: Mapping[str, Any]) -> dict[str, Any]:
        """The card P1 shows for one agent card: rebuilt from chain reads, never the agent's values."""
        kind = card.get("type")
        try:
            if kind == "mandate_draft":
                draft_id = card.get("draftId")
                if not isinstance(draft_id, str) or not 1 <= len(draft_id) <= 80:
                    raise cv.VerificationError("the draft has no draftId")
                return {"type": "mandate", **self._prepare_mandate_from(card, draft_id)}
            if kind == "approval":
                order = card.get("orderId")
                return {"type": "approval", **self._prepare_approval_from(order, card.get("invoice"))}
            if kind == "clarification":
                return self._clarification(card)
            if kind == "decision":
                return self._decision(card)
            if kind == "status":
                return {"type": "status", "wallet": cv.PanelReader(self.chain).wallet()}
        except cv.VerificationError as exc:
            return {"type": "unverified", "of": str(kind)[:40], "error": str(exc)}
        except ActionError as exc:
            return {"type": "unverified", "of": str(kind)[:40], "error": exc.message}
        return {"type": "unverified", "of": str(kind)[:40], "error": "unknown card type"}

    def _clarification(self, card: Mapping[str, Any]) -> dict[str, Any]:
        oc = self.chain
        now = oc.now()
        max_age = oc.vault.max_grade_age()
        roles = oc.roles()
        candidates = []
        for c in (card.get("candidates") or [])[:8]:
            aid = c.get("agentId") if isinstance(c, Mapping) else None
            if isinstance(aid, (int, str)) and not isinstance(aid, bool) and str(aid).isdigit() and len(str(aid)) < 78:
                candidates.append(oc.payee_row(int(aid), now=now, max_age=max_age, roles=roles))
        options = [str(o)[:80] for o in (card.get("options") or [])[:5] if isinstance(o, (str, int))]
        return {
            "type": "clarification",
            "question": str(card.get("question") or "")[:300],
            "field": str(card.get("field") or "")[:20],
            "options": options,
            "candidates": candidates,
        }

    def _decision(self, card: Mapping[str, Any]) -> dict[str, Any]:
        from acb.types import Decision

        oc = self.chain
        payee = card.get("payee") if isinstance(card.get("payee"), Mapping) else {}
        aid = payee.get("agentId")
        name = oc.directory.name_of(int(aid)) if isinstance(aid, (int, str)) and str(aid).isdigit() else "unknown"
        outcome = card.get("outcome")
        try:
            outcome_name = Decision(int(outcome)).name if not isinstance(outcome, str) else Decision[outcome].name
        except (KeyError, ValueError, TypeError):
            outcome_name = "UNKNOWN"
        codes = [int(c) for c in (card.get("reasons") or []) if isinstance(c, int) and not isinstance(c, bool)][:4]
        tx = card.get("txHash")
        tx = tx.lower() if cv.is_hex32(tx) else None
        order = card.get("orderId")
        amount = card.get("amount")
        return {
            "type": "decision",
            "orderId": order.lower() if cv.is_hex32(order) else None,
            "payee": {"agentId": str(aid) if aid is not None else None, "name": name},
            "amount": str(amount) if isinstance(amount, (int, str)) and str(amount).isdigit() else None,
            "outcome": outcome_name,
            "reasons": cv.reason_entries(codes),
            "txHash": tx,
            "explorer": oc.explorer_tx(tx),
            "offchain": bool(card.get("offchain")),
        }

    # ------------------------------------------------------------------ mandates

    def _prepare_mandate_from(self, draft: Mapping[str, Any], draft_id: str) -> dict[str, Any]:
        prepared = cv.prepare_mandate(self.chain, draft, draft_id=draft_id)
        return self._store(prepared)

    def prepare_mandate(self, draft_id: str, draft: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """The chain-rebuilt confirm card of the agent's draft ``draft_id``."""
        if not isinstance(draft_id, str) or not 1 <= len(draft_id) <= 80:
            raise ActionError(422, "bad draft id")
        if draft is None:
            try:
                draft = self._agent().draft(draft_id)
            except AgentUnavailable as exc:
                raise ActionError(502, str(exc)) from None
        if draft is None:
            raise ActionError(404, "the agent has no such draft")
        if draft.get("status") not in (None, "draft"):
            raise ActionError(409, f"the draft is {draft.get('status')!s:.40}, not open")
        try:
            return self._prepare_mandate_from(draft, draft_id)
        except cv.VerificationError as exc:
            raise ActionError(422, str(exc)) from None

    def confirm_mandate(self, draft_id: str, prepare_id: str, *, notify: bool = True) -> dict[str, Any]:
        """Register exactly the reviewed mandate (``registerMandate``), then tell the agent."""
        entry = self._take(prepare_id, "mandate", "draftId", draft_id)
        prepared = entry["prepared"]
        if not prepared["confirmable"]:
            raise ActionError(409, "this mandate cannot be registered: " + "; ".join(prepared["problems"]))
        problems = cv.recheck_mandate(self.chain, prepared)
        if problems:
            raise ActionError(409, "the chain changed since you reviewed this card: " + "; ".join(problems))
        v = prepared["values"]
        fn = self.chain.vault.register_mandate(
            v["mandateId"],
            [int(x) for x in v["payeeAgentIds"]],
            v["payeeWallets"],
            int(v["maxPerTx"]),
            int(v["maxTotal"]),
            int(v["validUntil"]),
            bool(v["recurring"]),
            v["purposeHash"],
        )

        def after(tx_hash: str) -> dict[str, Any]:
            out: dict[str, Any] = {"status": "registered", "mandateId": v["mandateId"], "txHash": tx_hash}
            out["explorer"] = self.chain.explorer_tx(tx_hash)
            self._emit(
                "mandate",
                {
                    "message": "mandate registered by the owner",
                    "mandateId": v["mandateId"],
                    "draftId": draft_id,
                    "payees": [
                        {"agentId": a, "name": self.chain.directory.name_of(int(a)), "wallet": w}
                        for a, w in zip(v["payeeAgentIds"], v["payeeWallets"], strict=True)
                    ],
                    "maxPerTx": v["maxPerTx"],
                    "maxTotal": v["maxTotal"],
                    "validUntil": v["validUntil"],
                    "status": "registered",
                },
                tx_hash=tx_hash,
            )
            if notify:
                out["agent"] = self._notify(lambda a: a.mandate_activated(v["mandateId"], tx_hash, draft_id))
            return out

        return self._transact(fn, "registerMandate", after)

    def revoke_mandate(self, mandate_id: str) -> dict[str, Any]:
        """``revokeMandate(mandateId)`` with the owner key."""
        if not cv.is_hex32(mandate_id):
            raise ActionError(422, "the mandate id must be 0x + 64 hex")
        mid = mandate_id.lower()
        info = self.chain.vault.get_mandate(mid)
        if not info.exists:
            raise ActionError(404, "no such mandate in the wallet vault")
        if not info.active:
            raise ActionError(409, "the mandate is already revoked")
        fn = self.chain.vault.revoke_mandate(mid)

        def after(tx_hash: str) -> dict[str, Any]:
            self._emit(
                "mandate",
                {"message": "mandate revoked by the owner", "mandateId": mid, "status": "revoked"},
                tx_hash=tx_hash,
            )
            return {
                "status": "revoked",
                "mandateId": mid,
                "txHash": tx_hash,
                "explorer": self.chain.explorer_tx(tx_hash),
            }

        return self._transact(fn, "revokeMandate", after)

    # ------------------------------------------------------------------ approvals

    def _prepare_approval_from(self, order: Any, raw_invoice: Any) -> dict[str, Any]:
        prepared = cv.prepare_approval(self.chain, order, raw_invoice)
        return self._store(prepared, raw_invoice=raw_invoice)

    def prepare_approval(self, order: str, raw_invoice: Any = None) -> dict[str, Any]:
        """The verified approval card of ``order`` (the signed invoice is fetched from the agent)."""
        if not cv.is_hex32(order):
            raise ActionError(422, "the order id must be 0x + 64 hex")
        if raw_invoice is None:
            try:
                raw_invoice = self._agent().approval_invoice(order)
            except AgentUnavailable as exc:
                raise ActionError(502, str(exc)) from None
        if raw_invoice is None:
            raise ActionError(404, "the agent has no signed invoice for this order")
        try:
            return self._prepare_approval_from(order, raw_invoice)
        except cv.VerificationError as exc:
            raise ActionError(422, str(exc)) from None

    def approve(self, order: str, prepare_id: str, *, notify: bool = True) -> dict[str, Any]:
        """``ownerApprove`` with exactly the reviewed values, then ask the agent to execute."""
        if not cv.is_hex32(order):
            raise ActionError(422, "the order id must be 0x + 64 hex")
        oid = order.lower()
        entry = self._take(prepare_id, "approval", "orderId", oid)
        prepared = entry["prepared"]
        problems = cv.recheck_approval(self.chain, prepared, entry["raw_invoice"])
        if problems:
            raise ActionError(409, "the chain changed since you reviewed this card: " + "; ".join(problems))
        v = prepared["values"]
        if int(v["expiry"]) <= self.chain.now():
            raise ActionError(409, "the reviewed approval has expired: review it again")
        fn = self.chain.vault.owner_approve(
            v["orderId"],
            int(v["payeeAgentId"]),
            v["expectedPayee"],
            int(v["amount"]),
            int(v["expiry"]),
            bool(v["asEscrow"]),
            int(v["escrowDeadline"]),
        )

        def after(tx_hash: str) -> dict[str, Any]:
            self._emit(
                "approval",
                {
                    "message": "approved by the owner",
                    "orderId": oid,
                    "payee": {
                        "agentId": v["payeeAgentId"],
                        "name": self.chain.directory.name_of(int(v["payeeAgentId"])),
                    },
                    "amount": v["amount"],
                    "asEscrow": v["asEscrow"],
                    "status": "approved",
                },
                reason_codes=[2007],
                tx_hash=tx_hash,
            )
            out: dict[str, Any] = {
                "status": "approved",
                "orderId": oid,
                "txHash": tx_hash,
                "explorer": self.chain.explorer_tx(tx_hash),
            }
            if notify:
                out["agent"] = self._notify(lambda a: a.approval_executed(oid))
            return out

        return self._transact(fn, "ownerApprove", after)

    def reject(self, order: str) -> dict[str, Any]:
        """The owner rejects an approval request: no transaction, the agent closes the order."""
        if not cv.is_hex32(order):
            raise ActionError(422, "the order id must be 0x + 64 hex")
        oid = order.lower()
        with self._lock:
            for key in [k for k, v in self._prepared.items() if v["prepared"].get("orderId") == oid]:
                del self._prepared[key]
        result = self._notify(lambda a: a.approval_rejected(oid))
        self._emit("approval", {"message": "rejected by the owner", "orderId": oid, "status": "rejected"})
        return {"status": "rejected", "orderId": oid, "agent": result}

    # ------------------------------------------------------------------ tier actions

    def freeze(self) -> dict[str, Any]:
        """``freeze(3202, evidenceHash)``: the owner's circuit breaker."""
        from acb.canonical import chash

        oc = self.chain
        if oc.vault.is_frozen():
            raise ActionError(409, "the wallet is already frozen")
        evidence = chash(
            {
                "schema": "acb-owner-action/1",
                "action": "freeze",
                "vault": oc.vault.address,
                "at": oc.now(),
                "nonce": "0x" + secrets.token_hex(16),
            }
        )
        fn = oc.vault.freeze(FREEZE_REASON, evidence)

        def after(tx_hash: str) -> dict[str, Any]:
            self._emit(
                "tier", {"message": "frozen by the owner", "toTier": 255}, reason_codes=[FREEZE_REASON], tx_hash=tx_hash
            )
            return {"status": "frozen", "txHash": tx_hash, "explorer": oc.explorer_tx(tx_hash)}

        return self._transact(fn, "freeze", after)

    def unfreeze(self, to_tier: Any, rotate_to: Any = None) -> dict[str, Any]:
        """``unfreeze(toTier, 3300, newAgent)``: back to tier 0 or 1, optionally rotating the agent key."""
        from acb.chain.contracts import ZERO_ADDRESS

        if isinstance(to_tier, bool) or not isinstance(to_tier, int) or to_tier not in (0, 1):
            raise ActionError(422, "toTier must be 0 or 1 (the human reset restarts on probation)")
        oc = self.chain
        vault = oc.vault
        if not vault.is_frozen():
            raise ActionError(409, "the wallet is not frozen")
        new_agent = ZERO_ADDRESS
        if rotate_to not in (None, ""):
            if not cv.is_address(rotate_to):
                raise ActionError(422, "rotateTo must be an address (0x + 40 hex) or empty")
            new_agent = rotate_to.lower()
            roles = oc.roles()
            if new_agent == ZERO_ADDRESS:
                raise ActionError(422, "rotateTo must not be the zero address")
            if new_agent in (roles["owner"], roles["autonomy"], roles["vault"]):
                raise ActionError(
                    422, "the new agent key must differ from the owner, the autonomy operator and the vault"
                )
            if new_agent == roles["agent"]:
                raise ActionError(422, "that is the current agent key: leave rotateTo empty to keep it")
        fn = vault.unfreeze(int(to_tier), UNFREEZE_REASON, new_agent)

        def after(tx_hash: str) -> dict[str, Any]:
            data = {"message": "unfrozen by the owner", "toTier": int(to_tier)}
            if new_agent != ZERO_ADDRESS:
                data["newAgent"] = new_agent
            self._emit("tier", data, reason_codes=[UNFREEZE_REASON], tx_hash=tx_hash)
            return {
                "status": "unfrozen",
                "toTier": int(to_tier),
                "rotatedTo": None if new_agent == ZERO_ADDRESS else new_agent,
                "txHash": tx_hash,
                "explorer": oc.explorer_tx(tx_hash),
            }

        return self._transact(fn, "unfreeze", after)

    # ------------------------------------------------------------------ BAD_DATA report

    def bad_data(self, order: str) -> dict[str, Any]:
        """The owner confirms a BAD_DATA report: sign the payload hash (EIP-191); the signer files it."""
        from acb.canonical import chash, sign_digest_eip191
        from acb.signer.reports import bad_data_payload
        from acb.types import OrderState

        if not cv.is_hex32(order):
            raise ActionError(422, "the order id must be 0x + 64 hex")
        oid = order.lower()
        oc = self.chain
        info = oc.vault.get_order(oid)
        if info.state not in (OrderState.PAID, OrderState.RELEASED):
            raise ActionError(409, "only a paid or released order can be reported as bad data")
        subject = int(info.payee_agent_id)
        payload = bad_data_payload(subject, oc.wallet_agent_id, oid, oc.vault.address)
        digest = chash(payload)

        def after(signature: str) -> dict[str, Any]:
            result = self._notify(lambda a: a.bad_data(oid, signature))
            self._emit(
                "report",
                {
                    "message": "BAD_DATA confirmed by the owner",
                    "kind": "BAD_DATA",
                    "orderId": oid,
                    "subject": {"agentId": str(subject), "name": oc.directory.name_of(subject)},
                },
            )
            return {
                "status": "signed",
                "orderId": oid,
                "subjectAgentId": str(subject),
                "digest": digest,
                "agent": result,
            }

        if self.signing == "browser":
            action_id = "sig-" + secrets.token_hex(12)
            with self._lock:
                self._pending_tx[action_id] = {
                    "kind": "sign",
                    "digest": digest,
                    "after": after,
                    "created": self.clock(),
                }
            return {
                "status": "sign_in_browser",
                "actionId": action_id,
                "kind": "personal_sign",
                "digest": digest,
                "from": oc.vault.owner().lower(),
            }
        check = self.owner_check()
        if not check["isVaultOwner"]:
            raise ActionError(403, "this console's owner key is not the owner of the wallet vault")
        return after(sign_digest_eip191(self.owner_account, digest))

    # ------------------------------------------------------------------ agent notifications

    def _notify(self, call: Callable[[AgentClient], dict[str, Any]]) -> dict[str, Any]:
        try:
            answer = call(self._agent())
        except (AgentUnavailable, ActionError) as exc:
            message = exc.message if isinstance(exc, ActionError) else str(exc)
            self._emit("error", {"message": "the agent was not notified", "error": message})
            return {"notified": False, "error": message}
        return {"notified": True, "answer": answer}

    # ------------------------------------------------------------------ snapshot (all panel data)

    def snapshot(self, *, max_age_s: float = 1.5) -> dict[str, Any]:
        """``GET /api/snapshot``: every panel's data; one failing section never hides the others."""
        cached = self._snapshot
        if cached is not None and self.clock() - cached[0] < max_age_s:
            out = dict(cached[1])
            out["events"] = self.merger.recent(500) if self.merger is not None else []
            out["feeds"] = self.merger.status() if self.merger is not None else []
            return out
        agent_future = self._refresh_agent_view()
        errors: list[str] = []
        out: dict[str, Any] = {"signing": self.signing, "reasonTexts": cv.reason_catalogue()}
        try:
            oc = self.chain
            out["network"] = {
                "name": oc.net.name,
                "chainId": int(oc.dep.chain_id),
                "explorerTx": oc.explorer_template(),
                "vault": oc.vault.address,
            }
            out["suggestedRotation"] = oc.dep.roles.get("agentSignerNext")
        except ActionError as exc:
            errors.append(exc.message)
            oc = None
        if oc is not None:
            reader = cv.PanelReader(oc)
            for key, fn in (("wallet", reader.wallet), ("grades", reader.grades), ("autonomy", reader.autonomy)):
                try:
                    out[key] = fn()
                except Exception as exc:  # noqa: BLE001 - one failing panel must not hide the others
                    errors.append(f"{key}: {type(exc).__name__}")
            try:
                out["owner"] = self.owner_check()
            except ActionError as exc:
                errors.append(exc.message)
        try:
            # an agent that answered last time answers fast; a stopped one is not waited for again
            online = self._agent_cache[0].get("online") or self._agent_cache[1] == ["the agent was not read yet"]
            agent_future.result(timeout=AGENT_VIEW_WAIT_S if online else 0.2)
        except concurrent.futures.TimeoutError:
            pass
        view, agent_errors = self._agent_cache
        out["agent"] = view
        errors.extend(agent_errors)
        out["errors"] = errors
        self._snapshot = (self.clock(), out)
        result = dict(out)
        result["events"] = self.merger.recent(500) if self.merger is not None else []
        result["feeds"] = self.merger.status() if self.merger is not None else []
        return result

    def _refresh_agent_view(self) -> concurrent.futures.Future:
        """Start one background read of the agent's state (a stopped agent can take seconds to refuse a
        loopback connection on Windows); the snapshot uses the newest finished read."""
        with self._lock:
            if self._agent_future is None or self._agent_future.done():

                def run() -> None:
                    errors: list[str] = []
                    view = self._agent_view(errors)
                    self._agent_cache = (view, errors)

                self._agent_future = self._agent_pool.submit(run)
            return self._agent_future

    def _agent_view(self, errors: list[str]) -> dict[str, Any]:
        """The agent's LLM mode, pending approvals and owner messages (untrusted; shown as text)."""
        if self.agent is None:
            return {"online": False}
        try:
            state = self.agent.state()
        except AgentUnavailable as exc:
            errors.append(str(exc))
            return {"online": False}
        llm = state.get("llm") if isinstance(state.get("llm"), dict) else {}
        approvals = []
        for item in (state.get("approvals") or [])[:20]:
            if not isinstance(item, dict) or not cv.is_hex32(item.get("orderId")):
                continue
            codes = [int(c) for c in (item.get("reasons") or []) if isinstance(c, int) and not isinstance(c, bool)]
            approvals.append({"orderId": item["orderId"].lower(), "reasons": cv.reason_entries(codes[:4])})
        messages = []
        for m in (state.get("messages") or [])[-50:]:
            if isinstance(m, dict):
                sender = m.get("from") if isinstance(m.get("from"), dict) else {}
                messages.append(
                    {
                        "messageId": str(m.get("messageId") or "")[:40],
                        "from": {
                            "agentId": str(sender.get("agentId") or "")[:78],
                            "name": str(sender.get("name") or "")[:80],
                        },
                        "receivedAt": m.get("receivedAt") if isinstance(m.get("receivedAt"), int) else None,
                        "text": str(m.get("text") or "")[:2000],
                        "flagged": bool(m.get("flagged")),
                        "detectorRules": [str(r)[:10] for r in (m.get("detectorRules") or [])[:12]],
                    }
                )
        dialogue = state.get("dialogue") if isinstance(state.get("dialogue"), dict) else {}
        return {
            "online": True,
            "llm": {
                "mode": str(llm.get("mode") or "")[:40],
                "model": str(llm.get("model") or "")[:80],
                "persona": str(llm.get("persona") or "")[:40],
                "maskFlaggedSpans": bool(llm.get("maskFlaggedSpans", True)),
            },
            "dialogueState": str(dialogue.get("state") or "")[:40],
            "approvals": approvals,
            "messages": messages,
        }


__all__ = ["PREPARE_TTL_S", "ActionError", "OwnerConsole", "owner_account_for"]
