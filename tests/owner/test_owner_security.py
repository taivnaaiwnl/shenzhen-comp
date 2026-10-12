"""Owner console HTTP security (spec 11.1): login, sessions, CSRF, CSP and nosniff on every response,
JSON-only POSTs, loopback Host only, login rate limit, no route for the agent's tokens."""

from __future__ import annotations

import pytest
from owner_helpers import PASSWORD, login, make_client

from acb.owner.security import CSP, LoginLimiter, Sessions, check_config, password_ok

STATE_CHANGING = [
    ("/api/chat", {"message": "status"}),
    ("/api/mandates/d-1/prepare", {}),
    ("/api/mandates/d-1/confirm", {"prepareId": "p-1"}),
    ("/api/mandates/0x" + "11" * 32 + "/revoke", {}),
    ("/api/approvals/0x" + "22" * 32 + "/prepare", {}),
    ("/api/approvals/0x" + "22" * 32 + "/approve", {"prepareId": "p-1"}),
    ("/api/approvals/0x" + "22" * 32 + "/reject", {}),
    ("/api/reports/0x" + "22" * 32 + "/bad-data", {}),
    ("/api/freeze", {}),
    ("/api/unfreeze", {"toTier": 0, "rotateTo": None}),
    ("/api/actions/tx-1/submitted", {"txHash": "0x" + "33" * 32}),
]


def assert_headers(response) -> None:
    assert response.headers.get("content-security-policy") == CSP
    assert response.headers.get("x-content-type-options") == "nosniff"


def test_csp_is_the_spec_value():
    assert CSP == "default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"


def test_every_response_carries_csp_and_nosniff(offline_console):
    client = make_client(offline_console)
    responses = [
        client.get("/"),
        client.get("/app.js"),
        client.get("/style.css"),
        client.get("/health"),
        client.get("/api/session"),
        client.get("/api/snapshot"),
        client.get("/stream"),
        client.get("/does-not-exist"),
        client.post("/login", json={"password": "wrong"}),
        client.post("/api/freeze", json={}),
        client.get("/events"),
        client.get("/", headers={"host": "evil.example"}),
    ]
    for r in responses:
        assert_headers(r)
    assert responses[1].headers["content-type"].startswith("text/javascript")
    assert responses[2].headers["content-type"].startswith("text/css")


def test_page_without_session_has_no_csrf_token(offline_console):
    client = make_client(offline_console)
    page = client.get("/").text
    assert '<meta name="acb-csrf" content="">' in page
    assert client.get("/api/session").json() == {"authenticated": False}


def test_login_sets_strict_httponly_cookie(offline_console):
    client = make_client(offline_console)
    r = client.post("/login", json={"password": PASSWORD})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert client.get("/api/session").json() == {"authenticated": True}


def test_wrong_password_and_rate_limit(offline_console):
    client = make_client(offline_console)
    for _ in range(5):
        assert client.post("/login", json={"password": "nope"}).status_code == 401
    r = client.post("/login", json={"password": PASSWORD})
    assert r.status_code == 429
    assert client.get("/api/session").json() == {"authenticated": False}


def test_login_requires_json(offline_console):
    client = make_client(offline_console)
    r = client.post("/login", data={"password": PASSWORD})
    assert r.status_code == 415


@pytest.mark.parametrize(("path", "body"), STATE_CHANGING)
def test_state_changing_endpoints_need_session(offline_console, path, body):
    client = make_client(offline_console)
    r = client.post(path, json=body)
    assert r.status_code == 401
    assert_headers(r)


@pytest.mark.parametrize(("path", "body"), STATE_CHANGING)
def test_state_changing_endpoints_need_csrf(offline_console, path, body):

    client = make_client(offline_console)
    token = login(client)
    assert client.post(path, json=body).status_code == 403
    assert client.post(path, json=body, headers={"X-ACB-CSRF": "0" * 64}).status_code == 403
    assert client.post(path, json=body, headers={"X-ACB-CSRF": token[:-1]}).status_code == 403
    r = client.post(path, json=body, headers={"X-ACB-CSRF": token})
    assert r.status_code not in (401, 403), r.text
    assert_headers(r)


def test_csrf_token_is_bound_to_the_session(offline_console):

    a = make_client(offline_console)
    b = make_client(offline_console)
    token_a = login(a)
    login(b)
    r = b.post("/api/freeze", json={}, headers={"X-ACB-CSRF": token_a})
    assert r.status_code == 403


def test_cross_origin_and_form_posts_are_refused(offline_console):

    client = make_client(offline_console)
    token = login(client)
    headers = {"X-ACB-CSRF": token}
    r = client.post("/api/chat", json={"message": "hi"}, headers={**headers, "origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/chat", content="message=hi", headers={**headers, "content-type": "text/plain"})
    assert r.status_code == 415
    r = client.post("/api/chat", json={"message": "hi"}, headers={**headers, "origin": "http://127.0.0.1:8700"})
    assert r.status_code == 200


def test_host_header_must_be_loopback(offline_console):
    client = make_client(offline_console)
    assert client.get("/", headers={"host": "attacker.example"}).status_code == 421
    assert client.get("/", headers={"host": "127.0.0.1:8700"}).status_code == 200
    assert client.get("/", headers={"host": "localhost:8700"}).status_code == 200


def test_logout_ends_the_session(offline_console):

    client = make_client(offline_console)
    token = login(client)
    assert client.post("/logout", json={}, headers={"X-ACB-CSRF": token}).status_code == 200
    assert client.get("/api/session").json() == {"authenticated": False}
    assert client.post("/api/freeze", json={}, headers={"X-ACB-CSRF": token}).status_code == 401


def test_forged_cookie_is_rejected(offline_console):
    client = make_client(offline_console)
    client.cookies.set("acb_owner_session", "forged.session")
    assert client.get("/api/session").json() == {"authenticated": False}


def test_sessions_sign_and_expire():
    now = [1000.0]
    s = Sessions("x" * 32, ttl_s=10, clock=lambda: now[0])
    session, cookie = s.create()
    assert s.get(cookie) is session
    sid, _, mac = cookie.rpartition(".")
    assert s.get(f"{sid}.{mac[:-1]}A") is None
    assert Sessions("y" * 32).get(cookie) is None
    now[0] += 11
    assert s.get(cookie) is None
    assert Sessions.csrf_ok(session, session.csrf)
    assert not Sessions.csrf_ok(session, "")
    assert not Sessions.csrf_ok(None, session.csrf)


def test_password_and_config_rules():
    assert password_ok("abc", "abc")
    assert not password_ok("abc", "abd")
    assert not password_ok("", "")
    with pytest.raises(RuntimeError):
        check_config("", "s" * 32)
    with pytest.raises(RuntimeError):
        check_config("long-enough", "short")
    check_config("long-enough", "s" * 32)
    limiter = LoginLimiter(limit=2, window_s=60, clock=lambda: 0.0)
    limiter.failed()
    assert not limiter.blocked()
    limiter.failed()
    assert limiter.blocked()


def test_serve_refuses_without_password(monkeypatch, capsys):
    from acb.cli import main

    monkeypatch.delenv("OWNER_UI_PASSWORD", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "s" * 40)
    assert main(["owner", "serve", "--network", "anvil", "--no-feeds"]) == 78
    assert "OWNER_UI_PASSWORD" in capsys.readouterr().err


def test_serve_binds_loopback_only():
    from acb.owner.app import serve

    with pytest.raises(SystemExit):
        serve(object(), host="0.0.0.0", port=1)


def test_chat_proxy_without_agent_answer(offline_console):
    """The chat proxy needs a session and CSRF; the agent's reply comes back as data, cards rebuilt."""

    client = make_client(offline_console)
    token = login(client)
    offline_console.agent.chat_answer = {
        "reply": "<b>hi</b>",
        "cards": [{"type": "mandate_draft", "draftId": "d-1"}],
        "llm": {"mode": "mock", "model": "mock", "fallback": True},
    }
    r = client.post("/api/chat", json={"message": "hello"}, headers={"X-ACB-CSRF": token})
    assert r.status_code == 200
    body = r.json()
    assert body["reply"] == "<b>hi</b>"
    assert body["llm"]["fallback"] is True
    # without a chain the draft cannot be rebuilt: it is not shown as a confirmable card
    assert body["cards"] == [{"type": "unverified", "of": "mandate_draft", "error": body["cards"][0]["error"]}]
    assert offline_console.agent.calls_of("chat") == ["hello"]
    r = client.post("/api/chat", json={"message": "x" * 2001}, headers={"X-ACB-CSRF": token})
    assert r.status_code == 422
