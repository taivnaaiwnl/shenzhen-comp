"""scripts/verify_evidence.py: the pure-Python keccak256 and the embedded event topics (spec 10.5)."""

from __future__ import annotations

import ast
import random

import pytest
from web3 import Web3

from acb.scenario.evidence import VERIFY_SCRIPT, load_verifier, topic_table


@pytest.fixture(scope="module")
def verifier():
    return load_verifier()


KNOWN = {
    b"": "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470",
    b"abc": "0x4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45",
}


@pytest.mark.parametrize("data,digest", sorted(KNOWN.items()))
def test_keccak_known_vectors(verifier, data, digest):
    assert verifier.keccak_hex(data) == digest


@pytest.mark.parametrize("length", [0, 1, 31, 32, 33, 135, 136, 137, 271, 272, 273, 1000, 4096])
def test_keccak_matches_web3_at_block_boundaries(verifier, length):
    rng = random.Random(length)
    data = bytes(rng.getrandbits(8) for _ in range(length))
    assert verifier.keccak256(data) == bytes(Web3.keccak(data))


def test_keccak_matches_web3_random(verifier):
    rng = random.Random(2026)
    for _ in range(200):
        data = bytes(rng.getrandbits(8) for _ in range(rng.randrange(0, 600)))
        assert verifier.keccak256(data) == bytes(Web3.keccak(data))


def test_embedded_topics_equal_abi(verifier):
    """The table in the script is generated from abi/ (``python -m acb.scenario.evidence topics``)."""
    assert {k: tuple(v) for k, v in verifier.EVENT_TOPICS.items()} == topic_table()


def test_topics_are_keccak_of_signatures(verifier):
    for entries in verifier.EVENT_TOPICS.values():
        for signature, topic in entries:
            assert verifier.keccak_hex(signature.encode()) == topic


def test_grade_posted_evidence_word_matches_abi(verifier):
    from acb.chain import abi as abi_mod

    event = next(
        e for e in abi_mod.abi_of("AgentCreditBureau") if e.get("type") == "event" and e["name"] == "GradePosted"
    )
    words = 0
    for param in event["inputs"]:
        if param.get("indexed"):
            continue
        if param["name"] == "evidenceHash":
            break
        words += 4 if param["type"].endswith("[4]") else 1
    assert words == verifier.GRADE_POSTED_EVIDENCE_WORD


def test_script_uses_the_standard_library_only():
    tree = ast.parse(VERIFY_SCRIPT.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    allowed = {"__future__", "argparse", "csv", "json", "sys", "time", "urllib", "pathlib"}
    assert imported <= allowed, imported - allowed


def test_script_is_ascii_lf():
    raw = VERIFY_SCRIPT.read_bytes()
    assert b"\r\n" not in raw
    assert all(b < 128 for b in raw)
