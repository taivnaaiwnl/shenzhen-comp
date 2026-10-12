"""Keys and roles (spec 2.1, 7.6, 13.1).

- Role index map (BIP-44 ``m/44'/60'/0'/0/<i>``): see :data:`ROLE_INDEX`.
- **Anvil**: :meth:`KeyRing.anvil` derives every role from the public Anvil test mnemonic. Nothing in
  ``.env``.
- **Base Sepolia**: ``acb keys init --network base_sepolia`` reads ``ACB_MNEMONIC`` once, derives the
  hot roles with the same map, generates the root-of-trust roles (owner, ACB admin) independently
  with ``Account.create()``, writes ``<ROLE>_PRIVATE_KEY`` lines and random service tokens into
  ``.env``, removes the ``ACB_MNEMONIC`` line and prints only addresses and the funding plan.
- :func:`load_role_key` is the only function that reads a private key; it refuses inside the wallet
  agent process (``ACB_PROCESS_ROLE=agent``).
- :func:`assert_no_private_keys` makes the wallet agent exit if any ``*_PRIVATE_KEY`` or
  ``*_MNEMONIC`` variable is visible (called at start and before every LLM call).

Nothing in this module prints, logs or returns key material as text.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path

from eth_account import Account
from eth_account.hdaccount import key_from_seed, seed_from_mnemonic
from eth_account.signers.local import LocalAccount

ANVIL_MNEMONIC = "test test test test test test test test test test test junk"
"""Public Foundry/Anvil test mnemonic (not a secret)."""

ROLE_INDEX: dict[str, int] = {
    "deployer": 0,
    "owner": 1,
    "agent_signer": 2,
    "bureau": 3,
    "autonomy": 4,
    "atlas": 5,
    "slowpoke": 6,
    "ghost": 7,
    "blaster": 8,
    "orion": 9,
    "nova": 10,
    "beacon": 11,
    "beacon_owner": 12,
    "echo1": 13,
    "echo2": 14,
    "echo3": 15,
    "attacker": 16,
    "admin": 17,
    "agent_signer_next": 18,
    "echo1_vault_owner": 19,
    "echo2_vault_owner": 20,
    "echo3_vault_owner": 21,
    "sybil1": 22,
    "sybil2": 23,
    "sybil3": 24,
    "sybil4": 25,
    "sybil5": 26,
    "sybil6": 27,
}

SIM_ROLES: tuple[str, ...] = (
    "atlas",
    "slowpoke",
    "ghost",
    "blaster",
    "orion",
    "nova",
    "beacon",
    "beacon_owner",
    "echo1",
    "echo2",
    "echo3",
    "attacker",
)
ROOT_ROLES: tuple[str, ...] = ("owner", "admin")
"""Root-of-trust roles: on Base Sepolia generated independently, never derived from the mnemonic."""
HOT_ROLES: tuple[str, ...] = ("deployer", "agent_signer", "agent_signer_next", "bureau", "autonomy", *SIM_ROLES)
"""Hot roles derived from ``ACB_MNEMONIC`` so the deployer can fund them."""
ANVIL_ONLY_ROLES: tuple[str, ...] = tuple(r for r, i in ROLE_INDEX.items() if i >= 19)
TESTNET_ROLES: tuple[str, ...] = tuple(r for r in ROLE_INDEX if r not in ANVIL_ONLY_ROLES)

SERVICE_TOKENS: tuple[str, ...] = (
    "SIGNER_TOKEN",
    "AGENT_TOKEN",
    "AGENT_ADMIN_TOKEN",
    "BUREAU_TOKEN",
    "AUTONOMY_TOKEN",
    "SIM_TOKEN",
    "EVENTS_TOKEN",
    "OWNER_UI_PASSWORD",
    "SESSION_SECRET",
)

MNEMONIC_VAR = "ACB_MNEMONIC"
_KEY_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")


class KeyAccessDenied(PermissionError):
    """A private key was requested inside a process that must not hold keys."""


class PrivateKeyInEnvironment(RuntimeError):
    """A ``*_PRIVATE_KEY`` or ``*_MNEMONIC`` variable is visible where it must not be."""


def normalize_role(role: str) -> str:
    """``agentSigner``, ``AGENT_SIGNER`` and ``agent-signer`` all become ``agent_signer``."""
    snake = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", role.strip())
    snake = snake.replace("-", "_").lower()
    if snake.startswith("sim_") and snake[4:] in SIM_ROLES:
        snake = snake[4:]
    if snake not in ROLE_INDEX:
        raise KeyError(f"unknown role {role!r}")
    return snake


def env_var(role: str) -> str:
    """Name of the role's key variable, e.g. ``AGENT_SIGNER_PRIVATE_KEY`` or ``SIM_ATLAS_PRIVATE_KEY``."""
    r = normalize_role(role)
    prefix = "SIM_" if r in SIM_ROLES else ""
    return f"{prefix}{r.upper()}_PRIVATE_KEY"


def _derive(seed: bytes, index: int) -> LocalAccount:
    return Account.from_key(key_from_seed(seed, f"m/44'/60'/0'/0/{index}"))


def derive_roles(mnemonic: str, roles: Iterable[str]) -> dict[str, LocalAccount]:
    """Accounts of ``roles`` derived from ``mnemonic`` with the role index map."""
    seed = seed_from_mnemonic(mnemonic, "")
    return {normalize_role(r): _derive(seed, ROLE_INDEX[normalize_role(r)]) for r in roles}


class KeyRing:
    """Role name -> ``LocalAccount``. A plain class: never serialised, ``repr`` shows addresses only."""

    def __init__(self, accounts: Mapping[str, LocalAccount]):
        self._accounts = {normalize_role(r): a for r, a in accounts.items()}

    @classmethod
    def anvil(cls) -> KeyRing:
        """Every role of :data:`ROLE_INDEX` from the Anvil test mnemonic (Anvil runs ``--accounts 30``)."""
        return _anvil_keyring()

    @classmethod
    def from_env(cls, roles: Iterable[str] | None = None, environ: Mapping[str, str] | None = None) -> KeyRing:
        """Keys of ``roles`` from ``<ROLE>_PRIVATE_KEY`` variables (via :func:`load_role_key`).

        Without ``roles``, every role whose variable is set and non-empty is loaded.
        """
        env = os.environ if environ is None else environ
        if roles is None:
            roles = [r for r in ROLE_INDEX if env.get(env_var(r))]
        return cls({r: load_role_key(r, env) for r in roles})

    def roles(self) -> tuple[str, ...]:
        return tuple(self._accounts)

    def __contains__(self, role: object) -> bool:
        try:
            return isinstance(role, str) and normalize_role(role) in self._accounts
        except KeyError:
            return False

    def account(self, role: str) -> LocalAccount:
        """The role's ``LocalAccount`` (for signing)."""
        r = normalize_role(role)
        if r not in self._accounts:
            raise KeyError(f"role {r!r} is not in this key ring")
        return self._accounts[r]

    def address(self, role: str) -> str:
        """The role's address, lowercase (the JSON convention of spec 0.1)."""
        return self.account(role).address.lower()

    def checksum_address(self, role: str) -> str:
        """The role's address, checksummed (for UI, logs and web3 calls)."""
        return self.account(role).address

    def addresses(self) -> dict[str, str]:
        return {r: a.address.lower() for r, a in self._accounts.items()}

    def __repr__(self) -> str:
        return f"KeyRing({', '.join(f'{r}={a.address}' for r, a in self._accounts.items())})"


@lru_cache(maxsize=1)
def _anvil_keyring() -> KeyRing:
    return KeyRing(derive_roles(ANVIL_MNEMONIC, ROLE_INDEX))


def _process_role(env: Mapping[str, str]) -> str:
    return (env.get("ACB_PROCESS_ROLE") or "").strip().lower()


def load_role_key(role: str, environ: Mapping[str, str] | None = None) -> LocalAccount:
    """Read ``<ROLE>_PRIVATE_KEY`` and return the account. The only reader of private keys.

    Raises :class:`KeyAccessDenied` in the wallet agent process (``ACB_PROCESS_ROLE=agent``) and
    ``KeyError`` / ``ValueError`` (without the value) when the variable is missing or malformed.
    """
    env = os.environ if environ is None else environ
    if _process_role(env) == "agent":
        raise KeyAccessDenied("the wallet agent process must not load private keys")
    name = env_var(role)
    value = (env.get(name) or "").strip().strip('"').strip("'")
    if not value:
        raise KeyError(f"{name} is not set")
    if not _KEY_RE.fullmatch(value):
        raise ValueError(f"{name} is not a 32-byte hex private key")
    return Account.from_key(value if value.startswith("0x") else "0x" + value)


def forbidden_variables(environ: Mapping[str, str] | None = None) -> list[str]:
    """Names (never values) of visible ``*_PRIVATE_KEY`` / ``*_MNEMONIC`` variables, case-insensitive."""
    env = os.environ if environ is None else environ
    return sorted(k for k in env if k.upper().endswith(("_PRIVATE_KEY", "_MNEMONIC")))


def assert_no_private_keys(environ: Mapping[str, str] | None = None, *, exit_process: bool = True) -> None:
    """Exit (or raise :class:`PrivateKeyInEnvironment`) if any key or mnemonic variable is visible.

    The wallet agent calls this at start and before every LLM call (spec 2.1, 7.6).
    """
    names = forbidden_variables(environ)
    if not names:
        return
    message = "refusing to run: private key or mnemonic variables are visible: " + ", ".join(names)
    if exit_process:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
        raise SystemExit(78)
    raise PrivateKeyInEnvironment(message)


# ---------------------------------------------------------------------------
# .env editing for `acb keys init`
# ---------------------------------------------------------------------------

_LINE_KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _line_key(line: str) -> str | None:
    if line.lstrip().startswith("#"):
        return None
    m = _LINE_KEY.match(line)
    return m.group(1) if m else None


def _quote(value: str) -> str:
    """A value as written to ``.env``: plain if it is a simple token, else double-quoted."""
    if re.fullmatch(r"[A-Za-z0-9_./:+-]*", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def rewrite_env_text(text: str, updates: Mapping[str, str], *, remove: Iterable[str] = ()) -> str:
    """Set ``updates`` (replacing existing lines in place, appending the rest) and drop ``remove``.

    Other lines (comments, ``OPENAI_API_KEY`` ...) are kept as they are. The result uses LF.
    """
    remove_set = {r.upper() for r in remove}
    pending = dict(updates)
    out: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        key = _line_key(line)
        if key is not None and key.upper() in remove_set:
            continue
        if key is not None and key in pending:
            out.append(f"{key}={_quote(pending.pop(key))}")
            continue
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    if pending:
        out.append("")
        out.append("# ---- written by `acb keys init` ----")
        out.extend(f"{k}={_quote(v)}" for k, v in pending.items())
    return "\n".join(out) + "\n"


def keys_init(
    env_file: str | os.PathLike[str],
    *,
    network: str = "base_sepolia",
    force: bool = False,
    out=None,
) -> dict[str, str]:
    """Implementation of ``acb keys init``. Returns role -> address (lowercase); prints no secrets.

    Refuses when ``network`` is not ``base_sepolia``, when ``ACB_MNEMONIC`` is missing, or when a role
    key is already set (unless ``force``).
    """
    from acb.canonical import atomic_write_bytes
    from acb.config import parse_env_file, parse_env_text

    out = out or sys.stdout
    if network != "base_sepolia":
        raise ValueError("keys init is only for base_sepolia; Anvil keys come from KeyRing.anvil()")
    path = Path(env_file)
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found")
    current = parse_env_file(path, strict=False)
    mnemonic = (current.get(MNEMONIC_VAR) or "").strip()
    if not mnemonic:
        raise KeyError(f"{MNEMONIC_VAR} is not set in {path}")
    existing = [env_var(r) for r in TESTNET_ROLES if current.get(env_var(r))]
    if existing and not force:
        raise FileExistsError(f"role keys already present in {path} ({len(existing)} set); use --force to replace them")

    accounts = derive_roles(mnemonic, HOT_ROLES)
    del mnemonic
    for role in ROOT_ROLES:
        accounts[role] = Account.create()
    updates: dict[str, str] = {env_var(r): _key_hex(a) for r, a in accounts.items()}
    for token in SERVICE_TOKENS:
        if not current.get(token):
            updates[token] = secrets.token_hex(32)

    text = path.read_text(encoding="utf-8-sig")
    new_text = rewrite_env_text(text, updates, remove=[MNEMONIC_VAR])
    parse_env_text(new_text, strict=True)  # the file must stay loadable by python-dotenv and Foundry
    atomic_write_bytes(path, new_text.encode("utf-8"))

    addresses = {r: accounts[r].address.lower() for r in TESTNET_ROLES}
    _print_addresses({r: accounts[r].address for r in TESTNET_ROLES}, out)
    deployer = accounts["deployer"].address
    out.write(
        "\nFunding plan (Base Sepolia, from faucets into the deployer only):\n"
        f"  deployer {deployer}: about 0.005 ETH and about 25 USDC\n"
        "  `acb deploy` then tops up every other hot key and the owner and admin keys with 0.0005 ETH,\n"
        "  and sends 15.00 USDC to the wallet vault and 6.00 USDC to the Beacon vault.\n"
        f"\n{MNEMONIC_VAR} was removed from {path.name}. Keep the mnemonic offline; no process reads it again.\n"
        "Owner and admin keys were generated independently and exist only in this .env file.\n"
    )
    return addresses


def _key_hex(account: LocalAccount) -> str:
    """0x-prefixed private key hex, only ever written to the git-ignored ``.env``."""
    return "0x" + bytes(account.key).hex()


def _print_addresses(addresses: Mapping[str, str], out) -> None:
    width = max(len(r) for r in addresses) if addresses else 0
    for role, address in addresses.items():
        index = ROLE_INDEX[role]
        out.write(f"  {index:>2}  {role:<{width}}  {address}\n")


# ---------------------------------------------------------------------------
# CLI: acb keys init | addresses | balances
# ---------------------------------------------------------------------------


def _cmd_init(args: argparse.Namespace) -> int:
    from acb.config import DEFAULT_ENV_FILE

    path = Path(args.env_file) if args.env_file else DEFAULT_ENV_FILE
    try:
        keys_init(path, network=args.network, force=args.force)
    except (ValueError, KeyError, FileNotFoundError, FileExistsError) as exc:
        sys.stderr.write(f"acb keys init: {exc}\n")
        return 2
    return 0


def _keyring_for(network: str, env_file: str | None) -> tuple[KeyRing | None, dict[str, str]]:
    """Key ring for display: Anvil, or the ``.env`` keys; with only a mnemonic, derived hot roles."""
    from acb.config import DEFAULT_ENV_FILE, load_env_file, parse_env_file

    if network == "anvil":
        return KeyRing.anvil(), {}
    path = Path(env_file) if env_file else DEFAULT_ENV_FILE
    load_env_file(path)
    ring = KeyRing.from_env()
    if ring.roles():
        return ring, {}
    values = parse_env_file(path, strict=False) if path.is_file() else {}
    mnemonic = (values.get(MNEMONIC_VAR) or os.environ.get(MNEMONIC_VAR) or "").strip()
    if mnemonic:
        derived = derive_roles(mnemonic, HOT_ROLES)
        return KeyRing(derived), {
            r: "not generated yet: run `acb keys init --network base_sepolia`" for r in ROOT_ROLES
        }
    return None, {}


def _cmd_addresses(args: argparse.Namespace) -> int:
    ring, notes = _keyring_for(args.network, args.env_file)
    if ring is None:
        sys.stderr.write("no keys found: run `acb keys init --network base_sepolia` first\n")
        return 2
    roles = [r for r in ROLE_INDEX if r in ring.roles()]
    _print_addresses({r: ring.checksum_address(r) for r in roles}, sys.stdout)
    for role, note in notes.items():
        sys.stdout.write(f"  {ROLE_INDEX[role]:>2}  {role}  ({note})\n")
    return 0


def _cmd_balances(args: argparse.Namespace) -> int:
    from web3 import Web3

    from acb.config import load_env_file, load_network

    if args.network != "anvil":
        load_env_file(args.env_file)
    net = load_network(args.network)
    w3 = Web3(Web3.HTTPProvider(args.rpc or net.rpc_url, request_kwargs={"timeout": 20}))
    usdc_address = args.usdc or net.usdc.get("address")
    if args.address:
        targets = {"address": Web3.to_checksum_address(args.address)}
    else:
        ring, _ = _keyring_for(args.network, args.env_file)
        if ring is None:
            sys.stderr.write("no keys found: run `acb keys init --network base_sepolia` first\n")
            return 2
        roles = [normalize_role(r) for r in args.role] if args.role else [r for r in ROLE_INDEX if r in ring.roles()]
        targets = {r: ring.checksum_address(r) for r in roles}
    erc20 = None
    if usdc_address:
        abi = [
            {
                "name": "balanceOf",
                "type": "function",
                "stateMutability": "view",
                "inputs": [{"name": "a", "type": "address"}],
                "outputs": [{"name": "", "type": "uint256"}],
            }
        ]
        erc20 = w3.eth.contract(address=Web3.to_checksum_address(usdc_address), abi=abi)
    sys.stdout.write(f"chain {w3.eth.chain_id}  block {w3.eth.block_number}\n")
    for label, address in targets.items():
        wei = w3.eth.get_balance(address)
        line = f"  {label:<18} {address}  {Web3.from_wei(wei, 'ether')} ETH"
        if erc20 is not None:
            units = erc20.functions.balanceOf(address).call()
            line += f"  {units // 10**6}.{units % 10**6:06d} USDC"
        sys.stdout.write(line + "\n")
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    """Mount ``acb keys init|addresses|balances``."""
    keys = subparsers.add_parser("keys", help="role keys: init (Base Sepolia), addresses, balances")
    sub = keys.add_subparsers(dest="keys_cmd", metavar="{init,addresses,balances}", required=True)

    p = sub.add_parser("init", help="derive hot keys from ACB_MNEMONIC, generate owner/admin keys, write .env")
    p.add_argument("--network", required=True, choices=["anvil", "base_sepolia"])
    p.add_argument("--env-file", default=None, help="env file to rewrite (default: the repository's .env)")
    p.add_argument("--force", action="store_true", help="replace role keys that are already set")
    p.set_defaults(func=_cmd_init)

    p = sub.add_parser("addresses", help="print the address of every role (never keys)")
    p.add_argument("--network", default="anvil", choices=["anvil", "base_sepolia"])
    p.add_argument("--env-file", default=None)
    p.set_defaults(func=_cmd_addresses)

    p = sub.add_parser("balances", help="ETH and USDC balances of the roles (read-only RPC calls)")
    p.add_argument("--network", default="base_sepolia", choices=["anvil", "base_sepolia"])
    p.add_argument("--env-file", default=None)
    p.add_argument("--role", action="append", help="only this role (repeatable)")
    p.add_argument("--address", default=None, help="a single address instead of the roles")
    p.add_argument("--rpc", default=None, help="RPC URL (default: from the network profile)")
    p.add_argument("--usdc", default=None, help="USDC token address (default: from the network profile)")
    p.set_defaults(func=_cmd_balances)


__all__ = [
    "ANVIL_MNEMONIC",
    "ANVIL_ONLY_ROLES",
    "HOT_ROLES",
    "ROLE_INDEX",
    "ROOT_ROLES",
    "SERVICE_TOKENS",
    "SIM_ROLES",
    "TESTNET_ROLES",
    "KeyAccessDenied",
    "KeyRing",
    "PrivateKeyInEnvironment",
    "assert_no_private_keys",
    "derive_roles",
    "env_var",
    "forbidden_variables",
    "keys_init",
    "load_role_key",
    "normalize_role",
    "register",
    "rewrite_env_text",
]
