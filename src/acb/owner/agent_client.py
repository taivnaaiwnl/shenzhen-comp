"""HTTP client of the wallet agent API (spec 8.8) as the owner console uses it.

P1 holds ``AGENT_TOKEN`` (chat and notifications) and the read-only ``EVENTS_TOKEN`` (state, decisions,
events); it never holds ``SIGNER_TOKEN``. Every answer of the agent is untrusted data: the console
takes only agentIds, numbers and purpose from a draft and only the signed invoice from an approval,
and verifies both against the chain (:mod:`acb.owner.chain_views`).

All requests go to the literal ``127.0.0.1`` (spec 2.1), with a timeout and a response size cap.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import httpx

LOOPBACK = "127.0.0.1"
DEFAULT_AGENT_PORT = 8701
MAX_RESPONSE_BYTES = 1024 * 1024
CONNECT_TIMEOUT_S = 1.0
"""A loopback connection that is not accepted within a second is a stopped agent."""


class AgentUnavailable(RuntimeError):
    """The agent did not answer, or answered with something that is not a JSON object."""


def agent_base_url(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    port = int(env.get("AGENT_PORT") or DEFAULT_AGENT_PORT)
    return f"http://{LOOPBACK}:{port}"


class AgentClient:
    """Calls of the owner console to the wallet agent (P2)."""

    def __init__(
        self,
        base_url: str,
        *,
        agent_token: str,
        events_token: str,
        timeout: float = 60.0,
        transport: Any = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.agent_token = agent_token
        self.events_token = events_token
        self.timeout = timeout
        self.transport = transport

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> AgentClient:
        env = os.environ if environ is None else environ
        return cls(
            agent_base_url(env),
            agent_token=env.get("AGENT_TOKEN", ""),
            events_token=env.get("EVENTS_TOKEN", ""),
        )

    # ---- transport ---------------------------------------------------------------------------------

    def _request(self, method: str, path: str, *, token: str, json: Any = None, timeout: float | None = None) -> Any:
        if not token:
            raise AgentUnavailable("the token for the agent API is not configured")
        headers = {"Authorization": f"Bearer {token}"}
        try:
            with (
                httpx.Client(
                    transport=self.transport, timeout=httpx.Timeout(timeout or self.timeout, connect=CONNECT_TIMEOUT_S)
                ) as client,
                client.stream(method, self.base_url + path, json=json, headers=headers) as response,
            ):
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise AgentUnavailable("the agent's answer is too large")
                status = response.status_code
        except httpx.HTTPError as exc:
            raise AgentUnavailable(f"the agent is not reachable ({type(exc).__name__})") from None
        try:
            import json as _json

            data = _json.loads(bytes(body).decode("utf-8")) if body else None
        except (UnicodeDecodeError, ValueError):
            raise AgentUnavailable(f"the agent answered HTTP {status} without JSON") from None
        if status in (401, 403, 503):
            raise AgentUnavailable(f"the agent refused the request (HTTP {status})")
        if status == 404:
            return None
        return data

    def _post(self, path: str, body: Any = None, *, timeout: float | None = None) -> dict[str, Any]:
        data = self._request("POST", path, token=self.agent_token, json=body, timeout=timeout)
        if not isinstance(data, dict):
            raise AgentUnavailable("the agent's answer is not a JSON object")
        return data

    def _get(self, path: str) -> Any:
        return self._request("GET", path, token=self.events_token, timeout=15.0)

    # ---- owner calls (AGENT_TOKEN) -------------------------------------------------------------

    def chat(self, message: str) -> dict[str, Any]:
        """``POST /chat {message}`` -> ``ChatResponse`` (untrusted)."""
        return self._post("/chat", {"message": message}, timeout=120.0)

    def mandate_activated(self, mandate_id: str, tx_hash: str, draft_id: str) -> dict[str, Any]:
        """``POST /mandates/activated``: the agent verifies the mandate on-chain itself."""
        return self._post("/mandates/activated", {"mandateId": mandate_id, "txHash": tx_hash, "draftId": draft_id})

    def approval_executed(self, order_id: str) -> dict[str, Any]:
        """``POST /approvals/{orderId}/executed-request``: the approval is mined; the agent asks the signer."""
        return self._post(f"/approvals/{order_id}/executed-request", timeout=180.0)

    def approval_rejected(self, order_id: str) -> dict[str, Any]:
        """``POST /approvals/{orderId}/rejected``: the owner rejected; no transaction."""
        return self._post(f"/approvals/{order_id}/rejected")

    def bad_data(self, order_id: str, owner_signature: str) -> dict[str, Any]:
        """``POST /reports/bad-data {orderId, ownerSignature}``."""
        return self._post("/reports/bad-data", {"orderId": order_id, "ownerSignature": owner_signature}, timeout=180.0)

    # ---- read-only calls (EVENTS_TOKEN) --------------------------------------------------------

    def state(self) -> dict[str, Any]:
        data = self._get("/state")
        if not isinstance(data, dict):
            raise AgentUnavailable("the agent's state is not a JSON object")
        return data

    def draft(self, draft_id: str) -> dict[str, Any] | None:
        """The agent's stored draft ``draft_id`` (only its ids, numbers and purpose are used)."""
        drafts = self.state().get("drafts")
        if isinstance(drafts, dict):
            found = drafts.get(draft_id)
            return found if isinstance(found, dict) else None
        return None

    def approval_invoice(self, order_id: str) -> Any:
        """The signed invoice the agent holds for ``order_id`` (verified by the caller), or None."""
        oid = order_id.lower()
        try:
            approvals = self.state().get("approvals") or []
        except AgentUnavailable:
            approvals = []
        for item in approvals if isinstance(approvals, list) else []:
            if isinstance(item, dict) and str(item.get("orderId", "")).lower() == oid:
                return item.get("invoice")
        found = self._get(f"/decisions/{oid}")
        if isinstance(found, dict):
            return found.get("invoice")
        return None


__all__ = ["DEFAULT_AGENT_PORT", "LOOPBACK", "AgentClient", "AgentUnavailable", "agent_base_url"]
