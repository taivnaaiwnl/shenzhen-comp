"""Rough Base Sepolia cost estimate: L2 gas price plus the L1 data fee from the OP-stack GasPriceOracle.

Read-only RPC calls; sends no transaction. Usage: ``uv run python scripts/gas_estimate.py [--rpc URL]``.
"""

import argparse

from web3 import Web3

ORACLE = "0x420000000000000000000000000000000000000F"
ABI = [
    {
        "name": "getL1Fee",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "_data", "type": "bytes"}],
        "outputs": [{"name": "", "type": "uint256"}],
    }
]
CASES = (("simple call (payment, report)", 120_000, 300), ("contract deploy (~8 KB code)", 2_500_000, 8_000))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rpc", default="https://sepolia.base.org")
    args = parser.parse_args()
    w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 20}))
    oracle = w3.eth.contract(address=ORACLE, abi=ABI)
    gas_price = w3.eth.gas_price
    print(f"L2 gas price: {gas_price / 1e9:.6f} gwei")
    for label, gas, size in CASES:
        l1 = oracle.functions.getL1Fee(b"\x01" * size).call()
        total = gas * gas_price + l1
        print(f"{label:32} ~{total / 1e18:.10f} ETH  (L2 {gas * gas_price / 1e18:.10f} + L1 {l1 / 1e18:.10f})")


if __name__ == "__main__":
    main()
