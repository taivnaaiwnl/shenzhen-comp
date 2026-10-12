"""The owner console web app (``acb owner serve``, http://127.0.0.1:8700, spec 11.1).

==============================================  ==========  ==============================================
Endpoint                                        Auth        Purpose
==============================================  ==========  ==============================================
``GET /``                                       none        the page (``<meta name="acb-csrf">`` when signed in)
``GET /app.js``, ``/style.css``                 none        static files (no build step, no CDN)
``POST /login {password}``                      none        session cookie (HttpOnly, SameSite=Strict)
``POST /logout``                                session     ends the session
``GET /api/session``                            none        ``{"authenticated": bool}``
``GET /stream``                                 session     SSE: merged events of every service (``?once=1``)
``GET /api/snapshot``                           session     all panel data (chain reads)
``POST /api/chat {message}``                    s + CSRF    proxy to the agent's ``/chat``; cards rebuilt by P1
``POST /api/mandates/{draft_id}/prepare``       s + CSRF    chain-rebuilt confirm card (addition, see below)
``POST /api/mandates/{draft_id}/confirm``       s + CSRF    ``registerMandate`` with the reviewed values
``POST /api/mandates/{id}/revoke``              s + CSRF    ``revokeMandate``
``POST /api/approvals/{order_id}/prepare``      s + CSRF    verified approval card (addition)
``POST /api/approvals/{order_id}/approve``      s + CSRF    ``ownerApprove`` with the verified values
``POST /api/approvals/{order_id}/reject``       s + CSRF    no transaction; tells the agent
``POST /api/reports/{order_id}/bad-data``       s + CSRF    the owner signs the BAD_DATA payload hash
``POST /api/freeze``                            s + CSRF    ``freeze(3202)``
``POST /api/unfreeze {toTier, rotateTo}``       s + CSRF    ``unfreeze(toTier, 3300, newAgent)``
``POST /api/actions/{id}/submitted``            s + CSRF    browser signing (11.4): the tx hash or signature
``GET /events``                                 EVENTS      the console's own events (10.3)
``GET /health``                                 none        liveness only
==============================================  ==========  ==============================================

``prepare`` endpoints exist because a confirm card must be rebuilt **before** the owner sees it; the
``confirm``/``approve`` endpoints then send exactly the stored values of that review (``prepareId``).
Every response carries the CSP of spec 11.1 and ``X-Content-Type-Options: nosniff``. POST requests
must be JSON (a cross-site form cannot send it without a CORS preflight, which is never granted) and
the ``Host`` must be the loopback address (DNS rebinding).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from acb.owner.actions import ActionError, OwnerConsole
from acb.owner.security import (
    COOKIE_NAME,
    CSRF_HEADER,
    SECURITY_HEADERS,
    SESSION_TTL_S,
    LoginLimiter,
    Sessions,
    password_ok,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
LOOPBACK = "127.0.0.1"
MAX_BODY = 64 * 1024
SSE_HEARTBEAT_S = 15.0
SSE_POLL_S = 0.25
_META_CSRF = '<meta name="acb-csrf" content="">'
_ALLOWED_HOSTS = re.compile(r"^(127\.0\.0\.1|localhost)(:\d{1,5})?$")


def create_app(
    console: OwnerConsole,
    *,
    password: str,
    session_secret: str,
    merger: Any = None,
    static_dir: Path = STATIC_DIR,
) -> FastAPI:
    """The FastAPI app of the console. ``merger`` (a :class:`~acb.owner.feeds.FeedMerger`) feeds ``/stream``."""
    app = FastAPI(title="ACB owner console", docs_url=None, redoc_url=None, openapi_url=None)
    sessions = Sessions(session_secret)
    limiter = LoginLimiter()
    index_template = (static_dir / "index.html").read_text(encoding="utf-8")
    if _META_CSRF not in index_template:
        raise RuntimeError("index.html must contain the acb-csrf meta placeholder")
    app.state.sessions = sessions
    app.state.console = console

    @app.middleware("http")
    async def headers(request: Request, call_next: Any) -> Response:
        host = request.headers.get("host", "")
        if not _ALLOWED_HOSTS.fullmatch(host):
            response: Response = JSONResponse({"detail": "bad host"}, status_code=421)
        else:
            response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers[key] = value
        if request.url.path.startswith(("/api/", "/login", "/logout", "/stream", "/events")) or request.url.path == "/":
            response.headers["Cache-Control"] = "no-store"
        return response

    # ------------------------------------------------------------------ helpers

    def session_of(request: Request):
        return sessions.get(request.cookies.get(COOKIE_NAME))

    def error(status: int, detail: str, **extra: Any) -> JSONResponse:
        return JSONResponse({"detail": detail, **extra}, status_code=status)

    async def read_json(request: Request) -> Any:
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ActionError(415, "requests must be application/json")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BODY:
                raise ActionError(413, "request body too large")
        if not raw:
            return {}
        try:
            return json.loads(bytes(raw).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ActionError(400, "the body is not JSON") from None

    def origin_ok(request: Request) -> bool:
        origin = request.headers.get("origin")
        if origin is None or origin == "null":
            return origin is None
        host = request.headers.get("host", "")
        return origin in (f"http://{host}", f"https://{host}")

    async def guarded(request: Request, handler: Any, *args: Any) -> Response:
        """Session + CSRF + JSON + same origin, then ``handler(body, *args)`` off the event loop."""
        session = session_of(request)
        if session is None:
            return error(401, "sign in first")
        if not sessions.csrf_ok(session, request.headers.get(CSRF_HEADER)):
            return error(403, "missing or wrong CSRF token")
        if not origin_ok(request):
            return error(403, "cross-origin request refused")
        try:
            body = await read_json(request)
            if not isinstance(body, dict):
                raise ActionError(422, "the body must be a JSON object")
            result = await run_in_threadpool(handler, body, *args)
        except ActionError as exc:
            return error(exc.status, exc.message, **exc.extra)
        return JSONResponse(result)

    # ------------------------------------------------------------------ page and static files

    @app.get("/")
    def index(request: Request) -> Response:
        session = session_of(request)
        token = session.csrf if session is not None else ""
        page = index_template.replace(_META_CSRF, f'<meta name="acb-csrf" content="{token}">')
        return Response(page, media_type="text/html; charset=utf-8")

    @app.get("/app.js")
    def app_js() -> Response:
        return FileResponse(static_dir / "app.js", media_type="text/javascript; charset=utf-8")

    @app.get("/style.css")
    def style_css() -> Response:
        return FileResponse(static_dir / "style.css", media_type="text/css; charset=utf-8")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "owner", "signing": console.signing}

    # ------------------------------------------------------------------ login

    @app.post("/login")
    async def login(request: Request) -> Response:
        if limiter.blocked():
            return error(429, "too many failed sign-ins; wait a minute")
        if not origin_ok(request):
            return error(403, "cross-origin request refused")
        try:
            body = await read_json(request)
        except ActionError as exc:
            return error(exc.status, exc.message)
        given = body.get("password") if isinstance(body, dict) else None
        if not isinstance(given, str) or not password_ok(given, password):
            limiter.failed()
            return error(401, "wrong password")
        _, cookie = sessions.create()
        response = JSONResponse({"status": "ok"})
        response.set_cookie(COOKIE_NAME, cookie, max_age=int(SESSION_TTL_S), httponly=True, samesite="strict", path="/")
        return response

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        session = session_of(request)
        if session is not None and not sessions.csrf_ok(session, request.headers.get(CSRF_HEADER)):
            return error(403, "missing or wrong CSRF token")
        sessions.drop(request.cookies.get(COOKIE_NAME))
        response = JSONResponse({"status": "signed out"})
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict")
        return response

    @app.get("/api/session")
    def session_info(request: Request) -> dict[str, Any]:
        return {"authenticated": session_of(request) is not None}

    # ------------------------------------------------------------------ read-only (session)

    @app.get("/api/snapshot")
    async def snapshot(request: Request) -> Response:
        if session_of(request) is None:
            return error(401, "sign in first")
        return JSONResponse(await run_in_threadpool(console.snapshot))

    @app.get("/stream")
    async def stream(request: Request) -> Response:
        if session_of(request) is None:
            return error(401, "sign in first")
        after = 0
        for candidate in (request.headers.get("last-event-id"), request.query_params.get("after")):
            if candidate and candidate.isdigit() and len(candidate) < 12:
                after = int(candidate)
                break

        # ?once=1: send what is there and close (non-browser clients and tests)
        once = request.query_params.get("once") == "1"

        async def gen():
            nonlocal after
            yield "retry: 2000\n\n"
            idle = 0.0
            while True:
                if await request.is_disconnected():
                    break
                items = merger.since(after, 200) if merger is not None else []
                for item in items:
                    after = item["id"]
                    yield f"id: {item['id']}\ndata: {json.dumps(item, ensure_ascii=True, separators=(',', ':'))}\n\n"
                if items:
                    idle = 0.0
                    continue
                if once:
                    break
                await asyncio.sleep(SSE_POLL_S)
                idle += SSE_POLL_S
                if idle >= SSE_HEARTBEAT_S:
                    idle = 0.0
                    yield ": keep-alive\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ owner actions (session + CSRF)

    def body_str(body: dict, key: str, *, required: bool = True) -> str | None:
        value = body.get(key)
        if value is None and not required:
            return None
        if not isinstance(value, str):
            raise ActionError(422, f"{key} must be a string")
        return value

    @app.post("/api/chat")
    async def chat(request: Request) -> Response:
        return await guarded(request, lambda b: console.chat(body_str(b, "message")))

    @app.post("/api/mandates/{draft_id}/prepare")
    async def mandate_prepare(draft_id: str, request: Request) -> Response:
        return await guarded(request, lambda b, d: console.prepare_mandate(d), draft_id)

    @app.post("/api/mandates/{draft_id}/confirm")
    async def mandate_confirm(draft_id: str, request: Request) -> Response:
        return await guarded(request, lambda b, d: console.confirm_mandate(d, body_str(b, "prepareId")), draft_id)

    @app.post("/api/mandates/{mandate_id}/revoke")
    async def mandate_revoke(mandate_id: str, request: Request) -> Response:
        return await guarded(request, lambda b, m: console.revoke_mandate(m), mandate_id)

    @app.post("/api/approvals/{order_id}/prepare")
    async def approval_prepare(order_id: str, request: Request) -> Response:
        return await guarded(request, lambda b, o: console.prepare_approval(o), order_id)

    @app.post("/api/approvals/{order_id}/approve")
    async def approval_approve(order_id: str, request: Request) -> Response:
        return await guarded(request, lambda b, o: console.approve(o, body_str(b, "prepareId")), order_id)

    @app.post("/api/approvals/{order_id}/reject")
    async def approval_reject(order_id: str, request: Request) -> Response:
        return await guarded(request, lambda b, o: console.reject(o), order_id)

    @app.post("/api/reports/{order_id}/bad-data")
    async def report_bad_data(order_id: str, request: Request) -> Response:
        return await guarded(request, lambda b, o: console.bad_data(o), order_id)

    @app.post("/api/freeze")
    async def freeze(request: Request) -> Response:
        return await guarded(request, lambda b: console.freeze())

    @app.post("/api/unfreeze")
    async def unfreeze(request: Request) -> Response:
        return await guarded(request, lambda b: console.unfreeze(b.get("toTier"), b.get("rotateTo")))

    @app.post("/api/actions/{action_id}/submitted")
    async def submitted(action_id: str, request: Request) -> Response:
        return await guarded(
            request,
            lambda b, a: console.submitted(
                a, body_str(b, "txHash", required=False), body_str(b, "signature", required=False)
            ),
            action_id,
        )

    # ------------------------------------------------------------------ the console's own events

    if console.bus is not None:
        from acb.logbus import events_router

        app.include_router(events_router(console.bus))

    return app


def serve(app: FastAPI, *, host: str = LOOPBACK, port: int = 8700) -> None:
    """Run the console on 127.0.0.1 only (one worker)."""
    if host != LOOPBACK:
        raise SystemExit(f"the owner console binds to {LOOPBACK} only (got {host!r})")
    import uvicorn

    uvicorn.run(app, host=LOOPBACK, port=int(port), workers=1, log_level="warning", access_log=False)


__all__ = ["LOOPBACK", "STATIC_DIR", "create_app", "serve"]
