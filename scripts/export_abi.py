"""Export the committed contract artefacts: contracts/out -> abi/*.json (spec 3.7, 15.3).

Each file is ``{"contractName", "abi", "bytecode", "deployedBytecode", "codehash"}`` where ``codehash`` is
the keccak256 of the runtime (deployed) bytecode, i.e. the ``EXTCODEHASH`` every TierVault instance has
(TierVault has no immutables, so the compiler output equals the on-chain code). Output is deterministic:
sorted keys, two-space indent, ASCII only, LF line endings, trailing newline.

Usage (from the repository root)::

    uv run python scripts/export_abi.py              # runs `forge build` first if forge is found
    uv run python scripts/export_abi.py --no-build   # only reads contracts/out
    uv run python scripts/export_abi.py --check      # exit 1 if abi/ differs from a fresh export

Only the standard library and eth-utils' keccak (installed with web3) are used.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from eth_utils import keccak

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "contracts" / "out"
ABI_DIR = ROOT / "abi"

# contract name -> source file name under contracts/src (forge writes out/<File>.sol/<Contract>.json)
CONTRACTS: dict[str, str] = {
    "AgentCreditBureau": "AgentCreditBureau.sol",
    "TierVault": "TierVault.sol",
    "MockUSDC": "MockUSDC.sol",
    "IdentityRegistryLite": "IdentityRegistryLite.sol",
    "ReputationRegistryLite": "ReputationRegistryLite.sol",
}


def find_forge() -> str | None:
    """Return the forge executable: PATH first, then the default Foundry install folder."""
    found = shutil.which("forge")
    if found:
        return found
    home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())
    for name in ("forge.exe", "forge"):
        candidate = home / ".foundry" / "bin" / name
        if candidate.is_file():
            return str(candidate)
    return None


def forge_build(forge: str) -> None:
    """Compile the contracts (sources only; tests are not needed for the artefacts)."""
    cmd = [forge, "build", "--root", str(ROOT), "--skip", "test", "--skip", "script"]
    result = subprocess.run(cmd, check=False, cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout + result.stderr)
        raise SystemExit(f"forge build failed (exit {result.returncode})")


def _hex(value: str) -> str:
    value = value.lower()
    return value if value.startswith("0x") else "0x" + value


def artefact(name: str, source: str) -> dict:
    """Build the exported artefact of one contract from forge's output JSON."""
    path = OUT_DIR / source / f"{name}.json"
    if not path.is_file():
        raise SystemExit(f"missing {path.relative_to(ROOT)}: run `forge build` first")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    bytecode = _hex(data["bytecode"]["object"])
    deployed = _hex(data["deployedBytecode"]["object"])
    if "__$" in bytecode or "__$" in deployed:
        raise SystemExit(f"{name}: unlinked library placeholders in bytecode")
    codehash = "0x" + keccak(hexstr=deployed).hex()
    return {
        "contractName": name,
        "abi": data["abi"],
        "bytecode": bytecode,
        "deployedBytecode": deployed,
        "codehash": codehash,
    }


def render(obj: dict) -> bytes:
    """Deterministic JSON bytes: sorted keys, indent 2, ASCII, LF, trailing newline."""
    text = json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False)
    return (text + "\n").encode("ascii")


def export(check: bool = False) -> int:
    """Write (or with ``check`` compare) abi/<Contract>.json for every contract. Returns an exit code."""
    ABI_DIR.mkdir(exist_ok=True)
    differences = []
    for name, source in CONTRACTS.items():
        data = render(artefact(name, source))
        target = ABI_DIR / f"{name}.json"
        if check:
            if not target.is_file() or target.read_bytes() != data:
                differences.append(target.name)
            continue
        if not target.is_file() or target.read_bytes() != data:
            tmp = target.with_suffix(".json.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
        print(f"abi/{target.name}  codehash {json.loads(data)['codehash']}")
    if differences:
        print("abi/ differs from a fresh export: " + ", ".join(differences), file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-build", action="store_true", help="do not run forge build first")
    parser.add_argument("--check", action="store_true", help="compare with abi/ instead of writing")
    args = parser.parse_args(argv)
    if not args.no_build:
        forge = find_forge()
        if forge is None:
            print("forge not found; exporting the existing contracts/out", file=sys.stderr)
        else:
            forge_build(forge)
    return export(check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
