"""``acb deploy`` and ``acb chain`` (spec 3.7, 2.1 P0, 15.3 CLI mounting).

- ``acb deploy all --network anvil|base_sepolia [--fresh]``: the four deploy phases; writes
  ``deployments/<network>.json``. A second run without ``--fresh`` skips every phase that is still
  valid. On Base Sepolia it shows the plan and needs ``--yes`` (real testnet transactions).
- ``acb deploy show --network <net>``: print the deployments file (addresses only, never keys).
- ``acb chain up [--port N] [--block-time S]`` / ``acb chain down`` / ``acb chain status``: the local
  Anvil node (P0), started detached; its pid and start token are kept in ``data/anvil/chain.json``
  (``ACB_CHAIN_STATE_FILE`` overrides it) so ``down`` never signals a reused pid.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from acb.config import data_dir

NETWORKS = ("anvil", "base_sepolia")


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# acb deploy
# ---------------------------------------------------------------------------


def _keyring(network: str):
    from acb.keys import TESTNET_ROLES, KeyRing

    if network == "anvil":
        return KeyRing.anvil()
    return KeyRing.from_env(TESTNET_ROLES)


def _cmd_deploy_all(args: argparse.Namespace) -> int:
    from acb.chain.deploy import DeploymentError, default_funding_plan, deploy_all
    from acb.chain.deployments import deployments_path
    from acb.chain.explorer import address_url
    from acb.chain.tx import TxError
    from acb.config import load_env_file, load_network

    load_env_file(args.env_file)
    net = load_network(args.network)
    try:
        keys = _keyring(args.network)
    except (KeyError, ValueError) as exc:
        _err(f"acb deploy: missing or invalid role keys ({exc}); run `acb keys init --network {args.network}`")
        return 2
    if args.network != "anvil" and not args.yes:
        plan = default_funding_plan(net, keys)
        _out(f"network {net.name} (chain {net.chain_id}), RPC {urlsplit(net.rpc_url).hostname}")
        _out(f"deployer {keys.checksum_address('deployer')}")
        _out(f"ETH top-ups (wei): {sum(plan.eth.values())} over {len(plan.eth)} keys")
        _out(f"USDC to vaults (base units): {dict(plan.usdc_to_vaults)}")
        _err("acb deploy: this sends real testnet transactions; re-run with --yes to proceed")
        return 2

    phases: list[str] = []

    def log(msg: str) -> None:
        phases.append(msg)
        _out(msg)

    try:
        dep, txs = deploy_all(net, keys, fresh=args.fresh, log=log)
    except (DeploymentError, TxError) as exc:
        _err(f"acb deploy: {exc}")
        return 1
    failed = [t for t in txs if t.status != t.expected_status]
    _out("")
    _out(f"deployments file: {deployments_path(net.name)}")
    for name, entry in dep.contracts.items():
        url = address_url(net, entry.address)
        _out(f"  {name:<20} {entry.address}" + (f"  {url}" if url else ""))
    for name, vault in dep.vaults.items():
        _out(f"  vault {name:<14} {vault.address}  agentId {vault.agent_id}")
    _out(f"transactions sent: {len(txs)}" + (" (every phase skipped)" if not txs else ""))
    return 1 if failed else 0


def _cmd_deploy_show(args: argparse.Namespace) -> int:
    from acb.chain.deployments import deployments_path, load_deployments

    try:
        dep = load_deployments(args.network)
    except FileNotFoundError:
        _err(f"no deployments file at {deployments_path(args.network)}: run `acb deploy all --network {args.network}`")
        return 2
    _out(json.dumps(dep.to_json_dict(), indent=2))
    return 0


# ---------------------------------------------------------------------------
# acb chain
# ---------------------------------------------------------------------------


CHAIN_STATE_ENV = "ACB_CHAIN_STATE_FILE"


def _state_path() -> Path:
    """``data/anvil/chain.json``, or ``ACB_CHAIN_STATE_FILE`` (tests)."""
    override = os.environ.get(CHAIN_STATE_ENV)
    return Path(override) if override else data_dir("anvil") / "chain.json"


def _read_state() -> dict | None:
    path = _state_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _running(state: dict | None) -> bool:
    from acb.launcher import process_start_token

    if not state or not state.get("pid") or not state.get("startToken"):
        return False
    return process_start_token(int(state["pid"])) == state["startToken"]


def _default_port() -> int:
    url = os.environ.get("ANVIL_RPC_URL") or "http://127.0.0.1:8545"
    try:
        return int(urlsplit(url).port or 8545)
    except ValueError:
        return 8545


def _cmd_chain_up(args: argparse.Namespace) -> int:
    from acb.canonical import atomic_write_bytes
    from acb.chain.anvil import start_anvil
    from acb.launcher import process_start_token

    state = _read_state()
    if _running(state):
        _out(f"anvil already running at {state['url']} (pid {state['pid']})")
        return 0
    port = args.port or _default_port()
    try:
        node = start_anvil(port=port, block_time=args.block_time, detach=True)
    except (RuntimeError, FileNotFoundError) as exc:
        _err(f"acb chain up: {exc}")
        return 1
    new_state = {
        "pid": node.pid,
        "port": node.port,
        "url": node.url,
        "blockTime": args.block_time,
        "startToken": process_start_token(node.pid),
    }
    atomic_write_bytes(_state_path(), (json.dumps(new_state, indent=2) + "\n").encode("utf-8"))
    _out(f"anvil running at {node.url} (pid {node.pid}, chain id 31337, 30 accounts)")
    if node.port != 8545:
        _out(f"set ANVIL_RPC_URL={node.url} for the other commands")
    return 0


def _cmd_chain_down(_args: argparse.Namespace) -> int:
    from acb.launcher import stop_pid

    state = _read_state()
    path = _state_path()
    if state is None:
        _out("anvil is not running (no state file)")
        return 0
    if not _running(state):
        _out("skipped: the recorded pid is not our anvil any more")
        path.unlink(missing_ok=True)
        return 0
    result = stop_pid(int(state["pid"]))
    path.unlink(missing_ok=True)
    _out(f"anvil {result}")
    return 0


def _cmd_chain_status(_args: argparse.Namespace) -> int:
    state = _read_state()
    if _running(state):
        _out(f"anvil running at {state['url']} (pid {state['pid']})")
        return 0
    _out("anvil is not running")
    return 1


def register(subparsers: argparse._SubParsersAction) -> None:
    """Mount ``acb deploy`` and ``acb chain``."""
    deploy = subparsers.add_parser("deploy", help="deploy contracts, identities, vaults and funding")
    dsub = deploy.add_subparsers(dest="deploy_cmd", metavar="{all,show}", required=True)
    p = dsub.add_parser("all", help="run every deploy phase (idempotent)")
    p.add_argument("--network", required=True, choices=NETWORKS)
    p.add_argument("--fresh", action="store_true", help="ignore deployments/<network>.json and deploy anew")
    p.add_argument("--yes", action="store_true", help="confirm sending transactions on a public network")
    p.add_argument("--env-file", default=None, help="env file to load (default: the repository's .env)")
    p.set_defaults(func=_cmd_deploy_all)
    p = dsub.add_parser("show", help="print deployments/<network>.json")
    p.add_argument("--network", required=True, choices=NETWORKS)
    p.set_defaults(func=_cmd_deploy_show)

    chain = subparsers.add_parser("chain", help="local Anvil node: up, down, status")
    csub = chain.add_subparsers(dest="chain_cmd", metavar="{up,down,status}", required=True)
    p = csub.add_parser("up", help="start Anvil (detached) on 127.0.0.1")
    p.add_argument("--port", type=int, default=None, help="default: the port of ANVIL_RPC_URL, else 8545")
    p.add_argument("--block-time", type=int, default=None, help="seconds per block (default: automine)")
    p.set_defaults(func=_cmd_chain_up)
    p = csub.add_parser("down", help="stop the Anvil started by `acb chain up`")
    p.set_defaults(func=_cmd_chain_down)
    p = csub.add_parser("status", help="is the local Anvil running")
    p.set_defaults(func=_cmd_chain_status)


__all__ = ["register"]
