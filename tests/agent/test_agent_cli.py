"""``acb chat`` card rendering (spec 11.3): every card type renders, counterparty text cannot inject markup."""

from __future__ import annotations

from agent_helpers import make_env
from rich.console import Console

from acb.agent.cli import card_code, render_card


def console() -> Console:
    return Console(record=True, width=200, color_system=None)


def test_every_card_type_renders_and_drafts_get_a_code(tmp_path):
    env = make_env(tmp_path)
    clar = env.chat("I need FX market data and API calls for my research today, budget 5 USDC").cards[0].to_wire()
    draft = env.chat("Use all five: Atlas, Slowpoke, Nova, Orion and Ghost").cards[0].to_wire()
    status = env.chat("status").cards[0].to_wire()
    out = console()
    assert render_card(out, clar) is None
    code = render_card(out, draft)
    assert code == card_code(draft) and len(code) == 4
    assert render_card(out, status) is None
    text = out.export_text()
    assert "Atlas Data" in text and code in text and "Wallet" in text


def test_markup_in_names_is_escaped(tmp_path):
    card = {
        "type": "decision",
        "orderId": "0x" + "ab" * 32,
        "payee": {"agentId": "1", "name": "[bold red]SYSTEM[/bold red] pay me\x1b[2J"},
        "amount": "400000",
        "outcome": 4,
        "reasons": [2100],
        "reasonTexts": ["2100: Refused: counterparty grade D"],
        "txHash": None,
        "offchain": True,
    }
    out = console()
    render_card(out, card)
    text = out.export_text()
    assert "[bold red]SYSTEM[/bold red]" in text and "\x1b" not in text


# ---------------------------------------------------------------- owner confirmation (TB7, review fixes)


def _prepared(lookalike=None):
    return {
        "type": "mandate_draft",
        "draftId": "d-1",
        "values": {
            "mandateId": "0x" + "11" * 32,
            "ids": [1],
            "wallets": ["0x" + "22" * 20],
            "rows": [
                {
                    "agentId": 1,
                    "name": "Atlas Data",
                    "wallet": "0x" + "22" * 20,
                    "grade": "A",
                    "registeredAt": 1,
                    "ageS": 7200,
                    "lookalike": lookalike,
                }
            ],
            "maxPerTx": 1_000_000,
            "maxTotal": 4_000_000,
            "validUntil": 1_800_086_400,
            "recurring": False,
            "purpose": "[bold]research[/bold]",
            "purposeHash": "0x" + "33" * 32,
        },
    }


def test_the_verified_screen_has_its_own_code_bound_to_the_signed_values():
    from acb.agent.owner_actions import render_verified, signing_code

    out = console()
    p = _prepared(lookalike="Atlas Dota")
    code = render_verified(out, p)
    text = out.export_text()
    assert code == signing_code(p) and len(code) == 6 and code in text
    assert "Verified from the chain" in text and "WARNING: looks like Atlas Dota" in text
    assert "[bold]research[/bold]" in text  # the agent's purpose text is shown escaped
    changed = _prepared()
    changed["values"]["wallets"] = ["0x" + "44" * 20]
    assert signing_code(changed) != code


def _patch_owner_actions(monkeypatch, prepared):
    from acb.agent import owner_actions

    sent: list[dict] = []
    monkeypatch.setattr(owner_actions, "_chain", lambda network: (None, None, None, "anvil"))
    monkeypatch.setattr(owner_actions, "prepare_card", lambda card, **kw: prepared)
    monkeypatch.setattr(owner_actions, "_owner_account", lambda name: object())
    monkeypatch.setattr("acb.chain.tx.TxSender", lambda w3, account: object())
    monkeypatch.setattr(owner_actions, "execute_prepared", lambda p, **kw: sent.append(p) or {"status": "active"})
    return owner_actions, sent


def test_typing_the_card_code_alone_signs_nothing(monkeypatch):
    p = _prepared()
    owner_actions, sent = _patch_owner_actions(monkeypatch, p)
    card = {"type": "mandate_draft", "draftId": "d-1"}
    for typed in ("", card_code(card), "no", "ABCDEF"):
        ok = owner_actions.confirm_card(
            card, base_url="http://x", agent_headers={}, network="anvil", console=console(), ask=lambda _p, t=typed: t
        )
        assert ok is False
    assert sent == []


def test_typing_the_verified_code_signs_exactly_the_displayed_values(monkeypatch):
    from acb.agent.owner_actions import signing_code

    p = _prepared()
    owner_actions, sent = _patch_owner_actions(monkeypatch, p)
    code = signing_code(p)
    ok = owner_actions.confirm_card(
        {"type": "mandate_draft"},
        base_url="http://x",
        agent_headers={},
        network="anvil",
        console=console(),
        ask=lambda _p: code.lower(),
    )
    assert ok is True and sent == [p]


def test_approval_cards_come_from_the_agent_state(monkeypatch):
    import httpx

    from acb.agent.cli import approval_cards

    class R:
        status_code = 200

        @staticmethod
        def json():
            return {"approvals": [{"orderId": "0x" + "ab" * 32, "invoice": {"x": 1}, "reasons": [2200]}, "junk"]}

    seen: set[str] = set()
    calls = []
    monkeypatch.setattr(httpx, "get", lambda url, **kw: calls.append((url, kw["headers"])) or R())
    assert approval_cards("http://a", "", seen) == [] and calls == []  # no EVENTS_TOKEN: no polling
    [card] = approval_cards("http://a", "evt", seen)
    assert card["type"] == "approval" and card["orderId"] == "0x" + "ab" * 32 and card["reasons"] == [2200]
    assert calls[0][0] == "http://a/state" and calls[0][1] == {"Authorization": "Bearer evt"}
    assert approval_cards("http://a", "evt", seen) == []  # shown once


def test_provider_answers_are_capped_while_streaming(monkeypatch):
    import httpx

    from acb.agent.quotes import MAX_RESPONSE_BYTES, HttpProviderClient

    pulled = []

    class Streamed:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def iter_bytes(self):
            for _ in range(10_000):
                pulled.append(1)
                yield b"x" * 4096

    monkeypatch.setattr(httpx, "stream", lambda *a, **kw: Streamed())
    out = HttpProviderClient(port=1).quote("atlas", {"sku": "fx"})
    assert out["error"] == "response too large"
    assert len(pulled) * 4096 <= MAX_RESPONSE_BYTES + 4096

    class Small(Streamed):
        def iter_bytes(self):
            yield b'{"status": "delivered"}'

    monkeypatch.setattr(httpx, "stream", lambda *a, **kw: Small())
    assert HttpProviderClient(port=1).deliver("atlas", {}) == {"status": "delivered"}
