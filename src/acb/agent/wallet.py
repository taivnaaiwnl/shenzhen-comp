"""The wallet agent (process P2): one object that owns the dialogue, the purchase runtime and their state.

:class:`WalletAgent` wires together the pinned :class:`~acb.agent.directory.Directory`, read-only chain
access (:class:`~acb.agent.chain.AgentChain`), the persistent :class:`~acb.agent.store.AgentStore`, the
signer and provider clients, the LLM (switchable by the runner), the redactor and the event bus. The
owner dialogue lives in :mod:`acb.agent.dialogue`, invoices, executions, deliveries and reports in
:mod:`acb.agent.runtime`; this module holds what both need. One lock serialises every turn and
purchase step (the signer has one transaction in flight anyway).

The process holds **no private keys**: it refuses to start, and refuses every LLM call, while a
``*_PRIVATE_KEY`` or ``*_MNEMONIC`` variable is visible (:func:`acb.agent.llm.guard_keys`).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from acb import reason_codes
from acb.agent.chain import AgentChain
from acb.agent.config import AgentConfig
from acb.agent.directory import Directory, DirectoryEntry, candidate
from acb.agent.llm import guard_keys, used_fallback
from acb.agent.quotes import ProviderClient, QuoteBook
from acb.agent.redact import Redactor
from acb.agent.signer_client import SignerAPI, SignerUnavailable, new_request_id
from acb.agent.store import AgentStore
from acb.agent.tools import usdc_floor_text, usdc_text
from acb.logbus import EventBus
from acb.policy import PolicyConfig
from acb.types import (
    TIER_FROZEN,
    Deployments,
    ExecuteRequest,
    ExecuteResponse,
    Grade,
    LLMResult,
    MandateView,
    PayeeCandidate,
    ToolSpec,
    VaultState,
    ViolationReport,
)

TIER_NAMES = {0: "Maker-checker", 1: "Small payments", 2: "Standard", 3: "Extended", TIER_FROZEN: "Frozen"}


class WalletAgent:
    """See the module docstring."""

    def __init__(
        self,
        *,
        dep: Deployments,
        chain: AgentChain,
        signer: SignerAPI,
        provider: ProviderClient,
        llm: Any,
        llm_info: dict[str, Any] | None = None,
        store: AgentStore | None = None,
        cfg: AgentConfig | None = None,
        pcfg: PolicyConfig | None = None,
        bus: EventBus | None = None,
        redactor: Redactor | None = None,
        out_dir: str | Path | None = None,
        network: str = "anvil",
        explorer_tx: str | None = None,
    ):
        from acb import config as acb_config
        from acb.agent.dialogue import Dialogue
        from acb.agent.runtime import Runtime

        self.dep = dep
        self.chain = chain
        self.signer = signer
        self.provider = provider
        self.cfg = cfg or AgentConfig.load()
        self.pcfg = pcfg or PolicyConfig.load()
        self.network = network
        self.directory = Directory.from_deployments(dep, max_edit_distance=self.cfg.lookalike.max_edit_distance)
        self.store = store or AgentStore(None)
        self.quotes = QuoteBook(self.store, self.pcfg)
        self.out_dir = Path(out_dir) if out_dir is not None else acb_config.out_dir()
        self.bus = bus or EventBus("agent", chain_id=int(dep.chain_id), out_dir=self.out_dir, explorer_tx=explorer_tx)
        self.explorer_tx = explorer_tx
        self.redactor = redactor or Redactor.from_env(
            bip39_min_words=self.cfg.redactor.bip39_min_words, key_prefixes=tuple(self.cfg.redactor.key_prefixes)
        )
        self.llm = llm
        self.llm_info: dict[str, Any] = dict(
            llm_info or {"mode": getattr(llm, "name", "llm"), "model": getattr(llm, "model", "")}
        )
        self.mask_flagged_spans = self.cfg.quarantine.mask_flagged_spans
        self.lock = threading.RLock()
        self.dialogue = Dialogue(self)
        self.runtime = Runtime(self)
        self.runtime.recover()

    # ------------------------------------------------------------------ identity

    @property
    def vault_address(self) -> str:
        return self.chain.vault_address

    @property
    def agent_id(self) -> int:
        return int(self.chain.vault_agent_id)

    # ------------------------------------------------------------------ LLM

    def complete(self, messages: list[dict], tools: list[ToolSpec]) -> tuple[LLMResult, bool]:
        """One LLM call (the client checks the environment for keys first); returns ``(result, fallback)``."""
        guard_keys()  # also here, whatever client was switched in
        result = self.llm.complete(messages, tools)
        return result, used_fallback(self.llm)

    def llm_view(self, fallback: bool = False) -> dict[str, Any]:
        """``ChatResponse.llm``: ``{"mode", "model", "fallback"}`` (plus persona and masking)."""
        return {
            "mode": self.llm_info.get("mode"),
            "model": self.llm_info.get("model"),
            "fallback": bool(fallback),
            "persona": self.llm_info.get("persona"),
            "maskFlaggedSpans": self.mask_flagged_spans,
        }

    def switch_llm(self, llm: Any, info: dict[str, Any], mask_flagged_spans: bool | None = None) -> None:
        with self.lock:
            self.llm = llm
            self.llm_info = dict(info)
            if mask_flagged_spans is not None:
                self.mask_flagged_spans = bool(mask_flagged_spans)

    # ------------------------------------------------------------------ signer

    def execute(self, req: ExecuteRequest) -> ExecuteResponse | None:
        """Send one request to the signer; None (and an ``error`` event) when it cannot be reached."""
        try:
            return self.signer.execute(req)
        except SignerUnavailable as exc:
            self.bus.emit("error", {"message": "signer unavailable", "requestId": req.request_id, "error": str(exc)})
            return None

    def report_violation(
        self,
        *,
        tool_name: str,
        raw_args: dict,
        raw_text: str | None,
        secret_match: str | None,
        turn: str,
    ) -> ExecuteResponse | None:
        """``REPORT_VIOLATION`` (spec 7.3): the signer classifies the call itself and records it on-chain."""
        report = ViolationReport(
            tool_name=str(tool_name)[:200],
            raw_args=jsonable(raw_args),
            raw_text=(raw_text[:2000] if isinstance(raw_text, str) else None),
            secret_match=secret_match,  # type: ignore[arg-type]
            turn=turn,  # type: ignore[arg-type]
        )
        req = ExecuteRequest(request_id=new_request_id(), kind="REPORT_VIOLATION", violation_report=report)
        resp = self.execute(req)
        self.bus.emit(
            "violation",
            {
                "source": "agent",
                "tool": report.tool_name[:80],
                "turn": turn,
                "secretMatch": secret_match,
                "status": resp.status if resp is not None else "signer_unavailable",
                "kind": resp.violation.kind.name if resp is not None and resp.violation is not None else None,
                "severity": resp.violation.severity if resp is not None and resp.violation is not None else None,
            },
            reason_codes=resp.reason_codes if resp is not None else (),
            tx_hash=(resp.violation.tx_hash if resp is not None and resp.violation is not None else None),
        )
        return resp

    def report_extra_calls(self, calls: Any, turn: str, *, sku_enum: Any = ()) -> bool:
        """Further tool calls of one LLM result (spec 8.1 allows exactly one per turn).

        Every call outside the turn's tool set, or carrying a secret, is reported (``REPORT_VIOLATION``);
        allowed extra calls are ignored. Returns True when at least one call was reported, so the caller
        executes nothing of that turn.
        """
        from acb.agent.redact import strongest
        from acb.agent.tools import parse_call

        reported = False
        for call in list(calls or ())[:8]:
            args, kinds = self.redactor.scan_value(call.arguments)
            secret = strongest(tuple(kinds))
            parsed = parse_call(turn, call.model_copy(update={"arguments": args}), sku_enum=sku_enum)  # type: ignore[arg-type]
            if secret is not None or parsed.status == "not_allowed":
                self.report_violation(tool_name=call.name, raw_args=args, raw_text=None, secret_match=secret, turn=turn)
                reported = True
        return reported

    # ------------------------------------------------------------------ chain views for the LLM and cards

    def candidate(self, entry: DirectoryEntry, block: int | None = None) -> PayeeCandidate:
        info = self.chain.grade_info(entry.agent_id, block)
        return candidate(
            entry,
            grade=info.grade,
            score=info.score,
            live_wallet=self.chain.live_wallet(entry.agent_id, block),
            registered_at=self.chain.registered_at(entry.agent_id),
            lookalike_of=self.directory.lookalike_of(entry.agent_id),
        )

    def payee_candidate(self, agent_id: int, block: int | None = None) -> PayeeCandidate:
        entry = self.directory.get(agent_id)
        if entry is None:
            return PayeeCandidate(
                agent_id=int(agent_id),
                name=f"agent #{int(agent_id)}",
                grade=Grade.NONE,
                score=None,
                live_wallet=None,
                registered_at=None,
                lookalike_of=None,
            )
        return self.candidate(entry, block)

    def status_context(self, vault: VaultState) -> dict[str, Any]:
        free = max(0, int(vault.balance) - int(vault.escrow_locked) - int(vault.reserve_floor))
        cfg = vault.config
        return {
            "tier": int(vault.tier),
            "tierName": TIER_NAMES.get(int(vault.tier), str(vault.tier)),
            "frozen": bool(vault.frozen),
            "balance_usdc": usdc_text(int(vault.balance)),
            "reserve_floor_usdc": usdc_text(int(vault.reserve_floor)),
            "escrow_locked_usdc": usdc_text(int(vault.escrow_locked)),
            "free_balance_usdc": usdc_floor_text(free),
            "per_tx_usdc": usdc_text(int(cfg.per_tx)),
            "window_cap_usdc": usdc_text(int(cfg.window_cap)),
            "new_payee_cap_usdc": usdc_text(int(cfg.new_payee_cap)),
            "window_spent_usdc": usdc_text(int(vault.window_spent)),
            "window_count": int(vault.window_count),
            "max_tx_per_window": int(cfg.max_tx),
        }

    def verified_mandates(self, block: int | None = None) -> list[MandateView]:
        """Active, unexpired mandates of our vault that the agent verified after ``/mandates/activated``."""
        verified = self.store.read("mandates")
        out = []
        now = self.chain.now(block)
        for mid, rec in verified.items():
            if rec.get("status") != "active":
                continue
            view = self.chain.mandate(mid, block)
            if view is not None and view.active and now <= view.valid_until:
                out.append(view)
        return out

    def mandate_context(self, m: MandateView, block: int | None = None) -> dict[str, Any]:
        payees = []
        for aid in m.payee_agent_ids:
            entry = self.directory.get(aid)
            grade = self.chain.grade_info(aid, block).grade
            payees.append(
                {
                    "agentId": int(aid),
                    "name": entry.name if entry else f"agent #{int(aid)}",
                    "grade": Grade(grade).name,
                    "skus": sorted(entry.skus) if entry else [],
                }
            )
        return {
            "mandateId": m.mandate_id,
            "payees": payees,
            "max_per_tx_usdc": usdc_text(int(m.max_per_tx)),
            "max_total_usdc": usdc_text(int(m.max_total)),
            "spent_usdc": usdc_text(int(m.spent)),
            "remaining_usdc": usdc_text(max(0, int(m.max_total) - int(m.spent))),
            "valid_until": int(m.valid_until),
            "recurring": bool(m.recurring),
        }

    def directory_context(self) -> list[dict[str, Any]]:
        return [
            {"agentId": e.agent_id, "key": e.key, "name": e.name, "category": e.category, "skus": sorted(e.skus)}
            for e in self.directory.providers
        ]

    def reason_texts(self, codes: tuple[int, ...] | list[int]) -> tuple[str, ...]:
        out = []
        for c in codes:
            try:
                out.append(f"{int(c)}: {reason_codes.text(int(c))}")
            except KeyError:
                out.append(f"{int(c)}: (unknown code)")
        return tuple(out)

    def explorer(self, tx_hash: str | None) -> str | None:
        if not tx_hash:
            return None
        return self.explorer_tx.format(hash=tx_hash) if self.explorer_tx else tx_hash


def jsonable(value: Any) -> Any:
    """A JSON-safe copy of LLM arguments (floats become strings; anything odd becomes its ``repr``)."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value if abs(value) <= 2**53 else str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return repr(value)


__all__ = ["TIER_NAMES", "WalletAgent", "jsonable"]
