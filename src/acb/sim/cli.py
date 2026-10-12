"""``acb sim`` (spec 9.4, 15.3 CLI mounting).

- ``acb sim serve [--network N] [--port P]``: the simulator service on 127.0.0.1 (``SIM_PORT``, default
  8704). Reads only ``os.environ`` (the launcher passes the ``SIM_*`` keys and tokens); never loads
  ``.env``. On Anvil every key comes from the public test mnemonic.
- ``acb sim run <action> [--args JSON] [--step S4]``: run one scripted action once, without the
  service (same code path as ``POST /admin/run``), and print the JSON result.
- ``acb sim actions``: list the actions.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

LOOPBACK = "127.0.0.1"


def build_simulator(network: str, *, environ: dict[str, str] | None = None) -> Any:
    """A :class:`~acb.sim.core.Simulator` for ``network`` from the environment and the deployments file."""
    from acb.chain.deployments import load_deployments
    from acb.config import load_network
    from acb.keys import SIM_ROLES, KeyRing, env_var
    from acb.sim.core import Simulator

    env = dict(os.environ if environ is None else environ)
    net = load_network(network, env)
    dep = load_deployments(network)
    if net.chain_id == 31337:
        keys = KeyRing.anvil()
    else:
        roles = [r for r in SIM_ROLES if env.get(env_var(r))]
        keys = KeyRing.from_env(roles, env)
    return Simulator(net, dep, keys)


def _network(args: argparse.Namespace) -> str:
    return args.network or (os.environ.get("NETWORK") or "anvil").strip()


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from acb.sim.service import create_app

    if args.host != LOOPBACK:
        sys.stderr.write("acb sim serve: refusing to bind anything but 127.0.0.1\n")
        return 2
    port = args.port or int(os.environ.get("SIM_PORT") or 8704)
    sim = build_simulator(_network(args))
    sys.stdout.write(
        f"sim: {len(sim.parties)} agents on {sim.net.name} (chain {sim.chain_id}), http://{LOOPBACK}:{port}\n"
    )
    sys.stdout.flush()
    uvicorn.run(create_app(sim), host=LOOPBACK, port=port, workers=1, log_level="warning")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from acb.sim.core import SimError

    try:
        payload = json.loads(args.args) if args.args else {}
    except ValueError:
        sys.stderr.write("acb sim run: --args is not JSON\n")
        return 2
    if not isinstance(payload, dict):
        sys.stderr.write("acb sim run: --args must be a JSON object\n")
        return 2
    if args.step:
        payload["step"] = args.step
    sim = build_simulator(_network(args))
    try:
        result = sim.run_action(args.action, payload)
    except SimError as exc:
        sys.stderr.write(json.dumps(exc.body) + "\n")
        return 1
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


def _cmd_actions(_args: argparse.Namespace) -> int:
    from acb.sim.actions import ACTIONS, ANVIL_ONLY

    for name in sorted(ACTIONS):
        sys.stdout.write(f"{name}{'  (anvil only)' if name in ANVIL_ONLY else ''}\n")
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    """Mount ``acb sim`` (15.3)."""
    sim = subparsers.add_parser("sim", help="counterparty simulator (P6)")
    sub = sim.add_subparsers(dest="sim_command", metavar="<sim command>")

    serve = sub.add_parser("serve", help="run the simulator service on 127.0.0.1")
    serve.add_argument("--network", default=None, help="anvil or base_sepolia (default: NETWORK or anvil)")
    serve.add_argument("--host", default=LOOPBACK, help=argparse.SUPPRESS)
    serve.add_argument("--port", type=int, default=None, help="default: SIM_PORT or 8704")
    serve.set_defaults(func=_cmd_serve)

    run = sub.add_parser("run", help="run one scripted action once and print the result")
    run.add_argument("action")
    run.add_argument("--args", default=None, help="JSON object of action arguments")
    run.add_argument("--step", default=None, help="scenario step id for the events, e.g. S4")
    run.add_argument("--network", default=None)
    run.set_defaults(func=_cmd_run)

    actions = sub.add_parser("actions", help="list the scripted actions")
    actions.set_defaults(func=_cmd_actions)

    def _help(_args: argparse.Namespace) -> int:
        sim.print_help()
        return 0

    sim.set_defaults(func=_help)


__all__ = ["build_simulator", "register"]
