"""Quote requests and the provider endpoints (spec 6.2 V08, 6.5 "PAY_CAPPED execution", 6.6, 9.3, 9.4).

A quote request is single use and bound to one mandate, one payee, one catalogue sku and one quantity
(``quoteRequestId = "qr-" + secrets.token_hex(16)``). It closes once an invoice is accepted for it, or
after the invoice validity plus ``quote.close_after_s``. :class:`QuoteBook` keeps them in the agent's
store, together with the superseded invoices of the capped re-quote (``capOf``) and their ``q'``.

The request a provider receives is built by code from structured fields only (quoteRequestId,
buyerAgentId, buyerVault, sku, quantity, capOf): no LLM text ever leaves to a counterparty.
:class:`HttpProviderClient` posts to ``http://127.0.0.1:<SIM_PORT>/p/<key>/quote`` and ``/deliver``; the
key comes from the pinned directory.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any, Protocol

from acb.agent.store import AgentStore
from acb.policy import PolicyConfig
from acb.policy.invoice import new_quote_request, superseded_key
from acb.types import QuoteRequest

MAX_RESPONSE_BYTES = 64 * 1024
"""Largest provider answer (quote or delivery) the agent reads."""


class ProviderClient(Protocol):
    """The provider endpoints of spec 9.3 / 9.4 (WP8). Errors are returned, never raised."""

    def quote(self, key: str, body: Mapping[str, Any]) -> dict[str, Any]: ...

    def deliver(self, key: str, body: Mapping[str, Any]) -> dict[str, Any]: ...


class HttpProviderClient:
    """:class:`ProviderClient` over HTTP to ``http://127.0.0.1:<port>/p/<key>``."""

    def __init__(self, port: int | None = None, *, timeout_s: float = 10.0, base_url: str | None = None):
        port = int(port or os.environ.get("SIM_PORT") or 8704)
        self.base_url = (base_url or f"http://127.0.0.1:{port}").rstrip("/")
        self.timeout_s = timeout_s

    def _post(self, key: str, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        import httpx

        if not key.replace("_", "").replace("-", "").isalnum():
            return {"status": "error", "error": "bad provider key"}
        try:
            # streamed: a provider can never make the agent buffer more than MAX_RESPONSE_BYTES
            with httpx.stream(
                "POST", f"{self.base_url}/p/{key}/{path}", json=dict(body), timeout=self.timeout_s
            ) as response:
                status_code = response.status_code
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        return {"status": "error", "error": "response too large", "httpStatus": status_code}
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            return {"status": "error", "error": type(exc).__name__}
        try:
            parsed = json.loads(b"".join(chunks))
        except ValueError:
            return {"status": "error", "error": "not JSON", "httpStatus": status_code}
        if status_code != 200:
            return {"status": "error", "httpStatus": status_code, "error": parsed}
        return parsed if isinstance(parsed, dict) else {"status": "error", "error": "not an object"}

    def quote(self, key: str, body: Mapping[str, Any]) -> dict[str, Any]:
        return self._post(key, "quote", body)

    def deliver(self, key: str, body: Mapping[str, Any]) -> dict[str, Any]:
        return self._post(key, "deliver", body)


class QuoteBook:
    """Single-use quote requests and superseded invoices, persisted in the agent store."""

    def __init__(self, store: AgentStore, cfg: PolicyConfig):
        self.store = store
        self.cfg = cfg

    def create(
        self, *, mandate_id: str, issuer_agent_id: int, sku: str, quantity: int, now: int, cap_of: str = ""
    ) -> QuoteRequest:
        qr = new_quote_request(
            mandate_id=mandate_id,
            issuer_agent_id=issuer_agent_id,
            sku=sku,
            quantity=quantity,
            now=now,
            cfg=self.cfg,
            cap_of=cap_of,
        )
        with self.store.edit() as data:
            data["quoteRequests"][qr.quote_request_id] = qr.to_wire()
        return qr

    def get(self, quote_request_id: str) -> QuoteRequest | None:
        raw = self.store.read("quoteRequests").get(quote_request_id)
        return QuoteRequest.model_validate(raw) if raw is not None else None

    def open_requests(self, now: int) -> tuple[QuoteRequest, ...]:
        """Requests not yet closed by time (used ones stay: V08 lets the same invoice pass its own)."""
        out = []
        for raw in self.store.read("quoteRequests").values():
            qr = QuoteRequest.model_validate(raw)
            if now <= qr.closes_at:
                out.append(qr)
        return tuple(out)

    def mark_used(self, quote_request_id: str, invoice_hash: str) -> None:
        with self.store.edit() as data:
            rec = data["quoteRequests"].get(quote_request_id)
            if rec is not None:
                rec["usedBy"] = invoice_hash

    def supersede(self, issuer_agent_id: int, invoice_id: str, requote_quantity: int) -> None:
        with self.store.edit() as data:
            data["superseded"][superseded_key(issuer_agent_id, invoice_id)] = int(requote_quantity)

    def superseded(self) -> dict[str, int]:
        return {k: int(v) for k, v in self.store.read("superseded").items()}


def quote_body(qr: QuoteRequest, *, buyer_agent_id: int, buyer_vault: str) -> dict[str, Any]:
    """The request a provider receives (spec 9.4), built from structured fields only."""
    return {
        "quoteRequestId": qr.quote_request_id,
        "buyerAgentId": str(int(buyer_agent_id)),
        "buyerVault": buyer_vault.lower(),
        "sku": qr.sku,
        "quantity": int(qr.quantity),
        "capOf": qr.cap_of,
    }


def deliver_body(order_id: str, invoice_id: str, tx_hash: str | None) -> dict[str, Any]:
    """``POST /p/<key>/deliver {orderId, invoiceId, txHash}`` (spec 9.3)."""
    body: dict[str, Any] = {"orderId": order_id.lower(), "invoiceId": invoice_id}
    if tx_hash:
        body["txHash"] = tx_hash.lower()
    return body


__all__ = ["HttpProviderClient", "ProviderClient", "QuoteBook", "deliver_body", "quote_body"]
