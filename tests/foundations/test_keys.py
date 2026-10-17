"""Keys and roles (spec 13.1, 7.6). `acb keys init` is tested only on synthetic .env files in a
temporary folder, with the public Anvil test mnemonic; the repository's .env is never touched."""

from __future__ import annotations

import hashlib
import io
import re
import subprocess
import sys
from pathlib import Path

import pytest
from eth_account import Account

from acb.config import parse_env_file
from acb.keys import (
    ANVIL_MNEMONIC,
    HOT_ROLES,
    ROLE_INDEX,
    ROOT_ROLES,
    SERVICE_TOKENS,
    TESTNET_ROLES,
    KeyAccessDenied,
    KeyRing,
    PrivateKeyInEnvironment,
    assert_no_private_keys,
    derive_roles,
    env_var,
    keys_init,
    load_role_key,
    normalize_role,
    rewrite_env_text,
)

REPO = Path(__file__).resolve().parents[2]
KEY_HEX = re.compile(r"0x[0-9a-f]{64}")


def _env_digest() -> str | None:
    """sha256 of the repository's .env (None if absent): compared, never printed, since it holds keys."""
    env = REPO / ".env"
    return hashlib.sha256(env.read_bytes()).hexdigest() if env.is_file() else None


ENV_AT_COLLECTION = _env_digest()
"""Taken when pytest imports this module, before any test of the session has run."""

# Anvil's well-known default accounts (public test keys)
ANVIL_ADDRESSES = {
    0: "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266",
    1: "0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
    2: "0x3c44cdddb6a900fa2b585dd299e03d12fa4293bc",
    9: "0xa0ee7a142d267c1f36714e4a8f75612f20a79720",
}


def test_role_index_map_matches_spec():
    assert ROLE_INDEX["deployer"] == 0 and ROLE_INDEX["owner"] == 1 and ROLE_INDEX["agent_signer"] == 2
    assert ROLE_INDEX["attacker"] == 16 and ROLE_INDEX["admin"] == 17 and ROLE_INDEX["agent_signer_next"] == 18
    assert ROLE_INDEX["sybil6"] == 27
    assert sorted(ROLE_INDEX.values()) == list(range(28))
    assert set(ROOT_ROLES) == {"owner", "admin"} and not set(ROOT_ROLES) & set(HOT_ROLES)
    assert set(HOT_ROLES) | set(ROOT_ROLES) == set(TESTNET_ROLES)


def test_anvil_keyring_matches_anvil_accounts():
    ring = KeyRing.anvil()
    assert len(ring.roles()) == 28
    for index, address in ANVIL_ADDRESSES.items():
        role = next(r for r, i in ROLE_INDEX.items() if i == index)
        assert ring.address(role) == address
    assert ring.checksum_address("deployer") == "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
    assert "agentSigner" in ring and "SIM_ATLAS" in ring and "nobody" not in ring
    assert not re.search(r"0x[0-9a-fA-F]{64}", repr(ring)), "repr shows addresses, never keys"


def test_role_names_and_env_vars():
    assert normalize_role("agentSignerNext") == "agent_signer_next"
    assert normalize_role("SIM_BEACON_OWNER") == "beacon_owner"
    assert env_var("atlas") == "SIM_ATLAS_PRIVATE_KEY"
    assert env_var("beaconOwner") == "SIM_BEACON_OWNER_PRIVATE_KEY"
    assert env_var("agent_signer") == "AGENT_SIGNER_PRIVATE_KEY"
    assert env_var("admin") == "ADMIN_PRIVATE_KEY"
    with pytest.raises(KeyError):
        normalize_role("root")


def test_load_role_key_and_from_env():
    key = "0x" + bytes(KeyRing.anvil().account("bureau").key).hex()
    env = {"BUREAU_PRIVATE_KEY": key, "SIM_ORION_PRIVATE_KEY": key[2:]}
    assert load_role_key("bureau", env).address == KeyRing.anvil().checksum_address("bureau")
    ring = KeyRing.from_env(environ=env)
    assert set(ring.roles()) == {"bureau", "orion"}
    with pytest.raises(KeyError):
        load_role_key("owner", env)
    with pytest.raises(ValueError) as exc:
        load_role_key("bureau", {"BUREAU_PRIVATE_KEY": "0x1234"})
    assert "0x1234" not in str(exc.value)


def test_load_role_key_refuses_in_the_agent_process():
    key = "0x" + bytes(KeyRing.anvil().account("bureau").key).hex()
    with pytest.raises(KeyAccessDenied):
        load_role_key("bureau", {"BUREAU_PRIVATE_KEY": key, "ACB_PROCESS_ROLE": "Agent"})


def test_assert_no_private_keys():
    assert_no_private_keys({"PATH": "x", "AGENT_TOKEN": "t"})
    for name in ("OWNER_PRIVATE_KEY", "acb_mnemonic", "Foo_Private_Key"):
        with pytest.raises(PrivateKeyInEnvironment) as exc:
            assert_no_private_keys({name: "secret-value"}, exit_process=False)
        assert name in str(exc.value) and "secret-value" not in str(exc.value)
    with pytest.raises(SystemExit):
        assert_no_private_keys({"X_MNEMONIC": ""})


@pytest.mark.parametrize("name", ["SIM_ATLAS_PRIVATE_KEY", "ACB_MNEMONIC", "some_private_key"])
def test_agent_process_with_a_key_variable_exits(name):
    """A process with ACB_PROCESS_ROLE=agent and any *_PRIVATE_KEY or *_MNEMONIC variable exits."""
    import os

    env = {k: v for k, v in os.environ.items() if not k.upper().endswith(("_PRIVATE_KEY", "_MNEMONIC"))}
    env.update({"ACB_PROCESS_ROLE": "agent", name: "0xdeadbeef", "PYTHONUTF8": "1"})
    code = "from acb.keys import assert_no_private_keys; assert_no_private_keys(); print('started')"
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=False, cwd=REPO)
    assert proc.returncode != 0 and "started" not in proc.stdout
    assert name.upper() in proc.stderr.upper() and "0xdeadbeef" not in proc.stderr
    if name != "ACB_MNEMONIC":
        return
    # the CLI entry point applies the same rule before doing anything
    proc = subprocess.run(
        [sys.executable, "-m", "acb", "--help"], env=env, capture_output=True, text=True, check=False, cwd=REPO
    )
    assert proc.returncode != 0 and "usage" not in proc.stdout


def test_agent_process_without_keys_starts():
    import os

    env = {k: v for k, v in os.environ.items() if not k.upper().endswith(("_PRIVATE_KEY", "_MNEMONIC"))}
    env.update({"ACB_PROCESS_ROLE": "agent", "PYTHONUTF8": "1"})
    code = "from acb.keys import assert_no_private_keys; assert_no_private_keys(); print('started')"
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=False, cwd=REPO)
    assert proc.returncode == 0 and "started" in proc.stdout


ENV_TEMPLATE = """# local settings
OPENAI_API_KEY=sk-test-not-a-real-key
# testnet only: role accounts are derived from this mnemonic by index
ACB_MNEMONIC={mnemonic}
OWNER_PRIVATE_KEY=
SIGNER_TOKEN=keep-this-existing-token
"""


def write_env(tmp_path: Path, quoted: bool) -> Path:
    mnemonic = f'"{ANVIL_MNEMONIC}"' if quoted else ANVIL_MNEMONIC
    p = tmp_path / ".env"
    p.write_bytes(ENV_TEMPLATE.format(mnemonic=mnemonic).encode("utf-8"))
    return p


@pytest.mark.parametrize("quoted", [True, False])
def test_keys_init_on_a_temporary_env(tmp_path, quoted):
    env_file = write_env(tmp_path, quoted)
    out = io.StringIO()
    addresses = keys_init(env_file, network="base_sepolia", out=out)
    text = env_file.read_text(encoding="utf-8")
    values = parse_env_file(env_file, strict=True)  # loadable by python-dotenv and Foundry

    assert "ACB_MNEMONIC" not in values and ANVIL_MNEMONIC not in text, "mnemonic line removed"
    assert values["OPENAI_API_KEY"] == "sk-test-not-a-real-key", "other lines are kept"
    assert values["SIGNER_TOKEN"] == "keep-this-existing-token", "existing tokens are kept"
    assert "\r" not in text and not text.startswith("\ufeff")

    derived = derive_roles(ANVIL_MNEMONIC, ROLE_INDEX)
    for role in HOT_ROLES:
        assert (
            Account.from_key(values[env_var(role)]).address.lower() == derived[role].address.lower() == addresses[role]
        )
    for role in ROOT_ROLES:
        acct = Account.from_key(values[env_var(role)])
        assert acct.address.lower() == addresses[role]
        assert acct.address.lower() not in {a.address.lower() for a in derived.values()}, "root keys are independent"
    for token in SERVICE_TOKENS:
        if token != "SIGNER_TOKEN":
            assert len(values[token]) >= 32
    tokens = [values[t] for t in SERVICE_TOKENS if t != "SIGNER_TOKEN"]
    assert len(set(tokens)) == len(tokens)
    for role in ("echo1_vault_owner", "sybil1"):
        assert env_var(role) not in values, "Anvil-only roles are not written"

    printed = out.getvalue()
    assert not KEY_HEX.search(printed), "only addresses are printed, never keys"
    assert "sk-test" not in printed and ANVIL_MNEMONIC not in printed
    assert derived["deployer"].address in printed and "Funding plan" in printed


def test_keys_init_refuses_to_overwrite_and_needs_a_mnemonic(tmp_path):
    env_file = write_env(tmp_path, True)
    keys_init(env_file, network="base_sepolia", out=io.StringIO())
    before = env_file.read_bytes()
    with pytest.raises(KeyError):
        keys_init(env_file, network="base_sepolia", out=io.StringIO())  # mnemonic already removed
    env_file.write_bytes(before + f'ACB_MNEMONIC="{ANVIL_MNEMONIC}"\n'.encode())
    with pytest.raises(FileExistsError):
        keys_init(env_file, network="base_sepolia", out=io.StringIO())
    with pytest.raises(ValueError):
        keys_init(env_file, network="anvil", out=io.StringIO())


def test_keys_cli_on_temporary_env(tmp_path):
    env_file = write_env(tmp_path, True)
    proc = subprocess.run(
        [sys.executable, "-m", "acb", "keys", "init", "--network", "base_sepolia", "--env-file", str(env_file)],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
    )
    assert proc.returncode == 0, proc.stderr
    assert not KEY_HEX.search(proc.stdout + proc.stderr)
    proc = subprocess.run(
        [sys.executable, "-m", "acb", "keys", "addresses", "--network", "anvil"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
    )
    assert proc.returncode == 0 and "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266" in proc.stdout
    assert not KEY_HEX.search(proc.stdout)


def test_rewrite_env_text_quotes_and_keeps_lines():
    text = "A=1\r\n# comment\nB=old\nACB_MNEMONIC=x y z\n"
    new = rewrite_env_text(text, {"B": "new value", "C": "plain"}, remove=["ACB_MNEMONIC"])
    assert new == 'A=1\n# comment\nB="new value"\n\n# ---- written by `acb keys init` ----\nC=plain\n'


def test_repository_env_is_not_modified_by_tests():
    """Guard: no test of the session writes the repository's .env. A developer's own ``acb keys init``
    output in it is legitimate; only a change since collection fails, and the file is never printed."""
    unchanged = _env_digest() == ENV_AT_COLLECTION
    assert unchanged, "the repository's .env changed during the test session"
