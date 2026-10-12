"""Browserless rendering of the owner console (spec 11.1, 11.2): the page renders the five panels from
``tests/owner/fixtures/events.jsonl``, and the XSS fixture (memo, agent name, LLM reply with script
tags) renders as text: no script node, no handler attribute, no ``javascript:`` link.

The DOM is a minimal shim in ``dom_harness.js`` run by Node; the tests skip when Node is missing.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from owner_helpers import FIXTURES, node_available, read_events

REPO = Path(__file__).resolve().parents[2]
STATIC = REPO / "src" / "acb" / "owner" / "static"
HARNESS = Path(__file__).resolve().parent / "dom_harness.js"

pytestmark = pytest.mark.skipif(node_available() is None, reason="node is not installed (browserless DOM harness)")


def items_of(events: list[dict], start: int = 1) -> list[dict]:
    return [{"id": start + i, "feed": e["service"], "event": e} for i, e in enumerate(events)]


def render(tmp_path: Path, scenario: dict) -> dict:
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(scenario), encoding="utf-8")
    proc = subprocess.run(
        [node_available(), str(HARNESS), str(STATIC / "app.js"), str(STATIC / "index.html"), str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["errors"] == [], out["errors"]
    return out


def snapshot_fixture() -> dict:
    return json.loads((FIXTURES / "snapshot.json").read_text(encoding="utf-8"))


def test_five_panels_render_from_the_event_fixture(tmp_path):
    """No chain data (a replay): every panel is filled from the merged event feed alone."""
    from acb.owner.chain_views import reason_catalogue

    snapshot = {"reasonTexts": reason_catalogue(), "errors": ["no chain"]}
    out = render(tmp_path, {"snapshot": snapshot, "items": items_of(read_events("events.jsonl"))})
    p = out["panels"]
    assert out["violations"] == []
    # 1 wallet: tier and the registered mandate from the feed
    assert "Tier" in p["wallet-body"] and "mandate" in p["wallet-body"] and "4.00 USDC" in p["wallet-body"]
    # 2 grades: Atlas A, Slowpoke B, Ghost D with reason texts
    assert "742" in p["grades-body"] and "410" in p["grades-body"]
    assert "Atlas Data" in p["grades-body"] and "Ghost Feeds" in p["grades-body"]
    assert "1002: Orders were paid but not delivered" in p["grades-body"]
    # 3 decision log: newest first, reason texts, off-chain drop, no duplicate of the signer's PAY
    d = p["decisions-body"]
    assert "Ghost Feeds" in d and "REFUSE" in d and "PAY_CAPPED" in d and "DROPPED" in d and "off-chain" in d
    assert d.index("DROPPED") < d.index("PAY_CAPPED"), "newest first"
    assert d.count("Atlas Data") == 1, "the agent's and the signer's event of one decision are merged"
    # 4 autonomy timeline: promotion 1 -> 2 and the violation
    assert "Tier 1 -> Tier 2" in p["autonomy-body"] and "PROVENANCE_AMOUNT" in p["autonomy-body"]
    # 5 messages: the signed message and the dropped invoice, as untrusted text
    assert "Please confirm your API key delivery address." in p["messages-body"]
    assert "Dropped invoice" in p["messages-body"] and "Limited offer" in p["messages-body"]
    # the approval request appears in the chat column with a review button
    assert any(b["text"].startswith("Review") for b in out["buttons"])


def test_five_panels_render_from_a_snapshot(tmp_path):
    snap = snapshot_fixture()
    out = render(tmp_path, {"snapshot": snap, "items": items_of(read_events("events.jsonl"))})
    p = out["panels"]
    assert out["violations"] == []
    assert "15.00 USDC" in p["wallet-body"] and "Reserve floor" in p["wallet-body"]
    assert "Revoke" in [b["text"] for b in out["buttons"]]
    assert "Freeze wallet" in [b["text"] for b in out["buttons"]]
    assert "Atlas Data" in p["grades-body"] and "acb bureau recompute" in p["grades-body"]
    assert "Tier 1 -> Tier 2" in p["autonomy-body"] and "3000: Promoted one tier" in p["autonomy-body"]
    assert "clean settlements 1 of 6" in p["autonomy-body"]
    assert "network: anvil" in p["banner-network"]
    assert "LLM: mock careful" in p["banner-llm"]


def test_frozen_wallet_offers_unfreeze_with_rotation(tmp_path):
    snap = snapshot_fixture()
    snap["wallet"]["frozen"] = True
    snap["wallet"]["tier"] = 255
    out = render(tmp_path, {"snapshot": snap, "items": []})
    assert "FROZEN" in out["panels"]["wallet-body"]
    assert "Unfreeze" in [b["text"] for b in out["buttons"]]
    assert "Freeze wallet" not in [b["text"] for b in out["buttons"]]


def test_xss_fixture_renders_as_text(tmp_path):
    xss = read_events("xss_events.jsonl")
    chat = json.loads((FIXTURES / "chat_response_xss.json").read_text(encoding="utf-8"))
    snap = snapshot_fixture()
    snap["agent"]["messages"] = [
        {
            "messageId": "m",
            "from": {"agentId": "9", "name": "<script>x()</script>"},
            "text": "<img src=x onerror=y>",
            "flagged": True,
            "detectorRules": ["<b>"],
        }
    ]
    snap["grades"][0]["name"] = "<script>window.__pwned=7</script>"
    snap["grades"][0]["evidenceUri"] = "javascript:alert(8)"
    scenario = {
        "snapshot": snap,
        "items": items_of(xss[:3]),
        "live": items_of(xss[3:], start=100),
        "chat": [chat],
    }
    out = render(tmp_path, scenario)
    assert out["violations"] == []
    assert out["scriptNodes"] == 0, out["tags"]
    assert out["pwned"] == 0
    assert set(out["tags"]) <= {
        "DIV",
        "SPAN",
        "TABLE",
        "THEAD",
        "TBODY",
        "TR",
        "TH",
        "TD",
        "A",
        "DL",
        "DT",
        "DD",
        "STRONG",
        "OL",
        "LI",
        "UL",
        "BUTTON",
        "PROGRESS",
        "P",
        "H3",
        "SELECT",
        "OPTION",
        "INPUT",
    }
    assert all(link is None or link.startswith("https://") for link in out["links"]), out["links"]
    text = " ".join(v or "" for v in out["panels"].values())
    # the payloads are visible as plain text
    assert "<script>window.__pwned=1</script>" in text
    assert '<img src=x onerror="window.__pwned=2">' in text
    assert "<script>window.__pwned=7</script>" in text
    assert "<img src=x onerror=y>" in text


def test_unverified_and_verified_cards(tmp_path):
    cards = [
        {"type": "unverified", "of": "mandate_draft", "error": "<script>bad()</script>"},
        {
            "type": "mandate",
            "draftId": "d-1",
            "prepareId": "p-1",
            "code": "ABC123",
            "confirmable": False,
            "validHours": 24,
            "purpose": "<b>FX data</b>",
            "values": {
                "mandateId": "0x" + "11" * 32,
                "maxPerTx": "1000000",
                "maxTotal": "4000000",
                "validUntil": 1792222222,
                "recurring": False,
            },
            "payees": [
                {
                    "agentId": "666",
                    "name": "unknown agent #666",
                    "inDirectory": False,
                    "grade": "NONE",
                    "liveWallet": None,
                    "lookalikeOf": None,
                }
            ],
            "problems": ["agent #666 is not in the pinned directory"],
            "warnings": [],
        },
    ]
    out = render(tmp_path, {"snapshot": None, "items": [], "cards": cards})
    chat = out["panels"]["chat-log"]
    assert "could not be verified" in chat and "<script>bad()</script>" in chat
    assert "NOT IN THE PINNED DIRECTORY" in chat and "Blocked: agent #666" in chat and "4.00 USDC" in chat
    confirm = [b for b in out["buttons"] if b["text"].startswith("Confirm")]
    assert confirm and confirm[0]["disabled"] is True, "a card with problems cannot be confirmed"
