"""Appeal review (acb.scenario.appeals) on fake vault events: only adverse evidence can support an appeal."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from acb.canonical import canonical_bytes, chash
from acb.scenario import appeals

ACB = "0x" + "a" * 40
VAULT = "0x" + "b" * 40
GHOST = 3
WARM, D1, D2 = ("0x" + c * 64 for c in "123")
DEADLINE = 1_000


class _Contract:
    def __init__(self, _w3, address):
        self.address = address.lower()


class _Eth:
    block_number = 600

    @staticmethod
    def get_block(number):
        return {"timestamp": 2_000 + int(number)}  # every refund block is after DEADLINE


def _chain(claims: dict[str, int], with_post: bool = True):
    """Events of a fake chain: Ghost's warm-up order was claimed and released, D1 and D2 were refunded."""
    bundle = {
        "schema": "acb-evidence/1",
        "orders": [
            {"orderId": WARM, "role": "payee", "outcome": "ON_TIME", "counted": True},
            {"orderId": D1, "role": "payee", "outcome": "FAILED", "counted": True},
            {"orderId": D2, "role": "payee", "outcome": "FAILED", "counted": True},
        ],
    }
    payload = {"schema": "acb-appeal/1", "agentId": str(GHOST), "claim": "delivered", "orderIds": [WARM, D1, D2]}
    events = {
        (ACB, "AppealFiled"): [
            {
                "args": {"appealId": 1, "agentId": GHOST, "appealHash": chash(payload)},
                "blockNumber": 500,
                "transactionHash": "0xappeal",
            }
        ],
        (ACB, "GradePosted"): [
            {
                "args": {"agentId": GHOST, "asOfBlock": 400, "evidenceHash": chash(bundle)},
                "blockNumber": 401,
                "transactionHash": "0xpost",
            }
        ]
        if with_post
        else [],
        (ACB, "VaultRegistered"): [{"args": {"vault": VAULT}, "blockNumber": 1, "transactionHash": "0xreg"}],
        (VAULT, "EscrowOpened"): [
            {
                "args": {"orderId": oid, "payeeAgentId": GHOST, "amount": 200_000, "deadline": DEADLINE},
                "blockNumber": 100 + i,
                "transactionHash": f"0xopen{i}",
            }
            for i, oid in enumerate((WARM, D1, D2))
        ],
        (VAULT, "DeliveryClaimed"): [
            {"args": {"orderId": oid}, "blockNumber": block, "transactionHash": f"0xclaim{oid[-1]}"}
            for oid, block in claims.items()
        ],
        (VAULT, "EscrowRefunded"): [
            {"args": {"orderId": oid, "reasonCode": 2400}, "blockNumber": 450, "transactionHash": f"0xrefund{oid[-1]}"}
            for oid in (D1, D2)
        ],
    }
    return bundle, payload, events


@pytest.fixture
def fake_chain(monkeypatch, tmp_path):
    from acb.chain import contracts

    state: dict = {}

    def logs(contract, event, _from_block, **filters):
        rows = state["events"].get((contract.address, event), [])
        oid = filters.get("orderId")
        return [r for r in rows if oid is None or r["args"].get("orderId") == oid]

    monkeypatch.setattr(contracts, "AcbContract", _Contract)
    monkeypatch.setattr(contracts, "VaultContract", _Contract)
    monkeypatch.setattr(appeals, "_logs", logs)
    dep = SimpleNamespace(
        contracts={"AgentCreditBureau": SimpleNamespace(address=ACB)},
        start_block=0,
        vaults={"beacon": SimpleNamespace(address=VAULT)},
    )
    w3 = SimpleNamespace(eth=_Eth())

    def review(claims: dict[str, int], *, publish_bundle: bool = True, with_post: bool = True) -> dict:
        bundle, payload, events = _chain(claims, with_post)
        state["events"] = events
        (tmp_path / "appeals").mkdir(exist_ok=True)
        (tmp_path / "appeals" / f"{chash(payload)}.json").write_bytes(canonical_bytes(payload))
        (tmp_path / "bundles").mkdir(exist_ok=True)
        path = tmp_path / "bundles" / f"{GHOST}-400.json"
        # publish_bundle=False: a stale file with the right name but other content (an earlier run's)
        path.write_bytes(canonical_bytes(bundle if publish_bundle else {**bundle, "orders": []}))
        return appeals.review_appeal(w3, dep, 1, payload_dirs=[tmp_path / "appeals"], out_dir=tmp_path)

    return review


def test_a_claimed_warm_up_order_does_not_uphold_the_appeal(fake_chain):
    result = fake_chain({WARM: 90})
    review = result["review"]
    assert not result["upheld"] and review["decision"] == "REJECTED" and review["reasonCode"] == 1103
    assert review["adverseEvidence"] == {"source": "bundle", "orderIds": sorted([D1, D2])}
    warm = next(o for o in review["orders"] if o["orderId"] == WARM)
    assert warm["claimed"] and not warm["adverseEvidence"] and warm["note"] == "not adverse evidence"


def test_an_adverse_order_claimed_before_its_refund_upholds(fake_chain):
    result = fake_chain({WARM: 90, D1: 440})
    assert result["upheld"] and result["review"]["reasonCode"] == 1102


def test_a_claim_after_the_refund_does_not_count(fake_chain):
    assert not fake_chain({D1: 460})["upheld"]


def test_a_bundle_that_does_not_hash_is_ignored(fake_chain):
    # a stale file with the right name but other content: the review falls back to the vault refunds
    result = fake_chain({WARM: 90}, publish_bundle=False)
    assert result["review"]["adverseEvidence"]["source"] == "vault events"
    assert result["review"]["adverseEvidence"]["orderIds"] == sorted([D1, D2])
    assert not result["upheld"]


def test_without_a_posting_the_vault_refunds_are_the_adverse_evidence(fake_chain):
    result = fake_chain({D2: 449}, with_post=False)
    assert result["review"]["adverseEvidence"]["source"] == "vault events"
    assert result["upheld"]


def test_open_appeals_are_the_unresolved_ones_of_the_appellant(monkeypatch):
    from acb.chain import contracts

    filed = [
        {"args": {"appealId": i, "agentId": agent}, "blockNumber": 500 + i, "transactionHash": f"0xf{i}"}
        for i, agent in ((1, GHOST), (2, 7), (3, GHOST))
    ]
    resolved = [{"args": {"appealId": 1}, "blockNumber": 600, "transactionHash": "0xr1"}]
    events = {"AppealFiled": filed, "AppealResolved": resolved}
    monkeypatch.setattr(contracts, "AcbContract", _Contract)
    monkeypatch.setattr(appeals, "_logs", lambda _c, event, _from, **_f: events[event])
    dep = SimpleNamespace(contracts={"AgentCreditBureau": SimpleNamespace(address=ACB)})
    assert appeals.open_appeals(None, dep, GHOST, 0) == [3]
    events["AppealResolved"] = [*resolved, {"args": {"appealId": 3}, "blockNumber": 601, "transactionHash": "0xr3"}]
    assert appeals.open_appeals(None, dep, GHOST, 0) == []
