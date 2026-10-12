"""Typed contract wrappers: one method per contract function (spec 15.3, Appendix B).

- **Reads** call the contract and return Python values, the views of Appendix B
  (:class:`~acb.types.VaultState`, :class:`~acb.types.MandateView`, :class:`~acb.types.GradeView`,
  :class:`~acb.types.TierConfig`) or the small frozen records defined here (:class:`GradeRecord`,
  :class:`OrderInfo`, ...). Every read takes an optional ``block`` (default ``"latest"``).
- **Writes** are call builders: they return the web3 ``ContractFunction``, which a
  :class:`~acb.chain.tx.TxSender` sends (``sender.send(vault.pay(params))``). Nothing here signs.

Addresses in results are lowercase; bytes32 values are lowercase ``0x`` hex (``Web3.to_hex``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from web3 import Web3

from acb.chain import abi as abi_mod
from acb.chain.client import get_logs_chunked
from acb.types import (
    TIER_FROZEN,
    Decision,
    Deployments,
    Grade,
    GradeView,
    MandateView,
    OrderState,
    Outcome,
    TierConfig,
    VaultState,
)

ZERO_ADDRESS = "0x" + "00" * 20
ZERO_HASH = "0x" + "00" * 32
BlockId = int | str

# ---------------------------------------------------------------------------
# conversions
# ---------------------------------------------------------------------------


def checksum(address: str) -> str:
    """Checksummed address for web3 calls."""
    return Web3.to_checksum_address(address)


def lower(address: Any) -> str:
    """Lowercase ``0x`` address (the JSON convention, spec 0.1)."""
    return str(address).lower()


def b32(value: str | bytes | int) -> bytes:
    """A bytes32 argument from ``0x`` hex (exactly 32 bytes), raw bytes or an int."""
    if isinstance(value, int):
        return value.to_bytes(32, "big")
    if isinstance(value, bytes | bytearray):
        raw = bytes(value)
    else:
        text = value[2:] if value.startswith(("0x", "0X")) else value
        raw = bytes.fromhex(text)
    if len(raw) != 32:
        raise ValueError(f"expected 32 bytes, got {len(raw)}")
    return raw


def hex32(value: bytes | str) -> str:
    """Lowercase ``0x`` + 64 hex."""
    if isinstance(value, str):
        return "0x" + value[2:].lower().rjust(64, "0") if value.startswith("0x") else "0x" + value.lower()
    return Web3.to_hex(bytes(value).rjust(32, b"\x00"))


def model_id_bytes(model_id: str | bytes) -> bytes:
    """``bytes32("acb-sc-1.0.0")``: a short ASCII string left-aligned, or 32 raw bytes / 0x hex."""
    if isinstance(model_id, bytes | bytearray):
        return b32(model_id)
    if model_id.startswith("0x") and len(model_id) == 66:
        return b32(model_id)
    raw = model_id.encode("ascii")
    if len(raw) > 32:
        raise ValueError("model id longer than 32 bytes")
    return raw.ljust(32, b"\x00")


def model_id_text(value: bytes | str) -> str:
    """Decode a bytes32 short string; non-ASCII content is returned as ``0x`` hex."""
    raw = b32(value) if not isinstance(value, bytes) else value
    stripped = raw.rstrip(b"\x00")
    try:
        text = stripped.decode("ascii")
    except UnicodeDecodeError:
        return hex32(raw)
    if not text.isprintable() or b"\x00" in stripped:
        return hex32(raw)
    return text


def pack_reasons(reasons: Iterable[int]) -> list[int]:
    """Up to four reason codes as the ``uint16[4]`` argument (missing slots are 0)."""
    values = [int(r) for r in reasons]
    if len(values) > 4:
        raise ValueError("at most four reason codes")
    for r in values:
        if not 0 <= r <= 0xFFFF:
            raise ValueError(f"reason code {r} out of uint16 range")
    return values + [0] * (4 - len(values))


def unpack_reasons(packed: int) -> tuple[int, int, int, int]:
    """``GradeRecord.reasons`` (4 x uint16, reasons[0] in the low bits)."""
    return tuple((int(packed) >> (16 * i)) & 0xFFFF for i in range(4))  # type: ignore[return-value]


def tier_config_from_chain(raw: Sequence[Any]) -> TierConfig:
    """``TierConfig`` struct tuple -> Appendix B :class:`~acb.types.TierConfig`."""
    per_tx, window_cap, new_payee_cap, max_tx, pay_mask, escrow_mask, min_clean = raw
    return TierConfig(
        per_tx=int(per_tx),
        window_cap=int(window_cap),
        new_payee_cap=int(new_payee_cap),
        max_tx=int(max_tx),
        pay_mask=int(pay_mask),
        escrow_mask=int(escrow_mask),
        min_clean_to_promote=int(min_clean),
    )


def tier_config_tuple(cfg: TierConfig) -> tuple[int, int, int, int, int, int, int]:
    """Appendix B :class:`~acb.types.TierConfig` -> the Solidity struct tuple."""
    return (
        cfg.per_tx,
        cfg.window_cap,
        cfg.new_payee_cap,
        cfg.max_tx,
        cfg.pay_mask,
        cfg.escrow_mask,
        cfg.min_clean_to_promote,
    )


ZERO_TIER_CONFIG = TierConfig(
    per_tx=0, window_cap=0, new_payee_cap=0, max_tx=0, pay_mask=0, escrow_mask=0, min_clean_to_promote=0
)

# ---------------------------------------------------------------------------
# small read records and input structs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GradeRecord:
    """``AgentCreditBureau.GradeRecord`` (3.1)."""

    grade: Grade
    prev_grade: Grade
    score: int
    posted_at: int
    prev_posted_at: int
    as_of_block: int
    reasons: tuple[int, int, int, int]
    poster: str
    model_id: str
    config_hash: str
    evidence_hash: str

    @property
    def reason_list(self) -> tuple[int, ...]:
        """The non-empty reason codes in order."""
        return tuple(r for r in self.reasons if r)


@dataclass(frozen=True)
class OrderInfo:
    """``TierVault.Order`` (3.2)."""

    state: OrderState
    outcome: Outcome
    decision: Decision
    created_at: int
    deadline: int
    claimed_at: int
    amount: int
    payee_agent_id: int
    payee: str
    mandate_id: str
    delivery_hash: str


@dataclass(frozen=True)
class MandateInfo:
    """``TierVault.Mandate`` (3.2); the payee lists live in ``MandateRegistered`` (see ``mandate_view``)."""

    max_per_tx: int
    max_total: int
    spent: int
    valid_until: int
    recurring: bool
    active: bool

    @property
    def exists(self) -> bool:
        return self.max_total > 0


@dataclass(frozen=True)
class ApprovalInfo:
    """``TierVault.Approval`` (3.2)."""

    payee_agent_id: int
    expected_payee: str
    amount: int
    expiry: int
    escrow_deadline: int
    as_escrow: bool
    used: bool

    @property
    def exists(self) -> bool:
        return self.expected_payee != ZERO_ADDRESS


@dataclass(frozen=True)
class VaultStatus:
    """``TierVault.VaultStatus`` (3.2)."""

    tier: int
    frozen: bool
    tier_before_freeze: int
    max_auto_tier: int
    balance: int
    escrow_locked: int
    reserve_floor: int
    spent: int
    count: int
    clean: int
    points: int
    last_tier_change_at: int


@dataclass(frozen=True)
class OutflowParams:
    """``TierVault.OutflowParams`` for ``pay`` and ``escrow``."""

    order_id: str
    payee_agent_id: int
    amount: int
    deadline: int
    mandate_id: str
    invoice_hash: str
    decision_hash: str
    decision: int
    reason_code: int

    def as_tuple(self) -> tuple:
        return (
            b32(self.order_id),
            int(self.payee_agent_id),
            int(self.amount),
            int(self.deadline),
            b32(self.mandate_id),
            b32(self.invoice_hash),
            b32(self.decision_hash),
            int(self.decision),
            int(self.reason_code),
        )


@dataclass(frozen=True)
class DecisionParams:
    """``TierVault.DecisionParams`` for ``recordDecision``."""

    order_id: str
    payee_agent_id: int
    amount: int
    decision: int
    reason_code: int
    invoice_hash: str
    decision_hash: str

    def as_tuple(self) -> tuple:
        return (
            b32(self.order_id),
            int(self.payee_agent_id),
            int(self.amount),
            int(self.decision),
            int(self.reason_code),
            b32(self.invoice_hash),
            b32(self.decision_hash),
        )


@dataclass(frozen=True)
class VaultInit:
    """``TierVault.VaultInit`` constructor argument (3.2)."""

    token: str
    bureau: str
    identity: str
    owner: str
    agent: str
    autonomy_operator: str
    agent_id: int
    configs: tuple[TierConfig, TierConfig, TierConfig, TierConfig]
    reserve_floor: int
    min_clean_amount: int
    window_seconds: int
    promotion_cooldown: int
    max_grade_age: int
    dispute_window: int
    max_auto_tier: int
    initial_tier: int

    def as_tuple(self) -> tuple:
        return (
            checksum(self.token),
            checksum(self.bureau),
            checksum(self.identity),
            checksum(self.owner),
            checksum(self.agent),
            checksum(self.autonomy_operator),
            int(self.agent_id),
            [tier_config_tuple(c) for c in self.configs],
            int(self.reserve_floor),
            int(self.min_clean_amount),
            int(self.window_seconds),
            int(self.promotion_cooldown),
            int(self.max_grade_age),
            int(self.dispute_window),
            int(self.max_auto_tier),
            int(self.initial_tier),
        )


# ---------------------------------------------------------------------------
# base wrapper
# ---------------------------------------------------------------------------


class BoundContract:
    """A contract of ``abi/<artefact>.json`` at ``address`` on ``w3``."""

    artefact: str = ""

    def __init__(self, w3: Any, address: str):
        self.w3 = w3
        self.address = lower(address)
        self.contract = w3.eth.contract(address=checksum(address), abi=abi_mod.abi_of(self.artefact))

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.address})"

    @property
    def events(self) -> Any:
        """``contract.events`` (pass ``events.Name`` to :func:`acb.chain.client.get_logs_chunked`)."""
        return self.contract.events

    def fn(self, name: str, *args: Any) -> Any:
        """Call builder for any function by name (prefer the typed methods)."""
        return getattr(self.contract.functions, name)(*args)

    def call(self, name: str, *args: Any, block: BlockId = "latest") -> Any:
        """Read any function by name at ``block``."""
        return getattr(self.contract.functions, name)(*args).call(block_identifier=block)

    def logs(self, event: str, from_block: BlockId, to_block: BlockId = "latest", **argument_filters: Any) -> list:
        """Decoded logs of ``event`` emitted by this contract, chunked (spec 0.4 item 4)."""
        return get_logs_chunked(getattr(self.contract.events, event), from_block, to_block, argument_filters or None)

    def _scan_start(self, from_block: BlockId | None) -> BlockId:
        """First block of a log scan: required on a public network (a scan from genesis in 200-block
        chunks would take hours), 0 by default on Anvil."""
        if from_block is not None:
            return from_block
        if int(self.w3.eth.chain_id) == 31337:
            return 0
        raise ValueError("pass from_block (the deployment's start_block) on a public network")

    def has_code(self) -> bool:
        return len(self.w3.eth.get_code(checksum(self.address))) > 0

    def codehash(self) -> str:
        """keccak256 of the deployed code (``EXTCODEHASH``)."""
        return Web3.to_hex(Web3.keccak(self.w3.eth.get_code(checksum(self.address))))


# ---------------------------------------------------------------------------
# AgentCreditBureau
# ---------------------------------------------------------------------------


class AcbContract(BoundContract):
    """``AgentCreditBureau`` (3.1)."""

    artefact = "AgentCreditBureau"

    # ---- reads ----
    def admin(self, *, block: BlockId = "latest") -> str:
        return lower(self.call("admin", block=block))

    def pending_admin(self, *, block: BlockId = "latest") -> str:
        return lower(self.call("pendingAdmin", block=block))

    def identity(self, *, block: BlockId = "latest") -> str:
        return lower(self.call("identity", block=block))

    def token(self, *, block: BlockId = "latest") -> str:
        return lower(self.call("token", block=block))

    def vault_code_hash(self, *, block: BlockId = "latest") -> str:
        return hex32(self.call("vaultCodeHash", block=block))

    def upgrade_delay(self, *, block: BlockId = "latest") -> int:
        return int(self.call("upgradeDelay", block=block))

    def max_upgrade_delay(self, *, block: BlockId = "latest") -> int:
        return int(self.call("MAX_UPGRADE_DELAY", block=block))

    def min_score(self, *, block: BlockId = "latest") -> int:
        return int(self.call("MIN_SCORE", block=block))

    def max_score(self, *, block: BlockId = "latest") -> int:
        return int(self.call("MAX_SCORE", block=block))

    def is_poster(self, address: str, *, block: BlockId = "latest") -> bool:
        return bool(self.call("isPoster", checksum(address), block=block))

    def is_vault(self, address: str, *, block: BlockId = "latest") -> bool:
        return bool(self.call("isVault", checksum(address), block=block))

    def vault_agent_id(self, address: str, *, block: BlockId = "latest") -> int:
        return int(self.call("vaultAgentId", checksum(address), block=block))

    def appeal_count(self, *, block: BlockId = "latest") -> int:
        return int(self.call("appealCount", block=block))

    def appeal_agent(self, appeal_id: int, *, block: BlockId = "latest") -> int:
        return int(self.call("appealAgent", int(appeal_id), block=block))

    def grade_of(self, agent_id: int, *, block: BlockId = "latest") -> tuple[Grade, int]:
        """Raw latest grade and its ``postedAt`` (``(NONE, 0)`` if never posted)."""
        grade, posted_at = self.call("gradeOf", int(agent_id), block=block)
        return Grade(int(grade)), int(posted_at)

    def effective_grade(self, agent_id: int, max_age: int = 0, *, block: BlockId = "latest") -> Grade:
        """Grade after the upgrade delay and the staleness rule (3.1)."""
        return Grade(int(self.call("effectiveGrade", int(agent_id), int(max_age), block=block)))

    def get_record(self, agent_id: int, *, block: BlockId = "latest") -> GradeRecord:
        raw = self.call("getRecord", int(agent_id), block=block)
        (grade, prev, score, posted, prev_posted, as_of, reasons, poster, model_id, config_hash, evidence_hash) = raw
        return GradeRecord(
            grade=Grade(int(grade)),
            prev_grade=Grade(int(prev)),
            score=int(score),
            posted_at=int(posted),
            prev_posted_at=int(prev_posted),
            as_of_block=int(as_of),
            reasons=unpack_reasons(int(reasons)),
            poster=lower(poster),
            model_id=model_id_text(model_id),
            config_hash=hex32(config_hash),
            evidence_hash=hex32(evidence_hash),
        )

    def reasons_of(self, agent_id: int, *, block: BlockId = "latest") -> tuple[int, int, int, int]:
        return tuple(int(r) for r in self.call("reasonsOf", int(agent_id), block=block))  # type: ignore[return-value]

    def grade_view(
        self, agent_id: int, *, max_age: int = 0, from_block: BlockId | None = None, block: BlockId = "latest"
    ) -> GradeView | None:
        """Appendix B :class:`~acb.types.GradeView` of the latest posting, or None if never graded.

        ``tx_hash`` and ``evidence_uri`` come from the latest ``GradePosted`` log at or after
        ``from_block`` (required on a public network: the deployment's ``start_block``).
        """
        record = self.get_record(agent_id, block=block)
        if record.grade == Grade.NONE:
            return None
        effective = self.effective_grade(agent_id, max_age, block=block)
        tx_hash: str | None = None
        evidence_uri = ""
        to_block = block if isinstance(block, int) else "latest"
        posted = self.logs("GradePosted", self._scan_start(from_block), to_block, agentId=int(agent_id))
        if posted:
            last = posted[-1]
            tx_hash = Web3.to_hex(last["transactionHash"])
            evidence_uri = str(last["args"]["evidenceURI"])
        return GradeView(
            agent_id=int(agent_id),
            grade=record.grade,
            effective_grade=effective,
            score=record.score,
            reasons=record.reason_list,
            model_id=record.model_id,
            config_hash=record.config_hash,
            evidence_hash=record.evidence_hash,
            as_of_block=record.as_of_block,
            posted_at=record.posted_at,
            tx_hash=tx_hash,
            evidence_uri=evidence_uri,
        )

    # ---- writes (call builders) ----
    def post_grade(
        self,
        agent_id: int,
        grade: int,
        score: int,
        reasons: Iterable[int],
        model_id: str | bytes,
        config_hash: str,
        evidence_hash: str,
        as_of_block: int,
        evidence_uri: str,
    ) -> Any:
        return self.contract.functions.postGrade(
            int(agent_id),
            int(grade),
            int(score),
            pack_reasons(reasons),
            model_id_bytes(model_id),
            b32(config_hash),
            b32(evidence_hash),
            int(as_of_block),
            str(evidence_uri),
        )

    def set_poster(self, poster: str, allowed: bool) -> Any:
        return self.contract.functions.setPoster(checksum(poster), bool(allowed))

    def set_vault_code_hash(self, code_hash: str) -> Any:
        return self.contract.functions.setVaultCodeHash(b32(code_hash))

    def set_upgrade_delay(self, delay: int) -> Any:
        return self.contract.functions.setUpgradeDelay(int(delay))

    def transfer_admin(self, new_admin: str) -> Any:
        return self.contract.functions.transferAdmin(checksum(new_admin))

    def accept_admin(self) -> Any:
        return self.contract.functions.acceptAdmin()

    def register_vault(self, vault: str) -> Any:
        return self.contract.functions.registerVault(checksum(vault))

    def file_appeal(self, agent_id: int, appeal_hash: str, uri: str) -> Any:
        return self.contract.functions.fileAppeal(int(agent_id), b32(appeal_hash), str(uri))

    def resolve_appeal(self, appeal_id: int, upheld: bool, evidence_hash: str) -> Any:
        return self.contract.functions.resolveAppeal(int(appeal_id), bool(upheld), b32(evidence_hash))


# ---------------------------------------------------------------------------
# TierVault
# ---------------------------------------------------------------------------


class VaultContract(BoundContract):
    """``TierVault`` (3.2)."""

    artefact = "TierVault"

    # ---- simple reads ----
    def token(self, *, block: BlockId = "latest") -> str:
        return lower(self.call("token", block=block))

    def bureau(self, *, block: BlockId = "latest") -> str:
        return lower(self.call("bureau", block=block))

    def identity(self, *, block: BlockId = "latest") -> str:
        return lower(self.call("identity", block=block))

    def owner(self, *, block: BlockId = "latest") -> str:
        return lower(self.call("owner", block=block))

    def pending_owner(self, *, block: BlockId = "latest") -> str:
        return lower(self.call("pendingOwner", block=block))

    def agent(self, *, block: BlockId = "latest") -> str:
        return lower(self.call("agent", block=block))

    def autonomy_operator(self, *, block: BlockId = "latest") -> str:
        return lower(self.call("autonomyOperator", block=block))

    def agent_id(self, *, block: BlockId = "latest") -> int:
        return int(self.call("agentId", block=block))

    def tier(self, *, block: BlockId = "latest") -> int:
        return int(self.call("tier", block=block))

    def tier_before_freeze(self, *, block: BlockId = "latest") -> int:
        return int(self.call("tierBeforeFreeze", block=block))

    def max_auto_tier(self, *, block: BlockId = "latest") -> int:
        return int(self.call("maxAutoTier", block=block))

    def last_tier_change_at(self, *, block: BlockId = "latest") -> int:
        return int(self.call("lastTierChangeAt", block=block))

    def promotion_cooldown(self, *, block: BlockId = "latest") -> int:
        return int(self.call("promotionCooldown", block=block))

    def window_seconds(self, *, block: BlockId = "latest") -> int:
        return int(self.call("windowSeconds", block=block))

    def max_grade_age(self, *, block: BlockId = "latest") -> int:
        return int(self.call("maxGradeAge", block=block))

    def dispute_window(self, *, block: BlockId = "latest") -> int:
        return int(self.call("disputeWindow", block=block))

    def clean_since_change(self, *, block: BlockId = "latest") -> int:
        return int(self.call("cleanSinceChange", block=block))

    def violation_points_since_change(self, *, block: BlockId = "latest") -> int:
        return int(self.call("violationPointsSinceChange", block=block))

    def reserve_floor(self, *, block: BlockId = "latest") -> int:
        return int(self.call("reserveFloor", block=block))

    def escrow_locked(self, *, block: BlockId = "latest") -> int:
        return int(self.call("escrowLocked", block=block))

    def min_clean_amount(self, *, block: BlockId = "latest") -> int:
        return int(self.call("minCleanAmount", block=block))

    def completed_with(self, payee_agent_id: int, *, block: BlockId = "latest") -> int:
        return int(self.call("completedWith", int(payee_agent_id), block=block))

    def mandate_payee(self, mandate_id: str, payee_agent_id: int, *, block: BlockId = "latest") -> str:
        return lower(self.call("mandatePayee", b32(mandate_id), int(payee_agent_id), block=block))

    def ring_size(self, *, block: BlockId = "latest") -> int:
        return int(self.call("RING_SIZE", block=block))

    def max_mandate_payees(self, *, block: BlockId = "latest") -> int:
        return int(self.call("MAX_MANDATE_PAYEES", block=block))

    def max_mandate_duration(self, *, block: BlockId = "latest") -> int:
        return int(self.call("MAX_MANDATE_DURATION", block=block))

    def max_dispute_window(self, *, block: BlockId = "latest") -> int:
        return int(self.call("MAX_DISPUTE_WINDOW", block=block))

    def is_frozen(self, *, block: BlockId = "latest") -> bool:
        return bool(self.call("isFrozen", block=block))

    def free_balance(self, *, block: BlockId = "latest") -> int:
        return int(self.call("freeBalance", block=block))

    def spent_in_window(self, *, block: BlockId = "latest") -> tuple[int, int]:
        spent, count = self.call("spentInWindow", block=block)
        return int(spent), int(count)

    # ---- struct reads ----
    def tier_config(self, t: int, *, block: BlockId = "latest") -> TierConfig:
        """Appendix B :class:`~acb.types.TierConfig` of tier ``t`` (0..3)."""
        return tier_config_from_chain(self.call("tierConfig", int(t), block=block))

    def get_order(self, order_id: str, *, block: BlockId = "latest") -> OrderInfo:
        raw = self.call("getOrder", b32(order_id), block=block)
        state, outcome, decision, created, deadline, claimed, amount, payee_id, payee, mandate_id, delivery = raw
        return OrderInfo(
            state=OrderState(int(state)),
            outcome=Outcome(int(outcome)),
            decision=Decision(int(decision)),
            created_at=int(created),
            deadline=int(deadline),
            claimed_at=int(claimed),
            amount=int(amount),
            payee_agent_id=int(payee_id),
            payee=lower(payee),
            mandate_id=hex32(mandate_id),
            delivery_hash=hex32(delivery),
        )

    def get_mandate(self, mandate_id: str, *, block: BlockId = "latest") -> MandateInfo:
        raw = self.call("getMandate", b32(mandate_id), block=block)
        return MandateInfo(*(int(v) if not isinstance(v, bool) else v for v in raw))

    def get_approval(self, order_id: str, *, block: BlockId = "latest") -> ApprovalInfo:
        payee_id, expected, amount, expiry, escrow_deadline, as_escrow, used = self.call(
            "getApproval", b32(order_id), block=block
        )
        return ApprovalInfo(
            payee_agent_id=int(payee_id),
            expected_payee=lower(expected),
            amount=int(amount),
            expiry=int(expiry),
            escrow_deadline=int(escrow_deadline),
            as_escrow=bool(as_escrow),
            used=bool(used),
        )

    def status(self, *, block: BlockId = "latest") -> VaultStatus:
        raw = self.call("status", block=block)
        return VaultStatus(*(bool(v) if isinstance(v, bool) else int(v) for v in raw))

    def state(self, *, block: BlockId = "latest") -> VaultState:
        """Appendix B :class:`~acb.types.VaultState`, every value read at the same block.

        While frozen (tier 255) ``config`` is all zero: nothing autonomous is allowed.
        """
        number = block if isinstance(block, int) else int(self.w3.eth.get_block(block)["number"])
        s = self.status(block=number)
        config = ZERO_TIER_CONFIG if s.frozen or s.tier == TIER_FROZEN else self.tier_config(s.tier, block=number)
        return VaultState(
            address=self.address,
            agent_id=self.agent_id(block=number),
            owner=self.owner(block=number),
            agent=self.agent(block=number),
            tier=s.tier,
            frozen=s.frozen,
            tier_before_freeze=s.tier_before_freeze,
            max_auto_tier=s.max_auto_tier,
            config=config,
            balance=s.balance,
            escrow_locked=s.escrow_locked,
            reserve_floor=s.reserve_floor,
            window_spent=s.spent,
            window_count=s.count,
            clean_since_change=s.clean,
            violation_points=s.points,
            last_tier_change_at=s.last_tier_change_at,
            max_grade_age=self.max_grade_age(block=number),
            dispute_window=self.dispute_window(block=number),
        )

    def mandate_view(
        self, mandate_id: str, *, from_block: BlockId | None = None, block: BlockId = "latest"
    ) -> MandateView | None:
        """Appendix B :class:`~acb.types.MandateView`: the stored mandate plus its ``MandateRegistered`` log.

        Returns None when the mandate does not exist. ``from_block`` is required on a public network
        (the deployment's ``start_block``).
        """
        info = self.get_mandate(mandate_id, block=block)
        if not info.exists:
            return None
        to_block = block if isinstance(block, int) else "latest"
        found = self.logs("MandateRegistered", self._scan_start(from_block), to_block, mandateId=b32(mandate_id))
        if not found:
            return None
        log = found[-1]
        args = log["args"]
        return MandateView(
            mandate_id=hex32(mandate_id),
            vault=self.address,
            payee_agent_ids=tuple(int(x) for x in args["payeeAgentIds"]),
            payee_wallets=tuple(lower(a) for a in args["payeeWallets"]),
            max_per_tx=info.max_per_tx,
            max_total=info.max_total,
            spent=info.spent,
            valid_until=info.valid_until,
            recurring=info.recurring,
            active=info.active,
            purpose_hash=hex32(args["purposeHash"]),
            register_tx=Web3.to_hex(log["transactionHash"]),
        )

    # ---- writes: mandates ----
    def register_mandate(
        self,
        mandate_id: str,
        payee_agent_ids: Sequence[int],
        payee_wallets: Sequence[str],
        max_per_tx: int,
        max_total: int,
        valid_until: int,
        recurring: bool,
        purpose_hash: str,
    ) -> Any:
        return self.contract.functions.registerMandate(
            b32(mandate_id),
            [int(x) for x in payee_agent_ids],
            [checksum(a) for a in payee_wallets],
            int(max_per_tx),
            int(max_total),
            int(valid_until),
            bool(recurring),
            b32(purpose_hash),
        )

    def revoke_mandate(self, mandate_id: str) -> Any:
        return self.contract.functions.revokeMandate(b32(mandate_id))

    # ---- writes: outflows ----
    def pay(self, params: OutflowParams) -> Any:
        return self.contract.functions.pay(params.as_tuple())

    def escrow(self, params: OutflowParams) -> Any:
        return self.contract.functions.escrow(params.as_tuple())

    def claim_delivered(self, order_id: str, delivery_hash: str) -> Any:
        return self.contract.functions.claimDelivered(b32(order_id), b32(delivery_hash))

    def release(self, order_id: str) -> Any:
        return self.contract.functions.release(b32(order_id))

    def refund(self, order_id: str, reason_code: int) -> Any:
        return self.contract.functions.refund(b32(order_id), int(reason_code))

    def owner_approve(
        self,
        order_id: str,
        payee_agent_id: int,
        expected_payee: str,
        amount: int,
        expiry: int,
        as_escrow: bool,
        escrow_deadline: int,
    ) -> Any:
        return self.contract.functions.ownerApprove(
            b32(order_id),
            int(payee_agent_id),
            checksum(expected_payee),
            int(amount),
            int(expiry),
            bool(as_escrow),
            int(escrow_deadline),
        )

    def revoke_approval(self, order_id: str) -> Any:
        return self.contract.functions.revokeApproval(b32(order_id))

    def execute_approved(self, order_id: str, invoice_hash: str, decision_hash: str) -> Any:
        return self.contract.functions.executeApproved(b32(order_id), b32(invoice_hash), b32(decision_hash))

    # ---- writes: recording ----
    def record_decision(self, params: DecisionParams) -> Any:
        return self.contract.functions.recordDecision(params.as_tuple())

    def record_delivery(self, order_id: str, outcome: int, delivery_hash: str) -> Any:
        return self.contract.functions.recordDelivery(b32(order_id), int(outcome), b32(delivery_hash))

    def record_violation(self, kind: int, severity: int, evidence_hash: str) -> Any:
        return self.contract.functions.recordViolation(int(kind), int(severity), b32(evidence_hash))

    # ---- writes: tiers ----
    def set_tier(self, new_tier: int, reason_code: int, evidence_hash: str) -> Any:
        return self.contract.functions.setTier(int(new_tier), int(reason_code), b32(evidence_hash))

    def freeze(self, reason_code: int, evidence_hash: str) -> Any:
        return self.contract.functions.freeze(int(reason_code), b32(evidence_hash))

    def unfreeze(self, to_tier: int, reason_code: int, new_agent: str | None = None) -> Any:
        return self.contract.functions.unfreeze(int(to_tier), int(reason_code), checksum(new_agent or ZERO_ADDRESS))

    # ---- writes: owner administration ----
    def set_tier_config(self, t: int, cfg: TierConfig) -> Any:
        return self.contract.functions.setTierConfig(int(t), tier_config_tuple(cfg))

    def set_reserve_floor(self, value: int) -> Any:
        return self.contract.functions.setReserveFloor(int(value))

    def set_min_clean_amount(self, value: int) -> Any:
        return self.contract.functions.setMinCleanAmount(int(value))

    def set_max_auto_tier(self, value: int) -> Any:
        return self.contract.functions.setMaxAutoTier(int(value))

    def set_promotion_cooldown(self, value: int) -> Any:
        return self.contract.functions.setPromotionCooldown(int(value))

    def set_max_grade_age(self, value: int) -> Any:
        return self.contract.functions.setMaxGradeAge(int(value))

    def set_window_seconds(self, value: int) -> Any:
        return self.contract.functions.setWindowSeconds(int(value))

    def set_dispute_window(self, value: int) -> Any:
        return self.contract.functions.setDisputeWindow(int(value))

    def set_agent(self, new_agent: str) -> Any:
        return self.contract.functions.setAgent(checksum(new_agent))

    def set_autonomy_operator(self, new_operator: str) -> Any:
        return self.contract.functions.setAutonomyOperator(checksum(new_operator))

    def withdraw(self, to: str, amount: int) -> Any:
        return self.contract.functions.withdraw(checksum(to), int(amount))

    def transfer_ownership(self, new_owner: str) -> Any:
        return self.contract.functions.transferOwnership(checksum(new_owner))

    def accept_ownership(self) -> Any:
        return self.contract.functions.acceptOwnership()


# ---------------------------------------------------------------------------
# ERC-8004 Identity
# ---------------------------------------------------------------------------


class IdentityContract(BoundContract):
    """ERC-8004 Identity registry (official v2.0.0 on Base Sepolia, ``IdentityRegistryLite`` on Anvil)."""

    artefact = "IdentityRegistryLite"

    # ---- reads ----
    def name(self, *, block: BlockId = "latest") -> str:
        return str(self.call("name", block=block))

    def symbol(self, *, block: BlockId = "latest") -> str:
        return str(self.call("symbol", block=block))

    def get_version(self, *, block: BlockId = "latest") -> str:
        return str(self.call("getVersion", block=block))

    def balance_of(self, owner: str, *, block: BlockId = "latest") -> int:
        return int(self.call("balanceOf", checksum(owner), block=block))

    def owner_of(self, agent_id: int, *, block: BlockId = "latest") -> str:
        """Owner of the identity NFT; reverts ``ERC721NonexistentToken`` for an unknown id."""
        return lower(self.call("ownerOf", int(agent_id), block=block))

    def try_owner_of(self, agent_id: int, *, block: BlockId = "latest") -> str | None:
        """Like :meth:`owner_of` but None for a nonexistent agent."""
        try:
            return self.owner_of(agent_id, block=block)
        except Exception:  # noqa: BLE001 - revert for an unknown token
            return None

    def get_approved(self, agent_id: int, *, block: BlockId = "latest") -> str:
        return lower(self.call("getApproved", int(agent_id), block=block))

    def is_approved_for_all(self, owner: str, operator: str, *, block: BlockId = "latest") -> bool:
        return bool(self.call("isApprovedForAll", checksum(owner), checksum(operator), block=block))

    def token_uri(self, agent_id: int, *, block: BlockId = "latest") -> str:
        return str(self.call("tokenURI", int(agent_id), block=block))

    def supports_interface(self, interface_id: str | bytes, *, block: BlockId = "latest") -> bool:
        raw = interface_id if isinstance(interface_id, bytes) else bytes.fromhex(interface_id.removeprefix("0x"))
        return bool(self.call("supportsInterface", raw, block=block))

    def get_metadata(self, agent_id: int, key: str, *, block: BlockId = "latest") -> bytes:
        return bytes(self.call("getMetadata", int(agent_id), str(key), block=block))

    def get_agent_wallet(self, agent_id: int, *, block: BlockId = "latest") -> str:
        """The agent's registered wallet (may be the zero address, spec 0.4)."""
        return lower(self.call("getAgentWallet", int(agent_id), block=block))

    def is_authorized_or_owner(self, spender: str, agent_id: int, *, block: BlockId = "latest") -> bool:
        return bool(self.call("isAuthorizedOrOwner", checksum(spender), int(agent_id), block=block))

    def eip712_domain(self, *, block: BlockId = "latest") -> tuple:
        return tuple(self.call("eip712Domain", block=block))

    # ---- writes ----
    def register(self, agent_uri: str | None = None, metadata: Sequence[tuple[str, bytes]] | None = None) -> Any:
        """``register()``, ``register(string)`` or ``register(string,(string,bytes)[])``."""
        if metadata is not None:
            fn = self.contract.get_function_by_signature("register(string,(string,bytes)[])")
            return fn(str(agent_uri or ""), [(str(k), bytes(v)) for k, v in metadata])
        if agent_uri is not None:
            return self.contract.get_function_by_signature("register(string)")(str(agent_uri))
        return self.contract.get_function_by_signature("register()")()

    def approve(self, to: str, agent_id: int) -> Any:
        return self.contract.functions.approve(checksum(to), int(agent_id))

    def set_approval_for_all(self, operator: str, approved: bool) -> Any:
        return self.contract.functions.setApprovalForAll(checksum(operator), bool(approved))

    def transfer_from(self, from_: str, to: str, agent_id: int) -> Any:
        return self.contract.functions.transferFrom(checksum(from_), checksum(to), int(agent_id))

    def safe_transfer_from(self, from_: str, to: str, agent_id: int, data: bytes | None = None) -> Any:
        if data is None:
            fn = self.contract.get_function_by_signature("safeTransferFrom(address,address,uint256)")
            return fn(checksum(from_), checksum(to), int(agent_id))
        fn = self.contract.get_function_by_signature("safeTransferFrom(address,address,uint256,bytes)")
        return fn(checksum(from_), checksum(to), int(agent_id), bytes(data))

    def set_metadata(self, agent_id: int, key: str, value: bytes) -> Any:
        return self.contract.functions.setMetadata(int(agent_id), str(key), bytes(value))

    def set_agent_uri(self, agent_id: int, new_uri: str) -> Any:
        return self.contract.functions.setAgentURI(int(agent_id), str(new_uri))

    def set_agent_wallet(self, agent_id: int, new_wallet: str, deadline: int, signature: bytes | str) -> Any:
        sig = signature if isinstance(signature, bytes) else bytes.fromhex(signature.removeprefix("0x"))
        return self.contract.functions.setAgentWallet(int(agent_id), checksum(new_wallet), int(deadline), sig)

    def unset_agent_wallet(self, agent_id: int) -> Any:
        return self.contract.functions.unsetAgentWallet(int(agent_id))

    # ---- helpers ----
    def agent_id_from_result(self, result: Any) -> int:
        """The ``agentId`` of the ``Registered`` log in a :class:`~acb.types.TxResult` of ``register``."""
        for log in result.logs:
            if log.get("event") == "Registered" and log.get("address") == self.address:
                return int(log["args"]["agentId"])
        raise LookupError("no Registered event from this registry in the transaction")


# ---------------------------------------------------------------------------
# ERC-8004 Reputation
# ---------------------------------------------------------------------------


class ReputationContract(BoundContract):
    """ERC-8004 Reputation registry (official v2.0.0 on Base Sepolia, ``ReputationRegistryLite`` on Anvil)."""

    artefact = "ReputationRegistryLite"

    # ---- reads ----
    def get_version(self, *, block: BlockId = "latest") -> str:
        return str(self.call("getVersion", block=block))

    def get_identity_registry(self, *, block: BlockId = "latest") -> str:
        return lower(self.call("getIdentityRegistry", block=block))

    def get_last_index(self, agent_id: int, client: str, *, block: BlockId = "latest") -> int:
        return int(self.call("getLastIndex", int(agent_id), checksum(client), block=block))

    def read_feedback(self, agent_id: int, client: str, feedback_index: int, *, block: BlockId = "latest") -> dict:
        value, decimals, tag1, tag2, revoked = self.call(
            "readFeedback", int(agent_id), checksum(client), int(feedback_index), block=block
        )
        return {
            "value": int(value),
            "valueDecimals": int(decimals),
            "tag1": tag1,
            "tag2": tag2,
            "isRevoked": bool(revoked),
        }

    def get_summary(
        self, agent_id: int, clients: Sequence[str], tag1: str = "", tag2: str = "", *, block: BlockId = "latest"
    ) -> tuple[int, int, int]:
        """``(count, summaryValue, summaryValueDecimals)``; reverts on an empty client list (spec 0.4)."""
        count, value, decimals = self.call(
            "getSummary", int(agent_id), [checksum(c) for c in clients], str(tag1), str(tag2), block=block
        )
        return int(count), int(value), int(decimals)

    def read_all_feedback(
        self,
        agent_id: int,
        clients: Sequence[str],
        tag1: str = "",
        tag2: str = "",
        include_revoked: bool = False,
        *,
        block: BlockId = "latest",
    ) -> list[dict]:
        clients_out, indexes, values, decimals, tags1, tags2, revoked = self.call(
            "readAllFeedback",
            int(agent_id),
            [checksum(c) for c in clients],
            str(tag1),
            str(tag2),
            bool(include_revoked),
            block=block,
        )
        return [
            {
                "client": lower(c),
                "feedbackIndex": int(i),
                "value": int(v),
                "valueDecimals": int(d),
                "tag1": t1,
                "tag2": t2,
                "isRevoked": bool(r),
            }
            for c, i, v, d, t1, t2, r in zip(clients_out, indexes, values, decimals, tags1, tags2, revoked, strict=True)
        ]

    def get_response_count(
        self, agent_id: int, client: str, feedback_index: int, responders: Sequence[str], *, block: BlockId = "latest"
    ) -> int:
        return int(
            self.call(
                "getResponseCount",
                int(agent_id),
                checksum(client),
                int(feedback_index),
                [checksum(r) for r in responders],
                block=block,
            )
        )

    def get_clients(self, agent_id: int, *, block: BlockId = "latest") -> list[str]:
        return [lower(a) for a in self.call("getClients", int(agent_id), block=block)]

    # ---- writes ----
    def give_feedback(
        self,
        agent_id: int,
        value: int,
        value_decimals: int,
        tag1: str,
        tag2: str,
        endpoint: str,
        feedback_uri: str,
        feedback_hash: str,
    ) -> Any:
        return self.contract.functions.giveFeedback(
            int(agent_id),
            int(value),
            int(value_decimals),
            str(tag1),
            str(tag2),
            str(endpoint),
            str(feedback_uri),
            b32(feedback_hash),
        )

    def revoke_feedback(self, agent_id: int, feedback_index: int) -> Any:
        return self.contract.functions.revokeFeedback(int(agent_id), int(feedback_index))

    def append_response(
        self, agent_id: int, client: str, feedback_index: int, response_uri: str, response_hash: str
    ) -> Any:
        return self.contract.functions.appendResponse(
            int(agent_id), checksum(client), int(feedback_index), str(response_uri), b32(response_hash)
        )


# ---------------------------------------------------------------------------
# ERC-20 (MockUSDC on Anvil, Circle test USDC on Base Sepolia)
# ---------------------------------------------------------------------------

_BLACKLIST_ABI = [
    {
        "name": "isBlacklisted",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "bool"}],
    }
]


class Erc20Contract(BoundContract):
    """ERC-20 token. ``mint``/``minter`` exist only on MockUSDC; ``is_blacklisted`` only on Circle USDC."""

    artefact = "MockUSDC"

    def name(self, *, block: BlockId = "latest") -> str:
        return str(self.call("name", block=block))

    def symbol(self, *, block: BlockId = "latest") -> str:
        return str(self.call("symbol", block=block))

    def decimals(self, *, block: BlockId = "latest") -> int:
        return int(self.call("decimals", block=block))

    def total_supply(self, *, block: BlockId = "latest") -> int:
        return int(self.call("totalSupply", block=block))

    def balance_of(self, owner: str, *, block: BlockId = "latest") -> int:
        return int(self.call("balanceOf", checksum(owner), block=block))

    def allowance(self, owner: str, spender: str, *, block: BlockId = "latest") -> int:
        return int(self.call("allowance", checksum(owner), checksum(spender), block=block))

    def minter(self, *, block: BlockId = "latest") -> str:
        return lower(self.call("minter", block=block))

    def is_blacklisted(self, account: str, *, block: BlockId = "latest") -> bool | None:
        """Circle FiatToken blacklist (6.2 C11); None when the token has no such function."""
        token = self.w3.eth.contract(address=checksum(self.address), abi=_BLACKLIST_ABI)
        try:
            return bool(token.functions.isBlacklisted(checksum(account)).call(block_identifier=block))
        except Exception:  # noqa: BLE001 - MockUSDC has no blacklist
            return None

    def transfer(self, to: str, amount: int) -> Any:
        return self.contract.functions.transfer(checksum(to), int(amount))

    def transfer_from(self, from_: str, to: str, amount: int) -> Any:
        return self.contract.functions.transferFrom(checksum(from_), checksum(to), int(amount))

    def approve(self, spender: str, amount: int) -> Any:
        return self.contract.functions.approve(checksum(spender), int(amount))

    def mint(self, to: str, amount: int) -> Any:
        """MockUSDC only (by its minter)."""
        return self.contract.functions.mint(checksum(to), int(amount))


# ---------------------------------------------------------------------------
# everything of one deployment
# ---------------------------------------------------------------------------


@dataclass
class ChainContracts:
    """The contracts of one :class:`~acb.types.Deployments`, bound to one ``Web3``."""

    w3: Any
    acb: AcbContract
    identity: IdentityContract
    reputation: ReputationContract
    usdc: Erc20Contract
    vaults: dict[str, VaultContract]

    def vault(self, name: str) -> VaultContract:
        """The vault ``name`` of the deployment (``wallet``, ``beacon``, ...)."""
        if name not in self.vaults:
            raise KeyError(f"no vault {name!r} in the deployment")
        return self.vaults[name]

    def vault_at(self, address: str) -> VaultContract:
        """A wrapper for any vault address (not necessarily in the deployment)."""
        return VaultContract(self.w3, address)


def bind(w3: Any, dep: Deployments) -> ChainContracts:
    """Bind every contract of ``dep`` to ``w3``."""
    c = dep.contracts
    return ChainContracts(
        w3=w3,
        acb=AcbContract(w3, c["AgentCreditBureau"].address),
        identity=IdentityContract(w3, c["IdentityRegistry"].address),
        reputation=ReputationContract(w3, c["ReputationRegistry"].address),
        usdc=Erc20Contract(w3, c["USDC"].address),
        vaults={name: VaultContract(w3, v.address) for name, v in dep.vaults.items()},
    )


__all__ = [
    "ZERO_ADDRESS",
    "ZERO_HASH",
    "ZERO_TIER_CONFIG",
    "AcbContract",
    "ApprovalInfo",
    "BoundContract",
    "ChainContracts",
    "DecisionParams",
    "Erc20Contract",
    "GradeRecord",
    "IdentityContract",
    "MandateInfo",
    "OrderInfo",
    "OutflowParams",
    "ReputationContract",
    "VaultContract",
    "VaultInit",
    "VaultStatus",
    "b32",
    "bind",
    "checksum",
    "hex32",
    "lower",
    "model_id_bytes",
    "model_id_text",
    "pack_reasons",
    "tier_config_from_chain",
    "tier_config_tuple",
    "unpack_reasons",
]
