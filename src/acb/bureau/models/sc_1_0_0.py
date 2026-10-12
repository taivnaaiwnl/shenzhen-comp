"""Scorecard ``acb-sc-1.0.0`` (spec 4.1-4.8): a pure function from public inputs to an evidence bundle.

``score(inputs, cfg, facts)`` reads only its arguments: no clock, no network, no files, no randomness.
All arithmetic is integer (floor division), so the same inputs give the same bundle bytes on any
machine. Changing a formula needs a new module ``sc_X_Y_Z.py`` and a new model id (spec 4.7); the
numbers (bins, weights, thresholds) live in ``config/scorecard.yaml`` and are bound by ``configHash``.

Pipeline: counted-order filters (4.1) -> clusters -> report weighting (4.2) -> features F1-F6 (4.3)
-> points (4.4) -> grade rules in order (4.5) -> reason codes (4.6) -> bundle (4.8).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from acb.bureau.clusters import build_clusters
from acb.bureau.config import FEATURE_IDS, ScorecardConfig
from acb.bureau.facts import EMPTY_FACTS, ChainFacts, order_key
from acb.canonical import chash, recover_eip191
from acb.injection import detect, invoice_fields
from acb.types import EVIDENCE_SCHEMA, Bundle, Grade, OrderRecord, ReportRecord, ScoreInputs, SignedInvoice

MODEL_ID = "acb-sc-1.0.0"
ZERO32 = "0x" + "00" * 32

SETTLED = frozenset({"ON_TIME", "LATE"})
SPIKE_OUTCOMES = frozenset({"ON_TIME", "LATE", "PENDING"})
ADVERSE_KINDS = frozenset({"NON_DELIVERY", "LATE_DELIVERY", "BAD_DATA", "INJECTION_ATTEMPT", "SPAM"})
INFORMATIONAL_KINDS = frozenset({"NON_DELIVERY", "LATE_DELIVERY"})

RULE_CODES = (("hardFlag", 1004), ("coldStart", 1101), ("circularCap", 1010), ("standingGate", 1012), ("ageGate", 1007))
STRONG_RECORD = 1100

# ---------------------------------------------------------------------------
# shared preparation (subject independent; cached per input object)
# ---------------------------------------------------------------------------


@dataclass
class _Prepared:
    clusters: dict[int, int]
    orders: tuple[OrderRecord, ...]  # sorted by (block, log_index)
    notes: tuple[str, ...]  # "" = counted
    by_agent: dict[int, list[int]]  # agentId -> indexes into orders (either role)
    graph: dict[int, set[int]]  # payer cluster -> payee clusters (settled counted orders)


_CACHE: list[tuple[Any, Any, Any, Any, _Prepared]] = []
_CACHE_SIZE = 4


def _invoice_note(o: OrderRecord, facts: ChainFacts) -> str:
    note = facts.invoice_notes.get(order_key(o.vault, o.order_id))
    if note:
        return note
    return "no_invoice" if o.invoice_hash == ZERO32 else "bad_invoice"


def _prepare(inputs: ScoreInputs, cfg: ScorecardConfig, facts: ChainFacts) -> _Prepared:
    for orders, agents, c, f, prep in _CACHE:
        if orders is inputs.orders and agents is inputs.agents and c is cfg and f is facts:
            return prep
    addresses = {aid: (info.identity_owner, *info.vault_owners) for aid, info in inputs.agents.items()}
    clusters = build_clusters(addresses)
    orders = tuple(sorted(inputs.orders, key=lambda o: (o.block, o.log_index)))
    registered = set(facts.vaults)
    notes: list[str] = []
    by_agent: dict[int, list[int]] = {}
    graph: dict[int, set[int]] = {}
    for i, o in enumerate(orders):
        if registered and o.vault not in registered:
            note = "unregistered_vault"
        elif o.amount < cfg.min_order_amount:
            note = "dust"
        elif not o.invoice_ok:
            note = _invoice_note(o, facts)
        elif clusters.get(o.payer_agent_id, o.payer_agent_id) == clusters.get(o.payee_agent_id, o.payee_agent_id):
            note = "self_dealing"
        else:
            note = ""
        notes.append(note)
        by_agent.setdefault(o.payer_agent_id, []).append(i)
        by_agent.setdefault(o.payee_agent_id, []).append(i)
        if note == "" and o.outcome in SETTLED:
            pc = clusters.get(o.payer_agent_id, o.payer_agent_id)
            ec = clusters.get(o.payee_agent_id, o.payee_agent_id)
            graph.setdefault(pc, set()).add(ec)
    prep = _Prepared(clusters, orders, tuple(notes), by_agent, graph)
    _CACHE.insert(0, (inputs.orders, inputs.agents, cfg, facts, prep))
    del _CACHE[_CACHE_SIZE:]
    return prep


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def standing(inputs: ScoreInputs, cfg: ScorecardConfig, agent_id: int) -> str:
    """``anchor`` for a configured anchor, else the grade name posted as of ``asOfBlock`` (NONE if unknown)."""
    if agent_id in cfg.anchor_set:
        return "anchor"
    info = inputs.agents.get(agent_id)
    return info.grade.name if info is not None else "NONE"


def decay(weight: int, age: int, cfg: ScorecardConfig) -> int:
    """Spec 4.2: after ``decay_start_s`` a weight contributes ``w * h // (h + age - start)``."""
    if age <= cfg.decay_start_s or weight == 0:
        return weight
    return weight * cfg.decay_h_s // (cfg.decay_h_s + age - cfg.decay_start_s)


def _lower_median(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def _reachable(graph: Mapping[int, set[int]], start: int, max_hops: int) -> set[int]:
    """Clusters reachable from ``start`` along payment edges in 1..max_hops hops."""
    seen: dict[int, int] = {start: 0}
    queue: deque[int] = deque([start])
    reached: set[int] = set()
    while queue:
        node = queue.popleft()
        depth = seen[node]
        if depth >= max_hops:
            continue
        for nxt in graph.get(node, ()):
            reached.add(nxt)
            if nxt not in seen:
                seen[nxt] = depth + 1
                queue.append(nxt)
    return reached


# ---------------------------------------------------------------------------
# reports (spec 4.2)
# ---------------------------------------------------------------------------


def _parse_invoice(raw: Any) -> SignedInvoice | None:
    if not isinstance(raw, dict):
        return None
    try:
        return SignedInvoice.from_wire(raw)
    except (ValueError, TypeError):
        return None


@lru_cache(maxsize=65_536)
def _recover(digest: str, signature: str) -> str | None:
    """EIP-191 signer of ``digest`` (pure, so memoised: secp256k1 recovery is slow in pure Python)."""
    try:
        return recover_eip191(digest, signature)
    except (ValueError, TypeError):
        return None


def _signer(si: SignedInvoice) -> str | None:
    return _recover(si.invoice_hash, si.signature)


def _payload_ok(r: ReportRecord) -> bool:
    if not isinstance(r.payload, dict):
        return False
    try:
        return chash(r.payload) == r.feedback_hash
    except TypeError:
        return False


def _verify_injection(r: ReportRecord, subject: int, facts: ChainFacts) -> tuple[bool, str]:
    """INJECTION_ATTEMPT is verified only from signed evidence (spec 4.2 a-d)."""
    if not _payload_ok(r):
        return False, "payload_missing_or_hash_mismatch"
    evidence = r.payload.get("evidence") if isinstance(r.payload, dict) else None
    si = _parse_invoice(evidence.get("invoice") if isinstance(evidence, dict) else None)
    if si is None:
        return False, "no_signed_invoice"
    if r.vault is None or r.order_id is None:
        return False, "no_order"
    refusal = facts.refusals.get(order_key(r.vault, r.order_id))
    if refusal is None:
        return False, "no_refusal_record"
    if refusal.invoice_hash != si.invoice_hash or refusal.payee_agent_id != subject:
        return False, "refusal_mismatch"
    if si.invoice.issuer_agent_id != subject:
        return False, "issuer_mismatch"
    wallet = facts.wallet_of(subject, refusal.block, refusal.log_index)
    signer = _signer(si)
    if wallet is None or signer is None or signer != wallet:
        return False, "not_signed_by_subject"
    if not detect(invoice_fields(si.wire_dict())).adverse:
        return False, "detector_not_adverse"
    return True, "verified"


def _verify_spam(r: ReportRecord, subject: int, facts: ChainFacts, cfg: ScorecardConfig) -> tuple[bool, str]:
    """SPAM is verified by at least ``spam_min_invoices`` distinct unsolicited invoices the subject signed."""
    if not _payload_ok(r):
        return False, "payload_missing_or_hash_mismatch"
    if r.vault is None:
        return False, "no_vault"
    evidence = r.payload.get("evidence") if isinstance(r.payload, dict) else None
    raw_list = evidence.get("invoices") if isinstance(evidence, dict) else None
    if not isinstance(raw_list, list):
        return False, "no_invoices"
    wallet = facts.wallet_of(subject, r.block, r.log_index)
    if wallet is None:
        return False, "no_subject_wallet"
    good: set[str] = set()
    for raw in raw_list:
        si = _parse_invoice(raw)
        if si is None:
            continue
        inv = si.invoice
        if inv.issuer_agent_id != subject or inv.buyer_vault != r.vault or inv.quote_request_id != "":
            continue
        if _signer(si) != wallet:
            continue
        good.add(si.invoice_hash)
    if len(good) < cfg.spam_min_invoices:
        return False, "too_few_signed_unsolicited_invoices"
    return True, "verified"


def _bad_data_backed(r: ReportRecord, subject: int, prep: _Prepared) -> bool:
    """BAD_DATA needs a settled, counted order of the reporter's vault (payer) with the subject (payee)."""
    if r.vault is None or r.order_id is None:
        return False
    for i in prep.by_agent.get(subject, ()):
        o = prep.orders[i]
        if (
            o.vault == r.vault
            and o.order_id == r.order_id
            and o.payee_agent_id == subject
            and o.payer_agent_id == r.reporter_agent_id
            and prep.notes[i] == ""
            and o.outcome in SETTLED
        ):
            return True
    return False


def _bound(r: ReportRecord, facts: ChainFacts) -> bool:
    """The reporter was the ``agent`` of the registered vault named in the endpoint, at the report's block."""
    if r.vault is None or r.order_id is None:
        return False
    if facts.vault_agent_ids.get(r.vault) != r.reporter_agent_id:
        return False
    if facts.vaults and r.vault not in facts.vaults:
        return False
    return facts.agent_of_vault(r.vault, r.block, r.log_index) == r.reporter


def _weigh_reports(
    inputs: ScoreInputs, cfg: ScorecardConfig, facts: ChainFacts, prep: _Prepared, subject: int
) -> tuple[list[dict[str, Any]], int, int, int]:
    """Bundle entries and the sums ``inj_w``, ``spam_w``, ``bad_w``."""
    reports = sorted(
        (r for r in inputs.reports if r.subject_agent_id == subject and r.block <= inputs.as_of_block),
        key=lambda r: (r.block, r.log_index),
    )
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None, str]] = set()
    used: dict[tuple[int, str], int] = {}
    sums = {"INJECTION_ATTEMPT": 0, "SPAM": 0, "BAD_DATA": 0}
    for r in reports:
        bound = False
        verified = False
        weight = 0
        if r.revoked:
            note = "revoked"
        elif (r.reporter, r.order_id, r.kind) in seen:
            note = "duplicate"
        else:
            seen.add((r.reporter, r.order_id, r.kind))
            bound = _bound(r, facts)
            if not bound:
                note = "unbound_reporter"
            elif r.kind == "POSITIVE":
                note = "positive_ignored"
            elif r.kind not in ADVERSE_KINDS:
                note = "unknown_kind"
            elif r.value != -1:
                note = "bad_value"
            elif r.kind in INFORMATIONAL_KINDS:
                note = "informational"
            elif r.kind == "INJECTION_ATTEMPT":
                verified, why = _verify_injection(r, subject, facts)
                weight = cfg.verified_weight if verified else 0
                note = "" if verified else f"unverified: {why}"
            elif r.kind == "SPAM":
                verified, why = _verify_spam(r, subject, facts, cfg)
                weight = cfg.spam_weight if verified else 0
                note = "" if verified else f"unverified: {why}"
            else:  # BAD_DATA
                verified = _bad_data_backed(r, subject, prep)
                if not verified:
                    note = "unbacked"
                else:
                    weight = cfg.adverse_weight[standing(inputs, cfg, r.reporter_agent_id)]
                    note = "" if weight else "no_standing"
            if weight:
                weight = decay(weight, max(0, inputs.as_of_timestamp - r.timestamp), cfg)
                key = (prep.clusters.get(r.reporter_agent_id, r.reporter_agent_id), r.kind)
                remaining = max(0, cfg.reporter_cap_milli - used.get(key, 0))
                if weight > remaining:
                    weight = remaining
                    note = "capped"
                used[key] = used.get(key, 0) + weight
                sums[r.kind] += weight
        entries.append(
            {
                "reporter": r.reporter,
                "reporterAgentId": str(r.reporter_agent_id),
                "kind": r.kind,
                "value": r.value,
                "orderId": r.order_id,
                "feedbackIndex": r.feedback_index,
                "feedbackHash": r.feedback_hash,
                "tx": r.tx,
                "logIndex": r.log_index,
                "bound": bound,
                "verified": verified,
                "weight": weight,
                "note": note,
            }
        )
    return entries, sums["INJECTION_ATTEMPT"], sums["SPAM"], sums["BAD_DATA"]


# ---------------------------------------------------------------------------
# features, points, grade, reasons
# ---------------------------------------------------------------------------


def derived(raw: Mapping[str, int], cfg: ScorecardConfig) -> dict[str, int]:
    """``rBps``, ``neg`` and ``flag`` from the weight sums (spec 4.3 F1-F3)."""
    on_w, late_w, fail_w = raw["onW"], raw["lateW"], raw["failW"]
    prior_w = cfg.prior_weight_milli
    r_bps = (10_000 * on_w + cfg.late_credit_bps * late_w + prior_w * cfg.prior_bps) // (
        on_w + late_w + fail_w + prior_w
    )
    return {
        "rBps": r_bps,
        "neg": fail_w + raw["clawW"] + raw["badW"],
        "flag": raw["injW"] + raw["spamW"] // 2,
    }


def points_of(features: Mapping[str, int], cfg: ScorecardConfig) -> dict[str, int]:
    """Points per feature (spec 4.4)."""
    return {fid: cfg.tables[fid].points(int(features[cfg.tables[fid].feature])) for fid in FEATURE_IDS}


def score_of(points: Mapping[str, int], cfg: ScorecardConfig) -> int:
    """``clamp(base + sum(points), lo, hi)``."""
    return max(cfg.clamp_lo, min(cfg.clamp_hi, cfg.base + sum(points.values())))


def grade_of(features: Mapping[str, int], score: int, cfg: ScorecardConfig) -> tuple[Grade, dict[str, bool]]:
    """Grade rules of spec 4.5 in order; returns the grade and which rules decided or changed it."""
    rules = {name: False for name, _ in RULE_CODES}
    if features["injW"] >= cfg.hard_flag_inj_w:
        rules["hardFlag"] = True
        return Grade.D, rules
    if features["n"] < cfg.cold_start_min_settled:
        rules["coldStart"] = True
        fail_w = features["failW"]
        if fail_w >= cfg.cold_d_fail_w:
            return Grade.D, rules
        if fail_w >= cfg.cold_c_fail_w or features["flag"] >= cfg.cold_c_flag or features["neg"] >= cfg.cold_c_neg:
            return Grade.C, rules
        return Grade.NEW, rules
    if score >= cfg.band_a:
        grade = Grade.A
    elif score >= cfg.band_b:
        grade = Grade.B
    elif score >= cfg.band_c:
        grade = Grade.C
    else:
        grade = Grade.D
    if features["cycBps"] > cfg.circular_cap_bps and grade in (Grade.A, Grade.B):
        grade = Grade.C
        rules["circularCap"] = True
    if grade in (Grade.A, Grade.B) and features["good"] == 0:
        grade = Grade.C
        rules["standingGate"] = True
    if grade == Grade.A and features["age"] < cfg.a_min_age_s:
        grade = Grade.B
        rules["ageGate"] = True
    return grade, rules


def reasons_of(
    features: Mapping[str, int], points: Mapping[str, int], rules: Mapping[str, bool], cfg: ScorecardConfig
) -> list[int]:
    """Reason codes of spec 4.6: rule codes first, then features by loss; top 4, padded with 0."""
    codes: list[int] = [code for name, code in RULE_CODES if rules.get(name)]
    losses: list[tuple[int, int]] = []
    for fid in FEATURE_IDS:
        table = cfg.tables[fid]
        loss = table.max - points[fid]
        if loss <= 0:
            continue
        code = table.code
        if fid == "F2" and features["failW"] <= 0 and table.code_alt is not None:
            code = table.code_alt
        if fid == "F3" and features["injW"] <= 0 and table.code_alt is not None:
            code = table.code_alt
        losses.append((loss, code))
    for _, code in sorted(losses, key=lambda x: (-x[0], x[1])):
        if code not in codes:
            codes.append(code)
    codes = codes[:4] or [STRONG_RECORD]
    return codes + [0] * (4 - len(codes))


def evaluate_features(raw: Mapping[str, int], cfg: ScorecardConfig) -> tuple[dict[str, int], dict[str, int], int]:
    """From the raw sums (onW, lateW, failW, clawW, badW, injW, spamW, n, age, div, good, cqBps, cycBps,
    spikeX100) to the full feature dict, the points and the score (used by the property tests)."""
    features = {**raw, **derived(raw, cfg)}
    points = points_of(features, cfg)
    return features, points, score_of(points, cfg)


FEATURE_ORDER = (
    "onW",
    "lateW",
    "failW",
    "clawW",
    "badW",
    "injW",
    "spamW",
    "rBps",
    "neg",
    "flag",
    "n",
    "age",
    "div",
    "good",
    "cqBps",
    "cycBps",
    "spikeX100",
)

# ---------------------------------------------------------------------------
# the model
# ---------------------------------------------------------------------------


def score(inputs: ScoreInputs, cfg: ScorecardConfig, facts: ChainFacts | None = None) -> Bundle:
    """Grade ``inputs.subject_agent_id`` and return its evidence bundle (spec 4.8). Pure."""
    facts = facts or EMPTY_FACTS
    prep = _prepare(inputs, cfg, facts)
    subject = int(inputs.subject_agent_id)
    clusters = prep.clusters
    subject_cluster = clusters.get(subject, subject)

    def cluster(agent_id: int) -> int:
        return clusters.get(agent_id, agent_id)

    on_w = late_w = claw_w = 0
    fail_by: dict[int, int] = {}
    n = 0
    cp_best: dict[int, int] = {}
    volume = weighted_volume = 0
    inbound: list[tuple[int, int]] = []
    payee_amounts: list[int] = []
    order_entries: list[dict[str, Any]] = []
    ref = cfg.ref_amount

    for i in sorted(set(prep.by_agent.get(subject, ()))):
        o = prep.orders[i]
        if o.block > inputs.as_of_block:
            continue
        if o.payee_agent_id == subject:
            role, cp = "payee", o.payer_agent_id
        elif o.payer_agent_id == subject:
            role, cp = "payer", o.payee_agent_id
        else:  # pragma: no cover - by_agent only holds the subject's orders
            continue
        note = prep.notes[i]
        st = standing(inputs, cfg, cp)
        order_entries.append(
            {
                "vault": o.vault,
                "orderId": o.order_id,
                "role": role,
                "counterpartyAgentId": str(cp),
                "counterpartyStanding": st,
                "amount": str(o.amount),
                "kind": o.kind,
                "outcome": o.outcome,
                "counted": note == "",
                "note": note,
                "invoiceHash": o.invoice_hash,
                "openTx": o.open_tx,
                "openLogIndex": o.log_index,
                "closeTx": o.close_tx,
                "closeLogIndex": o.close_log_index,
                "block": o.block,
            }
        )
        if note:
            continue
        p_w = cfg.positive_quality[st]
        if role == "payee":
            if o.outcome == "ON_TIME":
                on_w += p_w
            elif o.outcome == "LATE":
                late_w += p_w
            elif o.outcome == "FAILED":
                w = cfg.adverse_weight[st] * min(o.amount, ref) // ref
                w = decay(w, max(0, inputs.as_of_timestamp - o.timestamp), cfg)
                fail_by[cluster(cp)] = fail_by.get(cluster(cp), 0) + w
            if o.outcome in SPIKE_OUTCOMES:
                payee_amounts.append(o.amount)
        else:
            if o.outcome in SETTLED:
                on_w += p_w
            elif o.outcome == "CLAWBACK":
                claw_w += p_w
        if o.outcome in SETTLED:
            n += 1
            c = cluster(cp)
            cp_best[c] = max(cp_best.get(c, p_w), p_w)
            volume += o.amount
            weighted_volume += o.amount * p_w
            if role == "payee":
                inbound.append((c, o.amount))

    fail_w = sum(min(v, cfg.fail_cap_per_payer) for v in fail_by.values())
    report_entries, inj_w, spam_w, bad_w = _weigh_reports(inputs, cfg, facts, prep, subject)

    info = inputs.agents.get(subject)
    registered_block = info.registered_block if info else 0
    registered_ts = info.registered_timestamp if info else inputs.as_of_timestamp
    age = max(0, inputs.as_of_timestamp - registered_ts)

    div = sum(cp_best.values())
    good = sum(1 for v in cp_best.values() if v >= cfg.good_standing_min)
    cq_bps = (10 * weighted_volume) // volume if volume else 10_000

    total_in = sum(a for _, a in inbound)
    if total_in:
        reach = _reachable(prep.graph, subject_cluster, cfg.circular_max_hops)
        circular = sum(a for c, a in inbound if c in reach)
        cyc_bps = 10_000 * circular // total_in
    else:
        cyc_bps = 0

    spike_x100 = 0
    window = cfg.spike_window_last
    prior = payee_amounts[:-window] if window > 0 else []
    if len(prior) >= max(1, cfg.spike_min_prior_orders):
        med = _lower_median(prior)
        if med > 0:
            spike_x100 = 100 * max(payee_amounts[-window:]) // med

    raw = {
        "onW": on_w,
        "lateW": late_w,
        "failW": fail_w,
        "clawW": claw_w,
        "badW": bad_w,
        "injW": inj_w,
        "spamW": spam_w,
        "n": n,
        "age": age,
        "div": div,
        "good": good,
        "cqBps": cq_bps,
        "cycBps": cyc_bps,
        "spikeX100": spike_x100,
    }
    features, points, total = evaluate_features(raw, cfg)
    grade, rules = grade_of(features, total, cfg)
    reasons = reasons_of(features, points, rules, cfg)

    contracts = dict(inputs.contracts)
    member_ids = sorted(a for a, c in clusters.items() if c == subject_cluster) or [subject]
    return Bundle(
        schema=EVIDENCE_SCHEMA,
        model={
            "id": cfg.model_id,
            "configHash": cfg.config_hash,
            "detectorId": cfg.detector_id,
            "profile": cfg.profile,
            "anchors": [str(a) for a in cfg.anchors],
        },
        chain={
            "chainId": inputs.chain_id,
            "asOfBlock": inputs.as_of_block,
            "asOfBlockHash": inputs.as_of_block_hash,
            "asOfTimestamp": inputs.as_of_timestamp,
        },
        contracts={
            "acb": contracts.get("acb", ""),
            "identity": contracts.get("identity", ""),
            "reputation": contracts.get("reputation", ""),
            "vaults": sorted(facts.vaults),
        },
        subject={
            "agentId": str(subject),
            "cluster": [str(a) for a in member_ids],
            "registeredBlock": registered_block,
            "registeredTimestamp": registered_ts,
        },
        orders=tuple(order_entries),
        reports=tuple(report_entries),
        features={k: features[k] for k in FEATURE_ORDER},
        points=points,
        score=total,
        grade=grade.name,
        reasons=tuple(reasons),
        rules=rules,
        override=None,
        previous=inputs.previous,
    )


__all__ = [
    "MODEL_ID",
    "derived",
    "evaluate_features",
    "grade_of",
    "points_of",
    "reasons_of",
    "score",
    "score_of",
    "standing",
]
