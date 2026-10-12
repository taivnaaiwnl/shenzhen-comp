"""Agent service P2 (``acb agent serve``, 127.0.0.1:8701, spec 8.8).

=====================================================  ======================  ===================================
Endpoint                                               Auth                    Purpose
=====================================================  ======================  ===================================
``POST /chat {message}``                               ``AGENT_TOKEN``         one owner turn -> ``ChatResponse``
``POST /mandates/activated {mandateId, txHash,          ``AGENT_TOKEN``         P1 reports a registered mandate;
draftId}``                                                                     verified on-chain before use
``POST /purchase/{mandate_id}``                        ``AGENT_TOKEN``         quote-and-decide for the items of
                                                                               the last ``request_quotes``
``POST /approvals/{order_id}/executed-request``        ``AGENT_TOKEN``         owner approval mined -> PAY_APPROVED
``POST /approvals/{order_id}/rejected``                ``AGENT_TOKEN``         owner rejected (no tx; addition)
``POST /reports/bad-data {orderId, ownerSignature}``   ``AGENT_TOKEN``         owner-confirmed BAD_DATA report
``POST /deliveries/check``                             ``AGENT_TOKEN``         one delivery pass now (addition)
``POST /admin/llm {mode, persona, maskFlaggedSpans}``  ``AGENT_ADMIN_TOKEN``   runner only, needs
                                                                               ``ACB_ALLOW_LLM_SWITCH=1``
``POST /inbox/invoice``                                none (public)           always ``{"status": "accepted"}``
``POST /inbox/message``                                none (public)           always ``{"status": "accepted"}``
``GET /state``                                         ``EVENTS_TOKEN``        drafts, mandates, orders, dialogue
``GET /decisions/{id}``                                ``EVENTS_TOKEN``        full decision (order id, decision
                                                                               hash, invoice hash or invoice id)
``GET /events?after=<seq>``                            ``EVENTS_TOKEN``        event feed (10.3)
``GET /health``                                        none                    liveness only
=====================================================  ======================  ===================================

Tokens are compared in constant time (401 on a wrong token, 503 when the variable is not set).
Inbox endpoints answer every request, whatever it holds, with the same constant, so nothing about
tiers, budgets or decisions leaks (A12); they are size-capped (16 KiB), rate-limited globally (60 per
minute) and per verified issuer (10 per minute), and processed by one background worker.
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import threading
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from acb.agent.runtime import SlidingLimiter

LOOPBACK = "127.0.0.1"
ACCEPTED = {"status": "accepted"}
MAX_AUTH_BODY = 64 * 1024


def token_ok(authorization: str | None, expected: str) -> bool:
    if not expected:
        return False
    given = authorization[7:].strip() if authorization and authorization[:7].lower() == "bearer " else ""
    return bool(given) and hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


class InboxWorker:
    """One background thread that processes inbox items in arrival order (or inline, for tests)."""

    def __init__(
        self,
        handle: Callable[[str, Any], Any],
        *,
        synchronous: bool = False,
        maxsize: int = 256,
        on_error: Callable[[str, Exception], None] | None = None,
    ):
        self.handle = handle
        self.on_error = on_error
        self.synchronous = synchronous
        self.q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._thread: threading.Thread | None = None
        if not synchronous:
            self._thread = threading.Thread(target=self._run, name="agent-inbox", daemon=True)
            self._thread.start()

    def submit(self, kind: str, body: Any) -> None:
        if self.synchronous:
            self._one(kind, body)
            return
        try:
            self.q.put_nowait((kind, body))
        except queue.Full:
            pass

    def _one(self, kind: str, body: Any) -> None:
        try:
            self.handle(kind, body)
        except Exception as exc:  # noqa: BLE001 - one bad item must not stop the worker; the answer never changes
            if self.on_error is not None:
                self.on_error(kind, exc)

    def _run(self) -> None:
        while True:
            kind, body = self.q.get()
            self._one(kind, body)


class DeliveryWatcher:
    """Runs ``runtime.tick()`` every ``interval`` seconds (escrow claims, receipts, deadlines)."""

    def __init__(self, agent: Any, interval: float):
        self.agent = agent
        self.interval = float(interval)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="agent-deliveries", daemon=True)

    def start(self) -> DeliveryWatcher:
        if self.interval > 0:
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.agent.runtime.tick()
            except Exception as exc:  # noqa: BLE001 - keep watching
                self.agent.bus.emit("error", {"message": "delivery watcher", "error": type(exc).__name__})


def create_app(
    agent: Any,
    *,
    environ: Mapping[str, str] | None = None,
    inbox_synchronous: bool = False,
    llm_factory: Callable[..., tuple[Any, dict[str, Any]]] | None = None,
) -> Any:
    """The FastAPI app of the agent. Tokens and the switch flag are read from ``environ`` (default ``os.environ``)."""
    env = os.environ if environ is None else environ
    app = FastAPI(title="ACB wallet agent", docs_url=None, redoc_url=None, openapi_url=None)
    bus = agent.bus
    inbox_cfg = agent.cfg.inbox
    global_limit = SlidingLimiter(inbox_cfg.global_per_minute)
    lock = threading.Lock()

    def handle_inbox(kind: str, body: Any) -> None:
        if kind == "invoice":
            agent.runtime.inbox_invoice(body)
        else:
            agent.runtime.inbox_message(body)

    def inbox_error(kind: str, exc: Exception) -> None:
        bus.emit("error", {"message": f"inbox {kind} processing failed", "error": type(exc).__name__})

    worker = InboxWorker(handle_inbox, synchronous=inbox_synchronous, on_error=inbox_error)
    app.state.inbox = worker

    def denied(authorization: str | None, token_env: str, path: str) -> JSONResponse | None:
        expected = env.get(token_env, "")
        if not expected:
            return JSONResponse({"detail": f"{token_env} is not configured"}, status_code=503)
        if token_ok(authorization, expected):
            return None
        bus.emit("error", {"event": "agent.auth_fail", "path": path, "token": token_env})
        return JSONResponse({"detail": "invalid or missing token"}, status_code=401)

    async def body_json(request: Request, limit: int) -> Any:
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > limit:
                return _TooLarge
        if not raw:
            return None
        try:
            return json.loads(bytes(raw).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return _BadJson

    def bad(detail: str, status: int = 422) -> JSONResponse:
        return JSONResponse({"detail": detail}, status_code=status)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "agent", "chainId": agent.chain.chain_id, "vault": agent.vault_address}

    # ------------------------------------------------------------------ owner (AGENT_TOKEN)

    @app.post("/chat")
    async def chat(request: Request) -> Any:
        d = denied(request.headers.get("authorization"), "AGENT_TOKEN", "/chat")
        if d is not None:
            return d
        body = await body_json(request, MAX_AUTH_BODY)
        if not isinstance(body, dict) or set(body) != {"message"} or not isinstance(body["message"], str):
            return bad("body must be {message}")
        from fastapi.concurrency import run_in_threadpool

        response = await run_in_threadpool(agent.dialogue.owner_turn, body["message"])
        return JSONResponse(response.to_wire())

    @app.post("/mandates/activated")
    async def activated(request: Request) -> Any:
        d = denied(request.headers.get("authorization"), "AGENT_TOKEN", "/mandates/activated")
        if d is not None:
            return d
        body = await body_json(request, MAX_AUTH_BODY)
        keys = {"mandateId", "txHash", "draftId"}
        if not isinstance(body, dict) or set(body) != keys or not all(isinstance(body[k], str) for k in keys):
            return bad("body must be {mandateId, txHash, draftId}")
        if not _hex32(body["mandateId"]) or not _hex32(body["txHash"]):
            return bad("mandateId and txHash must be 0x + 64 hex")
        from fastapi.concurrency import run_in_threadpool

        result = await run_in_threadpool(agent.dialogue.activated, body["mandateId"], body["txHash"], body["draftId"])
        return JSONResponse(result)

    @app.post("/purchase/{mandate_id}")
    async def purchase(mandate_id: str, request: Request) -> Any:
        d = denied(request.headers.get("authorization"), "AGENT_TOKEN", "/purchase")
        if d is not None:
            return d
        if not _hex32(mandate_id):
            return bad("mandate id must be 0x + 64 hex")
        body = await body_json(request, MAX_AUTH_BODY)
        items = None
        if isinstance(body, dict) and set(body) <= {"items"} and "items" in body:
            items = body["items"]
        elif body not in (None, {}):
            return bad("body must be empty or {items}")
        from fastapi.concurrency import run_in_threadpool

        result = await run_in_threadpool(agent.runtime.run_purchase, mandate_id, items)
        return JSONResponse(result, status_code=200 if result.get("status") == "done" else 409)

    @app.post("/approvals/{order_id}/executed-request")
    async def approval_executed(order_id: str, request: Request) -> Any:
        d = denied(request.headers.get("authorization"), "AGENT_TOKEN", "/approvals")
        if d is not None:
            return d
        if not _hex32(order_id):
            return bad("order id must be 0x + 64 hex")
        from fastapi.concurrency import run_in_threadpool

        result = await run_in_threadpool(agent.runtime.execute_approved, order_id)
        return JSONResponse(result, status_code=409 if result.get("status") == "error" else 200)

    @app.post("/approvals/{order_id}/rejected")
    async def approval_rejected(order_id: str, request: Request) -> Any:
        d = denied(request.headers.get("authorization"), "AGENT_TOKEN", "/approvals")
        if d is not None:
            return d
        if not _hex32(order_id):
            return bad("order id must be 0x + 64 hex")
        from fastapi.concurrency import run_in_threadpool

        result = await run_in_threadpool(agent.runtime.reject_approval, order_id)
        return JSONResponse(result, status_code=409 if result.get("status") == "error" else 200)

    @app.post("/reports/bad-data")
    async def bad_data(request: Request) -> Any:
        d = denied(request.headers.get("authorization"), "AGENT_TOKEN", "/reports/bad-data")
        if d is not None:
            return d
        body = await body_json(request, MAX_AUTH_BODY)
        keys = {"orderId", "ownerSignature"}
        if not isinstance(body, dict) or set(body) != keys or not all(isinstance(body[k], str) for k in keys):
            return bad("body must be {orderId, ownerSignature}")
        if not _hex32(body["orderId"]):
            return bad("orderId must be 0x + 64 hex")
        from fastapi.concurrency import run_in_threadpool

        result = await run_in_threadpool(agent.runtime.bad_data, body["orderId"], body["ownerSignature"])
        return JSONResponse(result)

    @app.post("/deliveries/check")
    async def deliveries(request: Request) -> Any:
        d = denied(request.headers.get("authorization"), "AGENT_TOKEN", "/deliveries/check")
        if d is not None:
            return d
        from fastapi.concurrency import run_in_threadpool

        ids = await run_in_threadpool(agent.runtime.tick)
        return JSONResponse({"status": "done", "checked": ids})

    # ------------------------------------------------------------------ runner (AGENT_ADMIN_TOKEN)

    @app.post("/admin/llm")
    async def admin_llm(request: Request) -> Any:
        d = denied(request.headers.get("authorization"), "AGENT_ADMIN_TOKEN", "/admin/llm")
        if d is not None:
            return d
        if (env.get("ACB_ALLOW_LLM_SWITCH") or "0").strip() != "1":
            bus.emit("error", {"event": "agent.llm_switch_disabled"})
            return JSONResponse({"detail": "LLM switch disabled (ACB_ALLOW_LLM_SWITCH is not 1)"}, status_code=403)
        body = await body_json(request, MAX_AUTH_BODY)
        allowed = {"mode", "persona", "maskFlaggedSpans"}
        if not isinstance(body, dict) or not set(body) <= allowed or not body:
            return bad("body must be {mode, persona, maskFlaggedSpans}")
        mode = body.get("mode", agent.llm_info.get("mode"))
        persona = body.get("persona", agent.llm_info.get("persona") or "careful")
        mask = body.get("maskFlaggedSpans")
        if mode not in ("mock", "openai", "auto") or persona not in ("careful", "naive"):
            return bad("mode must be mock|openai|auto and persona careful|naive")
        if mask is not None and not isinstance(mask, bool):
            return bad("maskFlaggedSpans must be a boolean")
        factory = llm_factory or default_llm_factory
        try:
            llm, info = factory(mode=mode, persona=persona, environ=env)
        except (ValueError, TypeError) as exc:
            return bad(str(exc))
        from fastapi.concurrency import run_in_threadpool

        before = agent.llm_view()

        def switch() -> None:
            # switch_llm waits for the agent lock (a turn may hold it): never on the event loop
            with lock:
                agent.switch_llm(llm, info, mask)

        await run_in_threadpool(switch)
        after = agent.llm_view()
        bus.emit("llm_switch", {"from": before, "to": after})
        return JSONResponse({"status": "switched", "llm": after})

    # ------------------------------------------------------------------ public inbox

    @app.post("/inbox/invoice")
    async def inbox_invoice(request: Request) -> Any:
        body = await body_json(request, inbox_cfg.max_bytes)
        if isinstance(body, dict) and global_limit.allow("inbox"):
            worker.submit("invoice", body)
        return JSONResponse(ACCEPTED)

    @app.post("/inbox/message")
    async def inbox_message(request: Request) -> Any:
        body = await body_json(request, inbox_cfg.max_bytes)
        if isinstance(body, dict) and global_limit.allow("inbox"):
            worker.submit("message", body)
        return JSONResponse(ACCEPTED)

    # ------------------------------------------------------------------ read-only (EVENTS_TOKEN)

    @app.get("/state")
    def state(request: Request) -> Any:
        d = denied(request.headers.get("authorization"), "EVENTS_TOKEN", "/state")
        if d is not None:
            return d
        return JSONResponse(agent_state(agent))

    @app.get("/decisions/{ident}")
    def decision(ident: str, request: Request) -> Any:
        d = denied(request.headers.get("authorization"), "EVENTS_TOKEN", "/decisions")
        if d is not None:
            return d
        found = find_decision(agent, ident)
        if found is None:
            return JSONResponse({"found": False}, status_code=404)
        return JSONResponse({"found": True, **found})

    @app.get("/events")
    async def events(request: Request, after: int = 0, limit: int = 500, wait: float = 0.0) -> Any:
        """``GET /events?after=<seq>&limit=<n>&wait=<s>`` (envelopes of 10.3, snake_case)."""
        import asyncio
        import time

        d = denied(request.headers.get("authorization"), "EVENTS_TOKEN", "/events")
        if d is not None:
            return d
        after, limit, wait = max(0, after), min(max(1, limit), 5000), min(max(0.0, wait), 25.0)
        items = bus.since(after, limit)
        deadline = time.monotonic() + wait
        while not items and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            items = bus.since(after, limit)
        return JSONResponse([e.model_dump(mode="json") for e in items])

    return app


class _Marker:
    pass


_TooLarge = _Marker()
_BadJson = _Marker()


def _hex32(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 66
        and value[:2] == "0x"
        and all(c in "0123456789abcdefABCDEF" for c in value[2:])
    )


def agent_state(agent: Any) -> dict[str, Any]:
    """``GET /state``: drafts, mandates (verified, read from chain), pending orders, decisions, dialogue."""
    store = agent.store
    mandates = []
    for m in agent.verified_mandates():
        mandates.append(agent.mandate_context(m))
    orders = {
        oid: {k: v for k, v in rec.items() if k not in ("invoice", "quoteRequest", "delivery")}
        for oid, rec in store.read("orders").items()
    }
    approvals = [
        {"orderId": oid, "invoice": rec["invoice"], "reasons": rec.get("reasons", [])}
        for oid, rec in store.read("orders").items()
        if rec.get("status") == "awaiting_approval"
    ]
    dialogue = store.read("dialogue")
    return {
        "network": agent.network,
        "vault": agent.vault_address,
        "agentId": str(agent.agent_id),
        "llm": agent.llm_view(),
        "dialogue": {
            "state": dialogue.get("state"),
            "currentDraft": dialogue.get("currentDraft"),
            "pending": dialogue.get("pending"),
            "metrics": dialogue.get("metrics", []),
            "turns": dialogue.get("turns", [])[-20:],
        },
        "drafts": store.read("drafts"),
        "mandates": mandates,
        "mandateRecords": store.read("mandates"),
        "orders": orders,
        "approvals": approvals,
        "decisions": list(store.read("decisions").values())[-50:],
        "messages": store.read("messages")[-50:],
        "reports": store.read("reports"),
    }


def find_decision(agent: Any, ident: str) -> dict[str, Any] | None:
    """A decision by order id, decision hash, invoice hash or invoice id, with the signed invoice."""
    key = ident.lower()
    decisions = agent.store.read("decisions")
    orders = agent.store.read("orders")
    rec = decisions.get(key)
    if rec is None:
        for r in decisions.values():
            if key in (str(r.get("decisionHash", "")).lower(), str(r.get("invoiceHash", "")).lower()) or ident == r.get(
                "invoiceId"
            ):
                rec = r
                break
    if rec is None:
        return None
    out = dict(rec)
    order = orders.get(str(rec.get("orderId", "")).lower())
    if order is not None:
        out["invoice"] = order.get("invoice")
        out["orderStatus"] = order.get("status")
        out["delivery"] = order.get("delivery")
    out["explorer"] = [agent.explorer(h) for h in rec.get("txHashes") or []]
    return out


def default_llm_factory(*, mode: str, persona: str, environ: Mapping[str, str]) -> tuple[Any, dict[str, Any]]:
    """Build the LLM for ``POST /admin/llm`` from the process's ``LLM_*`` / ``MOCK_LLM_*`` settings."""
    from acb.agent.llm import build_llm
    from acb.config import llm_settings

    settings = llm_settings(dict(environ))
    effective = mode if mode != "auto" else ("openai" if settings.api_key else "mock")
    return build_llm(
        mode=effective,
        model=settings.model,
        api_key=settings.api_key,
        base_url=settings.base_url,
        timeout_s=settings.timeout_s,
        tool_mode=settings.tool_mode,
        extra_body=settings.extra_body,
        mock_script=settings.mock_script,
        persona=persona,
    )


def serve(agent: Any, *, host: str = LOOPBACK, port: int = 8701, watch_s: float | None = None) -> None:
    """Run the agent on 127.0.0.1 only, with the delivery watcher."""
    if host != LOOPBACK:
        raise SystemExit(f"the agent binds to {LOOPBACK} only (got {host!r})")
    import uvicorn

    watcher = DeliveryWatcher(agent, agent.cfg.delivery_watch_s if watch_s is None else watch_s).start()
    try:
        uvicorn.run(create_app(agent), host=LOOPBACK, port=int(port), workers=1, log_level="warning", access_log=False)
    finally:
        watcher.stop()


__all__ = [
    "ACCEPTED",
    "LOOPBACK",
    "DeliveryWatcher",
    "InboxWorker",
    "agent_state",
    "create_app",
    "default_llm_factory",
    "find_decision",
    "serve",
    "token_ok",
]
