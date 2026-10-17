"""Chunked ``eth_getLogs`` with retries against a mock provider that refuses ranges above 200 blocks
(spec 0.4 item 4, 4.11, 12.2), and the Web3 factory."""

from __future__ import annotations

import pytest
from chain_mocks import RpcError, mock_web3
from eth_abi import encode
from eth_utils import keccak
from web3 import Web3

from acb.chain import abi
from acb.chain.client import (
    chunk_blocks_of,
    get_logs_chunked,
    get_raw_logs_chunked,
    is_range_error,
    iter_chunks,
    make_web3,
    set_chunk_blocks,
)
from acb.types import NetworkProfile

LIMIT = 200
HEAD = 1000
CONTRACT = "0x" + "c0" * 20
POSTER_TOPIC = Web3.to_hex(keccak(text="PosterSet(address,bool)"))
POSTERS = ["0x" + "a1" * 20, "0x" + "b2" * 20]


def _log(block: int) -> dict:
    poster = POSTERS[block % 2]
    return {
        "address": CONTRACT,
        "topics": [POSTER_TOPIC, "0x" + "00" * 12 + poster[2:]],
        "data": Web3.to_hex(encode(["bool"], [True])),
        "blockNumber": hex(block),
        "blockHash": "0x" + f"{block:064x}",
        "transactionHash": "0x" + f"{block + 1:064x}",
        "transactionIndex": "0x0",
        "logIndex": "0x0",
        "removed": False,
    }


EVENT_BLOCKS = [b for b in range(HEAD + 1) if b % 37 == 0]


class LimitedNode:
    """Mock node: logs at every 37th block; ``eth_getLogs`` over more than 200 blocks fails with -32614."""

    def __init__(self, transient_failures: int = 0):
        self.ranges: list[tuple[int, int]] = []
        self.transient_failures = transient_failures

    def get_logs(self, params):
        flt = params[0]
        start, end = int(flt["fromBlock"], 16), int(flt["toBlock"], 16)
        self.ranges.append((start, end))
        if end - start + 1 > LIMIT:
            raise RpcError(-32614, "eth_getLogs is limited to a 200 range")
        if self.transient_failures:
            self.transient_failures -= 1
            raise RpcError(-32000, "upstream timeout")
        wanted = None
        topics = flt.get("topics") or []
        if len(topics) > 1 and topics[1]:
            wanted = {t.lower() for t in (topics[1] if isinstance(topics[1], list) else [topics[1]])}
        out = []
        for b in EVENT_BLOCKS:
            if start <= b <= end:
                log = _log(b)
                if wanted is None or log["topics"][1] in wanted:
                    out.append(log)
        return out

    def web3(self) -> Web3:
        return mock_web3(
            {
                "eth_chainId": lambda p: "0x14a34",
                "eth_blockNumber": lambda p: hex(HEAD),
                "eth_getBlockByNumber": lambda p: {
                    "number": hex(HEAD),
                    "timestamp": hex(1_700_000_000),
                    "hash": "0x" + "ee" * 32,
                    "parentHash": "0x" + "dd" * 32,
                },
                "eth_getLogs": self.get_logs,
            }
        )


def _event(w3: Web3):
    return w3.eth.contract(
        address=Web3.to_checksum_address(CONTRACT), abi=abi.abi_of("AgentCreditBureau")
    ).events.PosterSet


def test_chunked_logs_respect_200_block_limit():
    node = LimitedNode()
    w3 = node.web3()
    sleeps: list[float] = []
    logs = get_logs_chunked(_event(w3), 0, HEAD, chunk_blocks=200, sleep=sleeps.append)
    assert [log["blockNumber"] for log in logs] == EVENT_BLOCKS
    assert all(end - start + 1 <= LIMIT for start, end in node.ranges)
    assert node.ranges[0] == (0, 199) and node.ranges[-1] == (1000, 1000)
    assert len(node.ranges) == 6 and sleeps == []
    assert logs[0]["args"]["poster"].lower() == POSTERS[0]


def test_unchunked_query_would_fail_and_oversized_chunks_are_split():
    node = LimitedNode()
    w3 = node.web3()
    event = _event(w3)
    with pytest.raises(Exception) as info:
        event.get_logs(from_block=0, to_block=HEAD)
    assert is_range_error(info.value)
    node.ranges.clear()
    logs = get_logs_chunked(event, 0, HEAD, chunk_blocks=5000, sleep=lambda _s: None)
    assert [log["blockNumber"] for log in logs] == EVENT_BLOCKS
    accepted = [r for r in node.ranges if r[1] - r[0] + 1 <= LIMIT]
    assert sum(e - s + 1 for s, e in accepted) == HEAD + 1


def test_default_chunk_comes_from_web3_or_is_200():
    node = LimitedNode()
    w3 = node.web3()
    assert chunk_blocks_of(w3) == 200
    get_logs_chunked(_event(w3), 0, "latest", sleep=lambda _s: None)
    assert max(e - s + 1 for s, e in node.ranges) == 200
    set_chunk_blocks(w3, 100)
    node.ranges.clear()
    get_logs_chunked(_event(w3), 0, 499, sleep=lambda _s: None)
    assert node.ranges == [(0, 99), (100, 199), (200, 299), (300, 399), (400, 499)]


def test_retries_with_backoff_then_succeeds():
    node = LimitedNode(transient_failures=2)
    w3 = node.web3()
    sleeps: list[float] = []
    logs = get_logs_chunked(_event(w3), 0, 150, chunk_blocks=200, sleep=sleeps.append)
    assert [log["blockNumber"] for log in logs] == [b for b in EVENT_BLOCKS if b <= 150]
    assert sleeps == [0.5, 1.0]


def test_gives_up_after_three_retries():
    node = LimitedNode(transient_failures=10)
    w3 = node.web3()
    sleeps: list[float] = []
    with pytest.raises(Exception, match="upstream timeout"):
        get_logs_chunked(_event(w3), 0, 150, chunk_blocks=200, sleep=sleeps.append)
    assert sleeps == [0.5, 1.0, 2.0]
    assert len(node.ranges) == 4


def test_argument_filters_are_passed_as_topics():
    node = LimitedNode()
    w3 = node.web3()
    logs = get_logs_chunked(
        _event(w3), 0, HEAD, {"poster": Web3.to_checksum_address(POSTERS[1])}, chunk_blocks=200, sleep=lambda _s: None
    )
    assert logs and all(log["args"]["poster"].lower() == POSTERS[1] for log in logs)
    assert [log["blockNumber"] for log in logs] == [b for b in EVENT_BLOCKS if b % 2 == 1]


def test_raw_logs_chunked_and_empty_range():
    node = LimitedNode()
    w3 = node.web3()
    raw = get_raw_logs_chunked(w3, CONTRACT, [POSTER_TOPIC], 0, HEAD, chunk_blocks=200, sleep=lambda _s: None)
    assert len(raw) == len(EVENT_BLOCKS)
    assert get_logs_chunked(_event(w3), 10, 9) == []


def test_iter_chunks():
    assert list(iter_chunks(5, 5, 200)) == [(5, 5)]
    assert list(iter_chunks(0, 401, 200)) == [(0, 199), (200, 399), (400, 401)]
    with pytest.raises(ValueError):
        list(iter_chunks(0, 1, 0))


def test_make_web3_purposes():
    net = NetworkProfile.load(
        "base_sepolia",
        {"BASE_SEPOLIA_RPC_URL": "http://127.0.0.1:1/rpc", "BASE_SEPOLIA_RPC_URL_2": "http://127.0.0.1:2/logs"},
    )
    rpc = make_web3(net)
    logs = make_web3(net, "logs")
    assert rpc.provider.endpoint_uri.endswith("/rpc")
    assert logs.provider.endpoint_uri.endswith("/logs")
    assert chunk_blocks_of(rpc) == 200 and chunk_blocks_of(logs) == 200
    with pytest.raises(ValueError):
        make_web3(net, "other")
    anvil = NetworkProfile.load("anvil", {"ANVIL_RPC_URL": "http://127.0.0.1:3"})
    assert chunk_blocks_of(make_web3(anvil, "logs")) == 5000
    fallback = NetworkProfile.load("base_sepolia", {"BASE_SEPOLIA_RPC_URL": "http://127.0.0.1:1/rpc"})
    assert make_web3(fallback, "logs").provider.endpoint_uri.endswith("/rpc")


def test_log_scans_need_a_start_block_on_public_networks():
    from acb.chain.contracts import VaultContract

    w3 = mock_web3({"eth_chainId": lambda p: hex(84532)})
    vault = VaultContract(w3, CONTRACT)
    with pytest.raises(ValueError, match="start_block"):
        vault._scan_start(None)
    assert vault._scan_start(123) == 123


class _LaggingRpc:
    """Two load-balanced nodes: the fast one has block 105 (our receipt), the slow one only 100."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list]] = []
        self.slow_left = 2  # the next two requests reach the slow node

    def __call__(self, method: str, params: list) -> dict:
        self.calls.append((method, list(params)))
        head = 100 if self.slow_left > 0 else 105
        self.slow_left -= 1
        if method == "eth_blockNumber":
            return {"jsonrpc": "2.0", "id": 1, "result": hex(head)}
        if method == "eth_getBlockByNumber":
            return {"jsonrpc": "2.0", "id": 1, "result": None if int(params[0], 16) > head else {"number": params[0]}}
        block = head if params[1] == "pending" else int(params[1], 16)
        if block > head:
            return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "header not found"}}
        return {"jsonrpc": "2.0", "id": 1, "result": "0x01"}


def _read_your_writes(monkeypatch, mark: int):
    from acb.chain import client, tx

    monkeypatch.setattr(tx, "highest_receipt_block", lambda chain_id: mark)
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    w3 = type("W3", (), {"_acb_chain_id": 84532})()
    rpc = _LaggingRpc()
    return rpc, client.ReadYourWrites(w3).wrap_make_request(rpc)


def test_block_number_never_answers_below_our_own_receipts(monkeypatch):
    rpc, request = _read_your_writes(monkeypatch, mark=105)
    assert request("eth_blockNumber", [])["result"] == hex(105)  # the slow node said 100
    rpc.slow_left = 0
    assert request("eth_blockNumber", [])["result"] == hex(105)


def test_reads_at_an_explicit_block_wait_for_a_lagging_node(monkeypatch):
    rpc, request = _read_your_writes(monkeypatch, mark=105)
    assert request("eth_call", [{"to": CONTRACT}, hex(105)])["result"] == "0x01"
    assert [c[0] for c in rpc.calls] == ["eth_call"] * 3  # two "header not found", then the fast node
    rpc.slow_left = 1
    assert request("eth_getBlockByNumber", [hex(105), False])["result"] == {"number": hex(105)}
    rpc.slow_left = 2  # the head a log query to "latest" resolves to includes our own block
    assert request("eth_getBlockByNumber", ["latest", False])["result"] == {"number": hex(105)}
    rpc.slow_left = 1
    assert request("eth_call", [{"to": CONTRACT}, "pending"])["result"] == "0x01"  # tags other than latest pass


def test_a_pruned_old_block_fails_fast_instead_of_waiting(monkeypatch):
    _read_your_writes(monkeypatch, mark=105)  # the receipt mark and no sleeping
    calls = []

    def pruned(method, params):
        calls.append(method)
        return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "missing trie node abc"}}

    from acb.chain import client

    request = client.ReadYourWrites(type("W3", (), {"_acb_chain_id": 84532})()).wrap_make_request(pruned)
    assert "missing trie node" in request("eth_call", [{"to": CONTRACT}, hex(7)])["error"]["message"]
    assert calls == ["eth_call"]  # no archive: waiting would not bring the state back
