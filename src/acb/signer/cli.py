"""``acb signer serve`` (spec 7, 15.3 CLI mounting).

The signer process holds ``AGENT_SIGNER_PRIVATE_KEY`` (or, on Anvil without that variable, the Anvil
test key of the agent signer role), ``SIGNER_TOKEN`` and ``EVENTS_TOKEN``. It never calls
``load_env_file()``: the launcher passes its allow-listed environment (spec 2.1, 13.4).
"""

from __future__ import annotations

import argparse
import os
import sys


def _account(network: str):
    from acb.keys import KeyRing, load_role_key

    try:
        return load_role_key("agent_signer")
    except KeyError:
        if network == "anvil":
            return KeyRing.anvil().account("agent_signer")
        raise


def deployment_fingerprint(w3, dep) -> str:
    """Identity of one deployment on one chain instance: chain id, vault, start block and its hash."""
    from web3 import Web3

    from acb.canonical import chash

    block = w3.eth.get_block(int(dep.start_block))
    return chash(
        {
            "chainId": int(dep.chain_id),
            "vault": dep.vaults["wallet"].address,
            "startBlock": int(dep.start_block),
            "startBlockHash": Web3.to_hex(block["hash"]),
        }
    )


def _serve(args: argparse.Namespace) -> int:
    from acb import config
    from acb.chain.client import make_web3
    from acb.chain.deployments import load_deployments
    from acb.logbus import EventBus
    from acb.policy import PolicyConfig
    from acb.signer.executor import Executor
    from acb.signer.service import LOOPBACK, serve
    from acb.signer.state import prepare_state_dir
    from acb.types import NetworkProfile

    if args.host != LOOPBACK:
        sys.stderr.write(f"acb signer: refusing to bind to {args.host!r}; the signer listens on {LOOPBACK} only\n")
        return 2
    missing = [name for name in ("SIGNER_TOKEN", "EVENTS_TOKEN") if not os.environ.get(name)]
    if missing:
        sys.stderr.write(f"acb signer: {', '.join(missing)} not set; refusing to start without authentication\n")
        return 2
    network = args.network or config.network_name()
    net = NetworkProfile.load(network)
    w3 = make_web3(net)
    dep = load_deployments(network)
    account = _account(network)
    port = int(args.port or os.environ.get("SIGNER_PORT") or 8702)
    explorer = net.explorers[0].get("tx") if net.explorers else None
    # ACB_SIGNER_STATE_DIR is a test hook (not in the launcher's allow-list): default data/<network>/signer/
    base = os.environ.get("ACB_SIGNER_STATE_DIR") or config.data_dir(network, "signer")
    state_dir = prepare_state_dir(base, deployment_fingerprint(w3, dep))
    bus = EventBus("signer", chain_id=net.chain_id, explorer_tx=explorer)
    executor = Executor(
        w3=w3,
        dep=dep,
        account=account,
        state_dir=state_dir,
        out_dir=config.out_dir(),
        cfg=PolicyConfig.load(),
        bus=bus,
    )
    sys.stderr.write(f"acb signer: {executor.address} on {network} (chain {net.chain_id}), {LOOPBACK}:{port}\n")
    serve(executor, host=LOOPBACK, port=port)
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    """Mount ``acb signer serve``."""
    parser = subparsers.add_parser("signer", help="signer service P3 (holds the wallet agent's key)")
    sub = parser.add_subparsers(dest="signer_command", metavar="<signer command>")
    serve_p = sub.add_parser("serve", help="run the signer on 127.0.0.1 (default port SIGNER_PORT or 8702)")
    serve_p.add_argument("--network", default=None, help="anvil or base_sepolia (default: NETWORK or anvil)")
    serve_p.add_argument("--port", type=int, default=None)
    serve_p.add_argument("--host", default="127.0.0.1", help="must be 127.0.0.1")
    serve_p.set_defaults(func=_serve)
    parser.set_defaults(func=lambda _args: (parser.print_help(), 2)[1])


__all__ = ["register"]
