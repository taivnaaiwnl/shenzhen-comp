"""Grade posting and ERC-8004 mirroring with the bureau key (spec 4.10), monitoring triggers (4.7).

``Bureau.rescore(rounds)`` runs up to ``rounds`` rounds; each round:

1. ``asOfBlock = latest - confirmations`` (anvil 0, base_sepolia 2), and never before the last posting of
   the previous round, so every round reads the grades the previous round posted.
2. Index the chain up to ``asOfBlock``, build the snapshot, score every agent of the grading universe.
3. For each agent whose grade, reasons or score (by ``repost_min_score_delta``) changed since its last
   posting, or that was never posted and is either listed in ``deployments/<network>.json`` or has
   evidence of its own (a counted order or a bound report; see :func:`has_evidence`): write the bundle, ``ACB.postGrade(...)``, then mirror on the
   ERC-8004 Reputation registry (revoke the bureau's previous ``acb-grade`` feedback, then
   ``giveFeedback(agentId, score, 0, "acb-grade", "acb-sc-1.0.0", "", evidenceURI, evidenceHash)``),
   and emit a ``grade`` event.

Anyone can add agentIds to the grading universe for free (``recordDecision`` / ``ownerApprove`` of a
registered vault name any payee), so a first posting needs one of the two conditions above: otherwise
such names would drain the bureau key and post feedback about unrelated agents. They are still scored
(and their bundles kept in memory), only not posted, until they have evidence.

The bureau never posts about an agent it owns (``SelfGrading``), never about agents outside the
grading universe, and simulates every transaction before sending it (``TxSender``).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from acb.bureau.config import ARCHIVE_DIR, ScorecardConfig, archive_config, load_config
from acb.bureau.evidence import EvidenceStore, bundle_uri, default_base_uri, posted_values, write_bundle
from acb.bureau.indexer import Indexer
from acb.bureau.inputs import Snapshot, build_snapshot
from acb.bureau.models import load_model
from acb.chain.contracts import AcbContract, ReputationContract
from acb.chain.tx import TxError, TxSender, TxSimulationError
from acb.types import GRADE_RANK, Bundle, Deployments, Grade, GradeView, NetworkProfile, RescoreResult, TxResult


@dataclass
class Posting:
    """One posting decision of a round."""

    agent_id: int
    bundle: Bundle
    evidence_hash: str
    evidence_uri: str
    bundle_path: Path | None
    posted: bool
    reason: str
    tx: list[TxResult] = field(default_factory=list)
    grade_view: GradeView | None = None


SKIP_NO_EVIDENCE = "skipped: never posted, not in the deployment and no evidence of its own"


def _wait_noop(_s: float) -> None:  # pragma: no cover - replaced in tests
    time.sleep(_s)


def has_evidence(bundle: Bundle) -> bool:
    """A counted order (either role) or a bound report about the subject (spec 4.1, 4.2)."""
    return any(o.get("counted") for o in bundle.orders) or any(r.get("bound") for r in bundle.reports)


def psi_x10000(before: dict[str, int], after: dict[str, int]) -> int:
    """Population stability index between two grade distributions, times 10,000 (0.0001 smoothing)."""
    keys = sorted(set(before) | set(after))
    nb, na = sum(before.values()), sum(after.values())
    if not nb or not na:
        return 0
    total = 0.0
    for k in keys:
        p = max(before.get(k, 0) / nb, 0.0001)
        q = max(after.get(k, 0) / na, 0.0001)
        total += (q - p) * math.log(q / p)
    return round(total * 10_000)


class Bureau:
    """The bureau of one deployment: indexer, scorecard, poster (holds the bureau key when posting)."""

    def __init__(
        self,
        net: NetworkProfile,
        dep: Deployments,
        *,
        w3: Any,
        w3_logs: Any | None = None,
        account: Any | None = None,
        cfg: ScorecardConfig | None = None,
        out_dir: str | Path | None = None,
        base_uri: str | None = None,
        store: EvidenceStore | None = None,
        bus: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        archive_dir: str | Path | None = None,
    ):
        from acb import config as acb_config

        self.net = net
        self.dep = dep
        self.w3 = w3
        self.cfg = cfg or load_config(dep.anchors)
        self.model = load_model(self.cfg.model_id)
        self.index = Indexer(w3_logs or w3, dep)
        self.out_dir = Path(out_dir) if out_dir is not None else acb_config.out_dir()
        self.base_uri = base_uri or default_base_uri(net.name)
        self.store = store or EvidenceStore([str(self.out_dir), self.base_uri])
        self.bus = bus
        self.sleep = sleep
        self.sender = TxSender(w3, account) if account is not None else None
        self.address = account.address.lower() if account is not None else None
        self.acb = AcbContract(w3, dep.contracts["AgentCreditBureau"].address)
        self.reputation = ReputationContract(w3, dep.contracts["ReputationRegistry"].address)
        self.archive_dir = Path(archive_dir) if archive_dir is not None else None
        self.listed: frozenset[int] = frozenset(
            {int(a.agent_id) for a in dep.agents.values()} | {int(v.agent_id) for v in dep.vaults.values()}
        )
        self.monitoring: list[dict] = []

    # ---- scoring ------------------------------------------------------------------------------

    def snapshot(self, as_of_block: int) -> Snapshot:
        self.index.sync(as_of_block)
        return build_snapshot(self.index, as_of_block, self.cfg, self.store)

    def score_all(self, snap: Snapshot) -> dict[int, Bundle]:
        return {aid: self.model.score(snap.inputs_for(aid), self.cfg, snap.facts) for aid in snap.universe}

    def as_of_block(self, not_before: int = 0) -> int:
        """``latest - confirmations``, waiting (or mining one block on Anvil) until it is >= ``not_before``.

        ``postGrade`` needs ``asOfBlock < block.number`` and is simulated with ``eth_call`` on the latest
        block first, so with 0 confirmations (Anvil) the as-of block must be strictly below the latest
        one: an empty block is mined when needed.
        """
        deadline = time.monotonic() + 300
        confirmations = int(self.net.confirmations)
        target = max(int(not_before), int(self.w3.eth.block_number) - confirmations)
        while True:
            latest = int(self.w3.eth.block_number)
            if latest - confirmations >= target and latest > target:
                return max(target, self.index.start_block)
            if int(self.w3.eth.chain_id) == 31337:
                self.w3.provider.make_request("evm_mine", [])
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"chain did not reach block {target + confirmations}")
            self.sleep(2.0)

    def _changed(self, snap: Snapshot, aid: int, bundle: Bundle) -> str:
        last = snap.last_posting(aid)
        if last is None:
            if aid in self.listed or has_evidence(bundle):
                return "first posting"
            return SKIP_NO_EVIDENCE
        grade, score, reasons = posted_values(bundle)
        if Grade(int(last.args["grade"])).name != grade:
            return "grade changed"
        if tuple(int(r) for r in last.args["reasons"]) != tuple(reasons):
            return "reasons changed"
        if abs(int(last.args["score"]) - score) >= self.cfg.repost_min_score_delta:
            return "score changed"
        return ""

    # ---- posting ------------------------------------------------------------------------------

    def _emit(self, type_: str, data: dict, **kw: Any) -> None:
        if self.bus is not None:
            self.bus.emit(type_, data, **kw)

    def _send(self, fn: Any) -> TxResult:
        assert self.sender is not None
        return self.sender.send(fn)

    def _mirror(self, aid: int, score: int, uri: str, evidence_hash: str) -> list[TxResult]:
        txs: list[TxResult] = []
        if not self.cfg.mirror_enabled:
            return txs
        if self.cfg.mirror_revoke_previous:
            previous = self.reputation.read_all_feedback(aid, [self.address], self.cfg.mirror_tag1, "", False)
            for fb in previous:
                if fb["client"] == self.address and fb["tag1"] == self.cfg.mirror_tag1 and not fb["isRevoked"]:
                    txs.append(self._send(self.reputation.revoke_feedback(aid, fb["feedbackIndex"])))
        txs.append(
            self._send(
                self.reputation.give_feedback(
                    aid, score, 0, self.cfg.mirror_tag1, self.cfg.model_id, "", uri, evidence_hash
                )
            )
        )
        return txs

    def post(self, snap: Snapshot, aid: int, bundle: Bundle, reason: str) -> Posting:
        """Write the bundle, post the grade and mirror it."""
        path, digest = write_bundle(bundle, self.out_dir)
        uri = bundle_uri(self.base_uri, aid, snap.as_of_block)
        posting = Posting(aid, bundle, digest, uri, path, False, reason)
        if self.sender is None:
            posting.reason = "no bureau key (dry run)"
            return posting
        if snap.identity_owners.get(aid) == self.address:
            posting.reason = "skipped: the bureau owns this agent (SelfGrading)"
            return posting
        grade, score, reasons = posted_values(bundle)
        try:
            r = self._send(
                self.acb.post_grade(
                    aid,
                    int(Grade[grade]),
                    score,
                    reasons,
                    self.cfg.model_id,
                    self.cfg.config_hash,
                    digest,
                    snap.as_of_block,
                    uri,
                )
            )
        except TxSimulationError as exc:
            posting.reason = f"postGrade would revert: {exc.decoded.text}"
            self._emit("error", {"agentId": str(aid), "error": posting.reason})
            return posting
        posting.tx.append(r)
        if r.status != 1:
            posting.reason = f"postGrade reverted: {r.revert_error}"
            self._emit("error", {"agentId": str(aid), "error": posting.reason}, tx_hash=r.hash)
            return posting
        posting.posted = True
        try:
            posting.tx.extend(self._mirror(aid, score, uri, digest))
        except (TxError, ValueError) as exc:  # the grade is posted; the mirror is best effort
            self._emit("error", {"agentId": str(aid), "error": f"mirror failed: {exc}"})
        posting.grade_view = GradeView(
            agent_id=aid,
            grade=Grade[grade],
            effective_grade=self.acb.effective_grade(aid, 0),
            score=score,
            reasons=tuple(x for x in reasons if x),
            model_id=self.cfg.model_id,
            config_hash=self.cfg.config_hash,
            evidence_hash=digest,
            as_of_block=snap.as_of_block,
            posted_at=self.w3.eth.get_block(r.block)["timestamp"],
            tx_hash=r.hash,
            evidence_uri=uri,
        )
        prev = snap.previous(aid)
        self._emit(
            "grade",
            {
                "agentId": str(aid),
                "from": prev["grade"] if prev else "NONE",
                "to": grade,
                "score": score,
                "reasons": [x for x in reasons if x],
                "evidenceHash": digest,
                "evidenceUri": uri,
                "asOfBlock": snap.as_of_block,
                "bundle": str(path),
            },
            reason_codes=[x for x in reasons if x],
            tx_hash=r.hash,
        )
        return posting

    def archive(self) -> None:
        """Archive the combined config (spec 4.7) next to the run output, and in the repository archive
        ``config/scorecard/archive`` for every public network (or ``archive_dir`` when given), so a posting
        stays recomputable after ``scorecard.yaml`` changes. Anvil runs keep the repository clean."""
        archive_config(self.cfg, self.out_dir / "scorecard" / "archive")
        target = self.archive_dir
        if target is None and int(self.net.chain_id) != 31337:
            target = ARCHIVE_DIR
        if target is not None:
            archive_config(self.cfg, target)

    def rescore(self, rounds: int = 1) -> RescoreResult:
        """Run ``rounds`` rescore rounds (spec 4.10); returns every posting and unchanged agent."""
        if rounds < 1:
            raise ValueError("rounds must be >= 1")
        self.archive()
        postings: list[GradeView] = []
        unchanged: set[int] = set()
        txs: list[TxResult] = []
        not_before = 0
        prev_grades: dict[int, str] | None = None
        for round_no in range(1, rounds + 1):
            as_of = self.as_of_block(not_before)
            snap = self.snapshot(as_of)
            bundles = self.score_all(snap)
            round_posts: list[Posting] = []
            for aid in sorted(bundles):
                bundle = bundles[aid]
                reason = self._changed(snap, aid, bundle)
                if reason == SKIP_NO_EVIDENCE:
                    continue
                if not reason:
                    unchanged.add(aid)
                    continue
                p = self.post(snap, aid, bundle, reason)
                round_posts.append(p)
                txs.extend(p.tx)
                if p.posted and p.grade_view is not None:
                    unchanged.discard(aid)
                    postings.append(p.grade_view)
            grades = {aid: b.grade for aid, b in bundles.items()}
            self._monitor(round_no, prev_grades, grades)
            prev_grades = grades
            posted_blocks = [t.block for p in round_posts for t in p.tx]
            not_before = max(posted_blocks) if posted_blocks else as_of
            if not any(p.posted for p in round_posts):
                break  # nothing changed: further rounds would read the same grades
        return RescoreResult(rounds=rounds, postings=tuple(postings), unchanged=tuple(sorted(unchanged)), tx=tuple(txs))

    # ---- monitoring (spec 4.7) ----------------------------------------------------------------

    def _monitor(self, round_no: int, before: dict[int, str] | None, after: dict[int, str]) -> None:
        alerts: list[str] = []
        anchors_bad = [a for a in self.cfg.anchors if after.get(a) in ("C", "D")]
        if anchors_bad:
            alerts.append(f"anchor graded C/D: {anchors_bad}")
        moved = psi = 0
        if before:
            common = [a for a in after if a in before]
            moved = sum(1 for a in common if abs(GRADE_RANK[Grade[after[a]]] - GRADE_RANK[Grade[before[a]]]) >= 2)
            if common and moved * 5 > len(common):
                alerts.append(f"{moved} of {len(common)} agents moved two or more grades")
            dist_b: dict[str, int] = {}
            dist_a: dict[str, int] = {}
            for a in common:
                dist_b[before[a]] = dist_b.get(before[a], 0) + 1
                dist_a[after[a]] = dist_a.get(after[a], 0) + 1
            psi = psi_x10000(dist_b, dist_a)
            if psi > 2500:
                alerts.append(f"PSI {psi / 10_000:.4f} > 0.25")
        record = {"round": round_no, "moved2": moved, "psiX10000": psi, "alerts": alerts}
        self.monitoring.append(record)
        self._emit("info", {"monitoring": record})

    # ---- reads for the service ----------------------------------------------------------------

    def grade_views(self, max_age: int = 0) -> list[GradeView]:
        """The latest posting of every graded agent (from the indexer and ``getRecord``)."""
        latest = int(self.w3.eth.block_number)
        self.index.sync(latest)
        out = []
        for aid, logs in sorted(self.index.postings(latest).items()):
            out.append(self.grade_view(aid, logs[-1], max_age))
        return out

    def grade_view(self, aid: int, last: Any | None = None, max_age: int = 0) -> GradeView | None:
        if last is None:
            latest = int(self.w3.eth.block_number)
            self.index.sync(latest)
            logs = self.index.postings(latest).get(int(aid))
            if not logs:
                return None
            last = logs[-1]
        a = last.args
        return GradeView(
            agent_id=int(aid),
            grade=Grade(int(a["grade"])),
            effective_grade=self.acb.effective_grade(int(aid), max_age),
            score=int(a["score"]),
            reasons=tuple(int(r) for r in a["reasons"] if int(r)),
            model_id=_model_text(a["modelId"]) or self.cfg.model_id,
            config_hash=str(a["configHash"]).lower(),
            evidence_hash=str(a["evidenceHash"]).lower(),
            as_of_block=int(a["asOfBlock"]),
            posted_at=self.index.timestamp(last.block),
            tx_hash=last.tx,
            evidence_uri=str(a["evidenceURI"]),
        )


def _model_text(value: Any) -> str | None:
    from acb.chain.contracts import model_id_text

    try:
        return model_id_text(value)
    except (ValueError, TypeError):
        return None


__all__ = ["SKIP_NO_EVIDENCE", "Bureau", "Posting", "has_evidence", "psi_x10000"]
