"""Helpers of the owner console tests (WP7): the fake agent API, the test client and fixture readers.

- :class:`FakeAgent` stands in for the wallet agent API (spec 8.8) with recorded answers; it records
  every notification P1 sends (``/mandates/activated``, ``/approvals/.../executed-request``, ...).
- :func:`make_client`: a FastAPI ``TestClient`` on ``http://127.0.0.1:8700`` for a given console.
- :func:`login`: signs in and returns the CSRF token of the page.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PASSWORD = "owner-test-password"
SECRET = "s" * 40


class FakeAgent:
    """The agent API as P1 sees it; answers are recorded fixtures or set by the test."""

    def __init__(self, *, state: dict[str, Any] | None = None, chat_answer: dict[str, Any] | None = None):
        self.state_data = state if state is not None else {"drafts": {}, "approvals": [], "messages": [], "llm": {}}
        self.chat_answer = chat_answer if chat_answer is not None else {"reply": "ok", "cards": [], "llm": {}}
        self.calls: list[tuple[str, Any]] = []
        self.invoices: dict[str, Any] = {}

    def chat(self, message: str) -> dict[str, Any]:
        self.calls.append(("chat", message))
        return json.loads(json.dumps(self.chat_answer))

    def state(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.state_data))

    def draft(self, draft_id: str) -> dict[str, Any] | None:
        return self.state().get("drafts", {}).get(draft_id)

    def approval_invoice(self, order_id: str) -> Any:
        return self.invoices.get(order_id.lower())

    def mandate_activated(self, mandate_id: str, tx_hash: str, draft_id: str) -> dict[str, Any]:
        self.calls.append(("activated", {"mandateId": mandate_id, "txHash": tx_hash, "draftId": draft_id}))
        return {"status": "active", "mandateId": mandate_id}

    def approval_executed(self, order_id: str) -> dict[str, Any]:
        self.calls.append(("executed", order_id))
        return {"status": "executed", "orderId": order_id}

    def approval_rejected(self, order_id: str) -> dict[str, Any]:
        self.calls.append(("rejected", order_id))
        return {"status": "closed", "orderId": order_id}

    def bad_data(self, order_id: str, owner_signature: str) -> dict[str, Any]:
        self.calls.append(("bad_data", {"orderId": order_id, "ownerSignature": owner_signature}))
        return {"status": "executed"}

    def calls_of(self, kind: str) -> list[Any]:
        return [c[1] for c in self.calls if c[0] == kind]


def read_events(name: str) -> list[dict[str, Any]]:
    path = FIXTURES / name
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def make_client(console: Any, merger: Any = None) -> Any:
    from fastapi.testclient import TestClient

    from acb.owner.app import create_app

    app = create_app(console, password=PASSWORD, session_secret=SECRET, merger=merger)
    return TestClient(app, base_url="http://127.0.0.1:8700")


def login(client: Any) -> str:
    """Sign in; returns the CSRF token carried by the page."""
    r = client.post("/login", json={"password": PASSWORD})
    assert r.status_code == 200, r.text
    page = client.get("/").text
    m = re.search(r'<meta name="acb-csrf" content="([0-9a-f]*)">', page)
    assert m and m.group(1), "the signed-in page carries the CSRF token"
    return m.group(1)


def node_available() -> str | None:
    return shutil.which("node")


PLACEHOLDERS = ("ATLAS", "SLOWPOKE", "GHOST", "ORION", "NOVA", "WALLET", "VAULT")


def recorded(name: str, dep: Any = None) -> Any:
    """A recorded agent API answer of ``fixtures/<name>``; with ``dep`` the placeholders (``"ATLAS"``,
    ``"VAULT"``, ...) become the agent ids and vault address of that deployment."""
    text = (FIXTURES / name).read_text(encoding="utf-8")
    if dep is not None:
        for key in PLACEHOLDERS:
            if key == "VAULT":
                value = dep.vaults["wallet"].address
            elif key == "WALLET":
                value = str(int(dep.vaults["wallet"].agent_id))
            else:
                value = str(int(dep.agents[key.lower()].agent_id))
            text = text.replace(f'"{key}"', json.dumps(value))
    return json.loads(text)
