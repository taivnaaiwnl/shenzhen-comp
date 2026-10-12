"""Client of the signer API (spec 7.2) used by the wallet agent.

The agent never signs: it sends an :class:`~acb.types.ExecuteRequest` with ``Authorization: Bearer
<SIGNER_TOKEN>`` to ``http://127.0.0.1:<SIGNER_PORT>/v1/execute``. Request ids are fresh UUID4 strings;
when a call times out or the connection drops, the client asks ``GET /v1/requests/{id}`` (the signer
never executes a request id twice) before it gives up, so a retry can never pay twice.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Protocol

from acb.types import ExecuteRequest, ExecuteResponse


class SignerAPI(Protocol):
    """``execute(req) -> ExecuteResponse``; never raises for a refusal (status says so)."""

    def execute(self, req: ExecuteRequest) -> ExecuteResponse: ...


class SignerUnavailable(RuntimeError):
    """The signer could not be reached and the request's outcome is unknown."""


def new_request_id() -> str:
    """A lowercase UUID4 string (the signer's request id format)."""
    return str(uuid.uuid4())


class HttpSignerClient:
    """:class:`SignerAPI` over HTTP (``127.0.0.1`` only)."""

    def __init__(self, port: int | None = None, *, token: str | None = None, timeout_s: float = 150.0):
        port = int(port or os.environ.get("SIGNER_PORT") or 8702)
        self.base_url = f"http://127.0.0.1:{port}"
        self._token = token if token is not None else os.environ.get("SIGNER_TOKEN", "")
        self.timeout_s = timeout_s

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def execute(self, req: ExecuteRequest) -> ExecuteResponse:
        import httpx

        body = req.to_wire()
        try:
            response = httpx.post(
                f"{self.base_url}/v1/execute", json=body, headers=self._headers(), timeout=self.timeout_s
            )
        except httpx.HTTPError as exc:
            recovered = self.lookup(req.request_id)
            if recovered is not None:
                return recovered
            raise SignerUnavailable(f"signer unreachable: {type(exc).__name__}") from None
        return self._response(req, response)

    def lookup(self, request_id: str) -> ExecuteResponse | None:
        """``GET /v1/requests/{id}``: the stored response of an earlier request, or None."""
        import httpx

        try:
            response = httpx.get(f"{self.base_url}/v1/requests/{request_id}", headers=self._headers(), timeout=15.0)
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            return ExecuteResponse.model_validate(response.json())
        except ValueError:
            return None

    def _response(self, req: ExecuteRequest, response: Any) -> ExecuteResponse:
        if response.status_code == 200:
            return ExecuteResponse.model_validate(response.json())
        try:
            detail = response.json()
        except ValueError:
            detail = {}
        codes = tuple(int(c) for c in (detail.get("reasonCodes") or [4104])) if isinstance(detail, dict) else (4104,)
        return ExecuteResponse(
            request_id=req.request_id,
            status="rejected",
            decision=None,
            tx=(),
            violation=None,
            reason_codes=codes,
            log_seq=0,
        )


__all__ = ["HttpSignerClient", "SignerAPI", "SignerUnavailable", "new_request_id"]
