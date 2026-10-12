"""Deployment of the whole system (spec 3.7, 9.1, 13.1, 13.5; steps S1-S3 of 10.2).

Four phases, each idempotent and each saving ``deployments/<network>.json`` atomically:

1. :func:`deploy_contracts` - Anvil: MockUSDC and the ERC-8004 Lite registries; Base Sepolia: checks
   the official registries (``getVersion()`` and the EIP-1967 implementation slots must equal
   ``config/networks.yaml``, else abort) and uses Circle test USDC. Then the deployer deploys the
   ``AgentCreditBureau`` (``admin = ADMIN``, ``token = USDC``, ``vaultCodeHash`` from
   ``abi/TierVault.json``, ``upgradeDelay`` from the network profile) and the admin key calls
   ``setPoster(bureau, true)``.
2. :func:`register_agents` - the wallet's identity is registered by the OWNER key, every simulated
   agent by its own key, with ``agentURI = data:application/json;base64,<registration JSON of 9.1>``
   labelled "ACB demo, simulated agent. Not a real service.".
3. :func:`deploy_vaults` - the wallet vault ("wallet" profile: owner = OWNER, agent = AGENT_SIGNER,
   autonomyOperator = AUTONOMY), Beacon's vault ("market" profile: owner = beacon owner, agent =
   beacon), and on Anvil the Echo vaults; each registered with ``registerVault`` by the identity owner
   of the vault's agent. :func:`deploy_extra_vault` adds more vaults for the attack harness (Anvil).
4. :func:`fund` - ETH top-ups and USDC to the vaults (Anvil: minted; Base Sepolia: sent by the deployer).

A phase is skipped when the file already holds valid addresses with code (and, for agents and vaults,
the on-chain state still matches). :func:`deploy_all` runs the four phases.
"""

from __future__ import annotations

import base64
import os
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from acb.canonical import canonical_bytes
from acb.chain import abi as abi_mod
from acb.chain.client import check_chain_id, make_web3
from acb.chain.contracts import (
    AcbContract,
    Erc20Contract,
    IdentityContract,
    ReputationContract,
    VaultContract,
    VaultInit,
    checksum,
    lower,
)
from acb.chain.deployments import save_deployments, try_load_deployments
from acb.chain.tx import TxSender
from acb.config import CONFIG_DIR, REPO_ROOT, load_yaml
from acb.keys import KeyRing
from acb.types import (
    AgentEntry,
    ContractEntry,
    Deployments,
    FundingPlan,
    Grade,
    NetworkProfile,
    TierConfig,
    TierProfile,
    TiersConfig,
    TxResult,
    VaultEntry,
    grade_mask,
)

Log = Callable[[str], None]

EIP1967_IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
REGISTRATION_TYPE = "https://eips.ethereum.org/EIPS/eip-8004#registration-v1"
SIMULATED_LABEL = "ACB demo, simulated agent. Not a real service."
SIM_ENDPOINT = "http://127.0.0.1:8704/p/{key}"
WALLET_ENDPOINT = "http://127.0.0.1:8701"
USDC_UNIT = 1_000_000

FUNDING_USDC_BPS_ENV = "ACB_FUNDING_USDC_BPS"
"""Scales the USDC seed funding of every vault (10000 = the spec's 15.00 + 6.00 USDC)."""

DEFAULT_USDC_TO_VAULTS: dict[str, int] = {"wallet": 15 * USDC_UNIT, "beacon": 6 * USDC_UNIT}
"""Seed funding of 13.1 / S3: wallet vault 15.00 USDC, Beacon vault 6.00 USDC."""

ROLES_JSON: dict[str, str] = {
    "deployer": "deployer",
    "owner": "owner",
    "admin": "admin",
    "agentSigner": "agent_signer",
    "agentSignerNext": "agent_signer_next",
    "bureau": "bureau",
    "autonomy": "autonomy",
}
"""``roles`` of 13.5 (camelCase key -> key ring role)."""

WALLET_KEY = "wallet"
AGENT_ORDER: tuple[str, ...] = (
    WALLET_KEY,
    "atlas",
    "slowpoke",
    "ghost",
    "blaster",
    "orion",
    "nova",
    "beacon",
    "echo1",
    "echo2",
    "echo3",
    "attacker",
)
"""Identities registered in S2 (12 ``register`` transactions)."""

ETH_TOPUP_ROLES: tuple[str, ...] = (
    "owner",
    "admin",
    "agent_signer",
    "agent_signer_next",
    "bureau",
    "autonomy",
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
"""Keys topped up with ``funding.topup_eth`` on a public network (13.1)."""


class DeploymentError(RuntimeError):
    """A deployment precondition failed (wrong chain, changed official registry, missing funds, ...)."""


# ---------------------------------------------------------------------------
# personas (config/personas.yaml, WP8) and tiers (config/tiers.yaml, WP2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Persona:
    """What deploy needs from a persona: display name, category, catalogue, anchor flag."""

    key: str
    name: str
    category: str
    skus: dict[str, int] = field(default_factory=dict)
    anchor: bool = False
    description: str = ""


DEFAULT_PERSONAS: dict[str, Persona] = {
    p.key: p
    for p in (
        Persona("atlas", "Atlas Data", "fx-data", {"fx-live-15m": 200_000}, False, "FX live data in 15 minute slices."),
        Persona(
            "slowpoke",
            "Slowpoke Analytics",
            "fx-data",
            {"fx-history-day": 200_000, "fx-history-day-promo": 100_000},
            False,
            "Historical FX data by the day.",
        ),
        Persona("ghost", "Ghost Feeds", "fx-data", {"fx-backup-15m": 200_000}, False, "Backup FX data feed."),
        Persona("blaster", "Blaster Ads", "ads", {"ad-slot": 500_000}, False, "Advertising slots."),
        Persona(
            "orion",
            "Orion API",
            "api-calls",
            {"api-bundle-1k": 200_000, "api-priority-1k": 300_000},
            False,
            "API call bundles.",
        ),
        Persona("nova", "Nova Labs", "sample-data", {"fx-sample": 400_000}, False, "Sample FX data."),
        Persona("beacon", "Beacon Research", "research", {}, True, "Research buyer and verified data furnisher."),
        Persona("echo1", "Echo-1", "research", {}, False, "Research agent."),
        Persona("echo2", "Echo-2", "research", {}, False, "Research agent."),
        Persona("echo3", "Echo-3", "research", {}, False, "Research agent."),
        Persona("attacker", "Treasury Helper", "research", {}, False, "Treasury helper agent."),
    )
}
"""The personas of spec 9.1, used until WP8's ``config/personas.yaml`` exists."""

WALLET_PERSONA = Persona(WALLET_KEY, "Research Wallet", "research", {}, False, "")


def _skus_from_yaml(raw: Any) -> dict[str, int]:
    if not raw:
        return {}
    if isinstance(raw, Mapping):
        out: dict[str, int] = {}
        for sku, value in raw.items():
            if isinstance(value, Mapping):
                value = value.get("unit_price", value.get("unitPrice", value.get("price")))
            out[str(sku)] = _amount(value, f"price of sku {sku}")
        return out
    if isinstance(raw, list):
        out = {}
        for item in raw:
            if not isinstance(item, Mapping) or "sku" not in item:
                raise ValueError("each sku entry must be a mapping with 'sku'")
            value = item.get("unit_price", item.get("unitPrice", item.get("price")))
            out[str(item["sku"])] = _amount(value, f"price of sku {item['sku']}")
        return out
    raise ValueError("skus must be a mapping or a list")


def _amount(value: Any, what: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{what} must be an integer amount")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise ValueError(f"{what} must be an integer amount or a decimal string (base units)")


def parse_personas(data: Any) -> dict[str, Persona]:
    """Personas from the parsed ``config/personas.yaml``.

    Accepted layouts: ``{personas: {<key>: {...}}}``, ``{<key>: {...}}`` or ``{personas: [{key: ...}]}``.
    Each entry needs ``name`` (or ``display_name``); ``category``, ``skus`` (``{sku: price}`` or a list
    of ``{sku, unit_price}``), ``anchor`` and ``description`` are optional. Prices are base units.
    """
    if isinstance(data, Mapping) and "personas" in data:
        data = data["personas"]
    entries: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(data, Mapping):
        entries = [(str(k), v) for k, v in data.items() if isinstance(v, Mapping)]
    elif isinstance(data, list):
        entries = [(str(v.get("key")), v) for v in data if isinstance(v, Mapping) and v.get("key")]
    else:
        raise TypeError("personas.yaml: expected a mapping or a list of personas")
    out: dict[str, Persona] = {}
    for key, entry in entries:
        name = entry.get("name") or entry.get("display_name") or entry.get("displayName")
        if not name:
            raise ValueError(f"persona {key!r} has no name")
        out[key] = Persona(
            key=key,
            name=str(name),
            category=str(entry.get("category") or "research"),
            skus=_skus_from_yaml(entry.get("skus") or entry.get("catalogue") or entry.get("catalog")),
            anchor=bool(entry.get("anchor", False)),
            description=str(entry.get("description") or ""),
        )
    return out


def load_personas(path: str | os.PathLike[str] | None = None, *, log: Log | None = None) -> dict[str, Persona]:
    """``config/personas.yaml`` (WP8), or the spec's defaults (:data:`DEFAULT_PERSONAS`) when it is missing.

    Every key of :data:`AGENT_ORDER` except ``wallet`` must be present; a missing one is taken from the
    defaults.
    """
    target = Path(path) if path is not None else CONFIG_DIR / "personas.yaml"
    if not target.is_file():
        if log:
            log(f"note: {target.name} not found, using the personas of spec 9.1")
        return dict(DEFAULT_PERSONAS)
    found = parse_personas(load_yaml(target))
    merged = dict(DEFAULT_PERSONAS)
    merged.update(found)
    return merged


def _resolve(value: Any, profile: str, what: str) -> int:
    if isinstance(value, Mapping):
        if profile not in value:
            raise ValueError(f"tiers.yaml: {what} has no value for profile {profile!r}")
        value = value[profile]
    return _amount(value, what)


def _mask(grades: Any) -> int:
    return grade_mask(*(Grade[str(g)] for g in (grades or [])))


def parse_tiers(data: Mapping[str, Any], profile: str) -> TiersConfig:
    """``TiersConfig`` from the parsed ``config/tiers.yaml``, demo/production values resolved for ``profile``."""
    profiles: dict[str, TierProfile] = {}
    for name, raw in (data.get("profiles") or {}).items():
        tiers_raw = raw["tiers"]
        tiers = []
        for t in range(4):
            entry = tiers_raw.get(t, tiers_raw.get(str(t)))
            if entry is None:
                raise ValueError(f"tiers.yaml: profile {name!r} has no tier {t}")
            tiers.append(
                TierConfig(
                    per_tx=_amount(entry["per_tx"], "per_tx"),
                    window_cap=_amount(entry["window_cap"], "window_cap"),
                    new_payee_cap=_amount(entry["new_payee_cap"], "new_payee_cap"),
                    max_tx=int(entry["max_tx"]),
                    pay_mask=_mask(entry.get("pay")),
                    escrow_mask=_mask(entry.get("escrow")),
                    min_clean_to_promote=int(entry.get("min_clean_to_promote") or 0),
                )
            )
        profiles[str(name)] = TierProfile(
            window_seconds=_resolve(raw["window_seconds"], profile, "window_seconds"),
            promotion_cooldown=_resolve(raw["promotion_cooldown"], profile, "promotion_cooldown"),
            dispute_window=_resolve(raw["dispute_window"], profile, "dispute_window"),
            max_grade_age=_resolve(raw["max_grade_age"], profile, "max_grade_age"),
            max_auto_tier=int(raw["max_auto_tier"]),
            initial_tier=int(raw["initial_tier"]),
            reserve_floor=_amount(raw["reserve_floor"], "reserve_floor"),
            min_clean_amount=_amount(raw["min_clean_amount"], "min_clean_amount"),
            tiers=tuple(tiers),
        )
    return TiersConfig(profiles=profiles)


def load_tiers(profile: str = "demo", path: str | os.PathLike[str] | None = None) -> TiersConfig:
    """``config/tiers.yaml`` with the demo or production values resolved."""
    return parse_tiers(load_yaml(path or CONFIG_DIR / "tiers.yaml"), profile)


def default_funding_plan(net: NetworkProfile, keys: KeyRing | None = None) -> FundingPlan:
    """The funding plan of 13.1 / S3.

    ETH: on a public network every role of :data:`ETH_TOPUP_ROLES` held by ``keys`` is topped up to
    ``funding.topup_eth``; Anvil accounts are pre-funded, so no ETH is sent there. USDC: wallet vault
    15.00 and Beacon vault 6.00, multiplied by ``ACB_FUNDING_USDC_BPS / 10000`` (default: the network's
    ``funding.usdc_bps``, else 10000), so a smaller faucet balance can be used with one setting.
    """
    eth: dict[str, int] = {}
    if net.chain_id != 31337:
        topup = int(net.funding.get("topup_eth", 0))
        for role in ETH_TOPUP_ROLES:
            if keys is None or role in keys:
                eth[role] = topup
    bps = int(os.environ.get(FUNDING_USDC_BPS_ENV) or net.funding.get("usdc_bps") or 10_000)
    if not 0 <= bps <= 100_000:
        raise ValueError(f"{FUNDING_USDC_BPS_ENV} must be between 0 and 100000")
    usdc = {name: amount * bps // 10_000 for name, amount in DEFAULT_USDC_TO_VAULTS.items()}
    return FundingPlan(eth=eth, usdc_to_vaults=usdc)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def registration_json(key: str, persona: Persona) -> dict[str, Any]:
    """The ERC-8004 registration JSON of spec 9.1 for one agent."""
    if key == WALLET_KEY:
        description = (
            "ACB demo agent. Not a real service. Research wallet agent whose spending is bounded by its TierVault."
        )
        services = [{"name": "acb-wallet", "endpoint": WALLET_ENDPOINT}]
    else:
        line = persona.description or persona.name
        description = f"{SIMULATED_LABEL} {line}".strip()
        services = [{"name": "acb-quote", "endpoint": SIM_ENDPOINT.format(key=key)}]
    return {
        "type": REGISTRATION_TYPE,
        "name": persona.name,
        "description": description,
        "services": services,
        "x-acb": {"category": persona.category, "skus": sorted(persona.skus)},
    }


def agent_uri(key: str, persona: Persona) -> str:
    """``data:application/json;base64,`` + canonical registration JSON."""
    payload = base64.b64encode(canonical_bytes(registration_json(key, persona))).decode("ascii")
    return "data:application/json;base64," + payload


def git_commit() -> str:
    """``git rev-parse HEAD`` of the repository, or ``"uncommitted"``."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=False, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return "uncommitted"
    commit = out.stdout.strip()
    return commit if out.returncode == 0 and commit else "uncommitted"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class _Ctx:
    """Shared state of one deploy: web3, senders per role, log, results."""

    def __init__(self, net: NetworkProfile, keys: KeyRing, w3: Any | None, log: Log | None):
        self.net = net
        self.keys = keys
        self.w3 = w3 if w3 is not None else make_web3(net)
        self.log = log or (lambda _msg: None)
        self._senders: dict[str, TxSender] = {}
        check_chain_id(self.w3, net)

    @property
    def is_anvil(self) -> bool:
        return self.net.chain_id == 31337

    def sender(self, role: str) -> TxSender:
        if role not in self._senders:
            self._senders[role] = TxSender(self.w3, self.keys.account(role))
        return self._senders[role]

    def has_code(self, address: str | None) -> bool:
        if not address:
            return False
        return len(self.w3.eth.get_code(checksum(address))) > 0

    def require_ok(self, result: TxResult, what: str) -> TxResult:
        if result.status != 1:
            raise DeploymentError(f"{what} reverted: {result.revert_error} (tx {result.hash})")
        return result

    def ensure_eth(self, role: str, txs: list[TxResult]) -> None:
        """Top up ``role`` from the deployer when it is below ``funding.min_eth_service`` (public networks)."""
        if role == "deployer":
            return
        address = self.keys.checksum_address(role)
        balance = int(self.w3.eth.get_balance(address))
        minimum = int(self.net.funding.get("min_eth_service", 0))
        if balance >= minimum:
            return
        target = max(int(self.net.funding.get("topup_eth", 0)), minimum)
        amount = target - balance
        deployer_balance = int(self.w3.eth.get_balance(self.keys.checksum_address("deployer")))
        if deployer_balance < amount:
            raise DeploymentError(
                f"deployer has {deployer_balance} wei, needs {amount} wei to top up {role}: fund the deployer first"
            )
        self.log(f"  top up {role} with {amount} wei")
        txs.append(self.require_ok(self.sender("deployer").send_eth(address, amount), f"ETH top-up of {role}"))


def _empty_deployments(ctx: _Ctx) -> Deployments:
    return Deployments(
        network=ctx.net.name,
        chain_id=ctx.net.chain_id,
        deployed_at=_utc_now(),
        git_commit=git_commit(),
        start_block=int(ctx.w3.eth.block_number),
        roles={k: ctx.keys.address(r) for k, r in ROLES_JSON.items() if r in ctx.keys},
        contracts={},
        vault_code_hash=abi_mod.codehash_of("TierVault"),
        anchors=(),
        agents={},
        vaults={},
        abi_sha=abi_mod.abi_sha(),
    )


def _save(dep: Deployments) -> Deployments:
    save_deployments(dep)
    return dep


# ---------------------------------------------------------------------------
# phase 1: contracts
# ---------------------------------------------------------------------------


def _implementation(w3: Any, proxy: str) -> str:
    raw = bytes(w3.eth.get_storage_at(checksum(proxy), EIP1967_IMPLEMENTATION_SLOT))
    return "0x" + raw[-20:].hex()


def check_official_erc8004(w3: Any, net: NetworkProfile) -> dict[str, ContractEntry]:
    """Preflight of the official ERC-8004 registries (S0, 3.7): abort on any change.

    Checks ``getVersion()`` against ``expected_version``, the EIP-1967 implementation slots against
    ``identity_impl`` / ``reputation_impl``, and that Reputation points at the Identity proxy.
    """
    cfg = net.erc8004
    identity_addr = lower(cfg["identity"])
    reputation_addr = lower(cfg["reputation"])
    expected = str(cfg.get("expected_version", ""))
    identity = IdentityContract(w3, identity_addr)
    reputation = ReputationContract(w3, reputation_addr)
    problems = []
    entries = {}
    for label, contract, impl_key in (
        ("IdentityRegistry", identity, "identity_impl"),
        ("ReputationRegistry", reputation, "reputation_impl"),
    ):
        if not contract.has_code():
            raise DeploymentError(f"{label} at {contract.address} has no code")
        version = contract.get_version()
        impl = _implementation(w3, contract.address)
        if expected and version != expected:
            problems.append(f"{label} getVersion() is {version!r}, expected {expected!r}")
        if impl != lower(cfg[impl_key]):
            problems.append(f"{label} implementation is {impl}, expected {lower(cfg[impl_key])}")
        entries[label] = ContractEntry(address=contract.address, official=True, implementation=impl, version=version)
    if reputation.get_identity_registry() != identity_addr:
        problems.append("ReputationRegistry.getIdentityRegistry() is not the Identity proxy")
    if problems:
        raise DeploymentError("official ERC-8004 registries changed, refusing to deploy: " + "; ".join(problems))
    return entries


def _contracts_state(ctx: _Ctx, dep: Deployments) -> dict[str, bool]:
    """Which existing contract entries are still valid on chain."""
    c = dep.contracts
    ok = {name: name in c and ctx.has_code(c[name].address) for name in ("USDC", "IdentityRegistry")}
    ok["ReputationRegistry"] = "ReputationRegistry" in c and ctx.has_code(c["ReputationRegistry"].address)
    if ok["ReputationRegistry"] and ok["IdentityRegistry"]:
        rep = ReputationContract(ctx.w3, c["ReputationRegistry"].address)
        try:
            ok["ReputationRegistry"] = rep.get_identity_registry() == c["IdentityRegistry"].address
        except Exception:  # noqa: BLE001 - not a registry
            ok["ReputationRegistry"] = False
    acb_ok = "AgentCreditBureau" in c and ctx.has_code(c["AgentCreditBureau"].address)
    if acb_ok and ok["IdentityRegistry"] and ok["USDC"]:
        acb = AcbContract(ctx.w3, c["AgentCreditBureau"].address)
        try:
            acb_ok = (
                acb.vault_code_hash() == abi_mod.codehash_of("TierVault")
                and acb.identity() == c["IdentityRegistry"].address
                and acb.token() == c["USDC"].address
                and acb.admin() == ctx.keys.address("admin")
            )
        except Exception:  # noqa: BLE001 - not our contract
            acb_ok = False
    else:
        acb_ok = False
    ok["AgentCreditBureau"] = acb_ok
    return ok


_FROM_FILE: Any = object()


def deploy_contracts(
    net: NetworkProfile,
    keys: KeyRing,
    *,
    existing: Deployments | None = _FROM_FILE,
    w3: Any | None = None,
    log: Log | None = None,
) -> tuple[Deployments, list[TxResult]]:
    """Phase 1 (S1): ACB (+ MockUSDC and the ERC-8004 Lite registries on Anvil); admin ``setPoster(bureau)``.

    ``existing`` defaults to the current ``deployments/<network>.json``; ``None`` starts fresh. On a
    public network the official ERC-8004 registries are checked first, also when the phase is skipped.
    """
    ctx = _Ctx(net, keys, w3, log)
    txs: list[TxResult] = []
    if existing is _FROM_FILE:
        existing = try_load_deployments(net.name)
    dep = existing if existing is not None and existing.chain_id == net.chain_id else _empty_deployments(ctx)
    official = None if ctx.is_anvil else check_official_erc8004(ctx.w3, net)
    ok = _contracts_state(ctx, dep)
    if all(ok.values()) and AcbContract(ctx.w3, dep.contracts["AgentCreditBureau"].address).is_poster(
        keys.address("bureau")
    ):
        ctx.log(f"skip contracts: AgentCreditBureau valid at {dep.contracts['AgentCreditBureau'].address}")
        return dep, txs

    ctx.log("phase contracts")
    deployer = ctx.sender("deployer")
    contracts = dict(dep.contracts)
    first_new = not any(ok.values())
    start_block = int(ctx.w3.eth.block_number) if first_new else dep.start_block
    clear_agents = False
    clear_vaults = False

    if ctx.is_anvil:
        if not ok["USDC"]:
            r = ctx.require_ok(deployer.deploy("MockUSDC", keys.checksum_address("deployer")), "MockUSDC deploy")
            txs.append(r)
            contracts["USDC"] = ContractEntry(
                address=r.to, tx_hash=r.hash, block=r.block, extra={"decimals": 6, "mock": True}
            )
            ok["AgentCreditBureau"] = False
            ctx.log(f"  MockUSDC {r.to}")
        if not ok["IdentityRegistry"]:
            r = ctx.require_ok(deployer.deploy("IdentityRegistryLite"), "IdentityRegistryLite deploy")
            txs.append(r)
            version = IdentityContract(ctx.w3, r.to).get_version()
            contracts["IdentityRegistry"] = ContractEntry(address=r.to, tx_hash=r.hash, block=r.block, version=version)
            ok["ReputationRegistry"] = False
            ok["AgentCreditBureau"] = False
            clear_agents = True
            ctx.log(f"  IdentityRegistryLite {r.to}")
        if not ok["ReputationRegistry"]:
            identity_addr = contracts["IdentityRegistry"].address
            r = ctx.require_ok(
                deployer.deploy("ReputationRegistryLite", checksum(identity_addr)), "ReputationRegistryLite deploy"
            )
            txs.append(r)
            version = ReputationContract(ctx.w3, r.to).get_version()
            contracts["ReputationRegistry"] = ContractEntry(
                address=r.to, tx_hash=r.hash, block=r.block, version=version
            )
            ctx.log(f"  ReputationRegistryLite {r.to}")
    else:
        assert official is not None
        if (
            contracts.get("IdentityRegistry")
            and contracts["IdentityRegistry"].address != official["IdentityRegistry"].address
        ):
            clear_agents = True
        contracts.update(official)
        usdc_addr = lower(net.usdc["address"])
        usdc = Erc20Contract(ctx.w3, usdc_addr)
        if not usdc.has_code():
            raise DeploymentError(f"USDC at {usdc_addr} has no code")
        decimals = usdc.decimals()
        if decimals != int(net.usdc.get("decimals", 6)):
            raise DeploymentError(f"USDC decimals {decimals}, expected {net.usdc.get('decimals', 6)}")
        if contracts.get("USDC") and contracts["USDC"].address != usdc_addr:
            ok["AgentCreditBureau"] = False
        contracts["USDC"] = ContractEntry(address=usdc_addr, extra={"decimals": decimals, "mock": False})

    if not ok["AgentCreditBureau"] or clear_agents:
        r = ctx.require_ok(
            deployer.deploy(
                "AgentCreditBureau",
                keys.checksum_address("admin"),
                checksum(contracts["IdentityRegistry"].address),
                checksum(contracts["USDC"].address),
                bytes.fromhex(abi_mod.codehash_of("TierVault")[2:]),
                int(net.upgrade_delay_s),
            ),
            "AgentCreditBureau deploy",
        )
        txs.append(r)
        contracts["AgentCreditBureau"] = ContractEntry(
            address=r.to, tx_hash=r.hash, block=r.block, extra={"upgradeDelay": int(net.upgrade_delay_s)}
        )
        clear_vaults = True
        ctx.log(f"  AgentCreditBureau {r.to}")

    dep = dep.model_copy(
        update={
            "contracts": contracts,
            "start_block": start_block,
            "deployed_at": _utc_now(),
            "git_commit": git_commit(),
            "roles": {k: keys.address(r) for k, r in ROLES_JSON.items() if r in keys},
            "vault_code_hash": abi_mod.codehash_of("TierVault"),
            "abi_sha": abi_mod.abi_sha(),
            "agents": {} if clear_agents else dep.agents,
            "anchors": () if clear_agents else dep.anchors,
            "vaults": {} if (clear_vaults or clear_agents) else dep.vaults,
        }
    )
    _save(dep)

    acb = AcbContract(ctx.w3, contracts["AgentCreditBureau"].address)
    if not acb.is_poster(keys.address("bureau")):
        ctx.ensure_eth("admin", txs)
        txs.append(
            ctx.require_ok(ctx.sender("admin").send(acb.set_poster(keys.address("bureau"), True)), "setPoster(bureau)")
        )
        ctx.log("  admin setPoster(bureau)")
    return dep, txs


# ---------------------------------------------------------------------------
# phase 2: identities
# ---------------------------------------------------------------------------


def _agent_role(key: str) -> str:
    """Key ring role that registers (and owns) the identity of ``key``."""
    return "owner" if key == WALLET_KEY else key


def _agent_valid(ctx: _Ctx, identity: IdentityContract, entry: AgentEntry, role: str) -> bool:
    owner = identity.try_owner_of(entry.agent_id)
    return owner is not None and owner == entry.identity_owner == ctx.keys.address(role)


def register_agents(
    net: NetworkProfile,
    keys: KeyRing,
    dep: Deployments,
    *,
    personas: Mapping[str, Persona] | None = None,
    w3: Any | None = None,
    log: Log | None = None,
) -> tuple[Deployments, list[TxResult]]:
    """Phase 2 (S2): register every identity of :data:`AGENT_ORDER` from its own key (the wallet's from OWNER).

    Also writes the pinned directory (names, categories, catalogue prices) and ``anchors``.
    """
    ctx = _Ctx(net, keys, w3, log)
    txs: list[TxResult] = []
    personas = dict(personas) if personas is not None else load_personas(log=ctx.log)
    identity = IdentityContract(ctx.w3, dep.contracts["IdentityRegistry"].address)
    agents = dict(dep.agents)
    started = False
    for key in AGENT_ORDER:
        persona = WALLET_PERSONA if key == WALLET_KEY else personas.get(key)
        if persona is None:
            raise DeploymentError(f"persona {key!r} missing from config/personas.yaml")
        role = _agent_role(key)
        entry = agents.get(key)
        if entry is not None and _agent_valid(ctx, identity, entry, role):
            refreshed = entry.model_copy(
                update={"name": persona.name, "category": persona.category, "skus": persona.skus}
            )
            if refreshed != entry:
                agents[key] = refreshed
            continue
        if not started:
            ctx.log("phase agents")
            started = True
        ctx.ensure_eth(role, txs)
        r = ctx.require_ok(ctx.sender(role).send(identity.register(agent_uri(key, persona))), f"register {key}")
        txs.append(r)
        agent_id = identity.agent_id_from_result(r)
        agents[key] = AgentEntry(
            agent_id=agent_id,
            name=persona.name,
            identity_owner=keys.address(role),
            wallet=None if key == WALLET_KEY else identity.get_agent_wallet(agent_id),
            register_tx=r.hash,
            category=persona.category,
            skus=dict(persona.skus),
        )
        ctx.log(f"  {key}: agentId {agent_id}")
        dep = _save(dep.model_copy(update={"agents": dict(agents)}))
    anchors = tuple(agents[k].agent_id for k in AGENT_ORDER if k in agents and k in personas and personas[k].anchor)
    if not started:
        ctx.log("skip agents: every identity registered")
    if agents != dep.agents or anchors != dep.anchors:
        dep = _save(dep.model_copy(update={"agents": dict(agents), "anchors": anchors}))
    return dep, txs


# ---------------------------------------------------------------------------
# phase 3: vaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VaultSpec:
    """Who holds which role of a vault (3.7)."""

    name: str
    agent_key: str
    profile: str
    owner_role: str
    agent_role: str
    autonomy_role: str
    registrant_role: str


def vault_specs(net: NetworkProfile) -> tuple[VaultSpec, ...]:
    """Wallet and Beacon vaults; on Anvil also echo1-3 (distinct owners, wash ring A36b)."""
    specs = [
        VaultSpec("wallet", WALLET_KEY, "wallet", "owner", "agent_signer", "autonomy", "owner"),
        VaultSpec("beacon", "beacon", "market", "beacon_owner", "beacon", "autonomy", "beacon"),
    ]
    if net.chain_id == 31337:
        for i in (1, 2, 3):
            specs.append(
                VaultSpec(f"echo{i}", f"echo{i}", "market", f"echo{i}_vault_owner", f"echo{i}", "autonomy", f"echo{i}")
            )
    return tuple(specs)


def vault_init(
    dep: Deployments,
    profile: TierProfile,
    *,
    owner: str,
    agent: str,
    autonomy_operator: str,
    agent_id: int,
) -> VaultInit:
    """The ``VaultInit`` of one vault from a tier profile."""
    return VaultInit(
        token=dep.contracts["USDC"].address,
        bureau=dep.contracts["AgentCreditBureau"].address,
        identity=dep.contracts["IdentityRegistry"].address,
        owner=owner,
        agent=agent,
        autonomy_operator=autonomy_operator,
        agent_id=agent_id,
        configs=profile.tiers,
        reserve_floor=profile.reserve_floor,
        min_clean_amount=profile.min_clean_amount,
        window_seconds=profile.window_seconds,
        promotion_cooldown=profile.promotion_cooldown,
        max_grade_age=profile.max_grade_age,
        dispute_window=profile.dispute_window,
        max_auto_tier=profile.max_auto_tier,
        initial_tier=profile.initial_tier,
    )


def _vault_valid(ctx: _Ctx, dep: Deployments, entry: VaultEntry, agent_id: int) -> tuple[bool, bool]:
    """``(deployed_ok, registered)`` for an existing vault entry."""
    if not ctx.has_code(entry.address):
        return False, False
    vault = VaultContract(ctx.w3, entry.address)
    try:
        deployed_ok = (
            vault.codehash() == dep.vault_code_hash
            and vault.agent_id() == agent_id
            and vault.bureau() == dep.contracts["AgentCreditBureau"].address
            and vault.token() == dep.contracts["USDC"].address
        )
    except Exception:  # noqa: BLE001 - not a vault
        return False, False
    if not deployed_ok:
        return False, False
    acb = AcbContract(ctx.w3, dep.contracts["AgentCreditBureau"].address)
    return True, acb.is_vault(entry.address)


def _deploy_vault(
    ctx: _Ctx, dep: Deployments, spec: VaultSpec, tiers: TiersConfig, txs: list[TxResult]
) -> tuple[Deployments, bool]:
    """Deploy and register one vault unless it is already valid; returns (deployments, changed)."""
    if spec.agent_key not in dep.agents:
        raise DeploymentError(f"agent {spec.agent_key!r} is not registered; run register_agents first")
    if spec.profile not in tiers.profiles:
        raise DeploymentError(f"tier profile {spec.profile!r} missing from config/tiers.yaml")
    keys = ctx.keys
    agent_id = dep.agents[spec.agent_key].agent_id
    entry = dep.vaults.get(spec.name)
    deployed_ok, registered = (False, False) if entry is None else _vault_valid(ctx, dep, entry, agent_id)
    if deployed_ok and registered:
        return dep, False
    ctx.log(f"phase vaults: {spec.name}")
    vaults = dict(dep.vaults)
    if not deployed_ok:
        init = vault_init(
            dep,
            tiers.profiles[spec.profile],
            owner=keys.address(spec.owner_role),
            agent=keys.address(spec.agent_role),
            autonomy_operator=keys.address(spec.autonomy_role),
            agent_id=agent_id,
        )
        r = ctx.require_ok(ctx.sender("deployer").deploy("TierVault", init.as_tuple()), f"TierVault {spec.name}")
        txs.append(r)
        entry = VaultEntry(
            address=r.to,
            agent_id=agent_id,
            owner=keys.address(spec.owner_role),
            agent=keys.address(spec.agent_role),
            autonomy=keys.address(spec.autonomy_role),
            profile=spec.profile,
            deploy_tx=r.hash,
            register_vault_tx=None,
        )
        vaults[spec.name] = entry
        dep = _save(dep.model_copy(update={"vaults": dict(vaults)}))
        ctx.log(f"  vault {spec.name}: {r.to}")
    assert entry is not None
    acb = AcbContract(ctx.w3, dep.contracts["AgentCreditBureau"].address)
    ctx.ensure_eth(spec.registrant_role, txs)
    r = ctx.require_ok(
        ctx.sender(spec.registrant_role).send(acb.register_vault(entry.address)), f"registerVault {spec.name}"
    )
    txs.append(r)
    vaults[spec.name] = entry.model_copy(update={"register_vault_tx": r.hash})
    dep = _save(dep.model_copy(update={"vaults": dict(vaults)}))
    ctx.log(f"  registerVault {spec.name}")
    return dep, True


def deploy_vaults(
    net: NetworkProfile,
    keys: KeyRing,
    dep: Deployments,
    tiers: TiersConfig | None = None,
    *,
    w3: Any | None = None,
    log: Log | None = None,
) -> tuple[Deployments, list[TxResult]]:
    """Phase 3 (S2): wallet and Beacon vaults (+ echo1-3 on Anvil), each registered with ``registerVault``."""
    ctx = _Ctx(net, keys, w3, log)
    tiers = tiers or load_tiers(net.profile)
    txs: list[TxResult] = []
    changed_any = False
    for spec in vault_specs(net):
        dep, changed = _deploy_vault(ctx, dep, spec, tiers, txs)
        changed_any = changed_any or changed
    if not changed_any:
        ctx.log("skip vaults: every vault deployed and registered")
    return dep, txs


def deploy_extra_vault(
    net: NetworkProfile,
    keys: KeyRing,
    dep: Deployments,
    name: str,
    *,
    agent: str,
    owner_role: str,
    agent_role: str | None = None,
    autonomy_role: str = "autonomy",
    registrant_role: str | None = None,
    profile: str = "market",
    tiers: TiersConfig | None = None,
    register: bool = True,
    allow_public: bool = False,
    w3: Any | None = None,
    log: Log | None = None,
) -> tuple[Deployments, list[TxResult]]:
    """Deploy (and register) one more vault for the attack harness (Echo and sybil vaults on Anvil).

    ``agent`` names an entry of ``dep.agents`` (its ``agentId`` goes into the vault); ``agent_role`` is
    the key ring role of the vault's agent key (default: ``agent``); ``registrant_role`` the role that
    calls ``registerVault`` (default: the role owning the agent's identity). Example, wash ring A36a
    (one owner): ``deploy_extra_vault(net, keys, dep, "ringA1", agent="echo1", owner_role="echo1_vault_owner")``.
    Refuses on a public network unless ``allow_public``.
    """
    ctx = _Ctx(net, keys, w3, log)
    if not ctx.is_anvil and not allow_public:
        raise DeploymentError("deploy_extra_vault is for Anvil; pass allow_public=True to override")
    if agent not in dep.agents:
        raise DeploymentError(f"agent {agent!r} not in the deployment")
    agent_role = agent_role or agent
    if registrant_role is None:
        owner_addr = dep.agents[agent].identity_owner
        matches = [r for r in keys.roles() if keys.address(r) == owner_addr]
        if not matches:
            raise DeploymentError(f"no key for the identity owner of {agent!r}")
        registrant_role = matches[0]
    spec = VaultSpec(name, agent, profile, owner_role, agent_role, autonomy_role, registrant_role)
    txs: list[TxResult] = []
    if not register:
        tiers = tiers or load_tiers(net.profile)
        init = vault_init(
            dep,
            tiers.profiles[profile],
            owner=keys.address(owner_role),
            agent=keys.address(agent_role),
            autonomy_operator=keys.address(autonomy_role),
            agent_id=dep.agents[agent].agent_id,
        )
        r = ctx.require_ok(ctx.sender("deployer").deploy("TierVault", init.as_tuple()), f"TierVault {name}")
        txs.append(r)
        vaults = dict(dep.vaults)
        vaults[name] = VaultEntry(
            address=r.to,
            agent_id=dep.agents[agent].agent_id,
            owner=keys.address(owner_role),
            agent=keys.address(agent_role),
            autonomy=keys.address(autonomy_role),
            profile=profile,
            deploy_tx=r.hash,
            register_vault_tx=None,
        )
        return _save(dep.model_copy(update={"vaults": vaults})), txs
    dep, _ = _deploy_vault(ctx, dep, spec, tiers or load_tiers(net.profile), txs)
    return dep, txs


def register_extra_agent(
    net: NetworkProfile,
    keys: KeyRing,
    dep: Deployments,
    name: str,
    role: str,
    *,
    display_name: str | None = None,
    category: str = "research",
    allow_public: bool = False,
    w3: Any | None = None,
    log: Log | None = None,
) -> tuple[Deployments, list[TxResult]]:
    """Register one more simulated identity from ``role`` (sybils of the attack harness on Anvil)."""
    ctx = _Ctx(net, keys, w3, log)
    if not ctx.is_anvil and not allow_public:
        raise DeploymentError("register_extra_agent is for Anvil; pass allow_public=True to override")
    identity = IdentityContract(ctx.w3, dep.contracts["IdentityRegistry"].address)
    existing = dep.agents.get(name)
    if existing is not None and _agent_valid(ctx, identity, existing, role):
        return dep, []
    persona = Persona(name, display_name or name, category, {}, False, "Research agent.")
    r = ctx.require_ok(ctx.sender(role).send(identity.register(agent_uri(name, persona))), f"register {name}")
    agent_id = identity.agent_id_from_result(r)
    agents = dict(dep.agents)
    agents[name] = AgentEntry(
        agent_id=agent_id,
        name=persona.name,
        identity_owner=keys.address(role),
        wallet=identity.get_agent_wallet(agent_id),
        register_tx=r.hash,
        category=category,
        skus={},
    )
    return _save(dep.model_copy(update={"agents": agents})), [r]


# ---------------------------------------------------------------------------
# phase 4: funding
# ---------------------------------------------------------------------------


def fund(
    net: NetworkProfile,
    keys: KeyRing,
    dep: Deployments,
    plan: FundingPlan | None = None,
    *,
    w3: Any | None = None,
    log: Log | None = None,
) -> list[TxResult]:
    """Phase 4 (S3): bring every planned ETH and vault USDC balance up to its target.

    Only the difference is sent, so a second run sends nothing. Anvil mints MockUSDC (the deployer is
    the minter); a public network transfers from the deployer, after checking that the deployer holds
    enough ETH and USDC for the whole plan (nothing is sent otherwise).
    """
    ctx = _Ctx(net, keys, w3, log)
    plan = plan or default_funding_plan(net, keys)
    txs: list[TxResult] = []
    deployer = ctx.sender("deployer")
    usdc = Erc20Contract(ctx.w3, dep.contracts["USDC"].address)

    eth_needs: dict[str, int] = {}
    for role, target in plan.eth.items():
        balance = int(ctx.w3.eth.get_balance(keys.checksum_address(role)))
        if balance < target:
            eth_needs[role] = target - balance
    usdc_needs: dict[str, int] = {}
    for vault_name, target in plan.usdc_to_vaults.items():
        if vault_name not in dep.vaults:
            raise DeploymentError(f"funding plan names vault {vault_name!r}, which is not deployed")
        balance = usdc.balance_of(dep.vaults[vault_name].address)
        if balance < target:
            usdc_needs[vault_name] = target - balance
    if not eth_needs and not usdc_needs:
        ctx.log("skip funding: every balance at or above plan")
        return txs

    ctx.log("phase funding")
    mock = bool(dep.contracts["USDC"].extra.get("mock")) and ctx.is_anvil
    deployer_eth = int(ctx.w3.eth.get_balance(keys.checksum_address("deployer")))
    if sum(eth_needs.values()) > deployer_eth:
        raise DeploymentError(
            f"deployer holds {deployer_eth} wei, the plan needs {sum(eth_needs.values())} wei plus gas: fund it first"
        )
    if usdc_needs and not mock:
        held = usdc.balance_of(keys.address("deployer"))
        if sum(usdc_needs.values()) > held:
            raise DeploymentError(
                f"deployer holds {held} USDC base units, the plan needs {sum(usdc_needs.values())}: "
                f"claim more test USDC or lower {FUNDING_USDC_BPS_ENV}"
            )
    for role, amount in eth_needs.items():
        txs.append(ctx.require_ok(deployer.send_eth(keys.checksum_address(role), amount), f"ETH to {role}"))
        ctx.log(f"  {role}: +{amount} wei")
    for vault_name, amount in usdc_needs.items():
        to = dep.vaults[vault_name].address
        fn = usdc.mint(to, amount) if mock else usdc.transfer(to, amount)
        txs.append(ctx.require_ok(deployer.send(fn), f"USDC to {vault_name}"))
        ctx.log(f"  vault {vault_name}: +{amount} USDC base units")
    return txs


# ---------------------------------------------------------------------------
# all phases
# ---------------------------------------------------------------------------


def deploy_all(
    net: NetworkProfile,
    keys: KeyRing,
    *,
    fresh: bool = False,
    tiers: TiersConfig | None = None,
    personas: Mapping[str, Persona] | None = None,
    plan: FundingPlan | None = None,
    w3: Any | None = None,
    log: Log | None = None,
) -> tuple[Deployments, list[TxResult]]:
    """Run the four phases; ``fresh`` ignores an existing ``deployments/<network>.json`` and deploys anew."""
    ctx = _Ctx(net, keys, w3, log)
    existing = None if fresh else try_load_deployments(net.name)
    if existing is not None and existing.chain_id != net.chain_id:
        ctx.log(f"note: deployments file is for chain {existing.chain_id}, starting fresh")
        existing = None
    dep, txs = deploy_contracts(net, keys, existing=existing, w3=ctx.w3, log=log)
    dep, more = register_agents(net, keys, dep, personas=personas, w3=ctx.w3, log=log)
    txs += more
    dep, more = deploy_vaults(net, keys, dep, tiers, w3=ctx.w3, log=log)
    txs += more
    txs += fund(net, keys, dep, plan, w3=ctx.w3, log=log)
    return dep, txs


__all__ = [
    "AGENT_ORDER",
    "DEFAULT_PERSONAS",
    "DEFAULT_USDC_TO_VAULTS",
    "FUNDING_USDC_BPS_ENV",
    "SIMULATED_LABEL",
    "DeploymentError",
    "Persona",
    "VaultSpec",
    "agent_uri",
    "check_official_erc8004",
    "default_funding_plan",
    "deploy_all",
    "deploy_contracts",
    "deploy_extra_vault",
    "deploy_vaults",
    "fund",
    "load_personas",
    "load_tiers",
    "parse_personas",
    "parse_tiers",
    "register_agents",
    "register_extra_agent",
    "registration_json",
    "vault_init",
    "vault_specs",
]
