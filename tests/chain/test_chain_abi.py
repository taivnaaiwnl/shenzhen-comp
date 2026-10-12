"""abi/ loading, the error selector table and log decoding (spec 7.4); no chain needed."""

from __future__ import annotations

import json

import pytest
from eth_abi import encode
from eth_utils import keccak
from web3 import Web3

from acb.chain import abi


def _revert(sig: str, types: list[str], values: list) -> str:
    return Web3.to_hex(keccak(text=sig)[:4] + (encode(types, values) if types else b""))


def test_load_aliases_and_artefacts():
    names = abi.artefact_names()
    for name in ("AgentCreditBureau", "TierVault", "MockUSDC", "IdentityRegistryLite", "ReputationRegistryLite"):
        assert name in names
    assert abi.load("IdentityRegistry") is abi.load("IdentityRegistryLite")
    assert abi.load("USDC")["contractName"] == "MockUSDC"
    assert abi.bytecode_of("TierVault").startswith("0x")
    with pytest.raises(abi.AbiNotFound):
        abi.load("NoSuchContract")


def test_codehash_matches_runtime_bytecode():
    runtime = abi.load("TierVault")["deployedBytecode"]
    assert abi.codehash_of("TierVault") == Web3.to_hex(keccak(hexstr=runtime))
    assert abi.codehash_of("TierVault").startswith("0x") and len(abi.codehash_of("TierVault")) == 66


def test_abi_sha_is_stable_and_hex():
    first = abi.abi_sha()
    assert first == abi.abi_sha()
    assert first.startswith("0x") and len(first) == 66


def test_error_table_covers_every_abi_error():
    table = abi.error_table()
    expected = set()
    for name in abi.artefact_names():
        for entry in abi.abi_of(name):
            if entry["type"] == "error":
                expected.add(abi.signature(entry))
    assert expected <= {spec.signature for spec in table.values()}
    assert table[abi.ERROR_STRING_SELECTOR].name == "Error"
    assert table[abi.PANIC_SELECTOR].name == "Panic"
    # an error declared in two contracts is one entry listing both
    zero = table[abi.selector("ZeroAddress()")]
    assert {"AgentCreditBureau", "TierVault"} <= set(zero.contracts)


@pytest.mark.parametrize(
    ("sig", "types", "values", "text"),
    [
        ("Frozen()", [], [], "Frozen()"),
        (
            "GradeNotAllowed(uint256,uint8,uint8)",
            ["uint256", "uint8", "uint8"],
            [7, 2, 48],
            "GradeNotAllowed(7, 2, 48)",
        ),
        (
            "PayeeWalletChanged(uint256,address,address)",
            ["uint256", "address", "address"],
            [3, "0x" + "ab" * 20, "0x" + "CD" * 20],
            f"PayeeWalletChanged(3, 0x{'ab' * 20}, 0x{'cd' * 20})",
        ),
        ("OrderExists(bytes32)", ["bytes32"], [b"\x11" * 32], f"OrderExists(0x{'11' * 32})"),
        ("StaleAsOf(uint64,uint64)", ["uint64", "uint64"], [5, 9], "StaleAsOf(5, 9)"),
        ("ERC721NonexistentToken(uint256)", ["uint256"], [99], "ERC721NonexistentToken(99)"),
        ("Error(string)", ["string"], ["Self-feedback not allowed"], 'Error("Self-feedback not allowed")'),
        ("Panic(uint256)", ["uint256"], [0x11], "Panic(17)"),
    ],
)
def test_decode_revert_by_selector(sig, types, values, text):
    decoded = abi.decode_revert(_revert(sig, types, values))
    assert decoded.text == text
    assert decoded.name == sig.split("(")[0]
    assert decoded.selector == abi.selector(sig)
    json.dumps(decoded.args)  # JSON-safe
    assert abi.error_name(decoded.text) == decoded.name


def test_decode_revert_edge_cases():
    assert abi.decode_revert("0x").text == "Reverted()"
    assert abi.decode_revert(None).name == "Reverted"
    unknown = abi.decode_revert("0xdeadbeef" + "00" * 32)
    assert unknown.name == "UnknownError" and unknown.text == "UnknownError(0xdeadbeef)"
    # bytes input and a big uint256 argument (kept exact as a string in args)
    big = 2**200
    decoded = abi.decode_revert(
        bytes.fromhex(_revert("BadOrderState(bytes32,uint8)", ["bytes32", "uint8"], [b"\x00" * 32, 4])[2:])
    )
    assert decoded.text == f"BadOrderState(0x{'00' * 32}, 4)"
    decoded = abi.decode_revert(_revert("MandatePerTxExceeded(uint256,uint256)", ["uint256", "uint256"], [big, 1]))
    assert decoded.text == f"MandatePerTxExceeded({big}, 1)"
    assert decoded.args == (str(big), 1)


def test_decode_exception_forms():
    data = _revert("NotAgent()", [], [])

    class CustomError(Exception):
        def __init__(self, message, data):
            super().__init__(message)
            self.data = data

    assert abi.decode_exception(CustomError("0x...", data)).text == "NotAgent()"
    assert abi.decode_exception(Exception("execution reverted: Empty URI")).text == 'Error("Empty URI")'
    assert abi.decode_exception(Exception("execution reverted")).text == "Reverted()"
    rpc_like = Exception({"code": 3, "message": "execution reverted", "data": data})
    assert abi.decode_exception(rpc_like).text == "NotAgent()"


def test_event_table_and_transfer_disambiguation():
    topic = Web3.to_hex(keccak(text="Transfer(address,address,uint256)"))
    candidates = abi.event_table()[topic]
    assert {c.indexed_count for c in candidates} == {2, 3}
    a, b = "0x" + "11" * 20, "0x" + "22" * 20

    def pad(addr):
        return "0x" + "00" * 12 + addr[2:]

    erc20 = {
        "address": "0x" + "33" * 20,
        "topics": [topic, pad(a), pad(b)],
        "data": Web3.to_hex(encode(["uint256"], [500])),
        "logIndex": 0,
        "blockNumber": 1,
        "transactionHash": "0x" + "44" * 32,
        "blockHash": "0x" + "55" * 32,
        "transactionIndex": 0,
    }
    decoded = abi.decode_log(erc20)
    assert (
        decoded["event"] == "Transfer" and decoded["args"]["amount" if "amount" in decoded["args"] else "value"] == 500
    )
    erc721 = {**erc20, "topics": [topic, pad(a), pad(b), Web3.to_hex(encode(["uint256"], [7]))], "data": "0x"}
    decoded = abi.decode_log(erc721)
    assert decoded["event"] == "Transfer" and decoded["args"]["tokenId"] == 7
    assert decoded["args"]["from"] == a and decoded["transactionHash"] == "0x" + "44" * 32
    assert abi.decode_log({**erc20, "topics": ["0x" + "99" * 32]}) is None


def test_json_safe():
    assert abi.json_safe(b"\x01\x02") == "0x0102"
    assert abi.json_safe(2**60) == str(2**60)
    assert abi.json_safe(5) == 5
    assert abi.json_safe("0x" + "AB" * 20) == "0x" + "ab" * 20
    assert abi.json_safe((1, [b"\x00"])) == [1, ["0x00"]]
