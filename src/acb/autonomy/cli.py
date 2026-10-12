"""``acb autonomy serve|evaluate`` (spec 5.4, 15.3 CLI mounting).

- ``acb autonomy serve [--network N] [--port P]``: service P5 (reads ``os.environ`` only).
- ``acb autonomy evaluate [--network N] [--dry-run]``: one evaluation of rules 1-7; prints the
  ``AutonomyResult``. ``--dry-run`` simulates the planned transaction but does not send it.
"""

from __future__ import annotations

import argparse
import json
import sys

NETWORKS = ("anvil", "base_sepolia")


def _network(args: argparse.Namespace) -> str:
    from acb.config import network_name

    return args.network or network_name()


def _cmd_serve(args: argparse.Namespace) -> int:
    from acb.autonomy.service import serve

    return serve(_network(args), port=args.port)


def _cmd_evaluate(args: argparse.Namespace) -> int:
    from acb.autonomy.service import make_evaluator
    from acb.bureau.cli import load_role_env
    from acb.chain.tx import TxError
    from acb.logbus import EventBus

    network = _network(args)
    load_role_env("autonomy", args.env_file)
    try:
        evaluator = make_evaluator(network, bus=EventBus("autonomy"))
    except FileNotFoundError as exc:
        sys.stderr.write(f"acb autonomy evaluate: {exc}\n")
        return 2
    except (KeyError, ValueError) as exc:
        sys.stderr.write(f"acb autonomy evaluate: autonomy key not available ({exc})\n")
        return 2
    try:
        result = evaluator.evaluate(dry_run=args.dry_run)
    except TxError as exc:
        sys.stderr.write(f"acb autonomy evaluate: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(result.to_wire(), indent=2) + "\n")
    if evaluator.last_plan is not None and evaluator.last_plan.detail:
        sys.stdout.write(f"# {evaluator.last_plan.rule}: {evaluator.last_plan.detail}\n")
    return 0 if result.tx is None or result.tx.status == 1 else 1


def register(subparsers: argparse._SubParsersAction) -> None:
    """Mount ``acb autonomy ...`` (spec 15.3)."""
    p = subparsers.add_parser("autonomy", help="autonomy operator P5: serve, evaluate")
    sub = p.add_subparsers(dest="autonomy_command", metavar="<autonomy command>")
    p.set_defaults(func=lambda _a: (p.print_help(), 2)[1])

    s = sub.add_parser("serve", help="run the autonomy operator on 127.0.0.1 (AUTONOMY_PORT, default 8705)")
    s.add_argument("--network", choices=NETWORKS, default=None)
    s.add_argument("--port", type=int, default=None)
    s.set_defaults(func=_cmd_serve)

    e = sub.add_parser("evaluate", help="evaluate the wallet vault once (rules 1-7 of spec 5.4)")
    e.add_argument("--network", choices=NETWORKS, default=None)
    e.add_argument("--env-file", default=None, help="env file to read the operator's variables from (default .env)")
    e.add_argument("--dry-run", action="store_true", help="simulate the planned transaction, do not send it")
    e.set_defaults(func=_cmd_evaluate)


__all__ = ["register"]
