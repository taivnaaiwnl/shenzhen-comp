"""The wallet agent end to end on Anvil (marker ``chain``): real contracts (WP2/WP3), the real signer executor
(WP5) and the real simulator (WP8), in process. Requests and answers cross a JSON round trip, as over HTTP.

Covers the agent's side of the recorded run: S5 (dialogue, owner registration from the draft, on-chain
verification), S6 (PAY, ON_TIME), S7 (PAY_CAPPED re-quote, LATE), S8 (escrow, claim, release), S12 (grade D
refused on-chain), S14 (no claim, refund 2400, NON_DELIVERY), S15 (naive LLM, violation, demotion,
INJECTION_ATTEMPT) and S19 (SPAM from three signed unsolicited invoices).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from acb.agent.chain import Web3AgentChain
from acb.agent.config import AgentConfig
from acb.agent.mock_llm import MockLLM
from acb.agent.owner_actions import mandate_from_draft
from acb.agent.redact import Redactor
from acb.agent.store import AgentStore
from acb.agent.wallet import WalletAgent
from acb.canonical import chash
from acb.chain import clock
from acb.chain.contracts import bind
from acb.chain.tx import TxSender
from acb.keys import KeyRing
from acb.logbus import EventBus
from acb.policy import PolicyConfig
from acb.types import (
    ExecuteRequest,
    ExecuteResponse,
    Grade,
    MandateDraft,
    OrderState,
    Outcome,
)

pytestmark = pytest.mark.chain

INTENT = "I need FX market data and API calls for my research today, budget 5 USDC"
PICK = "Use all five: Atlas, Slowpoke, Nova, Orion and Ghost"
CORRECT = "Actually make the total 4 USDC, not 5"
SCORES = {Grade.A: 760, Grade.B: 680, Grade.C: 600, Grade.NEW: 600, Grade.D: 420}
H = "0x" + "11" * 32


def _rpc(w3, method: str, params: list) -> Any:
    response = w3.provider.make_request(method, params)
    if "error" in response:
        raise RuntimeError(f"{method} failed: {response['error']}")
    return response["result"]


class Chain:
    """Helpers over the deployment: owner and bureau transactions, clock."""

    def __init__(self, w3, dep):
        self.w3 = w3
        self.dep = dep
        self.keys = KeyRing.anvil()
        self.c = bind(w3, dep)
        self.senders: dict[str, TxSender] = {}

    def send(self, role: str, fn: Any):
        if role not in self.senders:
            self.senders[role] = TxSender(self.w3, self.keys.account(role))
        result = self.senders[role].send(fn)
        assert result.status == 1, result.revert_error
        return result

    def agent_id(self, key: str) -> int:
        return int(self.dep.agents[key].agent_id)

    def post_grade(self, key: str, grade: Grade) -> None:
        aid = self.agent_id(key)
        as_of = max(self.w3.eth.block_number - 1, self.c.acb.get_record(aid).as_of_block)
        evidence = chash({"test": key, "grade": int(grade), "block": as_of})
        self.send(
            "bureau",
            self.c.acb.post_grade(
                aid, int(grade), SCORES[grade], [1100], "acb-sc-1.0.0", H, evidence, as_of, "evidence/t.json"
            ),
        )

    def warp(self, seconds: int) -> int:
        return clock.warp(self.w3, seconds)


class InProcessSigner:
    """The real signer executor behind a JSON round trip (what HTTP would carry)."""

    def __init__(self, executor):
        self.executor = executor
        self.requests: list[ExecuteRequest] = []
        self.responses: list[ExecuteResponse] = []

    def execute(self, req: ExecuteRequest) -> ExecuteResponse:
        wire = ExecuteRequest.model_validate_json(json.dumps(req.to_wire()))
        self.requests.append(wire)
        resp = self.executor.execute(wire)
        out = ExecuteResponse.model_validate(json.loads(json.dumps(resp.to_wire())))
        self.responses.append(out)
        return out

    def kinds(self) -> list[tuple[str, str]]:
        return [(q.kind, r.status) for q, r in zip(self.requests, self.responses, strict=True)]


class InProcessProvider:
    """The real simulator's provider endpoints (errors become ``{"status": "error"}`` as over HTTP)."""

    def __init__(self, sim):
        self.sim = sim

    def _call(self, fn, key, body):
        from acb.sim.core import SimError

        try:
            return json.loads(json.dumps(fn(key, json.loads(json.dumps(body)))))
        except SimError as exc:
            return {"status": "error", "error": exc.body}

    def quote(self, key, body):
        return self._call(self.sim.quote, key, body)

    def deliver(self, key, body):
        return self._call(self.sim.deliver, key, body)


class NullInbox:
    def post(self, path, body):
        return {"statusCode": 200, "body": {"status": "accepted"}}


@pytest.fixture(scope="module")
def graded(anvil, deployed):
    """Grades posted once per module (Atlas A, Slowpoke B, Ghost A, Orion A, Nova NEW); reverted at the end."""
    _, w3 = anvil
    pristine = _rpc(w3, "evm_snapshot", [])
    try:
        ch = Chain(w3, deployed)
        for key, grade in (("atlas", Grade.A), ("slowpoke", Grade.B), ("ghost", Grade.A), ("orion", Grade.A)):
            ch.post_grade(key, grade)
        ch.post_grade("nova", Grade.NEW)
        ch.warp(ch.c.acb.upgrade_delay() + 1)
        yield ch
    finally:
        _rpc(w3, "evm_revert", [pristine])


class World:
    def __init__(self, ch: Chain, agent: WalletAgent, signer: InProcessSigner, sim):
        self.ch = ch
        self.agent = agent
        self.signer = signer
        self.sim = sim

    def chat(self, text: str):
        return self.agent.dialogue.owner_turn(text)

    def register(self) -> str:
        """S5 through the dialogue, then the owner registers the draft's values on-chain."""
        assert self.chat(INTENT).cards[0].type == "clarification"
        assert self.chat(PICK).cards[0].type == "mandate_draft"
        draft = self.chat(CORRECT).cards[0]
        assert isinstance(draft, MandateDraft)
        m = mandate_from_draft(draft.to_wire(), w3=self.ch.w3, dep=self.ch.dep, c=self.ch.c)
        vault = self.ch.c.vault("wallet")
        tx = self.ch.send(
            "owner",
            vault.register_mandate(
                m["mandateId"],
                m["ids"],
                m["wallets"],
                m["maxPerTx"],
                m["maxTotal"],
                m["validUntil"],
                m["recurring"],
                m["purposeHash"],
            ),
        )
        out = self.agent.dialogue.activated(m["mandateId"], tx.hash, draft.draft_id)
        assert out["status"] == "active", out
        return m["mandateId"]

    def buy(self, mid: str, key: str, sku: str, qty: int) -> list[dict]:
        aid = self.ch.agent_id(key)
        result = self.agent.runtime.run_purchase(mid, [{"payee_agent_id": aid, "sku": sku, "quantity": qty}])
        assert result["status"] == "done", result
        return result["results"]

    def order(self, oid: str):
        return self.ch.c.vault("wallet").get_order(oid)


@pytest.fixture
def world(anvil, anvil_profile, graded, tmp_path) -> World:
    from acb.signer.executor import Executor
    from acb.sim.core import Simulator

    w3, dep = graded.w3, graded.dep
    snapshot = _rpc(w3, "evm_snapshot", [])
    try:
        executor = Executor(
            w3=w3,
            dep=dep,
            account=graded.keys.account("agent_signer"),
            state_dir=tmp_path / "signer",
            out_dir=tmp_path / "signer-out",
            cfg=PolicyConfig.load(),
            bus=EventBus("signer", chain_id=dep.chain_id, out_dir=tmp_path / "signer-out"),
            evidence_base_uri="evidence/anvil/",
        )
        sim = Simulator(anvil_profile, dep, graded.keys, w3=w3, out_dir=tmp_path / "sim-out", inbox=NullInbox())
        signer = InProcessSigner(executor)
        agent = WalletAgent(
            dep=dep,
            chain=Web3AgentChain(w3, dep),
            signer=signer,
            provider=InProcessProvider(sim),
            llm=MockLLM("config/mock_llm/default.yaml"),
            llm_info={"mode": "mock", "model": "mock-careful", "persona": "careful"},
            store=AgentStore(tmp_path / "agent-state"),
            cfg=AgentConfig.load(),
            pcfg=PolicyConfig.load(),
            bus=EventBus("agent", chain_id=dep.chain_id, out_dir=tmp_path),
            redactor=Redactor.from_env({}),
            out_dir=tmp_path,
        )
        yield World(graded, agent, signer, sim)
    finally:
        _rpc(w3, "evm_revert", [snapshot])
        from acb.chain.tx import resync_all

        resync_all()


def test_s5_dialogue_registers_a_verified_mandate(world):
    mid = world.register()
    view = world.ch.c.vault("wallet").mandate_view(mid, from_block=world.ch.dep.start_block)
    assert view.max_total == 4_000_000 and view.max_per_tx == 1_000_000
    names = sorted(world.agent.directory.name_of(a) for a in view.payee_agent_ids)
    assert names == ["Atlas Data", "Ghost Feeds", "Nova Labs", "Orion API", "Slowpoke Analytics"]
    assert world.agent.dialogue.state() == "ACTIVE"
    assert world.agent.store.read("dialogue")["metrics"][-1]["turnsToMandate"] == 3


def test_s6_s7_s8_pay_capped_pay_and_escrow(world):
    mid = world.register()
    [s6] = world.buy(mid, "atlas", "fx-live-15m", 4)
    assert s6["status"] == "executed" and s6["outcome"] == "PAY" and s6["reasons"] == [2000]
    o = world.order(s6["orderId"])
    assert o.state == OrderState.PAID and o.outcome == Outcome.ON_TIME

    s7 = world.buy(mid, "slowpoke", "fx-history-day", 5)
    assert [r["status"] for r in s7] == ["superseded", "executed"]
    assert s7[1]["outcome"] == "PAY_CAPPED" and s7[1]["reasons"] == [2002]
    capped = s7[1]["orderId"]
    assert world.order(capped).amount == 400_000 and world.order(capped).outcome == Outcome.NONE  # late persona
    deadline = world.order(capped).deadline
    world.ch.warp(max(0, deadline - clock.now(world.ch.w3)) + 6)
    world.agent.runtime.tick()
    assert world.order(capped).outcome == Outcome.LATE

    [s8] = world.buy(mid, "nova", "fx-sample", 1)
    assert s8["outcome"] == "ESCROW" and s8["reasons"] == [2004]
    o = world.order(s8["orderId"])
    assert o.state == OrderState.RELEASED and o.outcome == Outcome.ON_TIME
    kinds = world.signer.kinds()
    assert ("RELEASE", "executed") in kinds and ("RECORD_DELIVERY", "executed") in kinds
    assert all(status == "executed" for _, status in kinds)


def test_s12_grade_d_is_refused_on_chain(world):
    mid = world.register()
    world.ch.post_grade("ghost", Grade.D)  # downgrades are immediate
    [res] = world.buy(mid, "ghost", "fx-backup-15m", 2)
    assert res["status"] == "refused" and res["outcome"] == "REFUSE" and res["reasons"] == [2100]
    assert world.order(res["orderId"]).state == OrderState.REFUSED
    assert world.signer.kinds() == [("RECORD_DECISION", "refused")]


def test_s14_escrow_without_claim_refund_and_non_delivery(world):
    mid = world.register()
    [first] = world.buy(mid, "nova", "fx-sample", 1)
    assert world.order(first["orderId"]).state == OrderState.RELEASED
    [second] = world.buy(mid, "nova", "fx-sample", 1)  # Nova's scripted outage
    oid = second["orderId"]
    assert world.order(oid).state == OrderState.ESCROWED
    deadline = world.order(oid).deadline
    world.ch.warp(max(0, deadline - clock.now(world.ch.w3)) + 31)
    world.agent.runtime.tick()
    o = world.order(oid)
    assert o.state == OrderState.REFUNDED and o.outcome == Outcome.FAILED
    assert world.signer.kinds()[-2:] == [("REFUND", "executed"), ("REPORT", "executed")]
    assert world.signer.requests[-1].report.kind == "NON_DELIVERY"


def test_s15_naive_llm_is_contained_on_chain(world):
    from acb.sim.market import set_override

    mid = world.register()
    set_override(world.sim, {"persona": "orion", "mode": "inject"})
    world.agent.switch_llm(
        MockLLM("config/mock_llm/default.yaml", persona="naive"), {"mode": "mock", "persona": "naive"}, False
    )
    tier_before = world.ch.c.vault("wallet").tier()
    [res] = world.buy(mid, "orion", "api-bundle-1k", 3)
    assert res["status"] == "refused" and res["outcome"] == "REFUSE"
    assert res["reasons"] == [2105, 2108, 2301]
    assert res["violation"]["kind"] == "PROVENANCE_AMOUNT" and res["violation"]["severity"] == 2
    assert world.ch.c.vault("wallet").tier() == tier_before - 1
    assert world.order(res["orderId"]).state == OrderState.REFUSED
    assert world.signer.kinds()[-2:] == [("RECORD_DECISION", "refused"), ("REPORT", "executed")]
    assert world.signer.requests[-1].report.kind == "INJECTION_ATTEMPT"


def test_s19_spam_is_dropped_and_reported(world):
    party = world.sim.parties["blaster"]
    signed = []
    for _ in range(3):
        inv, _hash = world.sim.issue_invoice(
            party,
            buyer_vault=world.agent.vault_address,
            buyer_agent_id=world.agent.agent_id,
            lines=[("ad-slot", "One premium ad slot, 24 hours", 500_000, 1)],
            quote_request_id="",
            memo="Exclusive offer: premium ad slots at launch prices, this week only.",
        )
        signed.append(inv)
    for inv in [*signed, signed[0]]:
        world.agent.runtime.inbox_invoice(json.loads(json.dumps(inv)))
    assert world.signer.kinds() == [("REPORT", "executed")]
    report = world.signer.requests[0].report
    assert report.kind == "SPAM" and len(report.spam_invoices) == 3
    drops = [e for e in world.agent.bus.since(0, 1000) if e.type == "decision" and e.data.get("outcome") == "DROPPED"]
    assert [e.reason_codes for e in drops] == [(2119,), (2119,), (2119,), (2107,)]


def test_request_ids_are_uuid4(world):
    world.register()
    world.agent.dialogue.owner_turn("freeze the wallet")
    [req] = [r for r in world.signer.requests if r.kind == "SELF_FREEZE"]
    assert uuid.UUID(req.request_id).version == 4
    assert world.signer.responses[-1].status == "executed"
    assert world.ch.c.vault("wallet").is_frozen()


def test_s18_maker_checker_owner_approval(world):
    from acb.agent.owner_actions import approval_from_card

    mid = world.register()
    vault = world.ch.c.vault("wallet")
    world.ch.send("autonomy", vault.set_tier(0, 3105, H))
    [res] = world.buy(mid, "atlas", "fx-live-15m", 2)
    assert res["outcome"] == "NEEDS_OWNER_APPROVAL" and res["reasons"] == [2200]
    oid = res["orderId"]
    ap = approval_from_card(res["approval"], w3=world.ch.w3, dep=world.ch.dep, c=world.ch.c)
    assert ap["orderId"] == oid and ap["amount"] == 400_000 and ap["asEscrow"] is False
    assert ap["name"] == "Atlas Data" and ap["lookalike"] is None
    # the vault's own NEEDS_OWNER_APPROVAL record is required: another invoice hash or amount has none
    from acb.agent.owner_actions import _needs_owner_record

    c, dep = world.ch.c, world.ch.dep
    assert _needs_owner_record(c, dep, oid, ap["invoiceHash"], 1, 400_000)
    assert not _needs_owner_record(c, dep, oid, "0x" + "00" * 32, 1, 400_000)
    assert not _needs_owner_record(c, dep, oid, ap["invoiceHash"], 1, 500_000)
    assert not _needs_owner_record(c, dep, "0x" + "12" * 32, ap["invoiceHash"], 1, 400_000)
    forged = json.loads(json.dumps(res["approval"]))
    forged["invoice"]["buyerVault"] = "0x" + "99" * 20  # breaks the signature and the buyer check
    with pytest.raises(ValueError):
        approval_from_card(forged, w3=world.ch.w3, dep=dep, c=c)
    world.ch.send(
        "owner",
        vault.owner_approve(
            ap["orderId"], ap["issuer"], ap["wallet"], ap["amount"], ap["expiry"], ap["asEscrow"], ap["escrowDeadline"]
        ),
    )
    out = world.agent.runtime.execute_approved(oid)
    assert out["status"] == "executed" and out["outcome"] == "PAY" and out["reasons"] == [2007]
    o = world.order(oid)
    assert o.state == OrderState.PAID and o.outcome == Outcome.ON_TIME
    assert ("PAY_APPROVED", "executed") in world.signer.kinds()
    with pytest.raises(ValueError, match="already exists"):  # the same card can never be approved twice
        approval_from_card(res["approval"], w3=world.ch.w3, dep=dep, c=c)
