"""Shared machinery of the attack suite and the benign suite (spec 12.3, 12.4).

A :class:`Harness` builds a live system the way the recorded run does (the runner's steps S0-S10: deploy,
market bootstrap, owner dialogue, mandate M1, a few payments, promotion to tier 2), then replaces the
simulator service by an in-process :class:`Market`: the same simulator code (:class:`acb.sim.core.Simulator`)
served on the simulator's port, with hooks that let a test answer a quote or a delivery with an invoice
or payload of its own (signed with the persona's real key). Everything on the wallet side runs exactly as
in the recorded run: the wallet agent P2, the signer P3, the bureau P4, the autonomy operator P5 as
separate processes with their own environments. The harness also registers a larger suite mandate MA
through the owner dialogue (owner console code, on-chain ``registerMandate``).

The harness observes, it never decides on the system's behalf: balances and vault events from the chain,
decisions and reports from the services' event logs, scores from a read-only bureau.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body

from acb import config
from acb.canonical import chash
from acb.scenario.env import Env, EnvError
from acb.scenario.runner import Runner, StepCtx, load_scenario_config

SUITE_MANDATE_MESSAGE = (
    "Suite mandate: pay Atlas Data, Slowpoke Analytics, Nova Labs, Orion API and Ghost Feeds "
    "up to 2.00 USDC per payment and 9.00 USDC in total, valid for 168 hours"
)


UNREACHABLE_PORT = 9
"""TCP discard port: connections to it are refused on this machine, so the "provider" is down."""


class HarnessError(RuntimeError):
    """The suite environment could not be prepared."""


# ---------------------------------------------------------------------------
# the in-process market (simulator with hooks)
# ---------------------------------------------------------------------------


QuoteHook = Callable[["Market", str, dict[str, Any]], dict[str, Any] | None]
DeliverHook = Callable[["Market", str, dict[str, Any]], dict[str, Any] | None]


class Market:
    """The simulator, in process, on the simulator's port; per-persona quote and delivery hooks."""

    def __init__(self, env: Env):
        from acb.keys import KeyRing
        from acb.sim.core import Simulator

        self.env = env
        self.sim = Simulator(env.net, env.dep, KeyRing.anvil())
        self.quote_hooks: dict[str, QuoteHook] = {}
        self.deliver_hooks: dict[str, DeliverHook] = {}
        self.requests: list[dict[str, Any]] = []
        self._server: Any = None
        self._thread: threading.Thread | None = None

    # ---- the app ----------------------------------------------------------------------------

    def app(self) -> Any:
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse

        from acb.sim.core import SimError

        app = FastAPI(title="ACB suite market", docs_url=None, redoc_url=None, openapi_url=None)
        market = self

        def answer(entry: dict[str, Any], call: Callable[[], dict[str, Any]]) -> JSONResponse:
            try:
                return JSONResponse(call())
            except SimError as exc:
                return JSONResponse(exc.body, status_code=exc.status)
            finally:
                entry["done"] = time.time()

        @app.get("/health")
        def health() -> dict[str, Any]:
            return {"ok": True, "service": "sim", "chainId": market.sim.chain_id, "network": market.sim.net.name}

        # Plain (sync) handlers: FastAPI runs them in its worker threads.
        @app.post("/p/{key}/quote")
        def quote(key: str, body: Annotated[Any, Body()] = None) -> Any:
            entry = {"path": f"/p/{key}/quote", "body": body, "at": time.time()}
            market.requests.append(entry)
            hook = market.quote_hooks.get(key)

            def run() -> dict[str, Any]:
                if hook is not None and isinstance(body, dict):
                    out = hook(market, key, body)
                    if out is not None:
                        return out
                return market.sim.quote(key, body)

            return answer(entry, run)

        @app.post("/p/{key}/deliver")
        def deliver(key: str, body: Annotated[Any, Body()] = None) -> Any:
            entry = {"path": f"/p/{key}/deliver", "body": body, "at": time.time()}
            market.requests.append(entry)
            hook = market.deliver_hooks.get(key)

            def run() -> dict[str, Any]:
                if hook is not None and isinstance(body, dict):
                    out = hook(market, key, body)
                    if out is not None:
                        return out
                return market.sim.deliver(key, body)

            return answer(entry, run)

        return app

    def start(self) -> None:
        import uvicorn

        cfg = uvicorn.Config(self.app(), host="127.0.0.1", port=int(self.env.ports["sim"]), log_level="warning")
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, name="suite-market", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self._server.started:
                return
            time.sleep(0.1)
        raise HarnessError("the suite market did not start")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)

    # ---- actions and helpers ------------------------------------------------------------------

    def run(self, action: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """A simulator admin action, in process (same code as ``POST /admin/run``)."""
        return self.sim.run_action(action, dict(args or {}))

    def issue(
        self, key: str, body: Mapping[str, Any], *, memo: str | None = None, quantity: int | None = None
    ) -> dict[str, Any]:
        """A real, solicited invoice of ``key`` for a quote body, with an optional memo of the test's."""
        party = self.sim.party(key)
        spec = self.sim.persona(key)
        unit, description = self.sim.catalogue(key)[body["sku"]]
        signed, _ = self.sim.issue_invoice(
            party,
            buyer_vault=body["buyerVault"],
            buyer_agent_id=int(body["buyerAgentId"]),
            lines=[(body["sku"], description, unit, int(quantity or body["quantity"]))],
            quote_request_id=body["quoteRequestId"],
            cap_of=body.get("capOf", ""),
            memo=spec.memo if memo is None else memo,
        )
        return signed

    def forge(
        self, key: str, body: Mapping[str, Any], mutate: Callable[[dict[str, Any]], None], *, signer: Any = None
    ) -> dict[str, Any]:
        """An invoice of ``key`` changed by ``mutate`` before signing (``signer``: another key, e.g. a spoof)."""
        from acb.sim.invoices import build_invoice, invoice_items, sign_wire_invoice

        party = self.sim.party(key)
        unit, description = self.sim.catalogue(key)[body["sku"]]
        now = self.sim.now()
        wire = build_invoice(
            invoice_id=f"inv-{key}-x{int(time.time() * 1000) % 10**9}",
            quote_request_id=body["quoteRequestId"],
            cap_of=body.get("capOf", ""),
            issuer_agent_id=party.agent_id,
            buyer_agent_id=int(body["buyerAgentId"]),
            buyer_vault=str(body["buyerVault"]).lower(),
            chain_id=self.sim.chain_id,
            token=self.sim.usdc,
            items=invoice_items([(body["sku"], description, unit, int(body["quantity"]))]),
            issued_at=now,
            expires_at=now + 600,
            delivery_deadline=now + 120,
            memo=self.sim.persona(key).memo,
        )
        wire.pop("signature", None)
        mutate(wire)
        signed, _ = sign_wire_invoice(signer or party.account, wire)
        return signed


# ---------------------------------------------------------------------------
# the harness
# ---------------------------------------------------------------------------


@dataclass
class Harness:
    """A prepared system for the suites (see the module docstring)."""

    env: Env
    runner: Runner
    market: Market | None = None
    facts: dict[str, Any] = field(default_factory=dict)
    _bureau: Any = None

    @classmethod
    def build(
        cls,
        *,
        run_id: str | None = None,
        echo: Callable[[str], None] | None = None,
        baseline_to: str = "S10",
        fixed_ports: bool = False,
    ) -> Harness:
        env = Env.create("anvil", run_id=run_id, llm_mode="mock", fixed_ports=fixed_ports, with_owner_ui=True)
        # A provider endpoint that refuses every connection (A32: the LLM is down). The agent starts with
        # MockLLM; A32 switches to this "openai" client, whose failures make the agent fall back to its
        # rule-based parser. No request leaves the machine and no real key is used.
        env.source_env.update(
            {
                "LLM_BASE_URL": f"http://127.0.0.1:{UNREACHABLE_PORT}/v1",
                "LLM_API_KEY": "suite-unreachable-provider",
                "LLM_MODEL": "suite-unreachable",
                "LLM_TIMEOUT_S": "3",
            }
        )
        env.source_env.pop("OPENAI_API_KEY", None)
        runner = Runner(env, echo=echo or (lambda _s: None))
        h = cls(env=env, runner=runner)
        try:
            if not runner.run("S0", baseline_to):
                failed = {k: v.get("failures") for k, v in runner.state["steps"].items() if v.get("status") == "FAILED"}
                raise HarnessError(f"baseline failed: {failed}")
            env.stop_service("sim")
            h.market = Market(env)
            h.market.start()
            h.market.run("set_override", {"persona": "orion", "mode": "honest"})
            h.register_suite_mandate()
        except BaseException:
            h.close()
            raise
        return h

    def close(self) -> None:
        if self.market is not None:
            self.market.stop()
        self.env.stop()

    # ---- mandates ---------------------------------------------------------------------------

    def register_suite_mandate(self, message: str = SUITE_MANDATE_MESSAGE) -> str:
        owner = self.env.owner()
        answer = owner.chat(message)
        card = next((c for c in answer.get("cards") or [] if c.get("type") == "mandate"), None)
        if card is None:
            raise HarnessError(f"the suite mandate produced no draft: {answer.get('reply')!r}")
        fresh = owner.prepare_mandate(card["draftId"])
        result = owner.confirm_mandate(card["draftId"], fresh["prepareId"])
        self.facts["mandateMA"] = result["mandateId"]
        return result["mandateId"]

    @property
    def mandate(self) -> str:
        return self.facts["mandateMA"]

    # ---- shortcuts --------------------------------------------------------------------------

    def agent_id(self, key: str) -> int:
        return int(self.env.dep.agents[key].agent_id)

    def vault(self, name: str = "wallet") -> Any:
        from acb.chain.contracts import VaultContract

        return VaultContract(self.env.w3, self.env.dep.vaults[name].address)

    def contracts(self) -> Any:
        return self.env.contracts()

    def purchase(self, payee: str, sku: str, quantity: int, *, mandate: str | None = None) -> list[dict[str, Any]]:
        body = [{"payee_agent_id": self.agent_id(payee), "sku": sku, "quantity": int(quantity)}]
        answer = self.env.purchase(mandate or self.mandate, body)
        if not isinstance(answer.body, dict):
            raise EnvError(f"purchase answered {answer.status}: {answer.body}")
        return list(answer.body.get("results") or [])

    def wallet_status(self) -> dict[str, Any]:
        from acb.chain.contracts import Erc20Contract

        v = self.vault()
        st = v.status()
        usdc = Erc20Contract(self.env.w3, self.env.dep.contracts["USDC"].address)
        return {
            "tier": int(st.tier),
            "frozen": bool(st.frozen),
            "tierBeforeFreeze": int(st.tier_before_freeze),
            "maxAutoTier": int(st.max_auto_tier),
            "clean": int(st.clean),
            "points": int(st.points),
            "balance": int(usdc.balance_of(self.env.dep.vaults["wallet"].address)),
            "escrowLocked": int(v.escrow_locked()),
        }

    def block(self) -> int:
        return int(self.env.w3.eth.block_number)

    def outflows(self, from_block: int, to_block: int | None = None, vault: str = "wallet") -> list[dict[str, Any]]:
        """``Paid`` and ``EscrowOpened`` of a vault in a block range: payee, amount, sender, tx."""
        v = self.vault(vault)
        out = []
        for event in ("Paid", "EscrowOpened"):
            for lg in v.logs(event, from_block, to_block if to_block is not None else "latest"):
                tx = self.env.w3.eth.get_transaction(lg["transactionHash"])
                out.append(
                    {
                        "event": event,
                        "orderId": "0x" + bytes(lg["args"]["orderId"]).hex(),
                        "payeeAgentId": int(lg["args"]["payeeAgentId"]),
                        "payee": str(lg["args"]["payee"]).lower(),
                        "amount": int(lg["args"]["amount"]),
                        "from": str(tx["from"]).lower(),
                        "tx": "0x" + bytes(lg["transactionHash"]).hex(),
                    }
                )
        return out

    def outside_intent(self, flows: list[dict[str, Any]], *, allowed: set[int] | None = None) -> int:
        """USDC that left the wallet to a payee outside every active owner mandate (or not to its pinned
        wallet). The vault makes this 0 unless a defence failed."""
        mandates = self.active_mandate_payees()
        lost = 0
        for f in flows:
            pinned = mandates.get(f["payeeAgentId"])
            if allowed is not None and f["payeeAgentId"] not in allowed or pinned is None or pinned != f["payee"]:
                lost += f["amount"]
        return lost

    def active_mandate_payees(self) -> dict[int, str]:
        v = self.vault()
        out: dict[int, str] = {}
        for lg in v.logs("MandateRegistered", self.env.dep.start_block, "latest"):
            mid = "0x" + bytes(lg["args"]["mandateId"]).hex()
            if not v.get_mandate(mid).active:
                continue
            for aid, wallet in zip(lg["args"]["payeeAgentIds"], lg["args"]["payeeWallets"], strict=True):
                out[int(aid)] = str(wallet).lower()
        return out

    def agent_events(self, mark: int, kind: str | None = None) -> list[dict[str, Any]]:
        log = self.runner.logs["agent"]
        events = log.since(mark)
        return [e for e in events if kind is None or e.get("type") == kind]

    def mark(self, name: str = "agent") -> int:
        return self.runner.logs[name].mark()

    def wait(self, what: str, check: Callable[[], Any], timeout: float = 90.0) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(1.0)
        raise TimeoutError(f"timed out waiting for {what}")

    def violation_pause(self) -> None:
        """Let the signer's repeated-violation window (10 minutes, chain clock) pass between attacks that
        each record a violation, so every attack is classified on its own."""
        cfg = config.load_yaml(config.REPO_ROOT / "config" / "policy.yaml")
        window = int(((cfg.get("signer") or {}).get("repeated_violations") or {}).get("window_s", 600))
        self.env.warp(window + 1)

    def owner_reset(self, to_tier: int = 1) -> dict[str, Any]:
        """The owner's human reset after a containment (``unfreeze(toTier, 3300, 0)``, no key rotation)."""
        return self.env.owner().unfreeze(int(to_tier), None)

    # ---- scores (read-only bureau) ----------------------------------------------------------

    def score(self, key_or_id: Any) -> dict[str, Any]:
        """The scorecard's current grade and score of an agent at the latest block (nothing is posted)."""
        from acb.bureau.poster import Bureau

        if self._bureau is None:
            # read-only, bound to this run's deployment object (never re-read from deployments/anvil.json)
            self._bureau = Bureau(self.env.net, self.env.dep, w3=self.env.w3, account=None, out_dir=self.env.out_dir)
        bureau = self._bureau
        latest = self.block()
        snap = bureau.snapshot(latest)
        aid = self.agent_id(key_or_id) if isinstance(key_or_id, str) and not key_or_id.isdigit() else int(key_or_id)
        bundle = bureau.model.score(snap.inputs_for(aid), bureau.cfg, snap.facts)
        data = bundle.as_canonical()
        return {
            "grade": data["grade"],
            "score": int(data["score"]),
            "reasons": [r for r in data["reasons"] if r],
            "bundle": data,
        }

    def rescore(self, rounds: int = 1) -> dict[str, Any]:
        return self.env.rescore(rounds)

    def effective_grade(self, key: str) -> str:
        from acb.chain.contracts import AcbContract

        acb = AcbContract(self.env.w3, self.env.dep.contracts["AgentCreditBureau"].address)
        return acb.effective_grade(self.agent_id(key), 0).name

    # ---- keys -------------------------------------------------------------------------------

    def stolen(self) -> Any:
        """The agent signer key held outside the signer (stolen-key attacks)."""
        return self.env.sender("agent_signer_current", self.current_agent_account())

    def current_agent_account(self) -> Any:
        agent = self.vault().agent().lower()
        for role in ("agent_signer", "agent_signer_next"):
            if self.env.keys.address(role).lower() == agent:
                return self.env.keys.account(role)
        raise HarnessError("the vault's agent key is not in the key ring")

    def sender(self, role: str) -> Any:
        return self.env.sender(role)

    def fresh_order(self, label: str) -> str:
        return chash({"schema": "acb-suite/1", "label": label, "run": self.env.run_id, "at": time.monotonic_ns()})

    def outflow(
        self,
        order_id: str,
        payee: str,
        amount: int,
        *,
        mandate: str | None = None,
        decision: int = 1,
        reason: int = 2000,
    ) -> Any:
        from acb.chain.contracts import OutflowParams

        return OutflowParams(
            order_id=order_id,
            payee_agent_id=self.agent_id(payee) if isinstance(payee, str) else int(payee),
            amount=int(amount),
            deadline=self.env.now() + 600,
            mandate_id=mandate or self.mandate,
            invoice_hash=chash({"schema": "acb-suite-invoice/1", "order": order_id}),
            decision_hash=chash({"schema": "acb-suite-decision/1", "order": order_id}),
            decision=decision,
            reason_code=reason,
        )


def step_ctx(sid: str) -> StepCtx:
    """A detached step context (for helpers shared with the runner's steps)."""
    return StepCtx(id=sid, title=sid, expect={})


def suite_cfg() -> dict[str, Any]:
    return load_scenario_config()


def write_report(path: Path, report: Mapping[str, Any]) -> Path:
    from acb.canonical import atomic_write_bytes

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, (json.dumps(report, indent=2, ensure_ascii=True) + "\n").encode("utf-8"))
    return path


__all__ = ["SUITE_MANDATE_MESSAGE", "Harness", "HarnessError", "Market", "step_ctx", "suite_cfg", "write_report"]
