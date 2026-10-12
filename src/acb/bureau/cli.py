"""``acb bureau serve|rescore|recompute|backtest|score`` (spec 4.9-4.12, 15.3 CLI mounting).

- ``acb bureau serve [--network N] [--port P]``: service P4 (reads ``os.environ`` only).
- ``acb bureau rescore --network anvil [--rounds 3]``: score the grading universe and post what changed.
- ``acb bureau recompute --network base_sepolia --tx <GradePosted tx>`` (or ``--agent-id N``): prints
  ``MATCH`` / ``MATCH_WITH_OVERRIDE`` / ``MISMATCH`` and a JSON report; exit code 0 / 0 / 1.
- ``acb bureau backtest --agents 300 --rounds 6 --seed 2026 [--out evidence/backtest.json]``.
- ``acb bureau score --network N --agent-id N``: compute a bundle without posting (dry run).

One-shot commands read only the variables the bureau process may hold (``config/processes.yaml``)
from ``--env-file`` (default ``.env``) without overriding the environment; ``recompute`` never reads a
private key.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import time
from pathlib import Path

NETWORKS = ("anvil", "base_sepolia")


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def load_role_env(role: str, env_file: str | None, *, keys: bool = True) -> list[str]:
    """Copy the variables ``role`` may hold (``config/processes.yaml``: base + role) from ``env_file``
    into ``os.environ`` unless already set; ``keys=False`` skips every ``*_PRIVATE_KEY``. Returns names."""
    from acb.config import DEFAULT_ENV_FILE, parse_env_file, processes_config

    path = Path(env_file) if env_file else DEFAULT_ENV_FILE
    if not path.is_file():
        return []
    allowed = [p.upper() for p in (*processes_config().get("base", []), *processes_config().get(role, []))]
    values = parse_env_file(path, strict=False)
    loaded = []
    for name, value in values.items():
        upper = name.upper()
        if value is None or name in os.environ:
            continue
        if not keys and upper.endswith(("_PRIVATE_KEY", "_MNEMONIC")):
            continue
        if any(fnmatch.fnmatchcase(upper, pattern) for pattern in allowed):
            os.environ[name] = value
            loaded.append(name)
    return loaded


def _network(args: argparse.Namespace) -> str:
    from acb.config import network_name

    return args.network or network_name()


def _cmd_serve(args: argparse.Namespace) -> int:
    from acb.bureau.service import serve

    return serve(_network(args), port=args.port)


def _bureau(args: argparse.Namespace, *, with_key: bool):
    from acb.bureau.service import make_bureau
    from acb.logbus import EventBus

    bus = EventBus("bureau", jsonl=True)
    return make_bureau(_network(args), with_key=with_key, bus=bus)


def _cmd_rescore(args: argparse.Namespace) -> int:
    from acb.chain.tx import TxError

    network = _network(args)
    load_role_env("bureau", args.env_file)
    try:
        bureau = _bureau(args, with_key=True)
    except FileNotFoundError as exc:
        _err(f"acb bureau rescore: {exc}; run `acb deploy all --network {network}` first")
        return 2
    except (KeyError, ValueError) as exc:
        _err(f"acb bureau rescore: bureau key not available ({exc})")
        return 2
    try:
        result = bureau.rescore(args.rounds)
    except TxError as exc:
        _err(f"acb bureau rescore: {exc}")
        return 1
    if args.json:
        _out(json.dumps(result.to_wire(), indent=2))
        return 0
    for g in result.postings:
        reasons = ",".join(str(r) for r in g.reasons)
        _out(f"posted agent {g.agent_id:>4}  {g.grade.name:<4} score {g.score}  reasons {reasons}  tx {g.tx_hash}")
    _out(f"postings: {len(result.postings)}; unchanged: {list(result.unchanged)}; transactions: {len(result.tx)}")
    for record in bureau.monitoring:
        for alert in record["alerts"]:
            _out(f"monitoring round {record['round']}: {alert}")
    failed = [t for t in result.tx if t.status != t.expected_status]
    return 1 if failed else 0


def _cmd_recompute(args: argparse.Namespace) -> int:
    from acb.bureau.evidence import EvidenceStore
    from acb.bureau.recompute import recompute
    from acb.chain.client import make_web3
    from acb.chain.deployments import load_deployments
    from acb.config import load_network

    network = _network(args)
    load_role_env("bureau", args.env_file, keys=False)
    if args.evidence_base:
        os.environ["EVIDENCE_BASE_URI"] = args.evidence_base
    net = load_network(network)
    try:
        dep = load_deployments(network)
    except FileNotFoundError as exc:
        _err(f"acb bureau recompute: {exc}")
        return 2
    extra = [args.evidence_base] if args.evidence_base else []
    store = EvidenceStore.for_network(network, extra=extra)
    dirs = tuple(store.sources)
    result = recompute(
        w3=make_web3(net, "rpc"),
        w3_logs=make_web3(net, "logs"),
        dep=dep,
        store=store,
        tx_hash=args.tx,
        agent_id=args.agent_id,
        bundle_dirs=dirs,
    )
    _out(result.result)
    _out(json.dumps(result.to_json(), indent=2))
    return result.exit_code


def _cmd_backtest(args: argparse.Namespace) -> int:
    from acb.bureau.backtest import run_backtest, write_report
    from acb.config import out_dir

    started = time.monotonic()
    report = run_backtest(agents=args.agents, rounds=args.rounds, seed=args.seed)
    target = Path(args.out) if args.out else out_dir() / "backtest.json"
    write_report(report, target)
    elapsed = time.monotonic() - started
    m = report["metrics"]
    _out(f"backtest: {args.agents} agents, {args.rounds} rounds, seed {args.seed} -> {target}")
    _out(
        f"AUC {m['aucBps'] / 100:.2f}%  KS {m['ksBps'] / 100:.2f}%  "
        f"false adverse {m['falseAdverseBps'] / 100:.2f}%  median rounds to D (defaulters) {m['medianRoundsToD']}  "
        f"smear max |delta| {m['smearMaxAbsDelta']}"
    )
    _out(f"runtime {elapsed:.1f} s")
    return 0


def _cmd_score(args: argparse.Namespace) -> int:
    network = _network(args)
    load_role_env("bureau", args.env_file, keys=False)
    bureau = _bureau(args, with_key=False)
    # read-only: never mines a block or waits (as rescore does); nothing is posted at this block
    latest = int(bureau.w3.eth.block_number)
    as_of = max(latest - int(bureau.net.confirmations), int(bureau.index.start_block))
    snap = bureau.snapshot(as_of)
    bundle = bureau.model.score(snap.inputs_for(args.agent_id), bureau.cfg, snap.facts)
    _out(json.dumps(bundle.as_canonical(), indent=2, sort_keys=True))
    _out(f"# network {network}, asOfBlock {as_of}, evidenceHash {bundle.evidence_hash}")
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    """Mount ``acb bureau ...`` (spec 15.3)."""
    p = subparsers.add_parser("bureau", help="credit bureau P4: serve, rescore, recompute, backtest")
    sub = p.add_subparsers(dest="bureau_command", metavar="<bureau command>")
    p.set_defaults(func=lambda _a: (p.print_help(), 2)[1])

    def common(q: argparse.ArgumentParser, *, env: bool = True) -> None:
        q.add_argument("--network", choices=NETWORKS, default=None, help="default: NETWORK or anvil")
        if env:
            q.add_argument(
                "--env-file", default=None, help="env file to read the bureau's variables from (default .env)"
            )

    s = sub.add_parser("serve", help="run the bureau service on 127.0.0.1 (BUREAU_PORT, default 8703)")
    common(s, env=False)
    s.add_argument("--port", type=int, default=None)
    s.set_defaults(func=_cmd_serve)

    r = sub.add_parser("rescore", help="score every agent and post changed grades (+ ERC-8004 mirror)")
    common(r)
    r.add_argument("--rounds", type=int, default=1, choices=range(1, 6), metavar="N")
    r.add_argument("--json", action="store_true", help="print the RescoreResult as JSON")
    r.set_defaults(func=_cmd_rescore)

    c = sub.add_parser("recompute", help="recompute a posted grade from logs and published files")
    common(c)
    group = c.add_mutually_exclusive_group(required=True)
    group.add_argument("--tx", help="transaction hash of a GradePosted event")
    group.add_argument("--agent-id", type=int, help="recompute this agent's latest posting")
    c.add_argument("--evidence-base", default=None, help="EVIDENCE_BASE_URI override (folder or URL)")
    c.set_defaults(func=_cmd_recompute)

    b = sub.add_parser("backtest", help="run the scorecard on a synthetic population (no chain)")
    b.add_argument("--agents", type=int, default=300)
    b.add_argument("--rounds", type=int, default=6)
    b.add_argument("--seed", type=int, default=2026)
    b.add_argument("--out", default=None, help="output file (default $ACB_OUT_DIR/backtest.json)")
    b.set_defaults(func=_cmd_backtest)

    sc = sub.add_parser("score", help="compute one agent's bundle without posting")
    common(sc)
    sc.add_argument("--agent-id", type=int, required=True)
    sc.set_defaults(func=_cmd_score)


__all__ = ["load_role_env", "register"]
