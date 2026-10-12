"""Autonomy on Anvil (spec 12.2 "Autonomy"): the Python model equals TierVault on 20 random sequences;
an outflow without a signer decision freezes (3205), also when it re-uses a public decisionHash or the
stolen key froze the vault itself; own grade D demotes to tier 0 without freezing; promotion needs two
distinct payees with matched decisions; every transaction is simulated first."""

from __future__ import annotations

import random
import secrets

import pytest

from acb.autonomy.evaluator import Evaluator, Plan
from acb.autonomy.model import OnchainState, apply
from acb.bureau.config import load_config
from acb.canonical import keccak_hex
from acb.chain import abi as abi_mod
from acb.chain.clock import now
from acb.chain.contracts import DecisionParams, OutflowParams, bind
from acb.chain.tx import TxSender
from acb.keys import KeyRing
from acb.logbus import EventBus
from acb.types import Decision, Grade, Outcome

pytestmark = pytest.mark.chain
RING = KeyRing.anvil()


class FakeSigner:
    """The signer's decision log as the operator sees it (spec 7.2): hash -> entry."""

    def __init__(self) -> None:
        self.entries: dict[str, dict] = {}

    def log(self, decision_hash: str, order_id: str, tx: str | None, kind: str = "PAY") -> None:
        self.entries[decision_hash.lower()] = {
            "found": True,
            "requestId": "r-" + decision_hash[2:10],
            "kind": kind,
            "orderId": order_id,
            "txHash": tx,
            "loggedAt": "2026-10-12T00:00:00Z",
        }

    def __call__(self, decision_hash: str):
        return self.entries.get(decision_hash.lower(), False)


class Chain:
    def __init__(self, w3, net, dep, out_dir):
        self.w3, self.net, self.dep, self.out = w3, net, dep, out_dir
        self.c = bind(w3, dep)
        self.vault = self.c.vault("wallet")
        self._senders: dict[str, TxSender] = {}

    def sender(self, role: str) -> TxSender:
        if role not in self._senders:
            self._senders[role] = TxSender(self.w3, RING.account(role))
        return self._senders[role]

    def ok(self, role: str, fn):
        r = self.sender(role).send(fn)
        assert r.status == 1, r.revert_error
        return r

    def raw(self, role: str, fn):
        """Send without simulation, so a call the contract rejects is mined as a revert."""
        return self.sender(role).send(fn, simulate=False, gas=500_000)

    def rpc(self, method: str, params: list):
        return self.w3.provider.make_request(method, params)["result"]

    def warp(self, seconds: int) -> None:
        self.rpc("evm_increaseTime", [seconds])
        self.rpc("evm_mine", [])

    def agent_id(self, key: str) -> int:
        return self.dep.agents[key].agent_id

    def post_grade(self, key_or_id, grade: Grade) -> None:
        aid = key_or_id if isinstance(key_or_id, int) else self.agent_id(key_or_id)
        cfg = load_config(self.dep.anchors)
        as_of = int(self.w3.eth.block_number) - 1
        self.ok(
            "bureau",
            self.c.acb.post_grade(
                aid,
                int(grade),
                740 if grade == Grade.A else 400,
                [1006],
                "acb-sc-1.0.0",
                cfg.config_hash,
                keccak_hex(secrets.token_bytes(8)),
                as_of,
                "t",
            ),
        )

    def mandate(self, payees: list[str]) -> str:
        mid = keccak_hex(secrets.token_bytes(32))
        ids = [self.agent_id(p) for p in payees]
        wallets = [self.c.identity.get_agent_wallet(i) for i in ids]
        self.ok(
            "owner",
            self.vault.register_mandate(
                mid, ids, wallets, 1_000_000, 20_000_000, now(self.w3) + 6 * 86400, False, "0x" + "00" * 32
            ),
        )
        return mid

    def pay_and_deliver(
        self,
        payee: str,
        mid: str,
        role: str = "agent_signer",
        signer: FakeSigner | None = None,
        decision_hash: str | None = None,
        order: str | None = None,
    ) -> str:
        """Pay and deliver; with ``signer`` the decision is logged as the signer would (same order and tx)."""
        oid = order or keccak_hex(secrets.token_bytes(32))
        dh = decision_hash or keccak_hex(secrets.token_bytes(32))
        p = OutflowParams(
            oid,
            self.agent_id(payee),
            200_000,
            now(self.w3) + 600,
            mid,
            keccak_hex(b"inv" + oid.encode()),
            dh,
            int(Decision.PAY),
            2000,
        )
        paid = self.ok(role, self.vault.pay(p))
        if signer is not None:
            signer.log(dh, oid, paid.hash)
        self.ok(role, self.vault.record_delivery(oid, int(Outcome.ON_TIME), keccak_hex(b"d" + oid.encode())))
        return dh

    def approved_clean(self, payee: str) -> None:
        oid = keccak_hex(secrets.token_bytes(32))
        wallet = self.c.identity.get_agent_wallet(self.agent_id(payee))
        self.ok(
            "owner", self.vault.owner_approve(oid, self.agent_id(payee), wallet, 200_000, now(self.w3) + 600, False, 0)
        )
        self.ok(
            "agent_signer",
            self.vault.execute_approved(oid, keccak_hex(b"i" + oid.encode()), keccak_hex(b"d" + oid.encode())),
        )
        self.ok("agent_signer", self.vault.record_delivery(oid, int(Outcome.ON_TIME), keccak_hex(b"x" + oid.encode())))

    def state(self) -> OnchainState:
        if not hasattr(self, "_fixed"):  # tier configs and the cooldown do not change in these tests
            mct = tuple(self.vault.tier_config(t).min_clean_to_promote for t in range(4))
            self._fixed = (self.vault.promotion_cooldown(), mct)
        cooldown, mct = self._fixed
        s = self.vault.status()
        return OnchainState(
            s.tier, s.tier_before_freeze, s.max_auto_tier, s.clean, s.points, s.last_tier_change_at, cooldown, mct
        )  # type: ignore[arg-type]


@pytest.fixture
def chain(anvil, anvil_profile, fresh_deployed, out_dir):
    _, w3 = anvil
    return Chain(w3, anvil_profile, fresh_deployed, out_dir)


def market_ready(ch: Chain) -> str:
    ch.post_grade(ch.dep.vaults["wallet"].agent_id, Grade.NEW)  # the wallet is graded NEW from round 1 on
    ch.post_grade("atlas", Grade.A)
    ch.post_grade("slowpoke", Grade.A)
    ch.warp(61)  # the upgrade delay of the demo profile
    return ch.mandate(["atlas", "slowpoke"])


def block_ts(ch: Chain, block: int) -> int:
    return int(ch.w3.eth.get_block(block)["timestamp"])


@pytest.mark.slow
def test_model_equals_contract_on_20_random_sequences(chain):
    ch = chain
    mid = market_ready(ch)
    compared = 0
    for seq in range(20):
        rng = random.Random(2026 + seq)
        snap = ch.rpc("evm_snapshot", [])
        try:
            model = ch.state()
            cleans = 0
            for _ in range(12):
                frozen = model.frozen
                op = rng.choices(
                    ["v1", "v2", "v3", "clean", "promote", "set", "freeze", "unfreeze", "max", "warp"],
                    weights=[18, 8, 4, 25 if not frozen and cleans < 6 else 0, 16, 6, 3, 12 if frozen else 2, 4, 14],
                )[0]
                event: dict | None = None
                if op in ("v1", "v2", "v3"):
                    sev = int(op[1])
                    r = ch.raw(
                        "agent_signer", ch.vault.record_violation({1: 1, 2: 4, 3: 6}[sev], sev, keccak_hex(b"v"))
                    )
                    event = {"op": "violation", "severity": sev}
                elif op == "clean":
                    if model.tier == 0:
                        ch.approved_clean(rng.choice(["atlas", "slowpoke"]))
                    else:
                        ch.pay_and_deliver(rng.choice(["atlas", "slowpoke"]), mid)
                    cleans += 1
                    model, err = apply(model, {"op": "clean"})
                    assert err is None
                elif op in ("promote", "set"):
                    if model.frozen:
                        target = rng.choice([0, 1, 2])
                    else:
                        target = model.tier + 1 if op == "promote" else rng.choice([0, 1, 2, 3, model.tier + 2])
                    r = ch.raw("autonomy", ch.vault.set_tier(target, 3000, keccak_hex(b"t")))
                    event = {"op": "set_tier", "tier": target}
                elif op == "freeze":
                    r = ch.raw("autonomy", ch.vault.freeze(3204, keccak_hex(b"f")))
                    event = {"op": "freeze"}
                elif op == "unfreeze":
                    to = rng.choice([0, 1, 1, 2])
                    r = ch.raw("owner", ch.vault.unfreeze(to, 3300))
                    event = {"op": "unfreeze", "tier": to}
                elif op == "max":
                    m = rng.choice([0, 1, 2, 3])
                    r = ch.raw("owner", ch.vault.set_max_auto_tier(m))
                    event = {"op": "set_max_auto_tier", "value": m}
                else:
                    ch.warp(rng.choice([10, 30, 61, 200]))
                if event is not None:
                    event["at"] = block_ts(ch, r.block)
                    model, err = apply(model, event)
                    assert (r.status == 1) == (err is None), (seq, event, r.revert_error, err)
                    if err is not None:
                        assert abi_mod.error_name(r.revert_error) == err, (seq, event, r.revert_error)
                onchain = ch.state()
                assert onchain.summary() == model.summary(), (seq, op, onchain, model)
                assert onchain.last_tier_change_at == model.last_tier_change_at
                compared += 1
        finally:
            ch.rpc("evm_revert", [snap])
    assert compared == 20 * 12


def evaluator(ch: Chain, lookup, bus=None) -> Evaluator:
    return Evaluator(ch.net, ch.dep, w3=ch.w3, lookup=lookup, account=RING.account("autonomy"), out_dir=ch.out, bus=bus)


def test_unmatched_outflow_freezes(chain):
    ch = chain
    mid = market_ready(ch)
    signer = FakeSigner()
    ch.pay_and_deliver("atlas", mid, signer=signer)  # an outflow the signer logged
    ev = evaluator(ch, signer)
    assert ev.evaluate().rule != "unmatched_outflow"
    ch.pay_and_deliver("slowpoke", mid)  # the stolen key pays outside the signer
    bus = EventBus("autonomy", jsonl=False)
    ev = evaluator(ch, signer, bus)
    result = ev.evaluate()
    assert result.rule == "unmatched_outflow" and result.reason_code == 3205
    assert result.tx is not None and result.tx.status == 1 and result.simulated_revert is None
    status = ch.vault.status()
    assert status.frozen and status.tier == 255
    changed = [lg for lg in result.tx.logs if lg["event"] == "TierChanged"]
    assert (
        changed
        and changed[0]["args"]["reasonCode"] == 3205
        and changed[0]["args"]["evidenceHash"] == result.evidence_hash
    )
    evidence = ch.out / "autonomy" / f"{result.evidence_hash}.json"
    assert evidence.is_file()
    assert [e.type for e in bus.since(0)] == ["info", "tier"]
    assert evaluator(ch, lambda h: False).evaluate().rule == "frozen"  # rule 1: no further action


def test_reused_decision_hash_is_unmatched(chain):
    """Every decisionHash is public on-chain: copying a logged one into a new order must still freeze."""
    ch = chain
    mid = market_ready(ch)
    signer = FakeSigner()
    legit = ch.pay_and_deliver("atlas", mid, signer=signer)
    assert evaluator(ch, signer).evaluate().rule != "unmatched_outflow"
    ch.pay_and_deliver("slowpoke", mid, decision_hash=legit)  # the stolen key re-uses the public hash
    result = evaluator(ch, signer).evaluate()
    assert (result.rule, result.reason_code) == ("unmatched_outflow", 3205)
    assert ch.vault.status().frozen


def test_hash_of_a_recorded_approval_request_is_unmatched(chain):
    """A NEEDS_OWNER_APPROVAL decision is on-chain (DecisionRecorded) and leaves the order open: paying that
    order with its hash is not an outflow the signer decided, even within the txHash grace period."""
    ch = chain
    mid = market_ready(ch)
    signer = FakeSigner()
    ch.pay_and_deliver("atlas", mid, signer=signer)
    oid, dh = keccak_hex(secrets.token_bytes(32)), keccak_hex(secrets.token_bytes(32))
    ch.ok(
        "agent_signer",
        ch.vault.record_decision(
            DecisionParams(
                oid, ch.agent_id("slowpoke"), 200_000, int(Decision.NEEDS_OWNER_APPROVAL), 2100, keccak_hex(b"i"), dh
            )
        ),
    )
    signer.log(dh, oid, None)  # the signer logged it (kind PAY, no transaction)
    assert evaluator(ch, signer).evaluate().rule != "unmatched_outflow"
    ch.pay_and_deliver("slowpoke", mid, decision_hash=dh, order=oid)
    result = evaluator(ch, signer).evaluate()
    assert (result.rule, result.reason_code) == ("unmatched_outflow", 3205)


def test_logged_refusal_and_other_tx_are_unmatched(chain):
    ch = chain
    mid = market_ready(ch)
    signer = FakeSigner()
    dh = ch.pay_and_deliver("atlas", mid)
    ev = evaluator(ch, signer)
    ev.observe()
    paid = next(lg for lg in ev.logs.vault_logs if lg.event == "Paid")
    order = str(paid.args["orderId"])
    signer.log(dh, order.upper().replace("0X", "0x"), paid.tx)
    assert evaluator(ch, signer).evaluate().rule != "unmatched_outflow"  # order ids compare case-insensitively
    signer.log(dh, order, None, kind="RECORD_DECISION")  # a decision that never allows an outflow
    assert evaluator(ch, signer).evaluate(dry_run=True).rule == "unmatched_outflow"
    signer.log(dh, order, "0x" + "ab" * 32)  # the signer mined a different transaction with this hash
    assert evaluator(ch, signer).evaluate().rule == "unmatched_outflow"


def test_pending_tx_hash_neither_freezes_nor_promotes(chain):
    """Between the receipt and the signer's txHash line the outflow is pending; after the grace period
    without a txHash it is unmatched."""
    ch = chain
    mid = market_ready(ch)
    signer = FakeSigner()
    for payee in ("atlas", "slowpoke", "atlas"):
        ch.pay_and_deliver(payee, mid, signer=signer)
    last = list(signer.entries)[-1]
    signer.entries[last]["txHash"] = None
    ch.warp(61)
    ev = evaluator(ch, signer)
    result = ev.evaluate()
    assert result.rule == "none" and "wait for the signer's mined transaction" in ev.last_plan.detail
    assert not ch.vault.status().frozen and ch.vault.status().tier == 1
    ch.warp(ev.cfg.decision_tx_grace_s + 1)
    assert evaluator(ch, signer).evaluate().rule == "unmatched_outflow"


def test_unmatched_outflow_covered_by_an_earlier_3205_freeze_is_not_rechecked(chain):
    ch = chain
    mid = market_ready(ch)
    ch.pay_and_deliver("atlas", mid)
    assert evaluator(ch, lambda h: False).evaluate().rule == "unmatched_outflow"
    ch.ok("owner", ch.vault.unfreeze(1, 3300, RING.address("agent_signer_next")))
    assert evaluator(ch, lambda h: False).evaluate().rule != "unmatched_outflow"


def test_self_freeze_does_not_hide_an_unmatched_outflow(chain):
    """A stolen key pays outside the signer and freezes the vault itself before the next poll: the operator
    still reports the outflow while frozen, and freezes with 3205 after an unfreeze without review."""
    ch = chain
    mid = market_ready(ch)
    signer = FakeSigner()
    ch.pay_and_deliver("atlas", mid, signer=signer)
    ch.pay_and_deliver("slowpoke", mid)  # forged decision hash
    ch.ok("agent_signer", ch.vault.freeze(3203, keccak_hex(b"self")))
    bus = EventBus("autonomy", jsonl=False)
    ev = evaluator(ch, signer, bus)
    assert ev.evaluate().rule == "frozen"
    alerts = [e.data["alert"] for e in bus.since(0) if e.type == "info"]
    assert len(alerts) == 1 and "outflow without a signer decision" in alerts[0]
    assert ev.evaluate().rule == "frozen" and len([e for e in bus.since(0) if e.type == "info"]) == 1  # once
    ch.ok("owner", ch.vault.unfreeze(1, 3300))  # no rotation, no review
    result = evaluator(ch, signer).evaluate()
    assert (result.rule, result.reason_code) == ("unmatched_outflow", 3205)
    ch.ok("owner", ch.vault.unfreeze(1, 3300, RING.address("agent_signer_next")))
    assert evaluator(ch, signer).evaluate().rule != "unmatched_outflow"  # covered by that 3205 freeze


def test_agent_freeze_with_3205_does_not_cover_its_outflow(chain):
    """The agent key may call freeze with any reason code; only the operator's or owner's 3205 counts."""
    ch = chain
    mid = market_ready(ch)
    ch.pay_and_deliver("atlas", mid)
    ch.ok("agent_signer", ch.vault.freeze(3205, keccak_hex(b"fake")))
    ch.ok("owner", ch.vault.unfreeze(1, 3300))
    assert evaluator(ch, lambda h: False).evaluate().rule == "unmatched_outflow"


def test_signer_unreachable_never_freezes_and_alerts(chain):
    ch = chain
    mid = market_ready(ch)
    for payee in ("atlas", "slowpoke", "atlas"):
        ch.pay_and_deliver(payee, mid)
    ch.warp(61)
    bus = EventBus("autonomy", jsonl=False)
    ev = evaluator(ch, lambda h: None, bus)
    rules = [ev.evaluate().rule for _ in range(3)]
    assert rules == ["none", "none", "none"]
    assert not ch.vault.status().frozen and ch.vault.status().tier == 1
    alerts = [e.data["alert"] for e in bus.since(0) if e.type == "info"]
    assert alerts == ["signer decision log unreachable"]
    assert "signer decision log unreachable" in ev.last_plan.detail


def test_own_grade_d_demotes_to_tier_0_without_freezing(chain):
    ch = chain
    wallet_id = ch.dep.vaults["wallet"].agent_id
    ch.post_grade(wallet_id, Grade.D)
    bus = EventBus("autonomy", jsonl=False)
    result = evaluator(ch, lambda h: True, bus).evaluate()
    assert (result.rule, result.to_tier, result.reason_code) == ("own_grade_d", 0, 3105)
    assert result.tx is not None and result.tx.status == 1
    status = ch.vault.status()
    assert status.tier == 0 and not status.frozen
    assert any(e.type == "info" and "own grade is D" in e.data["alert"] for e in bus.since(0))
    again = evaluator(ch, lambda h: True).evaluate()
    assert again.rule == "own_grade_d" and again.tx is None and not ch.vault.status().frozen


def test_promotion_needs_two_distinct_matched_payees(chain):
    ch = chain
    mid = market_ready(ch)
    signer = FakeSigner()
    for _ in range(3):
        ch.pay_and_deliver("atlas", mid, signer=signer)
    ch.warp(61)
    ev = evaluator(ch, signer)
    first = ev.evaluate()
    assert first.rule == "none" and "1 distinct payees < 2" in ev.last_plan.detail
    ch.pay_and_deliver("slowpoke", mid, signer=signer)
    result = ev.evaluate()
    assert (result.rule, result.from_tier, result.to_tier, result.reason_code) == ("promote", 1, 2, 3000)
    assert result.tx is not None and result.tx.status == 1 and ch.vault.status().tier == 2


def test_simulated_revert_is_logged_and_nothing_is_sent(chain, monkeypatch):
    ch = chain
    import acb.autonomy.evaluator as mod

    monkeypatch.setattr(mod, "decide", lambda obs, cfg: Plan("promote", "set_tier", 3, 3000))
    bus = EventBus("autonomy", jsonl=False)
    nonce = ch.w3.eth.get_transaction_count(RING.checksum_address("autonomy"))
    result = evaluator(ch, lambda h: True, bus).evaluate()
    assert result.tx is None and "PromotionTooLarge" in (result.simulated_revert or "")
    assert ch.w3.eth.get_transaction_count(RING.checksum_address("autonomy")) == nonce
    assert [e.type for e in bus.since(0)] == ["error"]
