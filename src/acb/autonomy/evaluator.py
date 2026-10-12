"""Autonomy operator, off-chain part (spec 5.4, process P5): rules 1-7 in order, first match wins.

1. Vault frozen -> no action. Outflows without a signer decision are still looked up while frozen and
   raise an owner alert, so a stolen key that freezes the vault itself cannot hide them.
2. **Unmatched outflow**: a ``Paid`` or ``EscrowOpened`` of the wallet vault that no signer decision
   accounts for (``GET /v1/decisions/{hash}`` on P3 with ``EVENTS_TOKEN``) -> ``freeze(3205)``. A stolen
   agent key used outside the signer is caught here. Every ``decisionHash`` is public on-chain, so the
   hash being known is not enough: see :func:`classify_outflow` (the logged decision must be an outflow
   decision for the same ``orderId`` and, once mined, the same transaction; a hash the vault recorded
   with ``DecisionRecorded`` never matches an outflow; one hash matches at most one outflow). Outflows
   already listed by an earlier ``freeze(3205)`` of the operator or the owner are not re-checked. If P3
   is unreachable the operator never freezes on that alone; after ``signer_unreachable_alert_after``
   consecutive checks it raises an owner alert, and it never promotes while it cannot check.
3. Own grade D -> ``setTier(0, 3105)`` and an owner alert (no freeze).
4. Own grade C and tier > 1 -> ``setTier(1, 3102)``.
5. Unsecured losses since the last tier change above ``loss_limit[tier]`` and tier > 0 ->
   ``setTier(tier - 1, 3103)``.
6. The promotion conditions of 5.3 hold, own grade in ``own_grade_promote``, the clean settlements since
   the last change involve at least ``min_distinct_payees`` payees and every one has a matching signer
   decision -> ``setTier(tier + 1, 3000)``.
7. Otherwise nothing.

Every transaction is simulated with ``eth_call`` first; a simulated revert is logged and nothing is
sent. Evidence ``acb-autonomy/1`` (canonical JSON, ``evidenceHash = chash(...)``) is saved under
``$ACB_OUT_DIR/autonomy/<evidenceHash>.json`` before the transaction. :func:`decide` is pure.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from acb.autonomy.config import AutonomyConfig, load_autonomy_config
from acb.bureau.indexer import Log, decode_raw
from acb.canonical import chash, write_canonical
from acb.chain.client import get_raw_logs_chunked
from acb.chain.contracts import AcbContract, VaultContract, VaultStatus
from acb.chain.tx import TxError, TxSender
from acb.types import GRADE_RANK, TIER_FROZEN, AutonomyResult, Deployments, Grade, NetworkProfile, Outcome

SCHEMA = "acb-autonomy/1"
R_PROMOTE = 3000
R_DEMOTE_OWN_C = 3102
R_DEMOTE_LOSS = 3103
R_DEMOTE_OWN_D = 3105
R_FREEZE_UNMATCHED = 3205
SETTLED = (int(Outcome.ON_TIME), int(Outcome.LATE))
OUTFLOW_KINDS = frozenset({"PAY", "ESCROW", "PAY_APPROVED"})
"""Signer request kinds whose decision may carry an outflow (RECORD_DECISION only records refusals)."""
OUTFLOW_OUTCOMES = frozenset({"PAY", "PAY_CAPPED", "ESCROW"})
"""Decision outcomes that allow an outflow (checked when the log entry carries an ``outcome``)."""

Lookup = Callable[[str], "dict[str, Any] | bool | None"]
"""``decision_hash -> entry`` (the signer's 7.2 answer: ``{"found": true, "kind", "orderId", "txHash",
...}``), ``False`` (the signer answered 404) or ``None`` (unreachable). Any other value is unmatched."""

MATCHED, PENDING, UNMATCHED, UNKNOWN = "matched", "pending", "unmatched", "unknown"


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def classify_outflow(entry: Any, *, order_id: str, tx: str, age_s: int, grace_s: int) -> str:
    """Does the signer's log entry account for this outflow (pure)?

    ``unknown``: the signer could not be asked. ``unmatched``: no entry, or the entry is for another
    order, is not an outflow decision (``kind`` / ``outcome``), or names another mined transaction.
    ``matched``: same order, an outflow kind, and the logged ``txHash`` is this event's transaction.
    ``pending``: same order and kind but the signer has not attached the mined ``txHash`` yet (it does
    so right after the receipt); after ``grace_s`` seconds a missing ``txHash`` is ``unmatched``.
    """
    if entry is None:
        return UNKNOWN
    if not isinstance(entry, dict) or entry.get("found") is not True:
        return UNMATCHED
    if _norm(entry.get("orderId")) != _norm(order_id):
        return UNMATCHED
    if str(entry.get("kind") or "") not in OUTFLOW_KINDS:
        return UNMATCHED
    if "outcome" in entry and str(entry.get("outcome") or "") not in OUTFLOW_OUTCOMES:
        return UNMATCHED
    logged_tx = _norm(entry.get("txHash"))
    if logged_tx:
        return MATCHED if logged_tx == _norm(tx) else UNMATCHED
    return PENDING if age_s <= grace_s else UNMATCHED


# ---------------------------------------------------------------------------
# signer decision log client
# ---------------------------------------------------------------------------


class SignerDecisionLog:
    """``GET http://127.0.0.1:<SIGNER_PORT>/v1/decisions/{hash}`` with ``EVENTS_TOKEN`` (spec 7.2)."""

    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: float = 3.0):
        port = os.environ.get("SIGNER_PORT") or "8702"
        self.base_url = (base_url or f"http://127.0.0.1:{port}").rstrip("/")
        self.token = token if token is not None else os.environ.get("EVENTS_TOKEN", "")
        self.timeout = timeout

    def __call__(self, decision_hash: str) -> dict[str, Any] | bool | None:
        import httpx

        try:
            r = httpx.get(
                f"{self.base_url}/v1/decisions/{decision_hash}",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self.timeout,
            )
        except httpx.HTTPError:
            return None
        if r.status_code == 404:
            return False
        if r.status_code != 200:
            return None  # 401/5xx: cannot tell, never treated as "not found"
        try:
            body = r.json()
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        return body if body.get("found") is True else False


# ---------------------------------------------------------------------------
# observation and the pure decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Outflow:
    event: str
    order_id: str
    payee_agent_id: int
    amount: int
    decision_hash: str
    block: int
    log_index: int
    tx: str


@dataclass(frozen=True)
class CleanOrder:
    order_id: str
    payee_agent_id: int
    amount: int
    decision_hash: str
    matched: bool | None
    tx: str
    log_index: int
    event: str


@dataclass(frozen=True)
class Observation:
    """What the operator saw as of one block."""

    vault: str
    as_of_block: int
    now: int
    status: VaultStatus
    min_clean_to_promote: int
    promotion_cooldown: int
    own_grade: Grade
    loss: int
    unmatched: tuple[Outflow, ...] = ()
    pending: int = 0
    signer_reachable: bool = True
    signer_alert: bool = False
    clean_orders: tuple[CleanOrder, ...] = ()
    grade_ref: dict | None = None


@dataclass(frozen=True)
class Plan:
    rule: str
    action: str = "none"  # none | freeze | set_tier
    to_tier: int | None = None
    reason_code: int | None = None
    alerts: tuple[str, ...] = ()
    detail: str = ""


def promotion_blockers(obs: Observation, cfg: AutonomyConfig) -> list[str]:
    """Why rule 6 does not promote (empty list = promote)."""
    st = obs.status
    out = []
    if st.tier + 1 > st.max_auto_tier:
        out.append("at maxAutoTier")
    if obs.now < st.last_tier_change_at + obs.promotion_cooldown:
        out.append("cooldown")
    if obs.min_clean_to_promote <= 0 or st.clean < obs.min_clean_to_promote:
        out.append(f"clean {st.clean} < {obs.min_clean_to_promote}")
    if st.points != 0:
        out.append(f"violation points {st.points}")
    if obs.own_grade not in cfg.own_grade_promote:
        out.append(f"own grade {obs.own_grade.name}")
    payees = {c.payee_agent_id for c in obs.clean_orders}
    if len(payees) < cfg.min_distinct_payees:
        out.append(f"{len(payees)} distinct payees < {cfg.min_distinct_payees}")
    if not obs.signer_reachable:
        out.append("signer decision log unreachable")
    if any(c.matched is not True for c in obs.clean_orders):
        out.append("a clean settlement has no signer decision")
    if obs.pending:
        out.append(f"{obs.pending} outflow(s) wait for the signer's mined transaction")
    return out


def decide(obs: Observation, cfg: AutonomyConfig) -> Plan:
    """Rules 1-7 of spec 5.4 (pure)."""
    st = obs.status
    if st.frozen or st.tier == TIER_FROZEN:
        if obs.unmatched:  # e.g. a stolen key that froze the vault itself: tell the owner before unfreezing
            orders = ", ".join(o.order_id for o in obs.unmatched)
            return Plan("frozen", alerts=(f"outflow without a signer decision (vault frozen): {orders}",))
        return Plan("frozen")
    if obs.unmatched:
        return Plan(
            "unmatched_outflow", "freeze", TIER_FROZEN, R_FREEZE_UNMATCHED, ("outflow without a signer decision",)
        )
    alerts: tuple[str, ...] = ("signer decision log unreachable",) if obs.signer_alert else ()
    if obs.own_grade == Grade.D:
        alerts = (*alerts, "the wallet's own grade is D: maker-checker until the owner reviews")
        if st.tier > 0:
            return Plan("own_grade_d", "set_tier", 0, R_DEMOTE_OWN_D, alerts)
        return Plan("own_grade_d", alerts=alerts, detail="already at tier 0")
    if obs.own_grade == Grade.C and st.tier > 1:
        return Plan("own_grade_c", "set_tier", 1, R_DEMOTE_OWN_C, alerts)
    if st.tier > 0 and obs.loss > cfg.loss_limit[st.tier]:
        return Plan("unsecured_loss", "set_tier", st.tier - 1, R_DEMOTE_LOSS, alerts)
    blockers = promotion_blockers(obs, cfg)
    if not blockers:
        return Plan("promote", "set_tier", st.tier + 1, R_PROMOTE, alerts)
    return Plan("none", alerts=alerts, detail="; ".join(blockers))


def effective_grade_at(
    records: list[tuple[int, Grade, Grade, int]], at: int, upgrade_delay: int, max_age: int
) -> Grade:
    """``AgentCreditBureau.effectiveGrade`` at time ``at`` from ``(postedAt, grade, prevGrade, prevPostedAt)``
    records (the latest last), as the vault saw it for the clean-record rule."""
    if not records:
        return Grade.NONE
    posted, g, prev, prev_posted = records[-1]
    if GRADE_RANK[g] > GRADE_RANK[prev] and at < posted + upgrade_delay:
        g, posted = prev, prev_posted
        if g == Grade.NONE:
            return Grade.NONE
    if max_age and at > posted + max_age:
        return g if g in (Grade.D, Grade.C) else Grade.NONE
    return g


# ---------------------------------------------------------------------------
# the operator
# ---------------------------------------------------------------------------


@dataclass
class _Logs:
    """Incremental, chunked log reader for one vault and the ACB (``GradePosted``)."""

    w3: Any
    vault: str
    acb: str
    start_block: int
    synced_to: int = -1
    vault_logs: list[Log] = field(default_factory=list)
    grade_logs: list[Log] = field(default_factory=list)
    timestamps: dict[int, int] = field(default_factory=dict)

    def sync(self, to_block: int) -> None:
        a = max(self.synced_to + 1, self.start_block)
        if to_block < a:
            return
        codec = self.w3.codec
        for raw in get_raw_logs_chunked(self.w3, self.vault, None, a, to_block):
            lg = decode_raw(raw, codec)
            if lg is not None:
                self.vault_logs.append(lg)
        for raw in get_raw_logs_chunked(self.w3, self.acb, None, a, to_block):
            lg = decode_raw(raw, codec)
            if lg is not None and lg.event == "GradePosted":
                self.grade_logs.append(lg)
        self.synced_to = to_block

    def ts(self, block: int) -> int:
        if block not in self.timestamps:
            self.timestamps[block] = int(self.w3.eth.get_block(block)["timestamp"])
        return self.timestamps[block]


class Evaluator:
    """The autonomy operator for one vault (default: the wallet's)."""

    def __init__(
        self,
        net: NetworkProfile,
        dep: Deployments,
        *,
        w3: Any,
        lookup: Lookup,
        account: Any | None = None,
        cfg: AutonomyConfig | None = None,
        out_dir: str | Path | None = None,
        bus: Any | None = None,
        vault_name: str = "wallet",
    ):
        from acb import config as acb_config

        self.net = net
        self.dep = dep
        self.w3 = w3
        self.lookup = lookup
        self.cfg = cfg or load_autonomy_config()
        self.out_dir = Path(out_dir) if out_dir is not None else acb_config.out_dir()
        self.bus = bus
        entry = dep.vaults[vault_name]
        self.vault_address = entry.address
        self.agent_id = int(entry.agent_id)
        self.vault = VaultContract(w3, entry.address)
        self.acb = AcbContract(w3, dep.contracts["AgentCreditBureau"].address)
        self.sender = TxSender(w3, account) if account is not None else None
        self.logs = _Logs(w3, entry.address, self.acb.address, int(dep.start_block))
        self.account_address = _norm(account.address) if account is not None else ""
        self._entries: dict[str, dict[str, Any]] = {}  # final signer entries (with a txHash) by hash
        self._covered: dict[str, set[tuple[str, int]] | None] = {}  # 3205 evidence hash -> listed outflows
        self._alerted: set[tuple[str, int]] = set()  # unmatched outflows already reported while frozen
        self.unreachable_streak = 0
        self.last_result: AutonomyResult | None = None
        self.last_plan: Plan | None = None
        self.lock = threading.Lock()

    # ---- observation ----
    def _emit(self, type_: str, data: dict, **kw: Any) -> None:
        if self.bus is not None:
            self.bus.emit(type_, data, **kw)

    def _lookup(self, decision_hash: str) -> Any:
        """The signer's entry for ``decision_hash``; entries with a mined ``txHash`` are final and cached."""
        if decision_hash in self._entries:
            return self._entries[decision_hash]
        entry = self.lookup(decision_hash)
        if isinstance(entry, dict) and entry.get("found") is True and entry.get("txHash"):
            self._entries[decision_hash] = entry
        return entry

    def _freeze_coverage(self, lg: Log) -> set[tuple[str, int]] | None:
        """The outflows a ``freeze(3205)`` listed in its evidence (None: the evidence file is not here)."""
        digest = _norm(lg.args.get("evidenceHash"))
        if digest not in self._covered:
            refs: set[tuple[str, int]] | None = None
            path = self.out_dir / "autonomy" / f"{digest}.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if chash(data) == digest and data.get("rule") == "unmatched_outflow":
                    refs = {(_norm(r["tx"]), int(r["logIndex"])) for r in data.get("refs") or []}
            except (OSError, ValueError, KeyError, TypeError):
                refs = None
            self._covered[digest] = refs
        return self._covered[digest]

    def _covered_outflows(self, logs: list[Log], outflows: list[Log], trusted: set[str]) -> set[tuple[int, int]]:
        """Positions of outflows an earlier ``freeze(3205)`` already reported (rule 2 does not re-check them).

        Only a freeze by the autonomy operator or the owner counts: the agent key may call ``freeze`` with
        any reason code. The outflows listed in that freeze's evidence are covered; without the evidence
        file, every outflow before the freeze is.
        """
        covered: set[tuple[int, int]] = set()
        for lg in logs:
            if lg.event != "TierChanged" or int(lg.args["toTier"]) != TIER_FROZEN:
                continue
            if int(lg.args["reasonCode"]) != R_FREEZE_UNMATCHED or _norm(lg.args.get("by")) not in trusted:
                continue
            refs = self._freeze_coverage(lg)
            for o in outflows:
                if o.pos < lg.pos and (refs is None or (_norm(o.tx), o.log_index) in refs):
                    covered.add(o.pos)
        return covered

    def _grade_records(self, agent_id: int, before: tuple[int, int]) -> list[tuple[int, Grade, Grade, int]]:
        out: list[tuple[int, Grade, Grade, int]] = []
        prev_posted = 0
        for lg in self.logs.grade_logs:
            if int(lg.args["agentId"]) != agent_id or lg.pos >= before:
                continue
            posted = self.logs.ts(lg.block)
            out.append((posted, Grade(int(lg.args["grade"])), Grade(int(lg.args["previousGrade"])), prev_posted))
            prev_posted = posted
        return out

    def observe(self) -> Observation:
        latest = int(self.w3.eth.block_number)
        self.logs.sync(latest)
        status = self.vault.status(block=latest)
        now = self.logs.ts(latest)
        frozen = status.frozen or status.tier == TIER_FROZEN
        min_clean = 0 if frozen else self.vault.tier_config(status.tier, block=latest).min_clean_to_promote
        cooldown = self.vault.promotion_cooldown(block=latest)
        max_age = self.vault.max_grade_age(block=latest)
        min_clean_amount = self.vault.min_clean_amount(block=latest)
        upgrade_delay = self.acb.upgrade_delay(block=latest)
        own = self.acb.effective_grade(self.agent_id, max_age, block=latest)
        logs = self.logs.vault_logs
        changes = [lg for lg in logs if lg.event == "TierChanged"]
        last_change = changes[-1].pos if changes else (0, -1)
        outflows = [lg for lg in logs if lg.event in ("Paid", "EscrowOpened")]
        opens = {str(lg.args["orderId"]): lg for lg in outflows}

        # rule 2: does a signer decision account for each outflow (classify_outflow)?
        trusted = {_norm(self.vault.autonomy_operator(block=latest)), _norm(self.vault.owner(block=latest))}
        trusted |= {self.account_address} - {""}
        covered = self._covered_outflows(logs, outflows, trusted)
        recorded = {_norm(lg.args["decisionHash"]) for lg in logs if lg.event == "DecisionRecorded"}
        verdicts: dict[str, str] = {}
        used: set[str] = set()
        unmatched: list[Outflow] = []
        reachable = True
        pending = 0
        for lg in outflows:
            order, dh = str(lg.args["orderId"]), _norm(lg.args["decisionHash"])
            if lg.pos in covered:
                verdicts[order] = UNMATCHED
                continue
            if dh in recorded or dh in used:  # a refusal / approval request, or a hash used twice
                verdict = UNMATCHED
            else:
                verdict = classify_outflow(
                    self._lookup(dh),
                    order_id=order,
                    tx=lg.tx,
                    age_s=max(0, now - self.logs.ts(lg.block)),
                    grace_s=self.cfg.decision_tx_grace_s,
                )
            verdicts[order] = verdict
            if verdict in (MATCHED, PENDING):
                used.add(dh)
            if verdict == UNKNOWN:
                reachable = False
            elif verdict == PENDING:
                pending += 1
            elif verdict == UNMATCHED:
                unmatched.append(
                    Outflow(
                        lg.event,
                        order,
                        int(lg.args["payeeAgentId"]),
                        int(lg.args["amount"]),
                        str(lg.args["decisionHash"]),
                        lg.block,
                        lg.log_index,
                        lg.tx,
                    )
                )
        loss = 0
        clean: list[CleanOrder] = []
        for lg in logs:
            if lg.pos <= last_change or lg.event not in ("DeliveryRecorded", "EscrowReleased"):
                continue
            opened = opens.get(str(lg.args["orderId"]))
            if opened is None:
                continue
            outcome = int(lg.args["outcome"])
            amount = int(opened.args["amount"])
            payee = int(opened.args["payeeAgentId"])
            if lg.event == "DeliveryRecorded" and outcome == int(Outcome.FAILED) and opened.event == "Paid":
                loss += amount
            if outcome not in SETTLED or amount < min_clean_amount:
                continue
            grade = effective_grade_at(
                self._grade_records(payee, lg.pos), self.logs.ts(lg.block), upgrade_delay, max_age
            )
            if grade not in (Grade.A, Grade.B):
                continue
            verdict = verdicts.get(str(lg.args["orderId"]), UNMATCHED)
            matched = True if verdict == MATCHED else None if verdict in (PENDING, UNKNOWN) else False
            clean.append(
                CleanOrder(
                    str(lg.args["orderId"]),
                    payee,
                    amount,
                    str(opened.args["decisionHash"]),
                    matched,
                    lg.tx,
                    lg.log_index,
                    lg.event,
                )
            )
        self.unreachable_streak = 0 if reachable else self.unreachable_streak + 1
        alert = self.unreachable_streak >= self.cfg.signer_unreachable_alert_after
        grade_logs = [lg for lg in self.logs.grade_logs if int(lg.args["agentId"]) == self.agent_id]
        grade_ref = (
            {"tx": grade_logs[-1].tx, "logIndex": grade_logs[-1].log_index, "event": "GradePosted"}
            if grade_logs
            else None
        )
        return Observation(
            vault=self.vault_address,
            as_of_block=latest,
            now=now,
            status=status,
            min_clean_to_promote=min_clean,
            promotion_cooldown=cooldown,
            own_grade=own,
            loss=loss,
            unmatched=tuple(unmatched),
            pending=pending,
            signer_reachable=reachable,
            signer_alert=alert,
            clean_orders=tuple(clean),
            grade_ref=grade_ref,
        )

    # ---- evidence and execution ----
    def evidence(self, obs: Observation, plan: Plan) -> dict[str, Any]:
        st = obs.status
        refs: list[dict] = []
        if plan.rule == "unmatched_outflow":
            refs = [{"tx": o.tx, "logIndex": o.log_index, "event": o.event} for o in obs.unmatched]
        elif plan.rule == "promote":
            refs = [{"tx": c.tx, "logIndex": c.log_index, "event": c.event} for c in obs.clean_orders]
        elif plan.rule in ("own_grade_d", "own_grade_c") and obs.grade_ref:
            refs = [obs.grade_ref]
        return {
            "schema": SCHEMA,
            "vault": obs.vault,
            "asOfBlock": obs.as_of_block,
            "fromTier": st.tier,
            "toTier": plan.to_tier,
            "rule": plan.rule,
            "reasonCode": plan.reason_code,
            "status": {
                "tier": st.tier,
                "frozen": st.frozen,
                "tierBeforeFreeze": st.tier_before_freeze,
                "maxAutoTier": st.max_auto_tier,
                "balance": str(st.balance),
                "escrowLocked": str(st.escrow_locked),
                "reserveFloor": str(st.reserve_floor),
                "spent": str(st.spent),
                "count": st.count,
                "clean": st.clean,
                "points": st.points,
                "lastTierChangeAt": st.last_tier_change_at,
            },
            "ownGrade": obs.own_grade.name,
            "loss": str(obs.loss),
            "cleanOrders": [
                {
                    "orderId": c.order_id,
                    "payeeAgentId": str(c.payee_agent_id),
                    "amount": str(c.amount),
                    "decisionHash": c.decision_hash,
                    "matched": c.matched,
                }
                for c in obs.clean_orders
            ],
            "refs": refs,
        }

    def evaluate(self, *, dry_run: bool = False) -> AutonomyResult:
        """Observe, decide, and (unless ``dry_run``) simulate and send the one transaction of the plan."""
        with self.lock:
            obs = self.observe()
            plan = decide(obs, self.cfg)
            self.last_plan = plan
            fresh = {(_norm(o.tx), o.log_index) for o in obs.unmatched} - self._alerted
            if plan.rule != "frozen" or fresh:  # while frozen, each unmatched outflow is reported once
                for alert in plan.alerts:
                    self._emit("info", {"alert": alert, "vault": obs.vault, "rule": plan.rule})
            if plan.rule == "frozen":
                self._alerted |= fresh
            result = AutonomyResult(
                rule=plan.rule,
                from_tier=obs.status.tier,
                to_tier=plan.to_tier,
                reason_code=plan.reason_code,
                evidence_hash=None,
                tx=None,
                simulated_revert=None,
            )
            if plan.action == "none":
                self.last_result = result
                return result
            evidence = self.evidence(obs, plan)
            digest = chash(evidence)
            write_canonical(self.out_dir / "autonomy" / f"{digest}.json", evidence)
            result = result.model_copy(update={"evidence_hash": digest})
            if plan.action == "freeze":
                fn = self.vault.freeze(int(plan.reason_code), digest)
            else:
                fn = self.vault.set_tier(int(plan.to_tier), int(plan.reason_code), digest)
            if self.sender is None:
                result = result.model_copy(update={"simulated_revert": "no autonomy key loaded"})
                self.last_result = result
                return result
            decoded = self.sender.simulate(fn)
            if decoded is not None:
                result = result.model_copy(update={"simulated_revert": decoded.text})
                self._emit(
                    "error",
                    {"vault": obs.vault, "rule": plan.rule, "simulatedRevert": decoded.text},
                    reason_codes=[plan.reason_code],
                )
                self.last_result = result
                return result
            if dry_run:
                self.last_result = result
                return result
            try:
                tx = self.sender.send(fn, simulate=False)
            except TxError as exc:
                self._emit("error", {"vault": obs.vault, "rule": plan.rule, "error": str(exc)})
                raise
            result = result.model_copy(update={"tx": tx})
            self._emit(
                "tier",
                {
                    "vault": obs.vault,
                    "fromTier": obs.status.tier,
                    "toTier": plan.to_tier,
                    "rule": plan.rule,
                    "evidenceHash": digest,
                    "status": tx.status,
                },
                reason_codes=[plan.reason_code],
                tx_hash=tx.hash,
            )
            self.last_result = result
            return result

    def run_forever(self, stop: threading.Event, poll_s: float | None = None) -> None:
        """Evaluate every ``poll_s`` seconds until ``stop`` is set (errors are logged, never raised)."""
        interval = float(self.cfg.poll_s if poll_s is None else poll_s)
        while not stop.wait(interval):
            try:
                self.evaluate()
            except Exception as exc:  # noqa: BLE001 - the loop must survive RPC hiccups
                self._emit("error", {"error": f"evaluate failed: {type(exc).__name__}: {exc}"})


__all__ = [
    "SCHEMA",
    "CleanOrder",
    "Evaluator",
    "Observation",
    "Outflow",
    "Plan",
    "SignerDecisionLog",
    "classify_outflow",
    "decide",
    "effective_grade_at",
    "promotion_blockers",
]
