"""Persistent agent state (spec 6.2: quote requests, seen invoices and pending orders survive a restart)."""

from __future__ import annotations

import json

import pytest

from acb.agent.store import STATE_FILE, AgentStore, prepare_state_dir


def test_state_survives_a_restart(tmp_path):
    store = AgentStore(tmp_path)
    with store.edit() as data:
        data["quoteRequests"]["qr-1"] = {"quoteRequestId": "qr-1"}
        data["seenInvoices"]["7:inv-1"] = "0x" + "aa" * 32
        data["orders"]["0x" + "bb" * 32] = {"status": "in_flight"}
    again = AgentStore(tmp_path)
    assert again.read("quoteRequests") == {"qr-1": {"quoteRequestId": "qr-1"}}
    assert again.read("seenInvoices") == {"7:inv-1": "0x" + "aa" * 32}
    assert again.pending_order_ids() == frozenset({"0x" + "bb" * 32})
    assert again.pending_order_ids(exclude="0x" + "bb" * 32) == frozenset()
    raw = (tmp_path / STATE_FILE).read_bytes()
    assert b"\r\n" not in raw and not raw.startswith(b"\xef\xbb\xbf")
    json.loads(raw)


def test_a_failed_edit_leaves_the_state_unchanged(tmp_path):
    store = AgentStore(tmp_path)
    with store.edit() as data:
        data["reports"]["a"] = "1"
    with pytest.raises(RuntimeError), store.edit() as data:
        data["reports"]["b"] = "2"
        raise RuntimeError("boom")
    assert store.read("reports") == {"a": "1"}
    assert AgentStore(tmp_path).read("reports") == {"a": "1"}


def test_floats_never_reach_the_file(tmp_path):
    store = AgentStore(tmp_path)
    with pytest.raises(TypeError), store.edit() as data:
        data["reports"]["x"] = 0.5
    assert "x" not in AgentStore(tmp_path).read("reports")


def test_prepare_state_dir_archives_another_deployment(tmp_path):
    folder = prepare_state_dir(tmp_path / "agent", "0x01")
    AgentStore(folder).put_order("0x" + "cc" * 32, {"status": "awaiting_delivery"})
    same = prepare_state_dir(tmp_path / "agent", "0x01")
    assert AgentStore(same).order("0x" + "cc" * 32) is not None
    fresh = prepare_state_dir(tmp_path / "agent", "0x02")
    assert AgentStore(fresh).order("0x" + "cc" * 32) is None
    assert (tmp_path / "agent" / "archive" / "1" / STATE_FILE).is_file()
