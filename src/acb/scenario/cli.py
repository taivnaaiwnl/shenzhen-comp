"""``acb scenario run|reset``, ``acb attacks run``, ``acb evidence export|verify`` (spec 10, 12.3, 15.3).

- ``acb scenario run --network anvil|base_sepolia [--llm mock|openai|auto] [--from S6] [--to S12]
  [--run-id <id>] [--keep]``: the recorded run (10.2). On Anvil it starts its own chain and services on
  free ports and stops them at the end (``--keep`` leaves them running for ``--from``). On Base Sepolia it
  sends real testnet transactions, so it also needs ``--yes``; without it only the read-only S0 preflight
  runs.
- ``acb scenario reset [--run-id <id>]``: stop the chain and services a kept run left running.
- ``acb attacks run --network anvil [--only A01,A05] [--export]``: the counted attack suite (12.3).
- ``acb attacks benign --network anvil [--export]``: the benign suite B01-B14 (12.4); ``--export`` writes
  ``evidence/benign_suite.json`` and adds its totals to ``evidence/attack_suite.json``.
- ``acb evidence export --run-id <id> [--network N]``: copy a run's curated evidence to ``evidence/<network>/``.
- ``acb evidence verify [path] [--rpc URL] [--receipts-only]``: run ``scripts/verify_evidence.py``.

As one-shot commands acting as a declared role (2.1), these load ``.env`` when they need it: always on
Base Sepolia, and on Anvil only for a real LLM (``--llm openai|auto``). On Anvil every key comes from the
public test mnemonic and every service token is made fresh for the run; keys in ``.env`` are never handed
to a local service.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

NETWORKS = ("anvil", "base_sepolia")


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def _load_env(args: argparse.Namespace, *, needed: bool) -> None:
    from acb import config

    if needed:
        path = Path(args.env_file) if getattr(args, "env_file", None) else config.REPO_ROOT / ".env"
        if path.is_file():
            config.load_env_file(path)


# ---------------------------------------------------------------------------
# scenario run
# ---------------------------------------------------------------------------


def _cmd_run(args: argparse.Namespace) -> int:
    from acb import config
    from acb.scenario.env import Env, EnvError, new_run_id
    from acb.scenario.runner import Runner, step_index

    network = args.network or config.network_name()
    llm = args.llm or ("mock" if network == "anvil" else "auto")
    _load_env(args, needed=network != "anvil" or llm != "mock")
    to_step = args.to or "S21"
    from_step = args.from_step or "S0"
    try:
        step_index(from_step)
        step_index(to_step)
    except ValueError as exc:
        _err(f"acb scenario run: {exc}")
        return 2
    if network != "anvil" and not args.yes:
        _out(
            "Base Sepolia sends real testnet transactions: running only the read-only S0 preflight (add --yes to run)."
        )
        to_step = "S0"
    run_id = args.run_id or new_run_id()
    state_path = config.REPO_ROOT / "runs" / run_id / "state.json"
    resume = from_step != "S0"
    if resume and not state_path.is_file():
        _err(f"acb scenario run: --from {from_step} needs an existing run (runs/{run_id}/state.json); pass --run-id")
        return 2
    try:
        env = Env.create(
            network,
            run_id=run_id,
            llm_mode=llm,
            fixed_ports=args.fixed_ports,
            with_owner_ui=not args.no_owner_ui,
        )
    except EnvError as exc:
        _err(f"acb scenario run: {exc}")
        return 2
    runner = Runner(env, state_path=state_path, keep_going=args.keep_going, fresh_build=args.fresh_build)
    keep = bool(args.keep)  # keeps the Anvil node only; the services always stop with the runner
    ok = False
    started = False
    try:
        if resume:
            from acb.scenario.resume import resume_run

            resume_run(runner, from_step)
        started = True
        _out(f"run {run_id} on {network} (llm {llm}), output {env.out_dir}")
        ok = runner.run(from_step, to_step)
    except KeyboardInterrupt:
        _err("interrupted")
    except EnvError as exc:
        _err(f"acb scenario run: {exc}")
    finally:
        if started:
            runner.save_state()
        env.stop(keep_chain=keep)
    if not started:
        return 2
    _summary(runner)
    return 0 if ok else 1


def _summary(runner: Any) -> None:
    from acb.canonical import chash

    steps = runner.state.get("steps") or {}
    line = " ".join(f"{sid}:{(e.get('status') or '?')[0]}" for sid, e in steps.items())
    _out(f"steps {line}")
    if runner.state.get("fingerprint"):
        _out(f"fingerprint {chash(json.loads(json.dumps(runner.state['fingerprint'])))}")
    folder = runner.env.out_dir / "evidence"
    if (folder / "evidence.json").is_file():
        _out(f"evidence staged in {folder} (acb evidence export --run-id {runner.env.run_id})")
    _out(f"result {runner.state.get('result', 'INCOMPLETE')}")


def _cmd_reset(args: argparse.Namespace) -> int:
    from acb.scenario.resume import reset_run

    stopped = reset_run(args.run_id, network=args.network)
    for line in stopped:
        _out(line)
    return 0


# ---------------------------------------------------------------------------
# attacks
# ---------------------------------------------------------------------------


def _cmd_attacks(args: argparse.Namespace) -> int:
    from acb.scenario.attacks import run_suite

    if (args.network or "anvil") != "anvil":
        _err("acb attacks run: the counted attack suite runs on Anvil only")
        return 2
    only = [a.strip().upper() for a in args.only.split(",")] if args.only else None
    report = run_suite(only=only, export=args.export, echo=_out, fixed_ports=args.fixed_ports)
    t = report["totals"]
    _out(
        f"attacks: {len(report['results'])} run; blocked {t['blocked']}, contained {t['contained']}, bounded "
        f"{t['bounded']}, escalated {t['escalated']}, failed {t['failed']}; funds lost outside intent {t['fundsLostOutsideIntent']}"
    )
    mismatched = [r["id"] for r in report["results"] if r["actual"] != r["expected"]]
    if mismatched:
        _out(f"category differs from the expected one for: {', '.join(mismatched)}")
    _out(f"written {report.get('path')}")
    return 0 if t["failed"] == 0 and not report.get("errors") else 1


def _cmd_benign(args: argparse.Namespace) -> int:
    from acb.scenario.benign import run_benign

    if (args.network or "anvil") != "anvil":
        _err("acb attacks benign: the benign suite runs on Anvil only")
        return 2
    report = run_benign(export=args.export, echo=_out, fixed_ports=args.fixed_ports)
    t = report["totals"]
    _out(f"benign: {t['tasks']} tasks; false positives {t['falsePositives']} (target {t['target']})")
    _out(f"written {report.get('exported') or report.get('path')}")
    return 0 if t["falsePositives"] <= 1 else 1


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------


def _cmd_export(args: argparse.Namespace) -> int:
    from acb import config
    from acb.scenario.evidence import EvidenceError, export, latest_run_id

    network = args.network or config.network_name()
    run_id = args.run_id or latest_run_id(network)
    if not run_id:
        _err(f"acb evidence export: no staged run for {network}; pass --run-id")
        return 2
    try:
        result = export(run_id, network, dest=Path(args.dest) if args.dest else None)
    except EvidenceError as exc:
        _err(f"acb evidence export: {exc}")
        return 1
    _out(
        f"exported {result['files']} files ({result['transactions']} transactions) from {result['from']} to {result['to']}"
    )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    from acb import config
    from acb.scenario.evidence import load_verifier

    network = args.network or config.network_name()
    path = Path(args.path) if args.path else config.REPO_ROOT / "evidence" / network / "evidence.json"
    if not path.is_file():
        _err(f"acb evidence verify: {path} not found")
        return 2
    verifier = load_verifier()
    rpc = args.rpc
    if rpc is None and not args.receipts_only:
        if network == "anvil":
            _err("acb evidence verify: an Anvil run's chain is gone after the run; use --receipts-only or --rpc")
            return 2
        rpc = verifier.DEFAULT_RPC
    report = verifier.verify(path, rpc_url=rpc, receipts_only=args.receipts_only, rps=args.rps)
    return 0 if report["ok"] else 1


# ---------------------------------------------------------------------------
# mounting
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction) -> None:
    """Mount ``acb scenario``, ``acb attacks`` and ``acb evidence`` (spec 15.3)."""
    sc = subparsers.add_parser("scenario", help="the recorded run S0-S21 (spec 10)")
    sc_sub = sc.add_subparsers(dest="scenario_command", metavar="<scenario command>")
    sc.set_defaults(func=lambda _a: (sc.print_help(), 2)[1])

    run = sc_sub.add_parser("run", help="run the steps S0-S21")
    run.add_argument("--network", choices=NETWORKS, default=None, help="default: NETWORK or anvil")
    run.add_argument(
        "--llm", choices=("mock", "openai", "auto"), default=None, help="default: mock on Anvil, auto on Base Sepolia"
    )
    run.add_argument("--from", dest="from_step", default=None, help="first step (resume), e.g. S6")
    run.add_argument("--to", default=None, help="last step, e.g. S12")
    run.add_argument("--run-id", default=None, help="run id (default r-YYYYMMDD-HHMMSS); required with --from")
    run.add_argument("--keep", action="store_true", help="leave the Anvil node running at the end (for --from)")
    run.add_argument("--keep-going", action="store_true", help="continue after a failed step")
    run.add_argument(
        "--fixed-ports", action="store_true", help="use the ports of .env / 8700-8705 instead of free ports"
    )
    run.add_argument("--no-owner-ui", action="store_true", help="do not start the owner console P1")
    run.add_argument("--fresh-build", action="store_true", help="S0: run forge build and compare the vault code hash")
    run.add_argument("--yes", action="store_true", help="Base Sepolia: really send the run's transactions")
    run.add_argument("--env-file", default=None, help="env file (default .env)")
    run.set_defaults(func=_cmd_run)

    reset = sc_sub.add_parser("reset", help="stop the chain and services a kept run left running")
    reset.add_argument("--run-id", default=None, help="default: every run with a state file")
    reset.add_argument("--network", choices=NETWORKS, default="anvil")
    reset.set_defaults(func=_cmd_reset)

    at = subparsers.add_parser("attacks", help="the counted attack suite A01-A63 (spec 12.3)")
    at_sub = at.add_subparsers(dest="attacks_command", metavar="<attacks command>")
    at.set_defaults(func=lambda _a: (at.print_help(), 2)[1])
    arun = at_sub.add_parser("run", help="run the attack suite on a fresh Anvil chain")
    arun.add_argument("--network", choices=NETWORKS, default="anvil")
    arun.add_argument("--only", default=None, help="comma-separated attack ids, e.g. A01,A05")
    arun.add_argument("--export", action="store_true", help="write evidence/attack_suite.json")
    arun.add_argument("--fixed-ports", action="store_true", help=argparse.SUPPRESS)
    arun.set_defaults(func=_cmd_attacks)
    aben = at_sub.add_parser("benign", help="run the benign suite B01-B14 (false positives, spec 12.4)")
    aben.add_argument("--network", choices=NETWORKS, default="anvil")
    aben.add_argument(
        "--export", action="store_true", help="write evidence/benign_suite.json and its totals into attack_suite.json"
    )
    aben.add_argument("--fixed-ports", action="store_true", help=argparse.SUPPRESS)
    aben.set_defaults(func=_cmd_benign)

    ev = subparsers.add_parser("evidence", help="export and verify run evidence (spec 10.4, 10.5)")
    ev_sub = ev.add_subparsers(dest="evidence_command", metavar="<evidence command>")
    ev.set_defaults(func=lambda _a: (ev.print_help(), 2)[1])
    exp = ev_sub.add_parser("export", help="copy a run's curated evidence into evidence/<network>/")
    exp.add_argument("--run-id", default=None, help="default: the newest staged run")
    exp.add_argument("--network", choices=NETWORKS, default=None)
    exp.add_argument(
        "--dest", default=None, help="export into this folder instead of evidence/<network>/ (local checks)"
    )
    exp.set_defaults(func=_cmd_export)
    ver = ev_sub.add_parser("verify", help="verify evidence/<network>/evidence.json (scripts/verify_evidence.py)")
    ver.add_argument("path", nargs="?", default=None)
    ver.add_argument("--network", choices=NETWORKS, default=None)
    ver.add_argument("--rpc", default=None)
    ver.add_argument("--receipts-only", action="store_true")
    ver.add_argument("--rps", type=float, default=4.0)
    ver.set_defaults(func=_cmd_verify)


__all__ = ["register"]
