"""Signer service P3 (spec 7.1, 7.2): FastAPI bound to 127.0.0.1 only.

========================================  ===============  ==========================================
Method and path                           Auth             Response
========================================  ===============  ==========================================
``GET /health``                           none             ``{"ok", "address", "chainId", "vault", "nonce"}``
``POST /v1/execute``                      ``SIGNER_TOKEN``  ``ExecuteResponse`` (camelCase JSON)
``GET /v1/requests/{request_id}``         ``SIGNER_TOKEN``  the stored ``ExecuteResponse`` or 404
``GET /v1/decisions/{decision_hash}``     ``EVENTS_TOKEN``  ``{"found": true, ...}`` or 404
``GET /events?after=<seq>``               ``EVENTS_TOKEN``  event envelopes (10.3)
========================================  ===============  ==========================================

Tokens are compared in constant time; a missing or wrong token is 401 and an ``error`` event
``signer.auth_fail`` (the token itself is never logged). Request bodies are capped at 64 KiB (413). An
unknown ``kind`` or a malformed body is 422 with reason 4104. One worker; the executor serialises
executions (one transaction in flight).
"""

from __future__ import annotations

import asyncio
import hmac
import os
import time
from typing import Any

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from acb.signer.executor import Executor
from acb.signer.state import valid_request_id
from acb.types import ExecuteRequest

MAX_BODY_BYTES = 64 * 1024
LOOPBACK = "127.0.0.1"
CODE_REJECT_AUTH = 4100
CODE_NOT_ALLOWED = 4104


def token_ok(authorization: str | None, expected: str) -> bool:
    """Constant-time ``Bearer`` check; an unset expected token never matches."""
    if not expected:
        return False
    given = ""
    if authorization and authorization[:7].lower() == "bearer ":
        given = authorization[7:].strip()
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")) and bool(given)


def create_app(executor: Executor, *, environ: dict[str, str] | None = None) -> Any:
    """The FastAPI app of the signer. Tokens are read from ``environ`` (default ``os.environ``)."""
    env = os.environ if environ is None else environ
    app = FastAPI(title="ACB signer", docs_url=None, redoc_url=None, openapi_url=None)
    bus = executor.bus

    def _auth(authorization: str | None, token_env: str, path: str) -> JSONResponse | None:
        if token_ok(authorization, env.get(token_env, "")):
            return None
        bus.emit(
            "error", {"event": "signer.auth_fail", "path": path, "token": token_env}, reason_codes=[CODE_REJECT_AUTH]
        )
        return JSONResponse({"detail": "invalid or missing token", "reasonCodes": [CODE_REJECT_AUTH]}, status_code=401)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return executor.health()

    @app.post("/v1/execute")
    async def execute(request: Request, authorization: str | None = Header(default=None)) -> Any:
        denied = _auth(authorization, "SIGNER_TOKEN", "/v1/execute")
        if denied is not None:
            return denied
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
            return JSONResponse({"detail": "body too large", "reasonCodes": [CODE_NOT_ALLOWED]}, status_code=413)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY_BYTES:
                return JSONResponse({"detail": "body too large", "reasonCodes": [CODE_NOT_ALLOWED]}, status_code=413)
        try:
            req = ExecuteRequest.model_validate_json(bytes(body))
        except ValidationError as exc:
            fields = {(e.get("loc") or ("",))[0] for e in exc.errors()}
            detail = "unknown kind" if "kind" in fields else "malformed request"
            return JSONResponse({"detail": detail, "reasonCodes": [CODE_NOT_ALLOWED]}, status_code=422)
        if not valid_request_id(req.request_id):
            return JSONResponse(
                {"detail": "requestId must be a UUID4", "reasonCodes": [CODE_NOT_ALLOWED]}, status_code=422
            )
        response = await asyncio.to_thread(executor.execute, req)
        return JSONResponse(response.to_wire())

    @app.get("/v1/requests/{request_id}")
    def get_request(request_id: str, authorization: str | None = Header(default=None)) -> Any:
        denied = _auth(authorization, "SIGNER_TOKEN", "/v1/requests")
        if denied is not None:
            return denied
        stored = executor.stored(request_id)
        if stored is None:
            return JSONResponse({"detail": "not found"}, status_code=404)
        return JSONResponse(stored.to_wire())

    @app.get("/v1/decisions/{decision_hash}")
    def get_decision(decision_hash: str, authorization: str | None = Header(default=None)) -> Any:
        denied = _auth(authorization, "EVENTS_TOKEN", "/v1/decisions")
        if denied is not None:
            return denied
        found = executor.decisions.lookup(decision_hash)
        if found is None:
            return JSONResponse({"found": False}, status_code=404)
        return JSONResponse(found)

    @app.get("/events")
    async def events(
        after: int = Query(0, ge=0),
        limit: int = Query(500, ge=1, le=5000),
        wait: float = Query(0.0, ge=0.0, le=25.0),
        authorization: str | None = Header(default=None),
    ) -> Any:
        denied = _auth(authorization, "EVENTS_TOKEN", "/events")
        if denied is not None:
            return denied
        items = bus.since(after, limit)
        deadline = time.monotonic() + wait
        while not items and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            items = bus.since(after, limit)
        return JSONResponse([e.model_dump(mode="json") for e in items])

    return app


def serve(executor: Executor, *, host: str = LOOPBACK, port: int = 8702) -> None:
    """Run the signer with one uvicorn worker; refuses any interface other than 127.0.0.1."""
    if host != LOOPBACK:
        raise SystemExit(f"the signer binds to {LOOPBACK} only (got {host!r})")
    import uvicorn

    uvicorn.run(create_app(executor), host=LOOPBACK, port=int(port), workers=1, log_level="warning", access_log=False)


__all__ = ["LOOPBACK", "MAX_BODY_BYTES", "create_app", "serve", "token_ok"]
