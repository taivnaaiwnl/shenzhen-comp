"""Return the test USDC left in a previous run's vaults to the deployer (Base Sepolia, owner keys only).

A failed recorded run leaves its funded vaults behind. Each vault owner calls ``withdraw(deployer, balance)``
(owner-only on TierVault), so the next fresh run can be funded again. Testnet only; prints addresses,
amounts and transaction hashes, never keys.

    uv run python scripts/recover_vault_usdc.py [--deployments deployments/base_sepolia.json] [--yes]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from acb import config
from acb.chain.client import make_web3
from acb.chain.contracts import Erc20Contract, VaultContract
from acb.chain.tx import TxSender
from acb.keys import KeyRing
from acb.types import NetworkProfile

ROOT = Path(__file__).resolve().parents[1]
OWNER_ROLE = {"wallet": "owner", "beacon": "beacon_owner"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--deployments", default=str(ROOT / "deployments" / "base_sepolia.json"))
    parser.add_argument("--yes", action="store_true", help="send the withdrawals (default: show only)")
    args = parser.parse_args()

    config.load_env_file()
    net = NetworkProfile.load("base_sepolia")
    w3 = make_web3(net)
    keys = KeyRing.from_env()
    dep = json.loads(Path(args.deployments).read_text(encoding="utf-8"))
    usdc = Erc20Contract(w3, net.usdc["address"])
    deployer = keys.checksum_address("deployer")
    for name, role in OWNER_ROLE.items():
        vault = (dep.get("vaults") or {}).get(name)
        if not vault:
            continue
        address = vault["address"]
        balance = int(usdc.balance_of(address))
        print(f"{name} vault {address}: {balance / 1e6:.2f} USDC (owner {keys.checksum_address(role)})")
        if balance == 0 or not args.yes:
            continue
        result = TxSender(w3, keys.account(role)).send(VaultContract(w3, address).withdraw(deployer, balance))
        print(f"  withdraw -> deployer: status {result.status}, tx {result.hash}")
    print(f"deployer now holds {int(usdc.balance_of(deployer)) / 1e6:.2f} USDC")


if __name__ == "__main__":
    main()
