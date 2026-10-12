"""Permissionless recompute of a posted grade (spec 4.9).

1. Fetch the ``GradePosted`` log (by transaction hash, or the latest posting of an agent); read
   ``asOfBlock``, ``modelId``, ``configHash`` and ``evidenceHash``.
2. Load the scorecard config whose ``chash`` equals ``configHash`` (current or archived); otherwise
   ``CONFIG_NOT_FOUND``. Load the model module named by ``modelId``.
3. Rebuild the inputs from chain **logs** up to ``asOfBlock`` (chunked) and from the published invoice
   and report files (each checked against its hash). Only logs and block headers are read.
4. Compute the bundle, hash it, compare: ``MATCH``, ``MATCH_WITH_OVERRIDE`` (the model part matches and
   the posted bundle discloses an override) or ``MISMATCH`` with a JSON diff. The posted grade, score
   and reasons must also equal what the bundle says, so a bureau key that posts a grade its evidence
   does not support is caught (attack A63).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from web3 import Web3

from acb.bureau.config import ScorecardConfig, find_config, load_config
from acb.bureau.evidence import EvidenceStore, bundle_name, posted_values, read_bundle_source
from acb.bureau.indexer import Indexer, Log
from acb.bureau.inputs import build_snapshot
from acb.bureau.models import load_model
from acb.canonical import chash
from acb.chain import abi as abi_mod
from acb.chain.contracts import model_id_text
from acb.config import REPO_ROOT
from acb.types import Bundle, Deployments, Grade

MATCH = "MATCH"
MATCH_WITH_OVERRIDE = "MATCH_WITH_OVERRIDE"
MISMATCH = "MISMATCH"
CONFIG_NOT_FOUND = "CONFIG_NOT_FOUND"
MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
POSTING_NOT_FOUND = "POSTING_NOT_FOUND"
EXIT_CODES = {
    MATCH: 0,
    MATCH_WITH_OVERRIDE: 0,
    MISMATCH: 1,
    CONFIG_NOT_FOUND: 2,
    MODEL_NOT_FOUND: 2,
    POSTING_NOT_FOUND: 2,
}
MAX_DIFF = 50


@dataclass
class RecomputeResult:
    result: str
    agent_id: int | None = None
    tx: str | None = None
    as_of_block: int | None = None
    posted_evidence_hash: str | None = None
    computed_evidence_hash: str | None = None
    posted: dict | None = None
    computed: dict | None = None
    diff: list[dict] = field(default_factory=list)
    detail: str = ""

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.result]

    def to_json(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "agentId": None if self.agent_id is None else str(self.agent_id),
            "tx": self.tx,
            "asOfBlock": self.as_of_block,
            "postedEvidenceHash": self.posted_evidence_hash,
            "computedEvidenceHash": self.computed_evidence_hash,
            "posted": self.posted,
            "computed": self.computed,
            "diff": self.diff,
            "detail": self.detail,
        }


def json_diff(published: Any, computed: Any, path: str = "$", out: list[dict] | None = None) -> list[dict]:
    """Paths where two JSON values differ (at most :data:`MAX_DIFF` entries)."""
    out = [] if out is None else out
    if len(out) >= MAX_DIFF:
        return out
    if isinstance(published, dict) and isinstance(computed, dict):
        for key in sorted(set(published) | set(computed)):
            json_diff(published.get(key), computed.get(key), f"{path}.{key}", out)
    elif isinstance(published, list) and isinstance(computed, list) and len(published) == len(computed):
        for i, (a, b) in enumerate(zip(published, computed, strict=True)):
            json_diff(a, b, f"{path}[{i}]", out)
    elif published != computed:
        out.append({"path": path, "published": published, "computed": computed})
    return out


def find_posting_by_tx(w3: Any, acb_address: str, tx_hash: str) -> Log | None:
    """The ``GradePosted`` log of our ACB in transaction ``tx_hash``."""
    receipt = w3.eth.get_transaction_receipt(tx_hash)
    codec = w3.codec
    for raw in receipt.get("logs", []):
        if str(raw.get("address", "")).lower() != acb_address.lower():
            continue
        d = abi_mod.decode_log(raw, codec)
        if d and d["event"] == "GradePosted":
            return Log(d["address"], d["event"], d["args"], d["blockNumber"], d["logIndex"], d["transactionHash"])
    return None


def find_latest_posting(index: Indexer, agent_id: int, to_block: int) -> Log | None:
    index.sync(to_block)
    logs = index.postings(to_block).get(int(agent_id))
    return logs[-1] if logs else None


def load_published_bundle(uri: str, agent_id: int, as_of_block: int, extra_dirs: tuple[str, ...] = ()) -> dict | None:
    """The published bundle at ``evidenceURI``, else ``<dir>/bundles/<name>`` in ``extra_dirs``.

    ``evidenceURI`` is read from the chain: only a file named ``<agentId>-<asOfBlock>.json`` is read, from
    an http(s) URL or from inside ``evidence/``, ``$ACB_OUT_DIR`` or ``extra_dirs`` (never any other path).
    """
    from acb import config as acb_config

    name = bundle_name(agent_id, as_of_block)
    local = (REPO_ROOT / "evidence", acb_config.out_dir(), *extra_dirs)
    candidates = [uri, *(str(Path(d) / "bundles" / name) for d in extra_dirs)]
    for loc in candidates:
        data = read_bundle_source(loc, name, local)
        if data is None:
            continue
        try:
            obj = json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def recompute_posting(
    posting: Log,
    *,
    w3_logs: Any,
    dep: Deployments,
    store: EvidenceStore,
    current_cfg: ScorecardConfig | None = None,
    bundle_dirs: tuple[str, ...] = (),
) -> RecomputeResult:
    """Recompute one ``GradePosted`` log (steps 2-4 of spec 4.9)."""
    a = posting.args
    agent_id = int(a["agentId"])
    as_of = int(a["asOfBlock"])
    posted_hash = str(a["evidenceHash"]).lower()
    config_hash_hex = str(a["configHash"]).lower()
    model_id = model_id_text(a["modelId"])
    posted = {
        "grade": Grade(int(a["grade"])).name,
        "score": int(a["score"]),
        "reasons": [int(r) for r in a["reasons"]],
        "modelId": model_id,
        "configHash": config_hash_hex,
        "evidenceURI": str(a["evidenceURI"]),
    }
    base = RecomputeResult(
        result=MISMATCH,
        agent_id=agent_id,
        tx=posting.tx,
        as_of_block=as_of,
        posted_evidence_hash=posted_hash,
        posted=posted,
    )
    cfg = find_config(config_hash_hex, current_cfg or load_config(dep.anchors))
    if cfg is None:
        base.result = CONFIG_NOT_FOUND
        base.detail = f"no scorecard config with hash {config_hash_hex} (current or archive)"
        return base
    try:
        model = load_model(model_id)
    except KeyError as exc:
        base.result = MODEL_NOT_FOUND
        base.detail = str(exc)
        return base
    if cfg.model_id != model_id:
        base.detail = f"config is for model {cfg.model_id}, posting says {model_id}"
        return base

    index = Indexer(w3_logs, dep)
    index.sync(as_of)
    snap = build_snapshot(index, as_of, cfg, store)
    bundle: Bundle = model.score(snap.inputs_for(agent_id), cfg, snap.facts)
    published = load_published_bundle(str(a["evidenceURI"]), agent_id, as_of, bundle_dirs)
    override = published.get("override") if isinstance(published, dict) else None
    if override:
        bundle = bundle.model_copy(update={"override": override})
    computed_hash = bundle.evidence_hash
    base.computed_evidence_hash = computed_hash
    grade, score_, reasons = posted_values(bundle)
    base.computed = {"grade": grade, "score": score_, "reasons": list(reasons)}

    problems = []
    if computed_hash != posted_hash:
        problems.append("evidence hash differs")
    if (grade, score_, list(reasons)) != (posted["grade"], posted["score"], posted["reasons"]):
        problems.append("posted grade, score or reasons differ from the bundle")
    if published is not None:
        if chash(published) != posted_hash:
            problems.append("published bundle file does not hash to the posted evidenceHash")
        base.diff = json_diff(published, bundle.as_canonical())
    elif computed_hash != posted_hash:
        base.diff = [{"path": "$", "published": None, "computed": "bundle not available for a diff"}]
    if problems:
        base.result = MISMATCH
        base.detail = "; ".join(problems)
        return base
    base.result = MATCH_WITH_OVERRIDE if override else MATCH
    base.detail = (
        "published bundle not found; hash compared with the on-chain evidenceHash" if published is None else ""
    )
    return base


def recompute(
    *,
    w3: Any,
    dep: Deployments,
    store: EvidenceStore,
    tx_hash: str | None = None,
    agent_id: int | None = None,
    w3_logs: Any | None = None,
    bundle_dirs: tuple[str, ...] = (),
) -> RecomputeResult:
    """``acb bureau recompute``: by transaction hash or for an agent's latest posting."""
    acb_address = dep.contracts["AgentCreditBureau"].address
    if tx_hash:
        posting = find_posting_by_tx(w3, acb_address, Web3.to_hex(Web3.to_bytes(hexstr=tx_hash)))
    elif agent_id is not None:
        posting = find_latest_posting(Indexer(w3_logs or w3, dep), agent_id, int(w3.eth.block_number))
    else:
        raise ValueError("pass tx_hash or agent_id")
    if posting is None:
        return RecomputeResult(
            result=POSTING_NOT_FOUND, agent_id=agent_id, tx=tx_hash, detail="no GradePosted log found"
        )
    return recompute_posting(posting, w3_logs=w3_logs or w3, dep=dep, store=store, bundle_dirs=bundle_dirs)


__all__ = [
    "CONFIG_NOT_FOUND",
    "MATCH",
    "MATCH_WITH_OVERRIDE",
    "MISMATCH",
    "MODEL_NOT_FOUND",
    "POSTING_NOT_FOUND",
    "RecomputeResult",
    "find_latest_posting",
    "find_posting_by_tx",
    "json_diff",
    "load_published_bundle",
    "recompute",
    "recompute_posting",
]
