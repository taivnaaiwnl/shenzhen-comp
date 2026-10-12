"""Evidence files of a run (spec 10.4) and their verification (10.5).

During S21 the runner builds the curated evidence of the run in a staging folder,
``$ACB_OUT_DIR/evidence/`` (= ``runs/<run_id>/out/<network>/evidence/``), and verifies it with
``scripts/verify_evidence.py`` (RPC and offline). Nothing writes into ``evidence/`` at runtime:
``acb evidence export --run-id <id>`` copies the staging folder into ``evidence/<network>/`` (only the
curated names of :data:`CURATED`; hand-made ``screenshots/`` are kept).

Staging folder layout::

    evidence.json  evidence.csv  HEADLINE.md  run.txt  narration.srt
    attack_suite.json  backtest.json  dialogue_eval.json         (when available)
    receipts/<txHash>.json  bundles/  reports/  invoices/  decisions/  violations/  autonomy/
    appeals/  dialogue/S5.json  scorecard/archive/
"""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import shutil
import statistics
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from acb import config

CURATED_FILES = (
    "evidence.json",
    "evidence.csv",
    "HEADLINE.md",
    "run.txt",
    "narration.srt",
    "attack_suite.json",
    "backtest.json",
    "dialogue_eval.json",
)
CURATED_DIRS = (
    "receipts",
    "bundles",
    "reports",
    "invoices",
    "decisions",
    "violations",
    "autonomy",
    "appeals",
    "dialogue",
    "scorecard",
)
CURATED = frozenset(CURATED_FILES + CURATED_DIRS)
"""Every top-level name ``acb evidence export`` may write into ``evidence/<network>/``."""
KEEP_ON_EXPORT = frozenset({"screenshots"})
ROOT_COPIES = ("backtest.json", "dialogue_eval.json")
"""Files ``acb evidence export`` also places at the top of ``evidence/`` (``attack_suite.json`` is written
there by ``acb attacks run --export``)."""
COPIED_TREES = ("bundles", "reports", "invoices", "violations", "autonomy", "appeals", "dialogue", "scorecard")
CSV_COLUMNS = (
    "seq",
    "step",
    "timestamp_utc",
    "actor",
    "signed_by_agent_key",
    "action",
    "function",
    "decision",
    "reason_codes",
    "tier_before",
    "tier_after",
    "chain_id",
    "block",
    "tx_hash",
    "status",
    "expected_status",
    "basescan_url",
    "blockscout_url",
    "log_line",
)
VERIFY_SCRIPT = config.REPO_ROOT / "scripts" / "verify_evidence.py"


class EvidenceError(RuntimeError):
    """The evidence could not be built, exported or validated."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def write_json(path: Path, obj: Any) -> None:
    from acb.canonical import atomic_write_bytes

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, (json.dumps(obj, indent=2, ensure_ascii=True) + "\n").encode("utf-8"))


def write_text(path: Path, text: str) -> None:
    from acb.canonical import atomic_write_bytes

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, text.encode("utf-8"))


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=config.REPO_ROOT, capture_output=True, text=True, timeout=10, check=False
        )
    except OSError:
        return "uncommitted"
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else "uncommitted"


def load_verifier() -> Any:
    """``scripts/verify_evidence.py`` as a module (it is a stand-alone script, not a package)."""
    spec = importlib.util.spec_from_file_location("acb_verify_evidence", VERIFY_SCRIPT)
    if spec is None or spec.loader is None:
        raise EvidenceError(f"cannot load {VERIFY_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def topic_table() -> dict[str, tuple[tuple[str, str], ...]]:
    """Event name -> ((signature, topic0), ...) over every ABI in ``abi/`` (embedded in the verifier)."""
    from web3 import Web3

    from acb.chain import abi as abi_mod

    table: dict[str, set[tuple[str, str]]] = {}
    for name in abi_mod.artefact_names():
        for entry in abi_mod.abi_of(name):
            if entry.get("type") == "event" and not entry.get("anonymous"):
                sig = abi_mod.signature(entry)
                table.setdefault(entry["name"], set()).add((sig, Web3.to_hex(Web3.keccak(text=sig))))
    return {k: tuple(sorted(v)) for k, v in sorted(table.items())}


def render_topic_table() -> str:
    lines = ["EVENT_TOPICS: dict[str, tuple[tuple[str, str], ...]] = {"]
    for name, entries in topic_table().items():
        inner = ", ".join(f'("{sig}", "{topic}")' for sig, topic in entries)
        lines.append(f'    "{name}": ({inner},),')
    lines.append("}  # fmt: skip")
    return "\n".join(lines)


def srt_time(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def render_srt(entries: Sequence[Mapping[str, Any]]) -> str:
    out = []
    for i, e in enumerate(entries, start=1):
        start = float(e["start"])
        end = max(float(e["end"]), start + 1.0)
        out.append(f"{i}\n{srt_time(start)} --> {srt_time(end)}\n{e['step']}: {e['text']}\n")
    return "\n".join(out)


def render_csv(transactions: Sequence[Mapping[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for t in transactions:
        explorer = t.get("explorer") or {}
        writer.writerow(
            [
                t["seq"],
                t["step"],
                t["timestampUtc"],
                t["actor"],
                str(bool(t["signedByAgentKey"])).lower(),
                t["action"],
                t["function"],
                t.get("decision") or "",
                ";".join(str(c) for c in t.get("reasonCodes") or []),
                t.get("tierBefore"),
                t.get("tierAfter"),
                t["chainId"],
                t["block"],
                t["txHash"],
                t["status"],
                t["expectedStatus"],
                explorer.get("basescan", ""),
                explorer.get("blockscout", ""),
                t["logLine"],
            ]
        )
    return buf.getvalue()


# ---------------------------------------------------------------------------
# validation of evidence.json (schema acb-run/1)
# ---------------------------------------------------------------------------

_TOP_KEYS = {
    "schema": str,
    "runId": str,
    "network": str,
    "chainId": int,
    "startedAt": str,
    "finishedAt": str,
    "gitCommit": str,
    "contracts": dict,
    "erc8004": dict,
    "llm": dict,
    "steps": list,
    "transactions": list,
    "offchainDecisions": list,
    "gradeChanges": list,
}
_TX_KEYS = {
    "seq": int,
    "step": str,
    "timestampUtc": str,
    "blockTimestamp": int,
    "actor": str,
    "actorAddress": str,
    "signedByAgentKey": bool,
    "action": str,
    "contract": str,
    "function": str,
    "reasonCodes": list,
    "chainId": int,
    "block": int,
    "txHash": str,
    "status": int,
    "expectedStatus": int,
    "expectedEvents": list,
    "gasUsed": int,
    "explorer": dict,
    "receiptFile": str,
    "logLine": str,
    "headline": bool,
    "artefacts": list,
    "notes": str,
}


def validate_evidence(obj: Mapping[str, Any], folder: Path | None = None) -> list[str]:
    """Problems of an ``acb-run/1`` object (empty = valid). With ``folder``, referenced files must exist."""
    problems: list[str] = []
    for key, kind in _TOP_KEYS.items():
        if not isinstance(obj.get(key), kind):
            problems.append(f"{key}: missing or not {kind.__name__}")
    if obj.get("schema") != "acb-run/1":
        problems.append("schema must be acb-run/1")
    seqs = []
    for i, t in enumerate(obj.get("transactions") or []):
        for key, kind in _TX_KEYS.items():
            value = t.get(key)
            if kind is int and isinstance(value, bool):
                problems.append(f"transactions[{i}].{key}: bool where int expected")
            elif not isinstance(value, kind):
                problems.append(f"transactions[{i}].{key}: missing or not {kind.__name__}")
        h = str(t.get("txHash", ""))
        if not (h.startswith("0x") and len(h) == 66 and h == h.lower()):
            problems.append(f"transactions[{i}].txHash is not a lowercase 32-byte hash")
        if t.get("status") not in (0, 1) or t.get("expectedStatus") not in (0, 1):
            problems.append(f"transactions[{i}]: status must be 0 or 1")
        seqs.append(t.get("seq"))
        if folder is not None:
            for ref in [t.get("receiptFile"), *(t.get("artefacts") or [])]:
                if ref and not (folder / ref).is_file():
                    problems.append(f"transactions[{i}] references missing file {ref}")
    if seqs != list(range(1, len(seqs) + 1)):
        problems.append("transactions must be numbered 1..n in order")
    for i, g in enumerate(obj.get("gradeChanges") or []):
        for key in ("agentId", "from", "to", "score", "reasons", "txHash", "bundle", "recompute"):
            if key not in g:
                problems.append(f"gradeChanges[{i}].{key} missing")
        if folder is not None and g.get("bundle") and not (folder / g["bundle"]).is_file():
            problems.append(f"gradeChanges[{i}] bundle {g['bundle']} missing")
    for i, s in enumerate(obj.get("steps") or []):
        for key in ("id", "title", "status", "startedAt", "txCount", "notes"):
            if key not in s:
                problems.append(f"steps[{i}].{key} missing")
    return problems


# ---------------------------------------------------------------------------
# building the staging folder
# ---------------------------------------------------------------------------


def _copy_tree(src: Path, dst: Path) -> int:
    if not src.is_dir():
        return 0
    count = 0
    for path in sorted(src.rglob("*")):
        if path.is_file() and path.suffix == ".json":
            target = dst / path.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            count += 1
    return count


def _signer_records(out_dir: Path) -> dict[str, dict[str, Any]]:
    """decisionHash -> the signer's request record (and tx hash -> decision hash)."""
    records: dict[str, dict[str, Any]] = {}
    path = out_dir / "signer.jsonl"
    if not path.is_file():
        return records
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        request = (event.get("data") or {}).get("request") or {}
        decision = request.get("signerDecision") or {}
        digest = str(decision.get("decisionHash") or "").lower()
        if not digest:
            continue
        records[digest] = {
            "schema": "acb-decision-record/1",
            "seq": event.get("seq"),
            "ts": event.get("ts"),
            "orderId": (event.get("data") or {}).get("orderId"),
            "payee": (event.get("data") or {}).get("payee"),
            "amount": (event.get("data") or {}).get("amount"),
            "reasonCodes": event.get("reason_codes") or [],
            "request": request,
        }
    return records


def _artefacts(rec: Mapping[str, Any], folder: Path, decisions_by_tx: Mapping[str, str]) -> list[str]:
    out: list[str] = []

    def add(rel: str) -> None:
        if (folder / rel).is_file() and rel not in out:
            out.append(rel)

    digest = decisions_by_tx.get(rec["txHash"])
    if digest:
        add(f"decisions/{digest}.json")
    for lg in rec["_events"]:
        a = lg["args"]
        ev = lg["event"]
        if ev in ("Paid", "EscrowOpened", "DecisionRecorded") and a.get("invoiceHash"):
            add(f"invoices/{str(a['invoiceHash']).lower()}.json")
        elif ev == "ViolationRecorded":
            add(f"violations/{str(a['evidenceHash']).lower()}.json")
        elif ev == "TierChanged":
            add(f"autonomy/{str(a['evidenceHash']).lower()}.json")
            add(f"violations/{str(a['evidenceHash']).lower()}.json")
        elif ev == "GradePosted":
            add(f"bundles/{int(a['agentId'])}-{int(a['asOfBlock'])}.json")
        elif ev == "NewFeedback":
            uri = str(a.get("feedbackURI") or "")
            name = uri.rsplit("/", 1)[-1]
            for sub in ("reports", "bundles"):
                if name:
                    add(f"{sub}/{name}")
        elif ev == "AppealFiled":
            add(f"appeals/{str(a['appealHash']).lower()}.json")
        elif ev == "AppealResolved":
            add(f"appeals/reviews/{str(a['evidenceHash']).lower()}.json")
    return out


def _find_dialogue_eval(out_dir: Path) -> Path | None:
    """The real-LLM dialogue evaluation (spec 8.9) of this run, or the committed one; never another run's."""
    candidates = [out_dir / "dialogue_eval.json", config.REPO_ROOT / "evidence" / "dialogue_eval.json"]
    for path in candidates:
        if path.is_file():
            return path
    return None


def build_backtest(out_dir: Path) -> Path:
    """``acb bureau backtest --agents 300 --rounds 6 --seed 2026`` (deterministic) into the run output."""
    from acb.bureau.backtest import run_backtest, write_report

    target = out_dir / "backtest.json"
    if not target.is_file():
        write_report(run_backtest(agents=300, rounds=6, seed=2026), target)
    return target


def build_run_evidence(run: Any, *, with_backtest: bool = True) -> dict[str, Any]:
    """Build (or rebuild) the staging folder of ``run``; returns ``{"folder", "transactions", "headline"}``."""
    from acb.scenario import headline as hl

    env = run.env
    rec = run.recorder
    out_dir = Path(env.out_dir)
    folder = out_dir / "evidence"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    rec.renumber()
    # artefacts
    for name in COPIED_TREES:
        _copy_tree(out_dir / name, folder / name)
    signer = _signer_records(out_dir)
    decisions_by_tx: dict[str, str] = {}
    for digest, body in signer.items():
        write_json(folder / "decisions" / f"{digest}.json", body)
        for t in (body.get("request") or {}).get("tx") or []:
            if t.get("hash"):
                decisions_by_tx[str(t["hash"]).lower()] = digest
    for r in rec.records:
        src = out_dir / r["receiptFile"]
        if src.is_file():
            target = folder / r["receiptFile"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
        r["artefacts"] = _artefacts(r, folder, decisions_by_tx)
    rows = hl.mark_headline(run, rec.records, run.cfg.get("headline") or [])
    transactions = [rec.public(r) for r in rec.records]
    # grade changes
    recomputed = {str(k).lower(): v for k, v in (run.facts.get("recomputeAll") or {}).items()}
    grade_changes = []
    from acb.types import Grade

    for r, lg in rec.iter_events(None, "GradePosted"):
        a = lg["args"]
        if int(a["grade"]) == int(a["previousGrade"]):
            continue
        bundle = f"bundles/{int(a['agentId'])}-{int(a['asOfBlock'])}.json"
        grade_changes.append(
            {
                "agentId": str(a["agentId"]),
                "agent": run.persona_of(a["agentId"]),
                "step": r["step"],
                "from": Grade(int(a["previousGrade"])).name,
                "to": Grade(int(a["grade"])).name,
                "score": int(a["score"]),
                "reasons": [int(x) for x in a["reasons"] if int(x)],
                "evidenceHash": str(a["evidenceHash"]).lower(),
                "txHash": r["txHash"],
                "bundle": bundle,
                "recompute": recomputed.get(r["txHash"], "NOT_RUN"),
            }
        )
    # off-chain decisions
    offchain = []
    for sid, entry in sorted(run.state["steps"].items(), key=lambda kv: int(kv[0][1:])):
        for d in entry.get("offchain") or []:
            offchain.append(
                {
                    "step": sid,
                    "decision": d.get("outcome"),
                    "reasonCodes": list(d.get("reasons") or []),
                    "invoiceHash": d.get("invoiceHash"),
                    "orderId": d.get("orderId"),
                    "logLine": d.get("logLine")
                    or f"{sid} {d.get('outcome')} reasons={','.join(str(c) for c in d.get('reasons') or [])}",
                }
            )
    # side files
    extra: dict[str, str | None] = {"attackSuite": None, "backtest": None, "dialogueEval": None}
    attack = config.REPO_ROOT / "evidence" / "attack_suite.json"
    if attack.is_file():
        shutil.copyfile(attack, folder / "attack_suite.json")
        extra["attackSuite"] = "attack_suite.json"
    if with_backtest:
        shutil.copyfile(build_backtest(out_dir), folder / "backtest.json")
        extra["backtest"] = "backtest.json"
    dialogue_eval = _find_dialogue_eval(out_dir)
    if dialogue_eval is not None:
        shutil.copyfile(dialogue_eval, folder / "dialogue_eval.json")
        extra["dialogueEval"] = "dialogue_eval.json"
    evidence = {
        "schema": "acb-run/1",
        "runId": env.run_id,
        "network": env.network,
        "chainId": int(env.net.chain_id),
        "startedAt": run.state.get("startedAt"),
        "finishedAt": max(
            (s.get("finishedAt") or "" for s in run.state["steps"].values()), default=run.state.get("startedAt")
        ),
        "gitCommit": git_commit(),
        "contracts": _contracts(env.dep),
        "erc8004": run.facts.get("erc8004") or {},
        "llm": _llm_block(run),
        "steps": _steps(run),
        "transactions": transactions,
        "offchainDecisions": offchain,
        "gradeChanges": grade_changes,
        "attackSuite": extra["attackSuite"],
        "backtest": extra["backtest"],
        "dialogueEval": extra["dialogueEval"],
        "mandate": {"mandateId": run.facts.get("mandateId"), "registerTx": run.facts.get("mandateTx")},
        "recompute": run.facts.get("recompute") or [],
    }
    problems = validate_evidence(evidence, folder)
    if problems:
        raise EvidenceError("evidence.json is not valid: " + "; ".join(problems[:5]))
    write_json(folder / "evidence.json", evidence)
    write_text(folder / "evidence.csv", render_csv(transactions))
    write_text(folder / "HEADLINE.md", hl.render(evidence, rows))
    write_text(folder / "run.txt", "\n".join(run.run_lines) + "\n")
    write_text(folder / "narration.srt", render_srt(run.narration))
    return {"folder": str(folder), "transactions": len(transactions), "headline": sum(1 for r in rows if r["record"])}


def _contracts(dep: Any) -> dict[str, Any]:
    c = dep.contracts
    return {
        "AgentCreditBureau": c["AgentCreditBureau"].address,
        "IdentityRegistry": c["IdentityRegistry"].address,
        "ReputationRegistry": c["ReputationRegistry"].address,
        "USDC": c["USDC"].address,
        "vaults": {name: v.address for name, v in dep.vaults.items()},
    }


def _llm_block(run: Any) -> dict[str, Any]:
    from acb.agent.prompts import prompt_hash

    llm = dict(run.facts.get("llm") or {})
    per_step: dict[str, str] = {}
    for spec in run.cfg.get("steps") or []:
        kind = spec.get("llm")
        if not kind:
            continue
        if str(kind).startswith("mock-naive"):
            per_step[spec["id"]] = kind
        else:
            per_step[spec["id"]] = llm.get("mode", "mock") if llm.get("mode") != "mock" else "mock-careful"
    latencies = [int(x) for x in run.facts.get("llmLatencies") or [] if isinstance(x, int)]
    return {
        "mode": llm.get("mode", run.env.llm_mode),
        "model": llm.get("model"),
        "promptVersion": llm.get("promptVersion", "acb-prompt-1.0.0"),
        "promptHash": prompt_hash(),
        "perStep": per_step,
        "latencyMsMedian": int(statistics.median(latencies)) if latencies else 0,
    }


def _steps(run: Any) -> list[dict[str, Any]]:
    out = []
    for spec in run.cfg.get("steps") or []:
        entry = run.state["steps"].get(spec["id"])
        if entry is None:
            continue
        notes = list(entry.get("notes") or []) + [f"FAILED: {f}" for f in entry.get("failures") or []]
        out.append(
            {
                "id": spec["id"],
                "title": spec.get("title", spec["id"]),
                "status": entry.get("status"),
                "startedAt": entry.get("startedAt"),
                "finishedAt": entry.get("finishedAt"),
                "txCount": len(run.recorder.of_step(spec["id"])),
                "decisions": [[d.get("outcome"), list(d.get("reasons") or [])] for d in entry.get("decisions") or []],
                "narration": spec.get("narration", ""),
                "notes": "; ".join(notes),
            }
        )
    return out


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


def verify_folder(
    folder: Path, *, rpc: str | None = None, rps: float = 50.0, out: Callable[[str], None] | None = None
) -> dict[str, Any]:
    """Run the stand-alone verifier on a staging or exported folder: offline, and against ``rpc``."""
    verifier = load_verifier()
    lines: list[str] = []
    sink = out or lines.append
    offline = verifier.verify(folder / "evidence.json", receipts_only=True, out=sink)
    online = verifier.verify(folder / "evidence.json", rpc_url=rpc, rps=rps, out=sink) if rpc else offline
    return {
        "ok": online["ok"],
        "verified": online["verified"],
        "total": online["total"],
        "failures": online["failures"],
        "offlineOk": offline["ok"],
        "offlineVerified": offline["verified"],
        "lines": lines,
    }


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def staging_folder(run_id: str, network: str) -> Path:
    return config.REPO_ROOT / "runs" / run_id / "out" / network / "evidence"


def latest_run_id(network: str) -> str | None:
    runs = sorted(
        (config.REPO_ROOT / "runs").glob(f"*/out/{network}/evidence/evidence.json"), key=lambda p: p.stat().st_mtime
    )
    return runs[-1].parents[3].name if runs else None


def export(run_id: str, network: str, *, dest: Path | None = None) -> dict[str, Any]:
    """Copy the curated staging folder of ``run_id`` into ``evidence/<network>/`` (spec 10.4)."""
    src = staging_folder(run_id, network)
    if not (src / "evidence.json").is_file():
        raise EvidenceError(f"{src} has no evidence.json: run the scenario through S21 first")
    evidence = json.loads((src / "evidence.json").read_text(encoding="utf-8"))
    problems = validate_evidence(evidence, src)
    if problems:
        raise EvidenceError("refusing to export invalid evidence: " + "; ".join(problems[:5]))
    unexpected = [p.name for p in src.iterdir() if p.name not in CURATED]
    if unexpected:
        raise EvidenceError(f"staging folder has non-curated entries: {unexpected}")
    target = dest or (config.REPO_ROOT / "evidence" / network)
    target.mkdir(parents=True, exist_ok=True)
    for entry in target.iterdir():
        if entry.name in KEEP_ON_EXPORT:
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    copied = 0
    for entry in sorted(src.iterdir()):
        if entry.is_dir():
            for path in sorted(entry.rglob("*")):
                if path.is_file():
                    out = target / path.relative_to(src)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, out)
                    copied += 1
        else:
            shutil.copyfile(entry, target / entry.name)
            copied += 1
    if dest is None:
        # the run-independent results also live at the top of evidence/ (spec 4.7, 8.9, 12.3)
        for name in ROOT_COPIES:
            if (src / name).is_file():
                shutil.copyfile(src / name, config.REPO_ROOT / "evidence" / name)
        # the newest counted attack suite (written by `acb attacks run --export`) goes next to the run
        suite = config.REPO_ROOT / "evidence" / "attack_suite.json"
        if suite.is_file() and (src / "attack_suite.json").is_file():
            shutil.copyfile(suite, target / "attack_suite.json")
    return {"from": str(src), "to": str(target), "files": copied, "transactions": len(evidence["transactions"])}


__all__ = [
    "CSV_COLUMNS",
    "CURATED",
    "EvidenceError",
    "build_backtest",
    "build_run_evidence",
    "export",
    "latest_run_id",
    "load_verifier",
    "render_csv",
    "render_srt",
    "render_topic_table",
    "staging_folder",
    "topic_table",
    "validate_evidence",
    "verify_folder",
]


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["topics"]:
        print(render_topic_table())
