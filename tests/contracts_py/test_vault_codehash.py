"""Code hash reproducibility (WP2).

- The committed abi/TierVault.json codehash is the keccak of its runtime bytecode and equals the runtime code
  of a fresh compilation into a temporary folder (no cache, no shared output).
- abi/ equals a fresh export of contracts/out (``scripts/export_abi.py --check``).
- On Anvil, two vaults deployed from the committed bytecode with different constructor arguments have the
  same EXTCODEHASH, equal to the committed codehash (marker ``chain``).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
from eth_utils import keccak

ROOT = Path(__file__).resolve().parents[2]
ABI_DIR = ROOT / "abi"


def _export_module():
    spec = importlib.util.spec_from_file_location("acb_export_abi", ROOT / "scripts" / "export_abi.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _artefact(name: str) -> dict:
    return json.loads((ABI_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _hex(obj: str) -> str:
    obj = obj.lower()
    return obj if obj.startswith("0x") else "0x" + obj


def test_codehash_is_keccak_of_runtime_code() -> None:
    art = _artefact("TierVault")
    assert art["codehash"] == "0x" + keccak(hexstr=art["deployedBytecode"]).hex()


def test_codehash_equals_fresh_build(tmp_path: Path) -> None:
    forge = _export_module().find_forge()
    if forge is None:
        pytest.skip("forge not found")
    out, cache = tmp_path / "out", tmp_path / "cache"
    cmd = [
        forge,
        "build",
        "--root",
        str(ROOT),
        "--out",
        str(out),
        "--cache-path",
        str(cache),
        "--skip",
        "test",
        "--skip",
        "script",
    ]
    result = subprocess.run(cmd, check=False, cwd=ROOT, capture_output=True, text=True, timeout=900)
    assert result.returncode == 0, result.stdout + result.stderr
    fresh = json.loads((out / "TierVault.sol" / "TierVault.json").read_text(encoding="utf-8"))
    assert not fresh["deployedBytecode"].get("immutableReferences"), "TierVault must not use immutables"
    runtime = _hex(fresh["deployedBytecode"]["object"])
    assert "0x" + keccak(hexstr=runtime).hex() == _artefact("TierVault")["codehash"]
    for name in ("AgentCreditBureau", "TierVault", "MockUSDC", "IdentityRegistryLite", "ReputationRegistryLite"):
        built = json.loads((out / f"{name}.sol" / f"{name}.json").read_text(encoding="utf-8"))
        assert _hex(built["bytecode"]["object"]) == _artefact(name)["bytecode"], f"{name} bytecode differs"


def test_abi_dir_equals_fresh_export_of_contracts_out() -> None:
    if not (ROOT / "contracts" / "out" / "TierVault.sol" / "TierVault.json").is_file():
        pytest.skip("contracts/out not built")
    assert _export_module().export(check=True) == 0


@pytest.mark.chain
def test_deployed_vaults_share_the_committed_codehash(anvil) -> None:
    from web3 import Web3

    _, w3 = anvil
    art = _artefact("TierVault")
    deployer = w3.eth.accounts[0]
    a1, a2, a3, a4, a5, a6 = w3.eth.accounts[1:7]
    zero_tier = (0, 0, 0, 0, 0, 0, 3)
    tier1 = (1_000_000, 5_000_000, 500_000, 10, 0x30, 0x3B, 3)
    tier2 = (2_000_000, 10_000_000, 1_000_000, 20, 0x30, 0x3B, 6)
    tier3 = (4_000_000, 20_000_000, 1_000_000, 32, 0x32, 0x3B, 0)
    factory = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"])

    def deploy(owner: str, agent: str, operator: str, agent_id: int, floor: int, initial: int) -> str:
        init = (
            a1,
            a2,
            a3,
            owner,
            agent,
            operator,
            agent_id,
            [zero_tier, tier1, tier2, tier3],
            floor,
            100_000,
            86_400,
            60,
            604_800,
            120,
            2,
            initial,
        )
        tx = factory.constructor(init).transact({"from": deployer})
        receipt = w3.eth.wait_for_transaction_receipt(tx)
        assert receipt.status == 1
        return receipt.contractAddress

    v1 = deploy(a4, a5, a6, 1, 3_000_000, 1)
    v2 = deploy(a6, a4, a5, 77, 0, 0)
    h1 = Web3.to_hex(keccak(w3.eth.get_code(v1)))
    h2 = Web3.to_hex(keccak(w3.eth.get_code(v2)))
    assert h1 == h2 == art["codehash"]
