"""Fill the measured values of the deliverables from the evidence files.

Every measured number a deliverable cites (transaction hashes, counts, attack-suite totals, backtest and
dialogue metrics, gas, timings, grades reached in the run) is written as a placeholder ``{{name}}`` and
registered in ``docs/deliverables/placeholders.md``: one table row per name with its meaning, its source
file and how to extract it. This script reads that registry and the evidence files, replaces every
``{{name}}`` (or ``{{name|filter}}``) in ``docs/deliverables/*.md`` and writes the result to
``docs/deliverables/out/*.md``. It fails loudly: an unknown name, a value that cannot be resolved or any
``{{...}}`` left over is an error, and nothing is written.

Usage (stdlib and PyYAML only)::

    uv run python scripts/render_deliverables.py                       # Base Sepolia evidence
    uv run python scripts/render_deliverables.py --network anvil       # dry run on the local evidence
    uv run python scripts/render_deliverables.py --check               # resolve every registry entry
    uv run python scripts/render_deliverables.py --keep-going          # write drafts, still exit 1

Exit code 0 only when every placeholder in every deliverable resolved (or, with ``--check``, every
registry entry).
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import io
import json
import re
import statistics
import sys
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent
DELIVERABLES = REPO / "docs" / "deliverables"
REGISTRY = DELIVERABLES / "placeholders.md"
OUT_DIR = DELIVERABLES / "out"
NETWORKS = REPO / "config" / "networks.yaml"
VERIFY_SCRIPT = REPO / "scripts" / "verify_evidence.py"

NAME = r"[a-z][a-z0-9_]*"
USE_RE = re.compile(r"\{\{\s*(" + NAME + r")\s*(?:\|\s*([a-z_]+)\s*)?\}\}")
LEFTOVER_RE = re.compile(r"\{\{[^{}\n]*\}\}")
ROW_RE = re.compile(r"^\|\s*`\{\{(" + NAME + r")\}\}`\s*\|(.*)\|\s*$")
GROUPS_HEADING = "## Attack class groups"

FORMATS = {
    "text",
    "int",
    "grouped",
    "num",
    "usdc",
    "eth",
    "gwei",
    "bps_pct",
    "bps_dec",
    "rate_pct",
    "duration",
    "hash",
    "address",
    "tier",
    "yesno",
    "table",
}
FILTERS = {"short", "basescan", "blockscout", "link"}
CATEGORIES = ("blocked", "contained", "bounded", "escalated", "failed", "error")


class Unresolved(Exception):
    """A placeholder whose value cannot be produced from the evidence."""


# ---------------------------------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------------------------------


class Entry:
    def __init__(self, name: str, meaning: str, source: str, extract: str, fmt: str, line: int) -> None:
        self.name = name
        self.meaning = meaning
        self.source = source
        self.extract = extract
        self.fmt = fmt
        self.line = line


def _cell(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text.startswith("`") and text.endswith("`") and text.count("`") == 2:
        text = text[1:-1]
    return text.strip()


def load_registry(path: Path = REGISTRY) -> tuple[dict[str, Entry], dict[str, list[str]]]:
    """The registry rows ``{name: Entry}`` and the attack class groups ``{group: [class, ...]}``."""
    if not path.is_file():
        raise SystemExit(f"registry not found: {path}")
    text = path.read_text(encoding="utf-8-sig")
    entries: dict[str, Entry] = {}
    problems: list[str] = []
    for no, line in enumerate(text.splitlines(), start=1):
        m = ROW_RE.match(line)
        if not m:
            continue
        cells = m.group(2).split("|")
        if len(cells) != 4:
            problems.append(f"placeholders.md:{no}: expected 5 columns, found {len(cells) + 1}")
            continue
        name = m.group(1)
        meaning, source, extract, fmt = (_cell(c) for c in cells)
        if name in entries:
            problems.append(f"placeholders.md:{no}: duplicate name {name} (first on line {entries[name].line})")
            continue
        base_fmt = fmt[5:-1] if fmt.startswith("list(") and fmt.endswith(")") else fmt
        if base_fmt not in FORMATS and fmt != "list":
            problems.append(f"placeholders.md:{no}: {name}: unknown format {fmt!r}")
        if not re.match(r"^(json|count|sum|median|min|max|fn):", extract):
            problems.append(
                f"placeholders.md:{no}: {name}: extract must start with json:, count:, sum:, median:, min:, max: or fn:"
            )
        entries[name] = Entry(name, meaning, source, extract, fmt, no)
    if not entries:
        problems.append("placeholders.md: no registry rows found")
    groups = _load_groups(text, problems)
    if problems:
        raise SystemExit("invalid registry:\n  " + "\n  ".join(problems))
    return entries, groups


def _load_groups(text: str, problems: list[str]) -> dict[str, list[str]]:
    start = text.find(GROUPS_HEADING)
    if start < 0:
        problems.append(f"placeholders.md: section {GROUPS_HEADING!r} not found")
        return {}
    m = re.search(r"```yaml\n(.*?)```", text[start:], re.DOTALL)
    if not m:
        problems.append("placeholders.md: no ```yaml block under the attack class groups heading")
        return {}
    data = yaml.safe_load(m.group(1)) or {}
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, dict) or not groups:
        problems.append("placeholders.md: the attack class groups block needs a non-empty 'groups' map")
        return {}
    seen: dict[str, str] = {}
    out: dict[str, list[str]] = {}
    for group, classes in groups.items():
        if not isinstance(classes, list) or not classes:
            problems.append(f"placeholders.md: attack group {group} has no classes")
            continue
        for c in classes:
            if c in seen:
                problems.append(f"placeholders.md: attack class {c} is in both {seen[c]} and {group}")
            seen[c] = group
        out[str(group)] = [str(c) for c in classes]
    return out


# ---------------------------------------------------------------------------------------------------
# path language: json:, count:, sum:, median:, min:, max:
# ---------------------------------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\.|\[[^\]]*\]|[A-Za-z0-9_\-]+")
_COND_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*(=|!=|~=)\s*(.*?)\s*$")
_HAS_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\?\s*$")


def _tokens(path: str) -> list[str]:
    pos = 0
    out = []
    while pos < len(path):
        m = _TOKEN_RE.match(path, pos)
        if not m:
            raise Unresolved(f"cannot parse path {path!r} at {path[pos:]!r}")
        out.append(m.group(0))
        pos = m.end()
    return [t for t in out if t != "."]


def _norm(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def _keep(item: Any, conds: list[tuple[str, str, str]]) -> bool:
    if not isinstance(item, dict):
        raise Unresolved("a filter needs a list of objects")
    for key, op, want in conds:
        if op == "?":
            if item.get(key) is None:
                return False
            continue
        if key not in item:
            return False
        have = _norm(item[key])
        if op == "=" and have != want:
            return False
        if op == "!=" and have == want:
            return False
        if op == "~=" and want not in have:
            return False
    return True


def evaluate(doc: Any, path: str) -> Any:
    """Walk ``path`` (``a.b[k=v,k2!=w][0].c``) through ``doc``. A key applied to a list maps over it."""
    cur = doc
    for tok in _tokens(path):
        if tok.startswith("["):
            inner = tok[1:-1].strip()
            if not isinstance(cur, list):
                raise Unresolved(f"{tok} applied to a non-list in {path!r}")
            if inner == "*":
                continue
            if re.fullmatch(r"-?\d+", inner):
                idx = int(inner)
                if not -len(cur) <= idx < len(cur):
                    raise Unresolved(f"index {idx} out of range (list of {len(cur)}) in {path!r}")
                cur = cur[idx]
                continue
            conds = []
            for part in inner.split(","):
                h = _HAS_RE.match(part)
                if h:
                    conds.append((h.group(1), "?", ""))
                    continue
                c = _COND_RE.match(part)
                if not c:
                    raise Unresolved(f"bad filter {part!r} in {path!r}")
                conds.append((c.group(1), c.group(2), c.group(3)))
            cur = [item for item in cur if _keep(item, conds)]
            continue
        if isinstance(cur, list):
            try:
                cur = [item[tok] for item in cur]
            except (KeyError, TypeError):
                raise Unresolved(f"key {tok!r} missing in a list element in {path!r}") from None
        elif isinstance(cur, dict):
            if tok not in cur:
                raise Unresolved(f"key {tok!r} not found in {path!r}")
            cur = cur[tok]
        else:
            raise Unresolved(f"key {tok!r} applied to a scalar in {path!r}")
    return cur


def _int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        raise Unresolved(f"not an integer: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value).strip()
    if re.fullmatch(r"0x[0-9a-fA-F]+", text):
        return int(text, 16)
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    raise Unresolved(f"not an integer: {value!r}")


def _numbers(value: Any) -> list[int]:
    if not isinstance(value, list):
        raise Unresolved("an aggregate needs a list")
    return [_int(v) for v in value]


def _median(values: list[int]) -> int | float:
    if not values:
        raise Unresolved("median of an empty list")
    m = statistics.median(values)
    return int(m) if float(m).is_integer() else m


def _count(value: Any) -> int:
    if not isinstance(value, list):
        raise Unresolved("count needs a list")
    return len(value)


def _extreme(pick: Callable[[list[int]], int]) -> Callable[[Any], int]:
    def run(value: Any) -> int:
        numbers = _numbers(value)
        if not numbers:
            raise Unresolved("min or max of an empty list")
        return pick(numbers)

    return run


AGGREGATES: dict[str, Callable[[Any], Any]] = {
    "json": lambda v: v,
    "count": _count,
    "sum": lambda v: sum(_numbers(v)),
    "median": lambda v: _median(_numbers(v)),
    "min": _extreme(min),
    "max": _extreme(max),
}


# ---------------------------------------------------------------------------------------------------
# context: sources, explorers
# ---------------------------------------------------------------------------------------------------


class Context:
    def __init__(self, network: str, groups: dict[str, list[str]]) -> None:
        self.network = network
        self.groups = groups
        self._cache: dict[str, Any] = {}
        nets = yaml.safe_load(NETWORKS.read_text(encoding="utf-8-sig")) if NETWORKS.is_file() else {}
        if network not in (nets or {}):
            raise SystemExit(f"unknown network {network!r} (config/networks.yaml)")
        self.explorers = {e["name"]: e for e in (nets[network].get("explorers") or [])}

    def path(self, rel: str) -> Path:
        rel = rel.strip().replace("<network>", self.network)
        p = (REPO / rel).resolve()
        if REPO.resolve() not in p.parents and p != REPO.resolve():
            raise Unresolved(f"source outside the repository: {rel}")
        return p

    def load(self, rel: str) -> Any:
        key = rel.strip()
        if key in self._cache:
            return self._cache[key]
        p = self.path(key)
        if not p.exists():
            raise Unresolved(f"source file missing: {key.replace('<network>', self.network)}")
        if p.is_dir():
            value: Any = p
        elif p.suffix == ".json":
            try:
                value = json.loads(p.read_text(encoding="utf-8-sig"))
            except ValueError as exc:
                raise Unresolved(f"{key}: invalid JSON ({exc})") from None
        else:
            value = p.read_text(encoding="utf-8-sig")
        self._cache[key] = value
        return value

    def evidence(self) -> dict[str, Any]:
        return self.load("evidence/<network>/evidence.json")

    def explorer_url(self, name: str, value: str) -> str:
        e = self.explorers.get(name)
        if e is None:
            raise Unresolved(f"no {name} explorer for network {self.network}")
        if re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
            return e["tx"].replace("{hash}", value)
        if re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
            return e["address"].replace("{addr}", value)
        raise Unresolved(f"{name} link needs a transaction hash or an address, got {value!r}")


# ---------------------------------------------------------------------------------------------------
# computed values (fn:)
# ---------------------------------------------------------------------------------------------------


def _tier_label(tier: Any) -> str:
    t = _int(tier)
    return "FROZEN" if t == 255 else str(t)


def _iso(text: str) -> datetime:
    return datetime.fromisoformat(str(text))


def fn_run_duration(ctx: Context) -> int:
    ev = ctx.evidence()
    return int((_iso(ev["finishedAt"]) - _iso(ev["startedAt"])).total_seconds())


def fn_tier_timeline(ctx: Context) -> str:
    txs = sorted(ctx.evidence()["transactions"], key=lambda t: t["seq"])
    if not txs:
        raise Unresolved("no transactions")
    parts = [_tier_label(txs[0]["tierBefore"])]
    cur = _int(txs[0]["tierBefore"])
    for t in txs:
        before, after = _int(t["tierBefore"]), _int(t["tierAfter"])
        if before != cur:
            parts.append(f"{_tier_label(before)} (before {t['step']})")
        if after != before:
            reasons = ", ".join(str(c) for c in t.get("reasonCodes") or [])
            parts.append(f"{_tier_label(after)} ({t['step']}{', ' + reasons if reasons else ''})")
        cur = after
    return " -> ".join(parts)


def _grade_changes(ctx: Context, agent: str) -> list[dict[str, Any]]:
    changes = [g for g in ctx.evidence()["gradeChanges"] if g.get("agent") == agent]
    if not changes:
        raise Unresolved(f"no grade changes for {agent}")
    return changes


def fn_grade_path(ctx: Context, agent: str) -> str:
    changes = _grade_changes(ctx, agent)
    parts = [str(changes[0]["from"])] + [f"{g['to']} ({g['step']})" for g in changes]
    return " -> ".join(parts)


def fn_grade_final(ctx: Context, agent: str) -> str:
    return str(_grade_changes(ctx, agent)[-1]["to"])


def _receipts(ctx: Context, agent_key_only: bool = False) -> list[dict[str, Any]]:
    cache_key = f"_receipts_{agent_key_only}"
    if cache_key in ctx._cache:
        return ctx._cache[cache_key]
    out = []
    for t in ctx.evidence()["transactions"]:
        if agent_key_only and not t.get("signedByAgentKey"):
            continue
        rel = t.get("receiptFile")
        if not rel:
            raise Unresolved(f"transaction {t.get('seq')} has no receiptFile")
        doc = ctx.load(f"evidence/<network>/{rel}")
        out.append(doc.get("receipt") or {})
    ctx._cache[cache_key] = out
    return out


def fn_eth_spent(ctx: Context, scope: str = "all") -> int:
    """Wei paid for the run's transactions: L2 execution fee plus the OP-stack L1 data fee."""
    if scope not in ("all", "agent_key", "l1_fee"):
        raise Unresolved(f"eth_spent scope must be all, agent_key or l1_fee, not {scope}")
    total = 0
    for r in _receipts(ctx, agent_key_only=scope == "agent_key"):
        l1 = _int(r["l1Fee"]) if r.get("l1Fee") is not None else 0
        if scope == "l1_fee":
            total += l1
        else:
            total += _int(r["gasUsed"]) * _int(r["effectiveGasPrice"]) + l1
    return total


def fn_gas_price_median(ctx: Context) -> int | float:
    return _median([_int(r["effectiveGasPrice"]) for r in _receipts(ctx)])


def fn_tx_unexpected_status(ctx: Context) -> int:
    return sum(1 for t in ctx.evidence()["transactions"] if t["status"] != t["expectedStatus"])


def fn_distinct_actor_addresses(ctx: Context) -> int:
    return len({str(t["actorAddress"]).lower() for t in ctx.evidence()["transactions"]})


def fn_block_span(ctx: Context) -> int:
    blocks = [_int(t["block"]) for t in ctx.evidence()["transactions"]]
    if not blocks:
        raise Unresolved("no transactions")
    return max(blocks) - min(blocks) + 1


def fn_csv_rows(ctx: Context) -> int:
    text = ctx.load("evidence/<network>/evidence.csv")
    rows = list(csv.reader(io.StringIO(text)))
    return max(0, len(rows) - 1)


def fn_headline_table(ctx: Context) -> str:
    text = ctx.load("evidence/<network>/HEADLINE.md")
    lines, started = [], False
    for line in text.splitlines():
        if line.startswith("|"):
            started = True
            lines.append(line)
        elif started:
            break
    if len(lines) < 3:
        raise Unresolved("HEADLINE.md has no table")
    if any("| missing |" in line for line in lines):
        raise Unresolved("HEADLINE.md lists a missing headline transaction")
    return "\n".join(lines)


def fn_verify_offline(ctx: Context, field: str) -> Any:
    """``scripts/verify_evidence.py --receipts-only`` on the evidence folder (stdlib, no network)."""
    if "_verify" not in ctx._cache:
        evidence_path = ctx.path("evidence/<network>/evidence.json")
        if not evidence_path.is_file():
            raise Unresolved(f"source file missing: evidence/{ctx.network}/evidence.json")
        spec = importlib.util.spec_from_file_location("acb_verify_evidence", VERIFY_SCRIPT)
        if spec is None or spec.loader is None:
            raise Unresolved(f"cannot load {VERIFY_SCRIPT}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ctx._cache["_verify"] = module.verify(evidence_path, receipts_only=True, out=lambda *_a, **_k: None)
    report = ctx._cache["_verify"]
    if field == "verified":
        return report["verified"]
    if field == "total":
        return report["total"]
    if field == "summary":
        return f"{report['verified']} / {report['total']}"
    raise Unresolved(f"verify_offline field must be verified, total or summary, not {field}")


def _attacks(ctx: Context) -> list[dict[str, Any]]:
    suite = ctx.load("evidence/attack_suite.json")
    results = suite.get("results") or []
    known = {c for classes in ctx.groups.values() for c in classes}
    unmapped = sorted({r["class"] for r in results if r["class"] not in known})
    if unmapped:
        raise Unresolved(f"attack classes without a group in placeholders.md: {', '.join(unmapped)}")
    return results


def _group_of(ctx: Context, cls: str) -> str:
    for group, classes in ctx.groups.items():
        if cls in classes:
            return group
    raise Unresolved(f"attack class {cls} has no group")


def fn_atk_count(ctx: Context, group: str, category: str) -> int:
    if group != "all" and group not in ctx.groups:
        raise Unresolved(f"unknown attack group {group}")
    if category != "total" and category not in CATEGORIES:
        raise Unresolved(f"unknown attack category {category}")
    rows = [r for r in _attacks(ctx) if group == "all" or _group_of(ctx, r["class"]) == group]
    if category == "total":
        return len(rows)
    return sum(1 for r in rows if str(r["actual"]).lower() == category)


def fn_atk_ids(ctx: Context, category: str) -> list[str]:
    return [r["id"] for r in _attacks(ctx) if str(r["actual"]).lower() == category]


def fn_atk_mismatch_ids(ctx: Context) -> list[str]:
    return [r["id"] for r in _attacks(ctx) if r["actual"] != r["expected"]]


def fn_atk_matched_expected(ctx: Context) -> int:
    return sum(1 for r in _attacks(ctx) if r["actual"] == r["expected"])


def fn_atk_tx_count(ctx: Context) -> int:
    return sum(len(r.get("txHashes") or []) for r in _attacks(ctx))


def _md(text: Any) -> str:
    return str(text if text is not None else "-").replace("|", "\\|").replace("\n", " ")


def _group_label(group: str) -> str:
    return group.replace("_", " ").capitalize()


def fn_atk_table(ctx: Context) -> str:
    lines = [
        "| Id | Class group | Attack | First layer | Expected | Actual | Reason codes |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in _attacks(ctx):
        reasons = ", ".join(str(c) for c in r.get("reasonCodes") or []) or "-"
        lines.append(
            f"| {r['id']} | {_group_label(_group_of(ctx, r['class']))} | {_md(r.get('title'))} | {_md(r.get('layer'))} "
            f"| {r['expected']} | {r['actual']} | {reasons} |"
        )
    return "\n".join(lines)


def fn_atk_group_table(ctx: Context) -> str:
    lines = [
        "| Class group | Attacks | Blocked | Contained | Bounded | Escalated | Failed |",
        "|---|---|---|---|---|---|---|",
    ]
    for group in [*ctx.groups, "all"]:
        label = "All" if group == "all" else _group_label(group)
        counts = [fn_atk_count(ctx, group, c) for c in ("total", *CATEGORIES[:5])]
        cells = " | ".join(str(c) for c in counts)
        lines.append(f"| {'**' + label + '**' if group == 'all' else label} | {cells} |")
    return "\n".join(lines)


def fn_benign_table(ctx: Context) -> str:
    suite = ctx.load("evidence/benign_suite.json")
    lines = ["| Id | Task | Expected | Actual | False positive |", "|---|---|---|---|---|"]
    for r in suite.get("results") or []:
        fp = "yes" if r.get("falsePositive") else "no"
        lines.append(f"| {r['id']} | {_md(r.get('title'))} | {r.get('expected')} | {r.get('actual')} | {fp} |")
    if len(lines) == 2:
        raise Unresolved("benign_suite.json has no results")
    return "\n".join(lines)


def fn_steps_table(ctx: Context) -> str:
    lines = ["| Step | Title | Status | Transactions |", "|---|---|---|---|"]
    for s in ctx.evidence()["steps"]:
        lines.append(f"| {s['id']} | {_md(s.get('title'))} | {s.get('status')} | {s.get('txCount')} |")
    if len(lines) == 2:
        raise Unresolved("evidence.json has no steps")
    return "\n".join(lines)


def fn_bt_defaulters_total(ctx: Context) -> int:
    m = ctx.load("evidence/backtest.json")["metrics"]
    return _int(m["defaultersReachedD"]) + _int(m["defaultersNeverD"])


FUNCTIONS: dict[str, Callable[..., Any]] = {
    "run_duration": fn_run_duration,
    "tier_timeline": fn_tier_timeline,
    "grade_path": fn_grade_path,
    "grade_final": fn_grade_final,
    "eth_spent": fn_eth_spent,
    "gas_price_median": fn_gas_price_median,
    "tx_unexpected_status": fn_tx_unexpected_status,
    "distinct_actor_addresses": fn_distinct_actor_addresses,
    "block_span": fn_block_span,
    "csv_rows": fn_csv_rows,
    "headline_table": fn_headline_table,
    "verify_offline": fn_verify_offline,
    "atk_count": fn_atk_count,
    "atk_ids": fn_atk_ids,
    "atk_mismatch_ids": fn_atk_mismatch_ids,
    "atk_matched_expected": fn_atk_matched_expected,
    "atk_tx_count": fn_atk_tx_count,
    "atk_table": fn_atk_table,
    "atk_group_table": fn_atk_group_table,
    "benign_table": fn_benign_table,
    "steps_table": fn_steps_table,
    "bt_defaulters_total": fn_bt_defaulters_total,
}


# ---------------------------------------------------------------------------------------------------
# formats and use-site filters
# ---------------------------------------------------------------------------------------------------


def _sig(d: Decimal, digits: int = 6) -> str:
    if d == 0:
        return "0"
    exp = d.adjusted() - (digits - 1)
    q = d.quantize(Decimal(1).scaleb(min(exp, 0)))
    text = format(q, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def fmt_value(value: Any, fmt: str) -> str:
    if value is None:
        raise Unresolved("value is null in the evidence")
    if fmt == "list" or (fmt.startswith("list(") and fmt.endswith(")")):
        if not isinstance(value, list):
            raise Unresolved(f"format {fmt} needs a list, got {type(value).__name__}")
        inner = "text" if fmt == "list" else fmt[5:-1]
        return ", ".join(fmt_value(v, inner) for v in value) if value else "none"
    if isinstance(value, (list, dict)) and fmt not in ("table",):
        raise Unresolved(f"format {fmt} needs a single value, got a {type(value).__name__} (missing [0]?)")
    if fmt == "text":
        return _norm(value)
    if fmt == "table":
        if not isinstance(value, str):
            raise Unresolved("format table needs text")
        return value
    if fmt == "int":
        return str(_int(value))
    if fmt == "grouped":
        return f"{_int(value):,}"
    if fmt == "num":
        if isinstance(value, bool):
            raise Unresolved(f"not a number: {value!r}")
        if isinstance(value, float):
            return str(int(value)) if value.is_integer() else f"{value:g}"
        return str(_int(value))
    if fmt == "usdc":
        d = Decimal(_int(value)) / Decimal(10**6)
        q = d.quantize(Decimal("0.01"))
        return format(q if q == d else d.normalize(), "f")
    if fmt == "eth":
        return _sig(Decimal(_int(value)) / Decimal(10**18))
    if fmt == "gwei":
        return _sig(Decimal(str(value)) / Decimal(10**9))
    if fmt == "bps_pct":
        return f"{Decimal(_int(value)) / Decimal(100):.2f}%"
    if fmt == "bps_dec":
        return f"{Decimal(_int(value)) / Decimal(10000):.4f}"
    if fmt == "rate_pct":
        try:
            d = Decimal(str(value))
        except ArithmeticError:
            raise Unresolved(f"not a rate: {value!r}") from None
        return f"{d * 100:.1f}%"
    if fmt == "duration":
        s = _int(value)
        if s < 60:
            return f"{s} s"
        if s < 3600:
            return f"{s // 60} min {s % 60:02d} s"
        return f"{s // 3600} h {(s % 3600) // 60:02d} min"
    if fmt == "hash":
        text = str(value)
        if not re.fullmatch(r"0x[0-9a-f]{64}", text):
            raise Unresolved(f"not a lowercase 32-byte hash: {text!r}")
        return text
    if fmt == "address":
        text = str(value)
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", text):
            raise Unresolved(f"not an address: {text!r}")
        return text
    if fmt == "tier":
        return _tier_label(value)
    if fmt == "yesno":
        if not isinstance(value, bool):
            raise Unresolved(f"format yesno needs a boolean, got {value!r}")
        return "yes" if value else "no"
    raise Unresolved(f"unknown format {fmt}")


def apply_filter(ctx: Context, text: str, filt: str | None, entry: Entry) -> str:
    if filt is None:
        return text
    if filt not in FILTERS:
        raise Unresolved(f"unknown filter |{filt} (known: {', '.join(sorted(FILTERS))})")
    if entry.fmt not in ("hash", "address"):
        raise Unresolved(f"filter |{filt} needs a hash or address placeholder, {entry.name} is {entry.fmt}")
    if filt == "short":
        return f"{text[:8]}...{text[-4:]}"
    if filt in ("basescan", "blockscout"):
        return ctx.explorer_url(filt, text)
    # link: a Markdown link on Basescan (Blockscout if no Basescan); a code span when there is no explorer
    for name in ("basescan", "blockscout"):
        if name in ctx.explorers:
            return f"[`{text[:8]}...{text[-4:]}`]({ctx.explorer_url(name, text)})"
    return f"`{text}`"


# ---------------------------------------------------------------------------------------------------
# resolution and rendering
# ---------------------------------------------------------------------------------------------------


_FN_RE = re.compile(r"^([a-z_][a-z0-9_]*)\s*(?:\((.*)\))?$")


def raw_value(ctx: Context, entry: Entry) -> Any:
    kind, _, expr = entry.extract.partition(":")
    expr = expr.strip()
    if kind == "fn":
        m = _FN_RE.match(expr)
        if not m or m.group(1) not in FUNCTIONS:
            raise Unresolved(f"unknown function in {entry.extract!r}")
        args = [a.strip() for a in (m.group(2) or "").split(",") if a.strip()]
        return FUNCTIONS[m.group(1)](ctx, *args)
    doc = ctx.load(entry.source)
    return AGGREGATES[kind](evaluate(doc, expr))


def resolve(ctx: Context, entry: Entry) -> str:
    key = f"_value_{entry.name}"
    if key not in ctx._cache:
        try:
            ctx._cache[key] = fmt_value(raw_value(ctx, entry), entry.fmt)
        except Unresolved:
            raise
        except (KeyError, TypeError, ValueError, IndexError, ArithmeticError, OSError) as exc:
            raise Unresolved(f"{type(exc).__name__}: {exc}") from None
    return ctx._cache[key]


def render_text(ctx: Context, entries: dict[str, Entry], text: str, label: str, problems: list[str]) -> str:
    def sub(m: re.Match[str]) -> str:
        name, filt = m.group(1), m.group(2)
        line = text.count("\n", 0, m.start()) + 1
        entry = entries.get(name)
        if entry is None:
            problems.append(f"{label}:{line}: {m.group(0)} is not in placeholders.md")
            return m.group(0)
        try:
            return apply_filter(ctx, resolve(ctx, entry), filt, entry)
        except Unresolved as exc:
            problems.append(f"{label}:{line}: {m.group(0)} unresolved: {exc}")
            return m.group(0)

    out = USE_RE.sub(sub, text)
    for m in LEFTOVER_RE.finditer(out):
        if USE_RE.fullmatch(m.group(0)):
            continue  # already reported above
        line = out.count("\n", 0, m.start()) + 1
        problems.append(f"{label}:{line}: {m.group(0)} is not a valid placeholder (lowercase name, optional |filter)")
    return out


def sources(folder: Path = DELIVERABLES) -> list[Path]:
    return sorted(p for p in folder.glob("*.md") if p.resolve() != REGISTRY.resolve())


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = text.replace("\r\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def consistency(ctx: Context) -> list[str]:
    """Cross-file checks: the CSV matches the JSON, and the deployment file belongs to this run."""
    problems: list[str] = []
    try:
        ev = ctx.evidence()
    except Unresolved:
        return problems  # reported by the placeholders that need it
    try:
        rows = fn_csv_rows(ctx)
        if rows != len(ev.get("transactions") or []):
            problems.append(
                f"evidence/{ctx.network}/evidence.csv has {rows} rows, evidence.json has "
                f"{len(ev.get('transactions') or [])} transactions"
            )
    except Unresolved:
        pass
    try:
        dep = ctx.load("deployments/<network>.json")
        dep_acb = str(dep["contracts"]["AgentCreditBureau"]["address"]).lower()
        ev_acb = str((ev.get("contracts") or {}).get("AgentCreditBureau", "")).lower()
        dep_tx = str(dep["contracts"]["AgentCreditBureau"].get("txHash") or "").lower()
        ev_tx = next(
            (
                str(t["txHash"]).lower()
                for t in ev.get("transactions") or []
                if t.get("function") == "deploy" and t.get("contract") == "AgentCreditBureau"
            ),
            dep_tx,
        )
        if (dep_acb != ev_acb or dep_tx != ev_tx) and ctx.network == "anvil":
            # deployments/anvil.json is git-ignored and rewritten by every local deploy: a dry run only warns
            print(
                "warning: deployments/anvil.json is not the deployment of evidence/anvil/; its rows are stale",
                file=sys.stderr,
            )
        elif dep_acb != ev_acb or dep_tx != ev_tx:
            problems.append(
                f"deployments/{ctx.network}.json is another deployment (AgentCreditBureau {dep_acb}, "
                f"tx {dep_tx}) than evidence/{ctx.network}/evidence.json ({ev_acb}, tx {ev_tx})"
            )
    except (Unresolved, KeyError, TypeError):
        pass
    if ev.get("network") != ctx.network:
        problems.append(f"evidence/{ctx.network}/evidence.json says network {ev.get('network')!r}")
    return problems


def cmd_check(ctx: Context, entries: dict[str, Entry]) -> int:
    bad = 0
    for problem in consistency(ctx):
        bad += 1
        print(f"FAIL  consistency: {problem}")
    for entry in entries.values():
        try:
            value = resolve(ctx, entry)
            shown = value if "\n" not in value else f"<table, {value.count(chr(10)) + 1} lines>"
            print(f"ok    {entry.name} = {shown}")
        except Unresolved as exc:
            bad += 1
            print(f"FAIL  {entry.name}: {exc}")
    failed = sum(1 for e in entries.values() if f"_value_{e.name}" not in ctx._cache)
    print(f"{len(entries) - failed} / {len(entries)} placeholders resolve on {ctx.network}")
    return 1 if bad else 0


def cmd_render(
    ctx: Context, entries: dict[str, Entry], keep_going: bool, src_dir: Path = DELIVERABLES, out_dir: Path = OUT_DIR
) -> int:
    files = sources(src_dir)
    if not files:
        print(f"no deliverables found in {src_dir}", file=sys.stderr)
        return 1
    problems: list[str] = list(consistency(ctx))
    rendered: dict[Path, str] = {}
    run_id = None
    try:
        run_id = ctx.evidence().get("runId")
    except Unresolved:
        pass
    for src in files:
        label = _rel(src)
        text = src.read_text(encoding="utf-8-sig")
        body = render_text(ctx, entries, text, label, problems)
        header = (
            f"<!-- generated by scripts/render_deliverables.py from {label}; network {ctx.network}; "
            f"run {run_id or 'unknown'}. Edit the source, not this file. -->\n"
        )
        rendered[out_dir / src.name] = header + body
    if problems:
        print(f"{len(problems)} problem(s):", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        if not keep_going:
            print("nothing written (use --keep-going to write drafts with the placeholders left in)", file=sys.stderr)
            return 1
    for path, text in rendered.items():
        write_lf(path, text)
        print(f"wrote {_rel(path)}")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replace {{placeholders}} in docs/deliverables/*.md with measured values."
    )
    parser.add_argument("--network", default="base_sepolia", help="evidence/<network>/ to read (default base_sepolia)")
    parser.add_argument("--check", action="store_true", help="resolve every registry entry and print it; write nothing")
    parser.add_argument(
        "--keep-going", action="store_true", help="write drafts even with unresolved placeholders (exit 1)"
    )
    parser.add_argument("--src", type=Path, default=DELIVERABLES, help="folder of the source deliverables (*.md)")
    parser.add_argument("--out", type=Path, default=OUT_DIR, help="output folder (default docs/deliverables/out)")
    args = parser.parse_args(argv)
    entries, groups = load_registry()
    ctx = Context(args.network, groups)
    if args.check:
        return cmd_check(ctx, entries)
    return cmd_render(ctx, entries, args.keep_going, args.src, args.out)


if __name__ == "__main__":
    sys.exit(main())
