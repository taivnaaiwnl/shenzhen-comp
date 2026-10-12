"""Hand-built scorecard inputs for the pure bureau tests (spec 15.2: WP4's stub for chain data).

:class:`Market` builds :class:`~acb.types.ScoreInputs` and :class:`~acb.bureau.facts.ChainFacts` without a
chain: agents with grades and owners, orders between vaults with outcomes, and reports. Addresses are
derived from agent ids so every agent has a distinct owner unless a test says otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from acb.bureau.facts import ChainFacts, HistoryEntry, RefusalRecord, order_key
from acb.canonical import keccak_hex
from acb.types import AgentInfo, Grade, OrderRecord, ReportRecord, ScoreInputs

AS_OF_BLOCK = 1000
AS_OF_TS = 1_800_000_000
AGE = 900  # spec 4.5: every golden subject is 900 s old
USDC = 1_000_000


def addr(tag: int, kind: int) -> str:
    """A distinct lowercase address for (tag, kind): kind 1 owner, 2 vault, 3 vault agent, 4 wallet."""
    return "0x" + f"{kind:02x}" + f"{tag:038x}"


def h32(text: str) -> str:
    return keccak_hex(text.encode())


@dataclass
class Market:
    """A small market as of :data:`AS_OF_BLOCK`."""

    as_of_block: int = AS_OF_BLOCK
    as_of_ts: int = AS_OF_TS
    agents: dict[int, dict] = field(default_factory=dict)
    orders: list[OrderRecord] = field(default_factory=list)
    reports: list[ReportRecord] = field(default_factory=list)
    refusals: dict[str, RefusalRecord] = field(default_factory=dict)
    wallets: dict[int, str] = field(default_factory=dict)
    _block: int = 100
    _seq: int = 0

    # ---- agents and vaults ----
    def agent(
        self,
        agent_id: int,
        grade: Grade = Grade.NEW,
        *,
        owner: str | None = None,
        vault_owner: str | None = None,
        has_vault: bool = True,
        wallet: str | None = None,
        age: int = AGE,
    ) -> int:
        self.agents[agent_id] = {
            "grade": grade,
            "owner": owner or addr(agent_id, 1),
            "vault_owner": vault_owner or addr(agent_id, 1),
            "has_vault": has_vault,
            "registered_ts": self.as_of_ts - age,
        }
        self.wallets[agent_id] = (wallet or addr(agent_id, 4)).lower()
        return agent_id

    def grade(self, agent_id: int, grade: Grade) -> None:
        self.agents[agent_id]["grade"] = grade

    def vault(self, agent_id: int) -> str:
        return addr(agent_id, 2)

    def vault_agent(self, agent_id: int) -> str:
        return addr(agent_id, 3)

    def _next(self) -> tuple[int, int]:
        self._seq += 1
        self._block += 1
        return self._block, 0

    # ---- orders ----
    def order(
        self,
        payer: int,
        payee: int,
        amount: int,
        outcome: str = "ON_TIME",
        *,
        kind: str = "escrow",
        invoice_ok: bool = True,
        vault: str | None = None,
    ) -> OrderRecord:
        block, log_index = self._next()
        oid = h32(f"order-{self._seq}")
        record = OrderRecord(
            vault=vault or self.vault(payer),
            order_id=oid,
            payer_agent_id=payer,
            payee_agent_id=payee,
            amount=amount,
            kind=kind,
            outcome=outcome,
            invoice_hash=h32(f"invoice-{self._seq}"),
            invoice_ok=invoice_ok,
            block=block,
            log_index=log_index,
            open_tx=h32(f"open-{self._seq}"),
            close_tx=None if outcome == "PENDING" else h32(f"close-{self._seq}"),
            close_log_index=None if outcome == "PENDING" else 1,
            timestamp=self.as_of_ts - 100,
        )
        self.orders.append(record)
        return record

    def orders_n(self, payer: int, payee: int, count: int, amount: int, outcome: str = "ON_TIME", **kw) -> None:
        for _ in range(count):
            self.order(payer, payee, amount, outcome, **kw)

    # ---- reports ----
    def report(
        self,
        reporter_agent: int,
        subject: int,
        kind: str,
        *,
        reporter: str | None = None,
        vault: str | None = "default",
        order_id: str | None = None,
        payload: dict | None = None,
        value: int | None = None,
        revoked: bool = False,
        timestamp: int | None = None,
        feedback_hash: str | None = None,
    ) -> ReportRecord:
        from acb.canonical import chash

        block, log_index = self._next()
        if vault == "default":
            vault = self.vault(reporter_agent)
        fh = feedback_hash or (chash(payload) if payload is not None else h32(f"fb-{self._seq}"))
        record = ReportRecord(
            reporter=(reporter or self.vault_agent(reporter_agent)).lower(),
            reporter_agent_id=reporter_agent,
            subject_agent_id=subject,
            kind=kind,
            value=value if value is not None else (1 if kind == "POSITIVE" else -1),
            order_id=order_id if order_id is not None else h32(f"rep-order-{self._seq}"),
            vault=vault,
            feedback_index=self._seq,
            feedback_hash=fh,
            tx=h32(f"rep-tx-{self._seq}"),
            log_index=log_index,
            block=block,
            timestamp=timestamp if timestamp is not None else self.as_of_ts - 50,
            revoked=revoked,
            payload=payload,
        )
        self.reports.append(record)
        return record

    def refusal(self, vault: str, order_id: str, payee: int, invoice_hash: str) -> RefusalRecord:
        block, log_index = self._next()
        rec = RefusalRecord(
            vault=vault,
            order_id=order_id,
            payee_agent_id=payee,
            invoice_hash=invoice_hash,
            block=block,
            log_index=log_index,
            tx=h32(f"refusal-{self._seq}"),
        )
        self.refusals[order_key(vault, order_id)] = rec
        return rec

    # ---- outputs ----
    def inputs(self, subject: int, previous: dict | None = None) -> ScoreInputs:
        agents = {}
        for aid, a in self.agents.items():
            agents[aid] = AgentInfo(
                agent_id=aid,
                registered_block=10,
                registered_timestamp=a["registered_ts"],
                identity_owner=a["owner"],
                vault_owners=(a["vault_owner"],) if a["has_vault"] else (),
                grade=a["grade"],
                anchor=False,
            )
        return ScoreInputs(
            chain_id=31337,
            as_of_block=self.as_of_block,
            as_of_block_hash=h32("as-of"),
            as_of_timestamp=self.as_of_ts,
            subject_agent_id=subject,
            agents=agents,
            orders=tuple(self.orders),
            reports=tuple(self.reports),
            contracts={"acb": addr(1, 9), "identity": addr(2, 9), "reputation": addr(3, 9)},
            previous=previous,
        )

    def facts(self) -> ChainFacts:
        vaults = tuple(sorted(self.vault(a) for a, info in self.agents.items() if info["has_vault"]))
        return ChainFacts(
            vaults=vaults,
            vault_agent_ids={self.vault(a): a for a, info in self.agents.items() if info["has_vault"]},
            vault_agents={
                self.vault(a): (HistoryEntry(1, 0, self.vault_agent(a)),)
                for a, info in self.agents.items()
                if info["has_vault"]
            },
            wallets={a: (HistoryEntry(1, 0, w),) for a, w in self.wallets.items()},
            refusals=dict(self.refusals),
        )
