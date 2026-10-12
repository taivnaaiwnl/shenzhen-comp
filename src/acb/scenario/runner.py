"""The recorded run S0-S21 (spec 10.1, 10.2): orchestration, resumable state and the assertions.

``acb scenario run --network anvil|base_sepolia [--llm mock|openai|auto] [--from S6] [--to S12]
[--run-id <id>]`` builds a :class:`Runner` around an :class:`~acb.scenario.env.Env` and runs the steps
of :mod:`acb.scenario.steps` in order. After each step the :class:`~acb.scenario.recorder.Recorder`
scans the new blocks, and the generic expectations of ``config/scenario.yaml`` are checked: transaction
functions and statuses, decisions and reason codes, violations, grade-change sets, effective grades and
the wallet's tier. A step is PASSED only if every expectation holds; the run stops at the first FAILED
step (``--keep-going`` continues).

Progress is persisted in ``runs/<run_id>/state.json`` (no secrets: process ids, ports, block windows,
facts such as the mandate id and order ids). With ``--from`` the runner re-attaches to a kept Anvil node
(``--keep``) or to Base Sepolia, restarts the services with fresh tokens and continues.

Modes: in mock mode decisions and reasons must match exactly. With a real LLM (``--llm openai|auto``)
the signer's outcome must be at least as strict as expected, no violation may occur outside S15/S16 and a
stricter LLM choice is recorded as a note (2302); data that depends on an earlier stricter choice
(transactions, grades, tiers) is then reported as notes instead of failures.
"""

from __future__ import annotations

import json
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from acb import config
from acb.canonical import atomic_write_bytes
from acb.scenario.env import Env, EnvError
from acb.scenario.recorder import Recorder, iso

STEP_IDS: tuple[str, ...] = tuple(f"S{i}" for i in range(22))
SCENARIO_CONFIG = config.REPO_ROOT / "config" / "scenario.yaml"
STRICTNESS = {"PAY": 0, "PAY_CAPPED": 1, "ESCROW": 2, "NEEDS_OWNER_APPROVAL": 3, "REFUSE": 4}
NAIVE_STEPS = frozenset({"S15", "S16"})
LF = chr(10)


def _print_flush(text: str) -> None:
    print(text, flush=True)


class StepFailure(AssertionError):
    """A step expectation did not hold (the message says which)."""


def load_scenario_config(path: str | Path | None = None) -> dict[str, Any]:
    data = config.load_yaml(Path(path) if path else SCENARIO_CONFIG) or {}
    if data.get("schema") != "acb-scenario/1":
        raise ValueError("config/scenario.yaml: schema must be acb-scenario/1")
    data["steps_by_id"] = {s["id"]: s for s in data.get("steps") or []}
    return data


def step_index(step: str) -> int:
    if step not in STEP_IDS:
        raise ValueError(f"unknown step {step!r}: S0..S21")
    return STEP_IDS.index(step)


def strictness(outcome: str | None) -> int:
    return STRICTNESS.get(str(outcome or ""), -1)


# ---------------------------------------------------------------------------
# event logs of the services
# ---------------------------------------------------------------------------


class EventLog:
    """Tail of one service's ``$ACB_OUT_DIR/<service>.jsonl`` (all events kept in memory)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.events: list[dict[str, Any]] = []
        self._offset = 0

    def poll(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        with open(self.path, "rb") as fh:
            fh.seek(self._offset)
            chunk = fh.read()
        complete = chunk.rfind(b"\n") + 1
        if complete <= 0:
            return []
        self._offset += complete
        new = []
        for line in chunk[:complete].decode("utf-8", errors="replace").splitlines():
            try:
                new.append(json.loads(line))
            except ValueError:
                continue
        self.events.extend(new)
        return new

    def mark(self) -> int:
        self.poll()
        return len(self.events)

    def since(self, mark: int) -> list[dict[str, Any]]:
        self.poll()
        return self.events[mark:]


# ---------------------------------------------------------------------------
# step context
# ---------------------------------------------------------------------------


@dataclass
class StepCtx:
    """What one step observed; filled by the step function, checked by :meth:`Runner.check`."""

    id: str
    title: str
    expect: dict[str, Any]
    started_at: float = 0.0
    started_utc: str = ""
    start_block: int = 0
    decisions: list[dict[str, Any]] = field(default_factory=list)
    offchain: list[dict[str, Any]] = field(default_factory=list)
    violations: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    marks: dict[str, int] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)

    def fail(self, message: str) -> None:
        self.failures.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)

    def require(self, condition: bool, message: str) -> None:
        if not condition:
            self.fail(message)


# ---------------------------------------------------------------------------
# the runner
# ---------------------------------------------------------------------------


class Runner:
    """See the module docstring."""

    def __init__(
        self,
        env: Env,
        *,
        cfg: Mapping[str, Any] | None = None,
        state_path: Path | None = None,
        echo: Callable[[str], None] | None = None,
        keep_going: bool = False,
        fresh_build: bool = False,
    ):
        self.env = env
        self.cfg = dict(cfg) if cfg is not None else load_scenario_config()
        self.state_path = state_path or (config.REPO_ROOT / "runs" / env.run_id / "state.json")
        self.echo = echo or _print_flush
        self.keep_going = keep_going
        self.fresh_build = fresh_build
        self.recorder: Recorder | None = None
        self.state: dict[str, Any] = {
            "schema": "acb-run-state/1",
            "runId": env.run_id,
            "network": env.network,
            "llm": env.llm_mode,
            "startedAt": iso(int(time.time())),
            "steps": {},
            "facts": {},
        }
        self.facts: dict[str, Any] = self.state["facts"]
        self.logs = {name: EventLog(env.out_dir / f"{name}.jsonl") for name in ("agent", "signer", "sim", "bureau")}
        self.run_lines: list[str] = []
        self.narration: list[dict[str, Any]] = []
        self.diverged = False
        self.t0 = time.time()
        self.bus = None

    # ---- helpers ------------------------------------------------------------------------------

    @property
    def real_llm(self) -> bool:
        return self.env.llm_mode != "mock"

    def say(self, line: str) -> None:
        self.run_lines.append(line)
        self.echo(line)
        try:
            with open(self.env.out_dir / "run.txt", "a", encoding="utf-8", newline=LF) as fh:
                fh.write(line + LF)
        except OSError:
            pass

    def agent_id(self, key: str) -> int:
        return int(self.env.dep.agents[key].agent_id)

    def persona_of(self, agent_id: Any) -> str:
        for key, entry in self.env.dep.agents.items():
            if str(entry.agent_id) == str(agent_id):
                return key
        return f"#{agent_id}"

    def save_state(self) -> None:
        launcher = self.env.launcher
        self.state["chain"] = {
            "rpcUrl": self.env.rpc_url,
            "anvilPid": self.env.anvil.pid if self.env.anvil is not None else None,
        }
        if launcher is not None:
            from acb.launcher import process_start_token

            self.state["services"] = {
                role: {"pid": c.pid, "port": c.port, "startToken": c.start_token or process_start_token(c.pid)}
                for role, c in launcher.children.items()
            }
            if self.env.anvil is not None:
                self.state["chain"]["anvilStartToken"] = process_start_token(self.env.anvil.pid)
        if self.recorder is not None:
            self.state["scannedTo"] = self.recorder.scanned_to
            self.state["recorder"] = {
                "expectRevert": {h: self.recorder.revert_errors.get(h) for h in sorted(self.recorder.expect_revert)},
                "notes": {h: list(n) for h, n in self.recorder.notes.items()},
            }
        self.state["narration"] = self.narration
        self.state["ports"] = dict(self.env.ports)
        self.state["outDir"] = str(self.env.out_dir)
        text = json.dumps(self.state, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(self.state_path, text.encode("utf-8"))

    def ensure_recorder(self) -> Recorder:
        if self.recorder is None:
            env = self.env
            self.recorder = Recorder(
                w3=env.w3,
                dep=env.dep,
                out_dir=env.out_dir,
                chain_id=int(env.net.chain_id),
                explorers=tuple(env.net.explorers or ()),
                network=env.network,
                min_interval=0.0 if env.network == "anvil" else 0.125,
            )
            self.recorder.scanned_to = int(self.state.get("scannedTo") or self.state.get("scanStart") or 0)
        return self.recorder

    # ---- running ------------------------------------------------------------------------------

    def run(self, from_step: str = "S0", to_step: str = "S21") -> bool:
        from acb.scenario import steps as step_mod

        first, last = step_index(from_step), step_index(to_step)
        ok = True
        for sid in STEP_IDS[first : last + 1]:
            fn = step_mod.STEPS[sid]
            ctx = self.begin(sid)
            try:
                fn(self, ctx)
            except (StepFailure, EnvError, AssertionError) as exc:
                ctx.fail(str(exc))
            except Exception as exc:  # noqa: BLE001 - a crash is a failed step with its traceback
                ctx.fail(f"{type(exc).__name__}: {exc}")
                ctx.data["traceback"] = traceback.format_exc()[-4000:]
            passed = self.finish(ctx)
            ok = ok and passed
            if not passed and not self.keep_going:
                break
        if "S21" in self.state["steps"] and self.recorder is not None:
            # refresh the staging evidence with the final step statuses (S21 built it while running)
            from acb.scenario.evidence import build_run_evidence

            try:
                build_run_evidence(self, with_backtest=True)
            except Exception as exc:  # noqa: BLE001 - reported, the run result stands
                self.say(f"evidence refresh failed: {type(exc).__name__}: {exc}")
                ok = False
        self.state["finishedAt"] = iso(int(time.time()))
        self.state["result"] = "PASSED" if ok else "FAILED"
        self.state["fingerprint"] = self.fingerprint()
        self.save_state()
        return ok

    def begin(self, sid: str) -> StepCtx:
        spec = self.cfg["steps_by_id"].get(sid, {"id": sid, "title": sid})
        ctx = StepCtx(id=sid, title=spec.get("title", sid), expect=dict(spec.get("expect") or {}))
        ctx.started_at = time.time()
        ctx.started_utc = iso(int(ctx.started_at))
        for name, log in self.logs.items():
            ctx.marks[name] = log.mark()
        if self.env.w3 is not None:
            ctx.start_block = int(self.env.w3.eth.block_number)
        ctx.data["scanFrom"] = self.recorder.scanned_to if self.recorder is not None else None
        self.say(f"{ctx.started_utc} {sid} START {ctx.title}")
        self.state["steps"][sid] = {"status": "RUNNING", "startedAt": ctx.started_utc}
        self.save_state()
        return ctx

    def finish(self, ctx: StepCtx) -> bool:
        if self.env.w3 is not None and self.env.dep is not None:
            rec = self.ensure_recorder()
            try:
                rec.scan(ctx.id)
                self.apply_claims(ctx)
            except Exception as exc:  # noqa: BLE001 - recording problems fail the step, never crash the run
                ctx.fail(f"recording failed: {type(exc).__name__}: {exc}")
        if not ctx.failures or self.keep_going:
            try:
                self.check(ctx)
            except Exception as exc:  # noqa: BLE001
                ctx.fail(f"check crashed: {type(exc).__name__}: {exc}")
        status = "PASSED" if not ctx.failures else "FAILED"
        finished = time.time()
        records = self.recorder.of_step(ctx.id) if self.recorder else []
        for r in records:
            self.say(r["logLine"])
        blocks = [ctx.data.get("scanFrom"), self.recorder.scanned_to] if self.recorder else None
        entry = {
            "status": status,
            "title": ctx.title,
            "startedAt": ctx.started_utc,
            "finishedAt": iso(int(finished)),
            "seconds": round(finished - ctx.started_at, 1),
            "txCount": len(records),
            "decisions": ctx.decisions,
            "offchain": ctx.offchain,
            "violations": ctx.violations,
            "failures": ctx.failures,
            "notes": ctx.notes,
            "data": {k: v for k, v in ctx.data.items() if k != "traceback"},
            "blocks": blocks,
        }
        if "traceback" in ctx.data:
            entry["traceback"] = ctx.data["traceback"]
        self.state["steps"][ctx.id] = entry
        spec = self.cfg["steps_by_id"].get(ctx.id, {})
        self.narration.append(
            {
                "step": ctx.id,
                "start": ctx.started_at - self.t0,
                "end": finished - self.t0,
                "text": spec.get("narration", ctx.title),
            }
        )
        for f in ctx.failures:
            self.say(f"{iso(int(finished))} {ctx.id} FAILED {f}")
        for n in ctx.notes:
            self.say(f"{iso(int(finished))} {ctx.id} NOTE {n}")
        self.say(f"{iso(int(finished))} {ctx.id} {status} ({entry['seconds']} s, {len(records)} tx)")
        self.save_state()
        return status == "PASSED"

    def apply_claims(self, ctx: StepCtx) -> None:
        """Steps whose transactions are mined asynchronously claim them by content (see steps)."""
        from acb.scenario import steps as step_mod

        for claimer in step_mod.CLAIMS:
            claimer(self)

    # ---- generic checks -----------------------------------------------------------------------

    def soft(self, ctx: StepCtx, ok: bool, message: str) -> None:
        """A data expectation: a failure, or a note once a real LLM made an earlier stricter choice."""
        if ok:
            return
        if self.diverged:
            ctx.note("not checked after a stricter LLM choice: " + message)
        else:
            ctx.fail(message)

    def check(self, ctx: StepCtx) -> None:
        exp = ctx.expect
        records = self.recorder.of_step(ctx.id) if self.recorder else []
        # transactions
        allowed_reverts = {(t["function"]) for t in exp.get("txs") or [] if int(t.get("status", 1)) == 0}
        for t in exp.get("txs") or []:
            want = int(t.get("count", 1))
            got = [r for r in records if r["function"] == t["function"] and r["status"] == int(t.get("status", 1))]
            self.soft(
                ctx,
                len(got) >= want,
                f"expected at least {want} x {t['function']} -> {t.get('status', 1)}, found {len(got)}",
            )
        for r in records:
            if r["status"] != r["expectedStatus"]:
                ctx.fail(f"{r['function']} {r['txHash']}: status {r['status']}, expected {r['expectedStatus']}")
            elif r["status"] == 0 and r["function"] not in allowed_reverts:
                ctx.fail(f"unexpected revert of {r['function']} {r['txHash']}: {r.get('_revert')}")
            emitted = {lg["event"] for lg in r.get("_events") or []}
            missing = [e for e in r.get("expectedEvents") or [] if e not in emitted]
            if missing:
                ctx.fail(f"{r['function']} {r['txHash']} did not emit {missing}")
        for fn in exp.get("forbid") or []:
            bad = [r for r in records if r["function"] == fn]
            ctx.require(not bad, f"{fn} must not be sent in {ctx.id} (found {len(bad)})")
        for name in exp.get("reverts") or []:
            found = any((r.get("_revert") or "").split("(")[0] == name for r in records if r["status"] == 0)
            ctx.require(found, f"expected a revert {name}")
        # decisions
        want_decisions = exp.get("decisions") or []
        if want_decisions:
            self.check_decisions(ctx, want_decisions)
        # violations
        if ctx.id in NAIVE_STEPS or exp.get("violation"):
            want_v = exp.get("violation")
            if want_v:
                match = [
                    v
                    for v in ctx.violations
                    if v.get("kind") == want_v["kind"] and int(v.get("severity", 0)) == int(want_v["severity"])
                ]
                ctx.require(bool(match), f"expected violation {want_v}, observed {ctx.violations}")
        elif ctx.violations:
            ctx.fail(f"no violation may occur in {ctx.id}, observed {ctx.violations}")
        # off-chain decisions
        for o in exp.get("offchain") or []:
            n = sum(1 for d in ctx.offchain if list(d.get("reasons") or []) == list(o["reasons"]))
            ctx.require(n == int(o.get("count", 1)), f"expected {o.get('count', 1)} off-chain {o['reasons']}, got {n}")
        # grades
        if "grade_changes" in exp:
            got_changes = self.grade_changes(ctx.id)
            want = {(k, v) for k, v in (exp["grade_changes"] or {}).items()}
            self.soft(ctx, got_changes == want, f"grade changes {sorted(got_changes)} != expected {sorted(want)}")
            ctx.data["gradeChanges"] = sorted(f"{k}:{v}" for k, v in got_changes)
        for key, grade in (exp.get("grades_after") or {}).items():
            got = self.effective_grade(key)
            self.soft(ctx, got == grade, f"{key} effective grade {got}, expected {grade}")
        for key, reasons in (exp.get("reasons") or {}).items():
            got = self.posted_reasons(ctx.id, key)
            self.soft(ctx, got == list(reasons), f"{key} posted reasons {got}, expected {reasons}")
        # tier
        if "tier_after" in exp:
            tier = self.wallet_tier()
            self.soft(ctx, tier == int(exp["tier_after"]), f"wallet tier {tier}, expected {exp['tier_after']}")
            if exp.get("tier_reason") is not None:
                reasons = [
                    int(lg["args"]["reasonCode"])
                    for r in records
                    for lg in r["_events"]
                    if lg["event"] == "TierChanged" and lg["address"] == self.env.dep.vaults["wallet"].address
                ]
                self.soft(
                    ctx,
                    int(exp["tier_reason"]) in reasons,
                    f"expected TierChanged reason {exp['tier_reason']}, saw {reasons}",
                )

    def check_decisions(self, ctx: StepCtx, want: list[Mapping[str, Any]]) -> None:
        got = ctx.decisions
        if not self.real_llm or ctx.id in NAIVE_STEPS:
            pairs = [(d.get("outcome"), list(d.get("reasons") or [])) for d in got]
            expected = [(w["outcome"], list(w["reasons"])) for w in want]
            ctx.require(pairs == expected, f"decisions {pairs} != expected {expected}")
            return
        for i, w in enumerate(want):
            if i >= len(got):
                self.soft(ctx, False, f"missing decision {w}")
                continue
            d = got[i]
            if strictness(d.get("outcome")) < strictness(w["outcome"]):
                ctx.fail(f"signer outcome {d.get('outcome')} is less strict than expected {w['outcome']}")
            elif strictness(d.get("outcome")) > strictness(w["outcome"]) or list(d.get("reasons") or []) != list(
                w["reasons"]
            ):
                ctx.note(f"stricter or different choice: {d.get('outcome')} {d.get('reasons')} (expected {w})")
                if strictness(d.get("outcome")) > strictness(w["outcome"]):
                    self.diverged = True

    # ---- chain reads ----------------------------------------------------------------------------

    def effective_grade(self, key: str) -> str:
        from acb.chain.contracts import AcbContract

        acb = AcbContract(self.env.w3, self.env.dep.contracts["AgentCreditBureau"].address)
        return acb.effective_grade(self.agent_id(key), 0).name

    def wallet_tier(self) -> int:
        from acb.chain.contracts import VaultContract

        v = VaultContract(self.env.w3, self.env.dep.vaults["wallet"].address)
        return 255 if v.is_frozen() else int(v.tier())

    def grade_changes(self, step: str) -> set[tuple[str, str]]:
        """``{(persona, new grade)}`` of the GradePosted events of a step whose grade differs from the
        grade posted before it (the event's raw previous grade)."""
        from acb.types import Grade

        out = set()
        for _r, lg in self.recorder.iter_events(step, "GradePosted"):
            a = lg["args"]
            if int(a["grade"]) != int(a["previousGrade"]):
                out.add((self.persona_of(a["agentId"]), Grade(int(a["grade"])).name))
        return out

    def posted_reasons(self, step: str, key: str) -> list[int]:
        aid = self.agent_id(key)
        last: list[int] = []
        for _r, lg in self.recorder.iter_events(step, "GradePosted"):
            if int(lg["args"]["agentId"]) == aid:
                last = [int(x) for x in lg["args"]["reasons"] if int(x)]
        return last

    # ---- run fingerprint ----------------------------------------------------------------------

    def fingerprint(self) -> dict[str, Any]:
        """What must be identical between two runs (decisions, reasons, violations, grade changes,
        transaction functions and statuses per step)."""
        out: dict[str, Any] = {}
        for sid in STEP_IDS:
            entry = self.state["steps"].get(sid)
            if not entry:
                continue
            txs = sorted(
                f"{r['function']}:{r['status']}" for r in (self.recorder.of_step(sid) if self.recorder else [])
            )
            out[sid] = {
                "status": entry.get("status"),
                "decisions": [[d.get("outcome"), list(d.get("reasons") or [])] for d in entry.get("decisions") or []],
                "offchain": sorted(json.dumps(list(d.get("reasons") or [])) for d in entry.get("offchain") or []),
                "violations": [[v.get("kind"), v.get("severity")] for v in entry.get("violations") or []],
                "gradeChanges": sorted(self.grade_changes(sid)) if self.recorder else [],
                "txs": txs,
            }
        return out


__all__ = [
    "NAIVE_STEPS",
    "STEP_IDS",
    "EventLog",
    "Runner",
    "StepCtx",
    "StepFailure",
    "load_scenario_config",
    "step_index",
    "strictness",
]
