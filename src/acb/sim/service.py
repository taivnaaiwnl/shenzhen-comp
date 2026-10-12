"""The simulator service ``acb sim serve`` (process P6, 127.0.0.1:8704, spec 9.4).

| Endpoint | Auth | Purpose |
|---|---|---|
| ``GET /health`` | none | liveness and chain id (no directory: nothing about the personas) |
| ``POST /p/{key}/quote`` | none | signed invoice for a quote request |
| ``POST /p/{key}/deliver`` | none | claim (escrow) then the delivery receipt and payload, or pending |
| ``POST /admin/run {action, args}`` | ``SIM_TOKEN`` | scripted behaviours for the runner |
| ``GET /events?after=`` | ``EVENTS_TOKEN`` | event feed (10.3) |

There is no directory endpoint: the wallet's directory is pinned (8.3). Public bodies are capped at
16 KiB. Tokens are compared in constant time; a missing token configuration answers 503.
"""

from __future__ import annotations

import hmac
import json
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from acb.logbus import events_router
from acb.sim.core import MAX_BODY_BYTES, SimError, Simulator

ADMIN_MAX_BODY = 64 * 1024


def _check_token(authorization: str | None, env_name: str) -> None:
    expected = os.environ.get(env_name, "")
    if not expected:
        raise SimError(503, f"{env_name} is not configured")
    given = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    if not given or not hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")):
        raise SimError(401, "invalid or missing token")


def create_app(sim: Simulator) -> Any:
    """The FastAPI app of the simulator."""
    app = FastAPI(title="ACB counterparty simulator", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sim = sim

    @app.exception_handler(SimError)
    async def _sim_error(_request: Request, exc: SimError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.body)

    async def _json(request: Request, limit: int) -> Any:
        raw = await request.body()
        if len(raw) > limit:
            raise SimError(413, f"body larger than {limit} bytes")
        try:
            return json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            raise SimError(400, "body is not JSON") from None

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "sim", "chainId": sim.chain_id, "network": sim.net.name}

    @app.post("/p/{key}/quote")
    async def quote(key: str, request: Request) -> Any:
        body = await _json(request, MAX_BODY_BYTES)
        return await run_in_threadpool(sim.quote, key, body)

    @app.post("/p/{key}/deliver")
    async def deliver(key: str, request: Request) -> Any:
        body = await _json(request, MAX_BODY_BYTES)
        return await run_in_threadpool(sim.deliver, key, body)

    @app.post("/admin/run")
    async def admin_run(request: Request) -> Any:
        _check_token(request.headers.get("authorization"), "SIM_TOKEN")
        body = await _json(request, ADMIN_MAX_BODY)
        if not isinstance(body, dict) or not isinstance(body.get("action"), str):
            raise SimError(422, "body must be {action, args}")
        extra = set(body) - {"action", "args"}
        if extra:
            raise SimError(422, {"status": "invalid", "fields": sorted(extra)})
        args = body.get("args") or {}
        if not isinstance(args, dict):
            raise SimError(422, "args must be an object")
        return await run_in_threadpool(sim.run_action, body["action"], args)

    app.include_router(events_router(sim.bus, "EVENTS_TOKEN"))
    return app


__all__ = ["create_app"]
