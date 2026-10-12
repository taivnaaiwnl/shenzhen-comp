"""From indexed logs and published files to :class:`~acb.types.ScoreInputs` and
:class:`~acb.bureau.facts.ChainFacts` as of one block (spec 4.1).

Everything is derived from logs at or before ``asOfBlock`` and from hash-checked files:

- **Identity owner** of an agentId: the ``to`` of its last ERC-721 ``Transfer``.
- **agentWallet** history: ``MetadataSet(agentId, "agentWallet", value)`` (the official registry emits it
  on register, ``setAgentWallet``, ``unsetAgentWallet`` and every transfer); an empty value means unset.
- **Vault owner and agent** history: ``VaultInitialized``, ``OwnershipTransferred``, ``AgentChanged``.
- **Orders**: ``Paid`` / ``EscrowOpened`` and their closing events; the outcome table of 4.1.
- **Counted-order invoice check**: the published invoice's hash, its EIP-191 signer (the payee's
  agentWallet as of the order's block), ``buyerVault``, ``issuerAgentId`` and ``amount``.
- **Grades** of counterparties and reporters: the latest ``GradePosted`` (raw, no upgrade delay).
- **Reports**: ``NewFeedback`` with tag ``acb-report``; ``FeedbackRevoked`` marks them revoked.

No historical ``eth_call`` is made, so recompute works without an archive node.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from acb.bureau.config import ScorecardConfig
from acb.bureau.evidence import EvidenceStore, check_invoice
from acb.bureau.facts import ChainFacts, HistoryEntry, RefusalRecord, order_key, value_at
from acb.bureau.indexer import REPORT_TAG, Indexer, Log
from acb.types import AgentInfo, Decision, Grade, OrderRecord, Outcome, ReportRecord, ScoreInputs

ZERO_ADDRESS = "0x" + "00" * 20
ENDPOINT_RE = re.compile(r"^acb:v1;order=(0x[0-9a-fA-F]{64});reporter=([0-9]{1,30});vault=(0x[0-9a-fA-F]{40})$")
REFUND_OUTCOME = {2400: "FAILED", 2401: "EXCLUDED", 2402: "EXCLUDED", 2403: "CLAWBACK"}


def parse_endpoint(endpoint: str) -> tuple[str | None, int | None, str | None]:
    """``acb:v1;order=<0x orderId>;reporter=<agentId>;vault=<0x vault>`` -> (orderId, reporter, vault)."""
    m = ENDPOINT_RE.fullmatch(endpoint.strip()) if isinstance(endpoint, str) else None
    if not m:
        return None, None, None
    return m.group(1).lower(), int(m.group(2)), m.group(3).lower()


def report_endpoint(order_id: str, reporter_agent_id: int, vault: str) -> str:
    """The endpoint string a reporter must use (spec 4.1)."""
    return f"acb:v1;order={order_id.lower()};reporter={int(reporter_agent_id)};vault={vault.lower()}"


def _wallet_value(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw.startswith("0x"):
        return None
    body = raw[2:]
    if len(body) == 40:
        value = "0x" + body.lower()
        return None if value == ZERO_ADDRESS else value
    return None


def identity_histories(
    index: Indexer, as_of: int
) -> tuple[dict[int, int], dict[int, tuple[HistoryEntry, ...]], dict[int, tuple[HistoryEntry, ...]]]:
    """From identity-registry logs up to ``as_of``: registration block, owner history and agentWallet
    history of every indexed agentId (spec 4.1, events-derived; no historical ``eth_call``)."""
    registered: dict[int, int] = {}
    owners: dict[int, list[HistoryEntry]] = {}
    wallets: dict[int, list[HistoryEntry]] = {}
    for lg in index.identity_events(as_of):
        if lg.event == "Registered":
            aid = int(lg.args["agentId"])
            registered.setdefault(aid, lg.block)
        elif lg.event == "Transfer":
            aid = int(lg.args["tokenId"])
            to = str(lg.args["to"]).lower()
            owners.setdefault(aid, []).append(HistoryEntry(lg.block, lg.log_index, None if to == ZERO_ADDRESS else to))
        elif lg.event == "MetadataSet" and lg.args.get("metadataKey") == "agentWallet":
            aid = int(lg.args["agentId"])
            wallets.setdefault(aid, []).append(
                HistoryEntry(lg.block, lg.log_index, _wallet_value(lg.args.get("metadataValue")))
            )
    return (
        registered,
        {aid: tuple(h) for aid, h in owners.items()},
        {aid: tuple(h) for aid, h in wallets.items()},
    )


@dataclass
class Snapshot:
    """Everything the scorecard needs as of one block, shared by every subject."""

    chain_id: int
    as_of_block: int
    as_of_block_hash: str
    as_of_timestamp: int
    agents: dict[int, AgentInfo]
    orders: tuple[OrderRecord, ...]
    reports: tuple[ReportRecord, ...]
    facts: ChainFacts
    contracts: dict[str, str]
    postings: dict[int, list[Log]] = field(default_factory=dict)
    universe: tuple[int, ...] = ()
    identity_owners: dict[int, str] = field(default_factory=dict)
    _base: ScoreInputs | None = None

    def previous(self, agent_id: int) -> dict | None:
        """The subject's latest posting at or before ``asOfBlock`` (bundle field ``previous``)."""
        logs = self.postings.get(int(agent_id))
        if not logs:
            return None
        last = logs[-1]
        return {
            "grade": Grade(int(last.args["grade"])).name,
            "evidenceHash": str(last.args["evidenceHash"]).lower(),
            "tx": last.tx,
        }

    def last_posting(self, agent_id: int) -> Log | None:
        logs = self.postings.get(int(agent_id))
        return logs[-1] if logs else None

    def inputs_for(self, agent_id: int) -> ScoreInputs:
        """:class:`ScoreInputs` for one subject (the shared parts are the same objects for every subject)."""
        if self._base is None:
            self._base = ScoreInputs(
                chain_id=self.chain_id,
                as_of_block=self.as_of_block,
                as_of_block_hash=self.as_of_block_hash,
                as_of_timestamp=self.as_of_timestamp,
                subject_agent_id=0,
                agents=self.agents,
                orders=self.orders,
                reports=self.reports,
                contracts=self.contracts,
                previous=None,
            )
        return self._base.model_copy(update={"subject_agent_id": int(agent_id), "previous": self.previous(agent_id)})


def build_snapshot(index: Indexer, as_of_block: int, cfg: ScorecardConfig, store: EvidenceStore) -> Snapshot:
    """Build the :class:`Snapshot` as of ``as_of_block`` (the index must be synced that far)."""
    if index.synced_to < as_of_block:
        index.sync(as_of_block)
    as_of = int(as_of_block)
    ts_of = index.timestamp
    as_of_ts, as_of_hash = index.block(as_of)

    # ---- identity registry: registration, owners, agentWallet -----------------------------------
    registered, owners, wallets = identity_histories(index, as_of)

    # ---- vaults: owners, agents, orders, refusals ------------------------------------------------
    vault_ids = index.registered_vaults(as_of)
    vault_owner_hist: dict[str, list[HistoryEntry]] = {}
    vault_agent_hist: dict[str, list[HistoryEntry]] = {}
    refusals: dict[str, RefusalRecord] = {}
    opens: list[tuple[str, Log]] = []
    closes: dict[str, Log] = {}
    for vault in sorted(vault_ids):
        for lg in index.vault_events(vault, as_of):
            a = lg.args
            if lg.event == "VaultInitialized":
                vault_owner_hist.setdefault(vault, []).append(
                    HistoryEntry(lg.block, lg.log_index, str(a["owner"]).lower())
                )
                vault_agent_hist.setdefault(vault, []).append(
                    HistoryEntry(lg.block, lg.log_index, str(a["agent"]).lower())
                )
            elif lg.event == "OwnershipTransferred":
                vault_owner_hist.setdefault(vault, []).append(
                    HistoryEntry(lg.block, lg.log_index, str(a["to"]).lower())
                )
            elif lg.event == "AgentChanged":
                vault_agent_hist.setdefault(vault, []).append(
                    HistoryEntry(lg.block, lg.log_index, str(a["newAgent"]).lower())
                )
            elif lg.event in ("Paid", "EscrowOpened"):
                opens.append((vault, lg))
            elif lg.event in ("DeliveryRecorded", "EscrowReleased", "EscrowRefunded"):
                closes.setdefault(order_key(vault, str(a["orderId"])), lg)
            elif lg.event == "DecisionRecorded" and int(a["decision"]) == int(Decision.REFUSE):
                key = order_key(vault, str(a["orderId"]))
                refusals.setdefault(
                    key,
                    RefusalRecord(
                        vault=vault,
                        order_id=str(a["orderId"]).lower(),
                        payee_agent_id=int(a["payeeAgentId"]),
                        invoice_hash=str(a["invoiceHash"]).lower(),
                        block=lg.block,
                        log_index=lg.log_index,
                        tx=lg.tx,
                    ),
                )

    wallet_hist = wallets
    facts_wallets = dict(wallets)

    orders: list[OrderRecord] = []
    invoice_notes: dict[str, str] = {}
    for vault, lg in opens:
        a = lg.args
        oid = str(a["orderId"]).lower()
        key = order_key(vault, oid)
        payee = int(a["payeeAgentId"])
        amount = int(a["amount"])
        ihash = str(a["invoiceHash"]).lower()
        kind = "pay" if lg.event == "Paid" else "escrow"
        close = closes.get(key)
        outcome = "PENDING"
        if close is not None:
            closing = {"pay": "DeliveryRecorded", "escrow": "EscrowReleased"}[kind]
            if close.event == closing:
                outcome = Outcome(int(close.args["outcome"])).name
            elif close.event == "EscrowRefunded" and kind == "escrow":
                outcome = REFUND_OUTCOME.get(int(close.args["reasonCode"]), "EXCLUDED")
            if outcome not in ("ON_TIME", "LATE", "FAILED", "EXCLUDED", "CLAWBACK"):
                outcome = "EXCLUDED"
        wallet = value_at(wallet_hist.get(payee), lg.block, lg.log_index)
        note = check_invoice(
            store.invoice(ihash),
            invoice_hash_hex=ihash,
            wallet=wallet,
            vault=vault,
            payee_agent_id=payee,
            amount=amount,
        )
        if note:
            invoice_notes[key] = note
        orders.append(
            OrderRecord(
                vault=vault,
                order_id=oid,
                payer_agent_id=vault_ids[vault],
                payee_agent_id=payee,
                amount=amount,
                kind=kind,
                outcome=outcome,
                invoice_hash=ihash,
                invoice_ok=note == "",
                block=lg.block,
                log_index=lg.log_index,
                open_tx=lg.tx,
                close_tx=close.tx if close is not None else None,
                close_log_index=close.log_index if close is not None else None,
                timestamp=ts_of(close.block if close is not None else lg.block),
            )
        )
    orders.sort(key=lambda o: (o.block, o.log_index))

    # ---- grades ---------------------------------------------------------------------------------
    postings = index.postings(as_of)
    grades = {aid: Grade(int(logs[-1].args["grade"])) for aid, logs in postings.items()}

    # ---- agents ---------------------------------------------------------------------------------
    universe = tuple(sorted(a for a in index.universe if a in registered))
    vault_owners_by_agent: dict[int, list[str]] = {}
    for vault, aid in vault_ids.items():
        owner = value_at(tuple(vault_owner_hist.get(vault, ())), as_of)
        if owner:
            vault_owners_by_agent.setdefault(aid, []).append(owner)
    agents: dict[int, AgentInfo] = {}
    identity_owners: dict[int, str] = {}
    for aid in universe:
        owner = value_at(owners.get(aid), as_of) or ZERO_ADDRESS
        identity_owners[aid] = owner
        agents[aid] = AgentInfo(
            agent_id=aid,
            registered_block=registered[aid],
            registered_timestamp=ts_of(registered[aid]),
            identity_owner=owner,
            vault_owners=tuple(sorted(set(vault_owners_by_agent.get(aid, [])))),
            grade=grades.get(aid, Grade.NONE),
            anchor=aid in cfg.anchor_set,
        )

    # ---- reports --------------------------------------------------------------------------------
    revoked: set[tuple[int, str, int]] = set()
    feedback: list[Log] = []
    for lg in index.reputation_events(as_of):
        if lg.event == "FeedbackRevoked":
            revoked.add((int(lg.args["agentId"]), str(lg.args["clientAddress"]).lower(), int(lg.args["feedbackIndex"])))
        elif lg.event == "NewFeedback" and lg.args.get("tag1") == REPORT_TAG:
            feedback.append(lg)
    reports: list[ReportRecord] = []
    for lg in feedback:
        a = lg.args
        subject = int(a["agentId"])
        if subject not in agents:
            continue
        client = str(a["clientAddress"]).lower()
        order_id, reporter_id, vault = parse_endpoint(str(a.get("endpoint", "")))
        decimals = int(a.get("valueDecimals", 0))
        value = int(a["value"]) if decimals == 0 else 0
        fhash = str(a["feedbackHash"]).lower()
        reports.append(
            ReportRecord(
                reporter=client,
                reporter_agent_id=reporter_id if reporter_id is not None else 0,
                subject_agent_id=subject,
                kind=str(a.get("tag2", "")),
                value=value,
                order_id=order_id,
                vault=vault,
                feedback_index=int(a["feedbackIndex"]),
                feedback_hash=fhash,
                tx=lg.tx,
                log_index=lg.log_index,
                block=lg.block,
                timestamp=ts_of(lg.block),
                revoked=(subject, client, int(a["feedbackIndex"])) in revoked,
                payload=store.report(fhash),
            )
        )

    facts = ChainFacts(
        vaults=tuple(sorted(vault_ids)),
        vault_agent_ids=dict(vault_ids),
        vault_agents={v: tuple(h) for v, h in vault_agent_hist.items()},
        wallets=facts_wallets,
        refusals=refusals,
        invoice_notes=invoice_notes,
    )
    return Snapshot(
        chain_id=int(index.dep.chain_id),
        as_of_block=as_of,
        as_of_block_hash=as_of_hash,
        as_of_timestamp=as_of_ts,
        agents=agents,
        orders=tuple(orders),
        reports=tuple(reports),
        facts=facts,
        contracts={"acb": index.acb, "identity": index.identity, "reputation": index.reputation},
        postings=postings,
        universe=universe,
        identity_owners=identity_owners,
    )


__all__ = ["ENDPOINT_RE", "Snapshot", "build_snapshot", "identity_histories", "parse_endpoint", "report_endpoint"]
