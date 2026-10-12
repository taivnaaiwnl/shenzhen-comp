"""Autonomy operator service P5 (``acb autonomy serve``, 127.0.0.1:8705, spec 2.1, 5.4, 15.3).

| Method and path | Auth | Response |
|---|---|---|
| ``GET /health`` | none | ``{"ok": true, "network", "vault", "operator"}`` |
| ``POST /evaluate`` | ``AUTONOMY_TOKEN`` | ``AutonomyResult`` |
| ``GET /status`` | ``EVENTS_TOKEN`` | vault status, last plan and result, signer reachability |
| ``GET /events?after=`` | ``EVENTS_TOKEN`` | event envelopes (10.3) |

The process holds only ``AUTONOMY_PRIVATE_KEY`` (Anvil: the Anvil key ring) and reads the signer's
decision log with ``EVENTS_TOKEN``. It evaluates every ``poll_s`` seconds on its own and whenever the
runner calls ``POST /evaluate`` after a bureau rescore.
"""

from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from typing import Any

from acb.autonomy.evaluator import Evaluator, SignerDecisionLog
from acb.bureau.service import LOOPBACK, MAX_BODY_BYTES, check_bearer


def autonomy_account(network: str) -> Any:
    from acb.keys import KeyRing, load_role_key

    if network == "anvil":
        return KeyRing.anvil().account("autonomy")
    return load_role_key("autonomy")


def make_evaluator(network: str, *, with_key: bool = True, bus: Any | None = None, lookup=None) -> Evaluator:
    from acb.chain.client import make_web3
    from acb.chain.deployments import load_deployments
    from acb.config import load_network

    net = load_network(network)
    dep = load_deployments(network)
    return Evaluator(
        net,
        dep,
        w3=make_web3(net, "rpc"),
        lookup=lookup or SignerDecisionLog(),
        account=autonomy_account(network) if with_key else None,
        bus=bus,
    )


def create_app(evaluator: Evaluator, bus: Any, *, poll_s: float | None = None) -> Any:
    """The FastAPI app of P5. ``poll_s`` (default from config; 0 disables) runs the evaluation loop."""
    from fastapi import FastAPI, Header, HTTPException, Request
    from fastapi.responses import JSONResponse

    from acb.chain.tx import TxError
    from acb.logbus import events_router

    interval = evaluator.cfg.poll_s if poll_s is None else poll_s
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(_app):
        thread = None
        if interval and interval > 0:
            thread = threading.Thread(target=evaluator.run_forever, args=(stop, interval), daemon=True)
            thread.start()
        try:
            yield
        finally:
            stop.set()

    app = FastAPI(
        title="ACB autonomy operator (P5)", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )

    @app.middleware("http")
    async def limit_body(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        return await call_next(request)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "network": evaluator.net.name,
            "vault": evaluator.vault_address,
            "operator": evaluator.sender.address if evaluator.sender else None,
        }

    @app.post("/evaluate")
    def evaluate(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        check_bearer(authorization, "AUTONOMY_TOKEN")
        try:
            result = evaluator.evaluate()
        except TxError as exc:
            raise HTTPException(status_code=502, detail=f"transaction failed: {exc}") from None
        return result.to_wire()

    @app.get("/status")
    def status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        check_bearer(authorization, "EVENTS_TOKEN")
        st = evaluator.vault.status()
        plan = evaluator.last_plan
        return {
            "vault": evaluator.vault_address,
            "status": {
                "tier": st.tier,
                "frozen": st.frozen,
                "tierBeforeFreeze": st.tier_before_freeze,
                "maxAutoTier": st.max_auto_tier,
                "clean": st.clean,
                "points": st.points,
                "lastTierChangeAt": st.last_tier_change_at,
            },
            "lastPlan": None
            if plan is None
            else {"rule": plan.rule, "action": plan.action, "toTier": plan.to_tier, "detail": plan.detail},
            "lastResult": evaluator.last_result.to_wire() if evaluator.last_result else None,
            "signerUnreachableStreak": evaluator.unreachable_streak,
        }

    app.include_router(events_router(bus, "EVENTS_TOKEN"))
    return app


def serve(network: str, *, host: str = LOOPBACK, port: int | None = None) -> int:
    """Run P5 until interrupted. Refuses any interface other than 127.0.0.1."""
    import uvicorn

    from acb.config import load_network
    from acb.logbus import EventBus

    if host != LOOPBACK:
        raise SystemExit("acb autonomy serve binds to 127.0.0.1 only")
    port = int(port or os.environ.get("AUTONOMY_PORT") or 8705)
    net = load_network(network)
    explorer = net.explorers[0].get("tx") if net.explorers else None
    bus = EventBus("autonomy", chain_id=net.chain_id, explorer_tx=explorer)
    evaluator = make_evaluator(network, bus=bus)
    app = create_app(evaluator, bus)
    bus.emit("info", {"started": "autonomy", "network": network, "port": port, "vault": evaluator.vault_address})
    uvicorn.run(app, host=LOOPBACK, port=port, log_level="warning", workers=1)
    return 0


__all__ = ["autonomy_account", "create_app", "make_evaluator", "serve"]
