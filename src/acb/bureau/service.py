"""Bureau service P4 (``acb bureau serve``, 127.0.0.1:8703, spec 2.1, 15.3).

| Method and path | Auth | Response |
|---|---|---|
| ``GET /health`` | none | ``{"ok": true, "network", "chainId", "bureau", "modelId", "configHash"}`` |
| ``GET /grades`` | ``EVENTS_TOKEN`` | ``list[GradeView]`` (camelCase) |
| ``GET /grades/{agentId}`` | ``EVENTS_TOKEN`` | ``{"grade": GradeView, "bundleUri", "bundle"}`` |
| ``POST /rescore {"rounds": n}`` | ``BUREAU_TOKEN`` | ``RescoreResult`` |
| ``GET /events?after=`` | ``EVENTS_TOKEN`` | event envelopes (10.3) |

The service reads ``os.environ`` only (never ``.env``) and holds ``BUREAU_PRIVATE_KEY`` (Anvil: the
Anvil key ring). Rescores are serialised by a lock (one transaction in flight).
"""

from __future__ import annotations

import hmac
import json
import os
import threading
from typing import Any

from pydantic import BaseModel, ConfigDict

from acb.bureau.poster import Bureau

LOOPBACK = "127.0.0.1"
MAX_BODY_BYTES = 65_536


class RescoreBody(BaseModel):
    """``POST /rescore`` body."""

    model_config = ConfigDict(extra="forbid")
    rounds: int = 1


def check_bearer(authorization: str | None, token_env: str) -> None:
    """Constant-time ``Authorization: Bearer`` check (503 if the token is not configured, 401 if wrong)."""
    from fastapi import HTTPException

    expected = os.environ.get(token_env, "")
    if not expected:
        raise HTTPException(status_code=503, detail=f"{token_env} is not configured")
    given = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    if not given or not hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="invalid or missing token")


def bureau_account(network: str) -> Any:
    """The bureau key: Anvil key ring on ``anvil``, else ``BUREAU_PRIVATE_KEY``."""
    from acb.keys import KeyRing, load_role_key

    if network == "anvil":
        return KeyRing.anvil().account("bureau")
    return load_role_key("bureau")


def make_bureau(network: str, *, with_key: bool = True, bus: Any | None = None, **kwargs: Any) -> Bureau:
    """A :class:`Bureau` for ``network`` from ``deployments/<network>.json`` and the environment."""
    from acb.chain.client import make_web3
    from acb.chain.deployments import load_deployments
    from acb.config import load_network

    net = load_network(network)
    dep = load_deployments(network)
    w3 = make_web3(net, "rpc")
    w3_logs = make_web3(net, "logs")
    account = bureau_account(network) if with_key else None
    return Bureau(net, dep, w3=w3, w3_logs=w3_logs, account=account, bus=bus, **kwargs)


def create_app(bureau: Bureau, bus: Any) -> Any:
    """The FastAPI app of P4 around an existing :class:`Bureau` and :class:`~acb.logbus.EventBus`."""
    from fastapi import FastAPI, Header, HTTPException, Request
    from fastapi.responses import JSONResponse

    from acb.bureau.evidence import bundle_name, read_bundle_source
    from acb.config import REPO_ROOT
    from acb.logbus import events_router

    app = FastAPI(title="ACB bureau (P4)", docs_url=None, redoc_url=None, openapi_url=None)
    lock = threading.Lock()

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
            "network": bureau.net.name,
            "chainId": bureau.net.chain_id,
            "bureau": bureau.address,
            "modelId": bureau.cfg.model_id,
            "configHash": bureau.cfg.config_hash,
        }

    @app.get("/grades")
    def grades(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        check_bearer(authorization, "EVENTS_TOKEN")
        with lock:
            views = bureau.grade_views()
        return [v.to_wire() for v in views]

    @app.get("/grades/{agent_id}")
    def grade(agent_id: int, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        check_bearer(authorization, "EVENTS_TOKEN")
        with lock:
            view = bureau.grade_view(agent_id)
        if view is None:
            raise HTTPException(status_code=404, detail="never graded")
        name = bundle_name(view.agent_id, view.as_of_block)  # never a name taken from the on-chain URI
        local = (REPO_ROOT / "evidence", bureau.out_dir)
        bundle = None
        for loc in (view.evidence_uri, str(bureau.out_dir / "bundles" / name)):
            data = read_bundle_source(loc, name, local)
            if data is not None:
                try:
                    bundle = json.loads(data.decode("utf-8"))
                    break
                except (UnicodeDecodeError, ValueError):
                    continue
        return {"grade": view.to_wire(), "bundleUri": view.evidence_uri, "bundle": bundle}

    @app.post("/rescore")
    def rescore(body: RescoreBody | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        check_bearer(authorization, "BUREAU_TOKEN")
        rounds = body.rounds if body is not None else 1
        if not 1 <= rounds <= 5:
            raise HTTPException(status_code=422, detail="rounds must be an integer 1..5")
        if bureau.sender is None:
            raise HTTPException(status_code=503, detail="no bureau key loaded")
        with lock:
            result = bureau.rescore(rounds)
        return result.to_wire()

    app.include_router(events_router(bus, "EVENTS_TOKEN"))
    return app


def serve(network: str, *, host: str = LOOPBACK, port: int | None = None) -> int:
    """Run P4 until interrupted. Refuses any interface other than 127.0.0.1."""
    import uvicorn

    from acb.logbus import EventBus

    if host != LOOPBACK:
        raise SystemExit("acb bureau serve binds to 127.0.0.1 only")
    port = int(port or os.environ.get("BUREAU_PORT") or 8703)
    from acb.config import load_network

    net = load_network(network)
    explorer = net.explorers[0].get("tx") if net.explorers else None
    bus = EventBus("bureau", chain_id=net.chain_id, explorer_tx=explorer)
    bureau = make_bureau(network, bus=bus)
    app = create_app(bureau, bus)
    bus.emit("info", {"started": "bureau", "network": network, "port": port, "bureau": bureau.address})
    uvicorn.run(app, host=LOOPBACK, port=port, log_level="warning", workers=1)
    return 0


__all__ = ["LOOPBACK", "bureau_account", "check_bearer", "create_app", "make_bureau", "serve"]
