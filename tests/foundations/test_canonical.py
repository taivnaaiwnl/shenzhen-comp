"""Canonical JSON, keccak helpers, orderId, EIP-191 and atomic writes (spec 0.1, 6.6, 9.2, 12.2)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from web3 import Web3

from acb.canonical import (
    atomic_write_bytes,
    canonical_bytes,
    chash,
    invoice_hash,
    keccak_hex,
    order_id,
    recover_eip191,
    recover_invoice_signer,
    short_string_bytes32,
    short_string_from_bytes32,
    sign_digest_eip191,
    sign_invoice,
    write_canonical,
)
from acb.config import foundry_tool
from acb.keys import KeyRing

VECTORS = json.loads((Path(__file__).parent / "vectors.json").read_text(encoding="utf-8"))
HEX32 = r"^0x[0-9a-f]{64}$"


def test_canonical_bytes_sorted_compact_ascii():
    assert canonical_bytes({"b": 1, "a": [True, None, "x"]}) == b'{"a":[true,null,"x"],"b":1}'
    assert canonical_bytes({"z": "\u00e9\u4e2d"}) == b'{"z":"\\u00e9\\u4e2d"}'
    assert canonical_bytes(("a", 1)) == b'["a",1]'


def test_canonical_is_deterministic_regardless_of_insertion_order():
    a = {"x": {"k2": 2, "k1": [1, {"b": 0, "a": 1}]}, "y": "v"}
    b = {"y": "v", "x": {"k1": [1, {"a": 1, "b": 0}], "k2": 2}}
    assert canonical_bytes(a) == canonical_bytes(b)
    assert chash(a) == chash(b)


@pytest.mark.parametrize("bad", [1.0, {"a": 0.5}, [1, [2.5]], {"n": float("nan")}, {"i": float("inf")}])
def test_floats_are_rejected(bad):
    with pytest.raises(TypeError):
        canonical_bytes(bad)


def test_big_ints_must_be_strings():
    canonical_bytes({"n": 2**53})
    canonical_bytes({"n": -(2**53)})
    with pytest.raises(TypeError):
        canonical_bytes({"n": 2**53 + 1})
    with pytest.raises(TypeError):
        canonical_bytes([-(2**53) - 1])
    assert canonical_bytes({"n": str(2**200)}) == f'{{"n":"{2**200}"}}'.encode()


@pytest.mark.parametrize("bad", [{1: "a"}, {"a": b"bytes"}, {"a": {1, 2}}, object()])
def test_other_types_are_rejected(bad):
    with pytest.raises(TypeError):
        canonical_bytes(bad)


def test_chash_format_and_keccak():
    h = chash({"a": 1})
    assert h == Web3.to_hex(Web3.keccak(b'{"a":1}'))
    assert h.startswith("0x") and len(h) == 66 and h == h.lower()
    assert keccak_hex(b"") == "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"


def test_vectors_chash():
    for case in VECTORS["chash"]:
        assert canonical_bytes(case["input"]).decode("ascii") == case["canonical"]
        assert chash(case["input"]) == case["hash"]


def test_vectors_order_id():
    for case in VECTORS["orderId"]:
        got = order_id(case["chainId"], case["vault"], case["issuerAgentId"], case["invoiceId"])
        assert got == case["orderId"]
        # checksummed vault gives the same id
        assert (
            order_id(case["chainId"], Web3.to_checksum_address(case["vault"]), case["issuerAgentId"], case["invoiceId"])
            == got
        )


def test_order_id_ignores_amount_but_not_identity():
    base = (84532, "0x5e1fa0c3b2d4e6f8a1b3c5d7e9f0a2b4c6d8e0f2", 57, "inv-1")
    assert order_id(*base) != order_id(84532, base[1], 58, "inv-1")
    assert order_id(*base) != order_id(31337, base[1], 57, "inv-1")
    assert order_id(*base) != order_id(84532, base[1], 57, "inv-2")


@pytest.mark.skipif(foundry_tool("cast") is None, reason="Foundry cast not installed")
def test_order_id_matches_solidity_abi_encode_via_cast(tmp_path):
    """Cross-check with Solidity's abi.encode as implemented by cast (run outside the repo: no .env)."""
    cast = str(foundry_tool("cast"))
    case = VECTORS["orderId"][0]

    def run(*args: str) -> str:
        return subprocess.run([cast, *args], capture_output=True, text=True, check=True, cwd=tmp_path).stdout.strip()

    key = run("keccak", case["invoiceId"])
    encoded = run(
        "abi-encode",
        "f(uint256,address,uint256,bytes32)",
        str(case["chainId"]),
        case["vault"],
        str(case["issuerAgentId"]),
        key,
    )
    assert run("keccak", encoded) == case["orderId"]


def test_vectors_invoice_hash_and_signature():
    inv = VECTORS["invoice"]
    unsigned = inv["unsigned"]
    assert canonical_bytes(unsigned).decode("ascii") == inv["canonical"]
    assert invoice_hash(unsigned) == inv["invoiceHash"]
    signed = {**unsigned, "signature": inv["signature"]}
    assert invoice_hash(signed) == inv["invoiceHash"], "signature is excluded from the hash"
    assert recover_eip191(inv["invoiceHash"], inv["signature"]) == inv["signerAddress"]
    assert recover_invoice_signer(signed) == inv["signerAddress"]
    atlas = KeyRing.anvil().account(inv["signerRole"])
    assert sign_digest_eip191(atlas, inv["invoiceHash"]) == inv["signature"], "RFC 6979: deterministic"
    assert sign_invoice(atlas, unsigned) == signed


def test_tampered_invoice_recovers_a_different_signer():
    inv = VECTORS["invoice"]
    signed = {**inv["unsigned"], "signature": inv["signature"], "amount": "3000000"}
    assert recover_invoice_signer(signed) != inv["signerAddress"]


def test_sign_accepts_hex_key_and_bytes_digest():
    acct = KeyRing.anvil().account("orion")
    digest = Web3.keccak(b"x")
    sig_a = sign_digest_eip191(acct, digest)
    sig_b = sign_digest_eip191("0x" + bytes(acct.key).hex(), Web3.to_hex(digest))
    assert sig_a == sig_b
    assert recover_eip191(digest, sig_a) == acct.address.lower()
    with pytest.raises(ValueError):
        sign_digest_eip191(acct, b"short")


def test_short_string_bytes32():
    for case in VECTORS["shortString"]:
        raw = short_string_bytes32(case["text"])
        assert "0x" + raw.hex() == case["bytes32"]
        assert short_string_from_bytes32(case["bytes32"]) == case["text"]
    with pytest.raises(ValueError):
        short_string_bytes32("x" * 33)


def test_atomic_write_bytes_and_write_canonical(tmp_path):
    target = tmp_path / "nested" / "dir" / "file.json"
    atomic_write_bytes(target, b"one")
    atomic_write_bytes(target, b"two")
    assert target.read_bytes() == b"two"
    assert [p.name for p in target.parent.iterdir()] == ["file.json"], "no temp files left behind"
    h = write_canonical(target, {"b": 2, "a": 1})
    assert target.read_bytes() == b'{"a":1,"b":2}'
    assert h == chash({"a": 1, "b": 2})


def test_atomic_write_failure_leaves_target_untouched(tmp_path):
    target = tmp_path / "f.json"
    target.write_bytes(b"original")
    with pytest.raises(TypeError):
        write_canonical(target, {"x": 1.5})
    assert target.read_bytes() == b"original"


def test_recover_rejects_malleable_and_malformed_signatures():
    from acb.canonical import SECP256K1_N

    acct = KeyRing.anvil().account("orion")
    digest = Web3.keccak(b"order")
    sig = bytes.fromhex(sign_digest_eip191(acct, digest)[2:])
    r, s, v = sig[:32], int.from_bytes(sig[32:64], "big"), sig[64]
    assert s <= SECP256K1_N // 2, "eth_account signs with low s"
    twin = r + (SECP256K1_N - s).to_bytes(32, "big") + bytes([55 - v])  # 27 <-> 28
    with pytest.raises(ValueError):
        recover_eip191(digest, twin)
    with pytest.raises(ValueError):
        recover_eip191(digest, sig[:64] + bytes([v - 27]))  # v must be 27 or 28
    with pytest.raises(ValueError):
        recover_eip191(digest, sig[:64])
    assert recover_eip191(digest, sig) == acct.address.lower()
