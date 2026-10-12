"""ABI parity of the exported artefacts (WP2, chain-free).

- abi/IdentityRegistryLite.json and abi/ReputationRegistryLite.json expose every official ERC-8004 v2.0.0
  signature pinned in abi/erc8004_official_signatures.json (spec 0.4, 3.4): functions with the same outputs
  and read/write kind, events with the same indexed flags, OpenZeppelin v5 errors.
- abi/AgentCreditBureau.json and abi/TierVault.json expose the signatures of spec 3.1 and 3.2.
- Every artefact has the agreed shape (spec 15.3) and is deterministic JSON.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from eth_utils import keccak

ROOT = Path(__file__).resolve().parents[2]
ABI_DIR = ROOT / "abi"
CONTRACTS = ["AgentCreditBureau", "TierVault", "MockUSDC", "IdentityRegistryLite", "ReputationRegistryLite"]

OUTFLOW = "(bytes32,uint256,uint128,uint64,bytes32,bytes32,bytes32,uint8,uint16)"
DECISION = "(bytes32,uint256,uint128,uint8,uint16,bytes32,bytes32)"
TIER_CONFIG = "(uint128,uint128,uint128,uint8,uint8,uint8,uint8)"


def load(name: str) -> dict:
    return json.loads((ABI_DIR / f"{name}.json").read_text(encoding="utf-8"))


def canonical_type(param: dict) -> str:
    """Solidity ABI type string, with tuples expanded (``(string,bytes)[]``)."""
    t = param["type"]
    if t.startswith("tuple"):
        inner = ",".join(canonical_type(c) for c in param["components"])
        return f"({inner}){t[len('tuple') :]}"
    return t


def signature(entry: dict) -> str:
    return f"{entry['name']}({','.join(canonical_type(p) for p in entry.get('inputs', []))})"


def entries(abi: list[dict], kind: str) -> dict[str, dict]:
    return {signature(e): e for e in abi if e["type"] == kind}


def read_kind(mutability: str) -> str:
    return "read" if mutability in ("view", "pure") else mutability


@pytest.fixture(scope="module")
def official() -> dict:
    return json.loads((ABI_DIR / "erc8004_official_signatures.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", CONTRACTS)
def test_artefact_shape_and_codehash(name: str) -> None:
    raw = (ABI_DIR / f"{name}.json").read_bytes()
    assert b"\r\n" not in raw, "LF line endings only"
    assert not raw.startswith(b"\xef\xbb\xbf"), "no BOM"
    art = json.loads(raw)
    assert set(art) == {"contractName", "abi", "bytecode", "deployedBytecode", "codehash"}
    assert art["contractName"] == name
    for key in ("bytecode", "deployedBytecode", "codehash"):
        assert art[key].startswith("0x") and art[key] == art[key].lower()
    assert len(art["deployedBytecode"]) > 2
    assert art["codehash"] == "0x" + keccak(hexstr=art["deployedBytecode"]).hex()
    # deterministic rendering: sorted keys, indent 2, trailing newline
    assert raw.decode("ascii") == json.dumps(art, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


@pytest.mark.parametrize(
    ("lite", "official_name"),
    [("IdentityRegistryLite", "IdentityRegistry"), ("ReputationRegistryLite", "ReputationRegistry")],
)
def test_erc8004_lite_matches_official_signatures(official: dict, lite: str, official_name: str) -> None:
    abi = load(lite)["abi"]
    functions = entries(abi, "function")
    events = entries(abi, "event")
    errors = entries(abi, "error")
    spec = official[official_name]
    for fn in spec["functions"]:
        assert fn["signature"] in functions, f"{lite} lacks {fn['signature']}"
        mine = functions[fn["signature"]]
        assert [canonical_type(o) for o in mine["outputs"]] == fn["outputs"], fn["signature"]
        assert read_kind(mine["stateMutability"]) == read_kind(fn["stateMutability"]), fn["signature"]
    for ev in spec["events"]:
        assert ev["signature"] in events, f"{lite} lacks event {ev['signature']}"
        assert [i["indexed"] for i in events[ev["signature"]]["inputs"]] == ev["indexed"], ev["signature"]
    for err in spec["errors"]:
        assert err in errors, f"{lite} lacks error {err}"


def test_pinned_file_lists_the_spec_events(official: dict) -> None:
    """The three events of spec 0.4 that the indexer filters on are pinned with their exact strings."""
    pinned = {e["signature"] for reg in ("IdentityRegistry", "ReputationRegistry") for e in official[reg]["events"]}
    assert {
        "Registered(uint256,string,address)",
        "MetadataSet(uint256,string,string,bytes)",
        "URIUpdated(uint256,string,address)",
        "NewFeedback(uint256,address,uint64,int128,uint8,string,string,string,string,string,bytes32)",
        "FeedbackRevoked(uint256,address,uint64)",
        "ResponseAppended(uint256,address,uint64,address,string,bytes32)",
    } <= pinned


ACB_FUNCTIONS = {
    "postGrade(uint256,uint8,uint16,uint16[4],bytes32,bytes32,bytes32,uint64,string)": ([], "nonpayable"),
    "gradeOf(uint256)": (["uint8", "uint64"], "view"),
    "effectiveGrade(uint256,uint64)": (["uint8"], "view"),
    "getRecord(uint256)": (
        ["(uint8,uint8,uint16,uint64,uint64,uint64,uint64,address,bytes32,bytes32,bytes32)"],
        "view",
    ),
    "reasonsOf(uint256)": (["uint16[4]"], "view"),
    "setPoster(address,bool)": ([], "nonpayable"),
    "setVaultCodeHash(bytes32)": ([], "nonpayable"),
    "setUpgradeDelay(uint64)": ([], "nonpayable"),
    "transferAdmin(address)": ([], "nonpayable"),
    "acceptAdmin()": ([], "nonpayable"),
    "registerVault(address)": ([], "nonpayable"),
    "fileAppeal(uint256,bytes32,string)": (["uint256"], "nonpayable"),
    "resolveAppeal(uint256,bool,bytes32)": ([], "nonpayable"),
    "admin()": (["address"], "view"),
    "pendingAdmin()": (["address"], "view"),
    "identity()": (["address"], "view"),
    "token()": (["address"], "view"),
    "vaultCodeHash()": (["bytes32"], "view"),
    "upgradeDelay()": (["uint64"], "view"),
    "isPoster(address)": (["bool"], "view"),
    "isVault(address)": (["bool"], "view"),
    "vaultAgentId(address)": (["uint256"], "view"),
    "appealCount()": (["uint256"], "view"),
    "appealAgent(uint256)": (["uint256"], "view"),
}
ACB_EVENTS = [
    "GradePosted(uint256,uint8,uint8,uint16,uint16[4],bytes32,bytes32,bytes32,uint64,address,string)",
    "PosterSet(address,bool)",
    "VaultCodeHashSet(bytes32)",
    "UpgradeDelaySet(uint64)",
    "VaultRegistered(address,uint256,address)",
    "AppealFiled(uint256,uint256,address,bytes32,string)",
    "AppealResolved(uint256,bool,bytes32)",
    "AdminTransferStarted(address,address)",
    "AdminTransferred(address,address)",
]
ACB_ERRORS = [
    "NotAdmin()",
    "NotPendingAdmin()",
    "NotPoster()",
    "ZeroAddress()",
    "ZeroHash()",
    "BadGrade(uint8)",
    "BadScore(uint16)",
    "AsOfInFuture(uint64)",
    "StaleAsOf(uint64,uint64)",
    "SelfGrading(uint256)",
    "BadVaultCode(bytes32)",
    "BadVaultConfig()",
    "VaultExists()",
    "NotAgentOwner(uint256)",
    "UnknownAppeal(uint256)",
    "BadDelay()",
]

VAULT_FUNCTIONS = {
    "registerMandate(bytes32,uint256[],address[],uint128,uint128,uint64,bool,bytes32)": ([], "nonpayable"),
    "revokeMandate(bytes32)": ([], "nonpayable"),
    "getMandate(bytes32)": (["(uint128,uint128,uint128,uint64,bool,bool)"], "view"),
    f"pay({OUTFLOW})": ([], "nonpayable"),
    f"escrow({OUTFLOW})": ([], "nonpayable"),
    "claimDelivered(bytes32,bytes32)": ([], "nonpayable"),
    "release(bytes32)": ([], "nonpayable"),
    "refund(bytes32,uint16)": ([], "nonpayable"),
    "ownerApprove(bytes32,uint256,address,uint128,uint64,bool,uint64)": ([], "nonpayable"),
    "revokeApproval(bytes32)": ([], "nonpayable"),
    "executeApproved(bytes32,bytes32,bytes32)": ([], "nonpayable"),
    f"recordDecision({DECISION})": ([], "nonpayable"),
    "recordDelivery(bytes32,uint8,bytes32)": ([], "nonpayable"),
    "recordViolation(uint8,uint8,bytes32)": ([], "nonpayable"),
    "setTier(uint8,uint16,bytes32)": ([], "nonpayable"),
    "freeze(uint16,bytes32)": ([], "nonpayable"),
    "unfreeze(uint8,uint16,address)": ([], "nonpayable"),
    f"setTierConfig(uint8,{TIER_CONFIG})": ([], "nonpayable"),
    "setReserveFloor(uint128)": ([], "nonpayable"),
    "setMinCleanAmount(uint128)": ([], "nonpayable"),
    "setMaxAutoTier(uint8)": ([], "nonpayable"),
    "setPromotionCooldown(uint64)": ([], "nonpayable"),
    "setMaxGradeAge(uint64)": ([], "nonpayable"),
    "setWindowSeconds(uint64)": ([], "nonpayable"),
    "setDisputeWindow(uint64)": ([], "nonpayable"),
    "setAgent(address)": ([], "nonpayable"),
    "setAutonomyOperator(address)": ([], "nonpayable"),
    "withdraw(address,uint128)": ([], "nonpayable"),
    "transferOwnership(address)": ([], "nonpayable"),
    "acceptOwnership()": ([], "nonpayable"),
    "tierConfig(uint8)": ([TIER_CONFIG], "view"),
    "getOrder(bytes32)": (
        ["(uint8,uint8,uint8,uint64,uint64,uint64,uint128,uint256,address,bytes32,bytes32)"],
        "view",
    ),
    "getApproval(bytes32)": (["(uint256,address,uint128,uint64,uint64,bool,bool)"], "view"),
    "mandatePayee(bytes32,uint256)": (["address"], "view"),
    "spentInWindow()": (["uint128", "uint8"], "view"),
    "isFrozen()": (["bool"], "view"),
    "freeBalance()": (["uint256"], "view"),
    "status()": (
        ["(uint8,bool,uint8,uint8,uint256,uint128,uint128,uint128,uint8,uint16,uint16,uint64)"],
        "view",
    ),
    "token()": (["address"], "view"),
    "bureau()": (["address"], "view"),
    "identity()": (["address"], "view"),
    "agentId()": (["uint256"], "view"),
    "owner()": (["address"], "view"),
    "pendingOwner()": (["address"], "view"),
    "agent()": (["address"], "view"),
    "autonomyOperator()": (["address"], "view"),
    "tier()": (["uint8"], "view"),
    "tierBeforeFreeze()": (["uint8"], "view"),
    "maxAutoTier()": (["uint8"], "view"),
    "lastTierChangeAt()": (["uint64"], "view"),
    "promotionCooldown()": (["uint64"], "view"),
    "windowSeconds()": (["uint64"], "view"),
    "maxGradeAge()": (["uint64"], "view"),
    "disputeWindow()": (["uint64"], "view"),
    "cleanSinceChange()": (["uint16"], "view"),
    "violationPointsSinceChange()": (["uint16"], "view"),
    "reserveFloor()": (["uint128"], "view"),
    "escrowLocked()": (["uint128"], "view"),
    "minCleanAmount()": (["uint128"], "view"),
    "completedWith(uint256)": (["uint32"], "view"),
    "RING_SIZE()": (["uint8"], "view"),
    "MAX_MANDATE_PAYEES()": (["uint8"], "view"),
    "MAX_MANDATE_DURATION()": (["uint64"], "view"),
}
VAULT_EVENTS = {
    "VaultInitialized(address,address,address,uint256,uint8)": [True, True, True, False, False],
    "MandateRegistered(bytes32,uint256[],address[],uint128,uint128,uint64,bool,bytes32)": [True] + [False] * 7,
    "MandateRevoked(bytes32)": [True],
    "Paid(bytes32,uint256,address,uint128,uint8,uint16,bytes32,bytes32,bytes32,uint64,uint8)": (
        [True] * 3 + [False] * 8
    ),
    "EscrowOpened(bytes32,uint256,address,uint128,uint64,bytes32,bytes32,bytes32,uint16,uint8)": (
        [True] * 3 + [False] * 7
    ),
    "DeliveryClaimed(bytes32,uint256,bytes32,uint64,bool)": [True, True, False, False, False],
    "EscrowReleased(bytes32,uint256,address,uint128,uint8,bytes32)": [True] * 3 + [False] * 3,
    "EscrowRefunded(bytes32,uint256,address,uint128,uint16,uint8)": [True] * 3 + [False] * 3,
    "DeliveryRecorded(bytes32,uint256,address,uint8,bytes32)": [True] * 3 + [False] * 2,
    "DecisionRecorded(bytes32,uint256,uint8,uint128,uint16,bytes32,bytes32,uint8)": [True] * 3 + [False] * 5,
    "OwnerApproved(bytes32,uint256,address,uint128,uint64,bool,uint64)": [True] * 3 + [False] * 4,
    "ApprovalRevoked(bytes32)": [True],
    "ViolationRecorded(uint8,uint8,bytes32,address,uint8)": [True, False, False, True, False],
    "TierChanged(uint8,uint8,uint16,bytes32,address)": [True, True, False, False, True],
    "TierConfigSet(uint8,uint128,uint128,uint128,uint8,uint8,uint8,uint8)": [True] + [False] * 7,
    "ConfigChanged(bytes32,uint256)": [True, False],
    "AgentChanged(address,address)": [True, True],
    "AutonomyOperatorChanged(address,address)": [True, True],
    "OwnershipTransferStarted(address,address)": [True, True],
    "OwnershipTransferred(address,address)": [True, True],
    "Withdrawn(address,uint128)": [True, False],
}
VAULT_ERRORS = [
    "NotOwner()",
    "NotPendingOwner()",
    "NotAgent()",
    "NotAutonomy()",
    "NotAuthorized()",
    "NotPayee()",
    "Frozen()",
    "NotFrozen()",
    "Reentrancy()",
    "ZeroAddress()",
    "ZeroHash()",
    "RolesNotDistinct()",
    "BadWindow()",
    "BadTier(uint8)",
    "SameTier()",
    "BadTierConfig(uint8)",
    "MaskIncludesD()",
    "BadDecision(uint8)",
    "BadOutcome(uint8)",
    "BadReason(uint16)",
    "OutcomeAlreadySet(bytes32)",
    "OrderExists(bytes32)",
    "BadOrderState(bytes32,uint8)",
    "ZeroAmount()",
    "BadDeadline()",
    "DeadlineNotPassed(uint64)",
    "MandateExists(bytes32)",
    "BadMandate()",
    "MandateInactive(bytes32)",
    "MandateExpired(bytes32)",
    "NotInMandate(bytes32,uint256)",
    "MandatePerTxExceeded(uint256,uint256)",
    "MandateBudgetExceeded(uint256,uint256,uint256)",
    "RecurringNeedsTier2()",
    "PayeeWalletMismatch(uint256,address,address)",
    "PayeeWalletChanged(uint256,address,address)",
    "PerTxCapExceeded(uint256,uint256)",
    "NewPayeeCapExceeded(uint256,uint256)",
    "WindowCapExceeded(uint256,uint256,uint256)",
    "TooManyTx(uint8,uint8)",
    "RingFull()",
    "GradeNotAllowed(uint256,uint8,uint8)",
    "SelfDealing(address)",
    "ReserveFloorBreached(uint256,uint256,uint256,uint256)",
    "InsufficientUnlocked(uint256,uint256)",
    "EscrowNotExpired(uint64)",
    "NoClaim(bytes32)",
    "AlreadyClaimed(bytes32)",
    "DeliveryClaimed(bytes32)",
    "DisputeWindowOpen(uint64)",
    "ApprovalMissing(bytes32)",
    "ApprovalUsed(bytes32)",
    "ApprovalExpired(bytes32)",
    "BadViolationKind(uint8)",
    "BadSeverity(uint8)",
    "PromotionTooLarge(uint8,uint8)",
    "AboveMaxAutoTier(uint8,uint8)",
    "PromotionCooldown(uint64)",
    "PromotionNotEarned(uint16,uint16,uint16)",
    "TransferFailed()",
]


def _check_functions(name: str, expected: dict) -> None:
    functions = entries(load(name)["abi"], "function")
    for sig, (outputs, mutability) in expected.items():
        assert sig in functions, f"{name} lacks {sig}"
        assert [canonical_type(o) for o in functions[sig]["outputs"]] == outputs, sig
        assert read_kind(functions[sig]["stateMutability"]) == read_kind(mutability), sig


def test_bureau_signatures_match_spec() -> None:
    _check_functions("AgentCreditBureau", ACB_FUNCTIONS)
    abi = load("AgentCreditBureau")["abi"]
    assert set(ACB_EVENTS) <= set(entries(abi, "event"))
    assert set(ACB_ERRORS) <= set(entries(abi, "error"))
    ctor = next(e for e in abi if e["type"] == "constructor")
    assert [canonical_type(p) for p in ctor["inputs"]] == ["address", "address", "address", "bytes32", "uint64"]


def test_vault_signatures_match_spec() -> None:
    _check_functions("TierVault", VAULT_FUNCTIONS)
    abi = load("TierVault")["abi"]
    events = entries(abi, "event")
    for sig, indexed in VAULT_EVENTS.items():
        assert sig in events, f"TierVault lacks event {sig}"
        assert [i["indexed"] for i in events[sig]["inputs"]] == indexed, sig
    assert set(VAULT_ERRORS) <= set(entries(abi, "error"))
    ctor = next(e for e in abi if e["type"] == "constructor")
    assert canonical_type(ctor["inputs"][0]) == (
        f"(address,address,address,address,address,address,uint256,{TIER_CONFIG}[4],"
        "uint128,uint128,uint64,uint64,uint64,uint64,uint8,uint8)"
    )
    # no receive / fallback / onERC721Received (spec 3.0)
    kinds = {e["type"] for e in abi}
    assert "receive" not in kinds and "fallback" not in kinds
    assert not any(e.get("name") == "onERC721Received" for e in abi)
    assert not any(e.get("stateMutability") == "payable" for e in abi if e["type"] == "function")


def test_mock_usdc_signatures() -> None:
    abi = load("MockUSDC")["abi"]
    functions = entries(abi, "function")
    for sig in (
        "name()",
        "symbol()",
        "decimals()",
        "totalSupply()",
        "balanceOf(address)",
        "allowance(address,address)",
        "transfer(address,uint256)",
        "transferFrom(address,address,uint256)",
        "approve(address,uint256)",
        "mint(address,uint256)",
    ):
        assert sig in functions, sig
    events = entries(abi, "event")
    assert [i["indexed"] for i in events["Transfer(address,address,uint256)"]["inputs"]] == [True, True, False]
    assert [i["indexed"] for i in events["Approval(address,address,uint256)"]["inputs"]] == [True, True, False]
    assert {"InsufficientBalance()", "InsufficientAllowance()"} <= set(entries(abi, "error"))
