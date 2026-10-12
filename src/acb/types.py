"""Shared types of every package (spec Appendix B; frozen 12 Oct 18:00 UTC+8).

Rules (spec 0.1, 15.3):

- Every model is a frozen pydantic v2 model with ``extra="forbid"``. Python attributes are snake_case;
  JSON on the wire is camelCase (``model_dump(by_alias=True)`` or :meth:`AcbModel.to_wire`). Input is
  accepted under either name. The event envelope :class:`Event` is the only model that keeps
  snake_case keys in JSON.
- Money is ``int`` base units (USDC has 6 decimals). :data:`Amount` serialises as a decimal string in
  JSON. Floats and booleans are rejected.
- Agent ids are ``int`` in Python and decimal strings in JSON (:data:`AgentId`), as in the invoice
  (9.2), bundle (4.8), deployments (13.5) and event (10.3) examples.
- Addresses and hashes are lowercase ``0x`` hex. Mixed-case (checksummed) input is lowercased.
- Signed wire objects (``Invoice``/``SignedInvoice``/``SignedDelivery`` ``from_wire``) accept only their canonical
  form, so the hash of the received object, of the model and of the stored file always agree.

Changes to this module go through ``docs/design/changes.md``.
"""

from __future__ import annotations

import re
from enum import IntEnum, StrEnum
from typing import Annotated, Any, Literal

from annotated_types import Ge
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StrictInt,
    StringConstraints,
    model_validator,
)
from pydantic.alias_generators import to_camel

# ---------------------------------------------------------------------------
# scalar aliases
# ---------------------------------------------------------------------------

_DECIMAL = re.compile(r"^[0-9]+$")
_HEX_ANYCASE = re.compile(r"^0[xX][0-9a-fA-F]*$")


def _strict_uint(value: Any) -> Any:
    """Accept an int (not bool) or a decimal string; reject floats, bools and anything else."""
    if isinstance(value, bool):
        raise ValueError("booleans are not integers here")  # noqa: TRY004 - pydantic needs ValueError
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _DECIMAL.fullmatch(value):
        return int(value)
    raise ValueError("expected an integer or a decimal string (floats are forbidden)")


def _lower_hex(value: Any) -> Any:
    """Lowercase a 0x-prefixed hex string (checksummed addresses become lowercase)."""
    if isinstance(value, str) and _HEX_ANYCASE.fullmatch(value):
        return "0x" + value[2:].lower()
    return value


Amount = Annotated[int, BeforeValidator(_strict_uint), Ge(0), PlainSerializer(str, when_used="json")]
"""Token base units: ``int`` in Python, decimal string in JSON."""

AgentId = Annotated[int, BeforeValidator(_strict_uint), Ge(0), PlainSerializer(str, when_used="json")]
"""ERC-8004 agent id (uint256): ``int`` in Python, decimal string in JSON."""

HexStr = Annotated[str, BeforeValidator(_lower_hex), StringConstraints(pattern=r"^0x[0-9a-f]*$")]
Hex32 = Annotated[str, BeforeValidator(_lower_hex), StringConstraints(pattern=r"^0x[0-9a-f]{64}$")]
Address = Annotated[str, BeforeValidator(_lower_hex), StringConstraints(pattern=r"^0x[0-9a-f]{40}$")]

WireInt = Annotated[StrictInt, Ge(0)]
"""A plain JSON integer in a signed wire object (invoice, delivery): no bool, float or string."""


def _require_canonical(model_wire: dict[str, Any], raw: dict[str, Any], what: str) -> None:
    """Reject ``raw`` unless it is byte-for-byte the canonical wire form the parsed model writes back.

    A signed wire object is hashed over its raw fields. If a non-canonical spelling (checksummed
    address, numeric amount, snake_case key, ...) parsed to the same model, the stored file, the
    model's hash and the signature would disagree (review fix, changes.md D-12).
    """
    from acb.canonical import canonical_bytes  # local import: canonical imports nothing from here

    try:
        same = canonical_bytes(model_wire) == canonical_bytes(raw)
    except TypeError as exc:
        raise ValueError(f"non-canonical {what}: {exc}") from None
    if not same:
        raise ValueError(f"non-canonical {what}: fields must be exactly the canonical wire form")


class AcbModel(BaseModel):
    """Base of every shared model: frozen, no extra fields, camelCase on the wire."""

    model_config = ConfigDict(frozen=True, extra="forbid", alias_generator=to_camel, populate_by_name=True)

    def to_wire(self) -> dict[str, Any]:
        """JSON-ready dict with camelCase keys and amounts as strings."""
        return self.model_dump(mode="json", by_alias=True)


# ---------------------------------------------------------------------------
# enums (spec 0.2; never renumber)
# ---------------------------------------------------------------------------


class Grade(IntEnum):
    NONE = 0
    NEW = 1
    D = 2
    C = 3
    B = 4
    A = 5


class Decision(IntEnum):
    NONE = 0
    PAY = 1
    PAY_CAPPED = 2
    ESCROW = 3
    REFUSE = 4
    NEEDS_OWNER_APPROVAL = 5
    # 6 reserved (retired CREDIT)


class OrderState(IntEnum):
    NONE = 0
    PAID = 1
    ESCROWED = 2
    RELEASED = 3
    REFUNDED = 4
    REFUSED = 5
    # 6 reserved (retired CREDIT_DRAW)


class Outcome(IntEnum):
    NONE = 0
    ON_TIME = 1
    LATE = 2
    FAILED = 3


class ViolationKind(IntEnum):
    LIMIT_PROPOSAL = 1
    DUPLICATE_PROPOSAL = 2
    FROZEN_PROPOSAL = 3
    PROVENANCE_AMOUNT = 4
    PROVENANCE_PAYEE = 5
    INJECTION_FOLLOWED = 6
    PRIVILEGE_ESCALATION = 7
    DRAIN_ATTEMPT = 8
    UNKNOWN_TOOL = 9
    SECRET_EXFILTRATION = 10
    REPEATED_VIOLATIONS = 11


class ToolAction(StrEnum):
    PAY = "PAY"
    PAY_CAPPED = "PAY_CAPPED"
    ESCROW = "ESCROW"
    REFUSE = "REFUSE"
    REQUEST_OWNER_APPROVAL = "REQUEST_OWNER_APPROVAL"


TOOL_ACTION_TO_DECISION: dict[ToolAction, Decision] = {
    ToolAction.PAY: Decision.PAY,
    ToolAction.PAY_CAPPED: Decision.PAY_CAPPED,
    ToolAction.ESCROW: Decision.ESCROW,
    ToolAction.REQUEST_OWNER_APPROVAL: Decision.NEEDS_OWNER_APPROVAL,
    ToolAction.REFUSE: Decision.REFUSE,
}
"""The mapping table of spec 8.3 (used by C18 and MockLLM)."""

DECISION_TO_TOOL_ACTION: dict[Decision, ToolAction] = {d: a for a, d in TOOL_ACTION_TO_DECISION.items()}

STRICTNESS: dict[Decision, int] = {
    Decision.PAY: 0,
    Decision.PAY_CAPPED: 1,
    Decision.ESCROW: 2,
    Decision.NEEDS_OWNER_APPROVAL: 3,
    Decision.REFUSE: 4,
}
"""Scale of C18: PAY < PAY_CAPPED < ESCROW < NEEDS_OWNER_APPROVAL < REFUSE."""

GRADE_RANK: dict[Grade, int] = {Grade.D: 0, Grade.C: 1, Grade.NONE: 2, Grade.NEW: 2, Grade.B: 3, Grade.A: 4}
"""Grade order worst to best (``AcbTypes.rank``): D < C < NEW = NONE < B < A."""

TIER_FROZEN = 255

VIOLATION_SEVERITY_MINOR = 1
VIOLATION_SEVERITY_SERIOUS = 2
VIOLATION_SEVERITY_SEVERE = 3


def grade_mask(*grades: Grade) -> int:
    """Bit mask of grades as used on-chain: ``1 << grade`` (NONE 0x01 ... A 0x20)."""
    mask = 0
    for g in grades:
        mask |= 1 << int(g)
    return mask


def mask_has(mask: int, grade: Grade) -> bool:
    """True if ``grade`` is in the on-chain grade ``mask``."""
    return bool(mask & (1 << int(grade)))


def stricter(a: Decision, b: Decision) -> Decision:
    """The stricter of two decisions on the :data:`STRICTNESS` scale (ties keep ``a``)."""
    return b if STRICTNESS[b] > STRICTNESS[a] else a


# ---------------------------------------------------------------------------
# chain views
# ---------------------------------------------------------------------------


class TierConfig(AcbModel):
    per_tx: Amount
    window_cap: Amount
    new_payee_cap: Amount
    max_tx: int
    pay_mask: int
    escrow_mask: int
    min_clean_to_promote: int


class VaultState(AcbModel):
    address: Address
    agent_id: AgentId
    owner: Address
    agent: Address
    tier: int
    frozen: bool
    tier_before_freeze: int
    max_auto_tier: int
    config: TierConfig
    balance: Amount
    escrow_locked: Amount
    reserve_floor: Amount
    window_spent: Amount
    window_count: int
    clean_since_change: int
    violation_points: int
    last_tier_change_at: int
    max_grade_age: int
    dispute_window: int


class MandateView(AcbModel):
    mandate_id: Hex32
    vault: Address
    payee_agent_ids: tuple[AgentId, ...]
    payee_wallets: tuple[Address, ...]
    max_per_tx: Amount
    max_total: Amount
    spent: Amount
    valid_until: int
    recurring: bool
    active: bool
    purpose_hash: Hex32
    register_tx: Hex32 | None


class PayeeView(AcbModel):
    agent_id: AgentId
    grade: Grade
    score: int | None
    posted_at: int | None
    live_wallet: Address | None
    pinned_wallet: Address | None
    display_name: str
    registered_at: int | None
    unsecured_exposure: Amount
    payment_history: tuple[Amount, ...]


class GradeView(AcbModel):
    agent_id: AgentId
    grade: Grade
    effective_grade: Grade
    score: int
    reasons: tuple[int, ...]
    model_id: str
    config_hash: Hex32
    evidence_hash: Hex32
    as_of_block: int
    posted_at: int
    tx_hash: Hex32 | None
    evidence_uri: str


# ---------------------------------------------------------------------------
# invoices, quotes, deliveries
# ---------------------------------------------------------------------------

INVOICE_SCHEMA = "acb-invoice/1"
DELIVERY_SCHEMA = "acb-delivery/1"


class InvoiceItem(AcbModel):
    sku: str
    description: str
    unit_price: Amount
    quantity: WireInt


class Invoice(AcbModel):
    invoice_id: str
    quote_request_id: str
    cap_of: str
    issuer_agent_id: AgentId
    buyer_agent_id: AgentId
    buyer_vault: Address
    chain_id: WireInt
    token: Address
    items: tuple[InvoiceItem, ...]
    amount: Amount
    issued_at: WireInt
    expires_at: WireInt
    delivery_deadline: WireInt
    memo: str

    def wire_dict(self) -> dict[str, Any]:
        """The unsigned ``acb-invoice/1`` JSON object of spec 9.2 (the input of ``invoiceHash``)."""
        return {"schema": INVOICE_SCHEMA, **self.to_wire()}

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> Invoice:
        """Parse a wire invoice; ``schema`` must be ``acb-invoice/1``; ``signature`` is ignored.

        Only the canonical form is accepted: camelCase keys, lowercase hex, amounts and agent ids as
        decimal strings, plain JSON integers. Anything else raises ``ValueError``, so
        ``invoice_hash(Invoice.from_wire(raw)) == invoice_hash(raw)`` always holds.
        """
        body = dict(raw)
        if body.pop("schema", None) != INVOICE_SCHEMA:
            raise ValueError(f"schema must be {INVOICE_SCHEMA}")
        body.pop("signature", None)
        model = cls.model_validate(body)
        _require_canonical(model.wire_dict(), {k: v for k, v in raw.items() if k != "signature"}, "invoice")
        return model


class SignedInvoice(AcbModel):
    invoice: Invoice
    signature: HexStr
    invoice_hash: Hex32

    @model_validator(mode="after")
    def _hash_matches_invoice(self) -> SignedInvoice:
        from acb.canonical import invoice_hash  # local import: canonical imports nothing from here

        if self.invoice_hash != invoice_hash(self.invoice):
            raise ValueError("invoice_hash does not match the invoice")
        return self

    def wire_dict(self) -> dict[str, Any]:
        """The signed wire form of spec 9.2 (invoice fields plus ``signature``)."""
        return {**self.invoice.wire_dict(), "signature": self.signature}

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> SignedInvoice:
        """Parse a signed wire invoice (canonical form only) and compute its ``invoiceHash``.

        The signature must be lowercase ``0x`` hex as well, so ``si.wire_dict() == raw`` and the file
        written from the model is the file that was signed.
        """
        from acb.canonical import invoice_hash  # local import: canonical imports nothing from here

        if "signature" not in raw:
            raise ValueError("signature missing")
        invoice = Invoice.from_wire(raw)
        model = cls(invoice=invoice, signature=raw["signature"], invoice_hash=invoice_hash(invoice))
        _require_canonical(model.wire_dict(), raw, "signed invoice")
        return model


class QuoteRequest(AcbModel):
    quote_request_id: str
    mandate_id: Hex32
    issuer_agent_id: AgentId
    sku: str
    quantity: int
    cap_of: str
    created_at: int
    closes_at: int
    used_by: Hex32 | None


class ValidationContext(AcbModel):
    now: int
    chain_id: int
    usdc: Address
    vault_address: Address
    vault_agent_id: AgentId
    catalogue: dict[int, dict[str, Amount]]
    quote_requests: tuple[QuoteRequest, ...]
    superseded: dict[str, int]
    pinned_wallets: dict[int, Address]
    live_wallets: dict[int, Address]


# ---------------------------------------------------------------------------
# detector, proposals, policy
# ---------------------------------------------------------------------------


class DetectorHit(AcbModel):
    field: str
    rule: str
    start: int
    end: int


class DetectorResult(AcbModel):
    quarantine: bool
    adverse: bool
    hits: tuple[DetectorHit, ...]
    normalized_sha: Hex32


class ValidatedInvoice(AcbModel):
    signed: SignedInvoice
    quote_request: QuoteRequest
    detector: DetectorResult


class InvoiceRejection(AcbModel):
    code: int
    rule: str
    detail: str
    invoice_hash: Hex32 | None
    keep_as_spam_evidence: bool


class SignedDelivery(AcbModel):
    order_id: Hex32
    invoice_id: str
    issuer_agent_id: AgentId
    payload_hash: Hex32
    delivered_at: WireInt
    signature: HexStr

    def unsigned_wire_dict(self) -> dict[str, Any]:
        """The ``acb-delivery/1`` receipt without ``signature`` (signed as ``chash`` of this, spec 9.3)."""
        body = self.to_wire()
        body.pop("signature")
        return {"schema": DELIVERY_SCHEMA, **body}

    def wire_dict(self) -> dict[str, Any]:
        return {**self.unsigned_wire_dict(), "signature": self.signature}

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> SignedDelivery:
        """Parse a signed ``acb-delivery/1`` receipt; only the canonical wire form is accepted."""
        body = dict(raw)
        if body.pop("schema", None) != DELIVERY_SCHEMA:
            raise ValueError(f"schema must be {DELIVERY_SCHEMA}")
        model = cls.model_validate(body)
        _require_canonical(model.wire_dict(), raw, "delivery")
        return model


class Proposal(AcbModel):
    source: Literal["llm", "fallback", "owner", "runner"]
    turn: Literal["owner", "invoice"]
    tool: str
    action: ToolAction | None
    invoice_id: str | None
    payee_agent_id: AgentId | None
    amount: Amount | None
    raw_args: dict
    raw_text: str | None
    llm_model: str | None


class PolicyContext(AcbModel):
    now: int
    chain_id: int
    usdc: Address
    vault: VaultState
    payee: PayeeView
    invoice: ValidatedInvoice | None
    invoice_sig_valid: bool
    mandate: MandateView | None
    order_id: Hex32
    order_state: OrderState
    pending_order_ids: frozenset[str]
    seen_invoice_conflict: bool
    denylisted: bool


class CheckResult(AcbModel):
    id: str
    name: str
    passed: bool
    code: int | None
    outcome: Decision | None
    detail: str


class Violation(AcbModel):
    kind: ViolationKind
    severity: int
    reason: str
    evidence: dict


class PolicyDecision(AcbModel):
    outcome: Decision
    tool_action: ToolAction
    executable: bool
    amount: Amount
    quantity: int | None
    requote_quantity: int | None
    payee_agent_id: AgentId
    order_id: Hex32
    reasons: tuple[int, ...]
    checks: tuple[CheckResult, ...]
    record_onchain: bool
    decision_hash: Hex32
    policy_version: str
    config_hash: Hex32


# ---------------------------------------------------------------------------
# signer
# ---------------------------------------------------------------------------


class ReportRequest(AcbModel):
    kind: Literal["NON_DELIVERY", "LATE_DELIVERY", "BAD_DATA", "INJECTION_ATTEMPT", "SPAM"]
    subject_agent_id: AgentId
    order_id: Hex32 | None
    invoice: SignedInvoice | None
    spam_invoices: tuple[SignedInvoice, ...] = ()
    owner_signature: HexStr | None = None


class ViolationReport(AcbModel):
    tool_name: str
    raw_args: dict
    raw_text: str | None
    secret_match: Literal["exact", "bip39", "prefix"] | None
    turn: Literal["owner", "invoice"]


class ExecuteRequest(AcbModel):
    request_id: str
    kind: Literal[
        "PAY",
        "ESCROW",
        "PAY_APPROVED",
        "RELEASE",
        "REFUND",
        "RECORD_DELIVERY",
        "RECORD_DECISION",
        "REPORT",
        "SELF_FREEZE",
        "REPORT_VIOLATION",
    ]
    proposal: Proposal | None = None
    invoice: SignedInvoice | None = None
    quote_request: QuoteRequest | None = None
    mandate_id: Hex32 | None = None
    agent_decision: PolicyDecision | None = None
    delivery: SignedDelivery | None = None
    received_at: int | None = None
    report: ReportRequest | None = None
    violation_report: ViolationReport | None = None
    order_id: Hex32 | None = None
    reason_code: int | None = None


class TxResult(AcbModel):
    hash: Hex32
    status: int
    block: int
    gas_used: int
    function: str
    from_address: Address
    to: Address
    nonce: int
    chain_id: int
    expected_status: int
    revert_error: str | None
    logs: tuple[dict, ...]


class ViolationOut(AcbModel):
    kind: ViolationKind
    severity: int
    tx_hash: Hex32 | None


class ExecuteResponse(AcbModel):
    request_id: str
    status: Literal["executed", "refused", "rejected", "failed"]
    decision: PolicyDecision | None
    tx: tuple[TxResult, ...]
    violation: ViolationOut | None
    reason_codes: tuple[int, ...]
    log_seq: int


# ---------------------------------------------------------------------------
# configuration and deployment
# ---------------------------------------------------------------------------


class NetworkProfile(AcbModel):
    name: str
    chain_id: int
    rpc_url: str
    log_rpc_url: str
    confirmations: int
    logs_chunk_blocks: int
    erc8004: dict
    usdc: dict
    explorers: tuple[dict, ...]
    profile: Literal["demo", "production"]
    upgrade_delay_s: int
    funding: dict

    @classmethod
    def load(cls, network: str, environ: dict[str, str] | None = None) -> NetworkProfile:
        """Load ``config/networks.yaml`` entry ``network``; RPC URLs come from the environment."""
        from acb.config import load_network  # local import: config imports this module

        return load_network(network, environ)

    def explorer_tx_url(self, tx_hash: str) -> str | None:
        """URL of a transaction on the first explorer of this network, or None (Anvil)."""
        for explorer in self.explorers:
            template = explorer.get("tx")
            if template:
                return template.format(hash=tx_hash)
        return None


class TierProfile(AcbModel):
    window_seconds: int
    promotion_cooldown: int
    dispute_window: int
    max_grade_age: int
    max_auto_tier: int
    initial_tier: int
    reserve_floor: Amount
    min_clean_amount: Amount
    tiers: tuple[TierConfig, TierConfig, TierConfig, TierConfig]


class TiersConfig(AcbModel):
    profiles: dict[str, TierProfile]  # demo/production values already resolved


class FundingPlan(AcbModel):
    eth: dict[str, Amount]  # role -> wei
    usdc_to_vaults: dict[str, Amount]  # vault name -> USDC base units


class ContractEntry(AcbModel):
    address: Address
    tx_hash: Hex32 | None = None
    block: int | None = None
    official: bool = False
    implementation: Address | None = None
    version: str | None = None
    extra: dict = {}


class AgentEntry(AcbModel):
    agent_id: AgentId
    name: str
    identity_owner: Address
    wallet: Address | None = None  # omitted for the wallet agent itself (13.5 example)
    register_tx: Hex32 | None
    category: str
    skus: dict[str, Amount]


class VaultEntry(AcbModel):
    address: Address
    agent_id: AgentId
    owner: Address
    agent: Address
    autonomy: Address
    profile: str
    deploy_tx: Hex32 | None
    register_vault_tx: Hex32 | None


DEPLOYMENTS_SCHEMA = "acb-deployments/1"
_CONTRACT_ENTRY_KEYS = {"address", "txHash", "block", "official", "implementation", "version"}


class Deployments(AcbModel):
    network: str
    chain_id: int
    deployed_at: str
    git_commit: str
    start_block: int
    roles: dict[str, Address]
    contracts: dict[str, ContractEntry]
    vault_code_hash: Hex32
    anchors: tuple[AgentId, ...]
    agents: dict[str, AgentEntry]
    vaults: dict[str, VaultEntry]
    abi_sha: Hex32

    def to_json_dict(self) -> dict[str, Any]:
        """The file layout of spec 13.5: adds ``schema``, nests ``abi.sha``, flattens ``extra``."""
        body = self.to_wire()
        abi_sha = body.pop("abiSha")
        contracts = {}
        for name, entry in body["contracts"].items():
            extra = entry.pop("extra", {}) or {}
            kept = {k: v for k, v in entry.items() if v is not None and not (k == "official" and v is False)}
            contracts[name] = {**kept, **extra}
        body["contracts"] = contracts
        return {"schema": DEPLOYMENTS_SCHEMA, **body, "abi": {"sha": abi_sha}}

    @classmethod
    def from_json_dict(cls, raw: dict[str, Any]) -> Deployments:
        """Parse the file layout of spec 13.5 (inverse of :meth:`to_json_dict`)."""
        body = dict(raw)
        if body.pop("schema", None) != DEPLOYMENTS_SCHEMA:
            raise ValueError(f"schema must be {DEPLOYMENTS_SCHEMA}")
        abi = body.pop("abi", None) or {}
        body["abiSha"] = abi.get("sha")
        contracts = {}
        for name, entry in (body.get("contracts") or {}).items():
            known = {k: v for k, v in entry.items() if k in _CONTRACT_ENTRY_KEYS}
            extra = {k: v for k, v in entry.items() if k not in _CONTRACT_ENTRY_KEYS}
            contracts[name] = {**known, "extra": extra}
        body["contracts"] = contracts
        return cls.model_validate(body)


# KeyRing is a plain class (not pydantic, never serialised): see acb.keys.KeyRing.

# ---------------------------------------------------------------------------
# bureau
# ---------------------------------------------------------------------------


class OrderRecord(AcbModel):
    vault: Address
    order_id: Hex32
    payer_agent_id: AgentId
    payee_agent_id: AgentId
    amount: Amount
    kind: Literal["pay", "escrow"]
    outcome: Literal["ON_TIME", "LATE", "FAILED", "PENDING", "EXCLUDED", "CLAWBACK"]
    invoice_hash: Hex32
    invoice_ok: bool
    block: int
    log_index: int
    open_tx: Hex32
    close_tx: Hex32 | None
    close_log_index: int | None
    timestamp: int


class ReportRecord(AcbModel):
    reporter: Address
    reporter_agent_id: AgentId
    subject_agent_id: AgentId
    kind: str
    value: int
    order_id: Hex32 | None
    vault: Address | None
    feedback_index: int
    feedback_hash: Hex32
    tx: Hex32
    log_index: int
    block: int
    timestamp: int
    revoked: bool
    payload: dict | None


class AgentInfo(AcbModel):
    agent_id: AgentId
    registered_block: int
    registered_timestamp: int
    identity_owner: Address
    vault_owners: tuple[Address, ...]
    grade: Grade
    anchor: bool


class ScoreInputs(AcbModel):
    chain_id: int
    as_of_block: int
    as_of_block_hash: Hex32
    as_of_timestamp: int
    subject_agent_id: AgentId
    agents: dict[int, AgentInfo]
    orders: tuple[OrderRecord, ...]
    reports: tuple[ReportRecord, ...]
    contracts: dict[str, Address]
    previous: dict | None


EVIDENCE_SCHEMA = "acb-evidence/1"


class Bundle(BaseModel):
    """Evidence bundle ``acb-evidence/1`` (spec 4.8); keys exactly as in 4.8 (camelCase inside dicts)."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_: Literal["acb-evidence/1"] = Field(default=EVIDENCE_SCHEMA, alias="schema")
    model: dict
    chain: dict
    contracts: dict
    subject: dict
    orders: tuple[dict, ...]
    reports: tuple[dict, ...]
    features: dict
    points: dict
    score: int
    grade: str
    reasons: tuple[int, ...]
    rules: dict
    override: dict | None = None
    previous: dict | None = None

    def as_canonical(self) -> dict[str, Any]:
        """The 4.8 JSON object (the input of ``evidenceHash``)."""
        return self.model_dump(mode="json", by_alias=True)

    @property
    def evidence_hash(self) -> str:
        """``keccak256(canonical_bytes(bundle))``."""
        from acb.canonical import chash

        return chash(self.as_canonical())


class RescoreResult(AcbModel):
    rounds: int
    postings: tuple[GradeView, ...]
    unchanged: tuple[int, ...]
    tx: tuple[TxResult, ...]


class AutonomyResult(AcbModel):
    rule: str
    from_tier: int
    to_tier: int | None
    reason_code: int | None
    evidence_hash: Hex32 | None
    tx: TxResult | None
    simulated_revert: str | None


# ---------------------------------------------------------------------------
# agent API cards (8.8)
# ---------------------------------------------------------------------------


class PayeeCandidate(AcbModel):
    agent_id: AgentId
    name: str
    grade: Grade
    score: int | None
    live_wallet: Address | None
    registered_at: int | None
    lookalike_of: str | None


class Clarification(AcbModel):
    type: Literal["clarification"] = "clarification"
    question: str
    field: str
    options: tuple[str, ...]
    candidates: tuple[PayeeCandidate, ...]


class MandateDraft(AcbModel):
    type: Literal["mandate_draft"] = "mandate_draft"
    draft_id: str
    purpose: str
    payees: tuple[PayeeCandidate, ...]
    max_per_tx: Amount
    max_total: Amount
    valid_hours: int
    recurring: bool
    diff: dict | None


class DecisionCard(AcbModel):
    type: Literal["decision"] = "decision"
    order_id: Hex32
    payee: PayeeCandidate
    amount: Amount
    outcome: Decision
    reasons: tuple[int, ...]
    reason_texts: tuple[str, ...]
    tx_hash: Hex32 | None
    offchain: bool


class ApprovalRequest(AcbModel):
    type: Literal["approval"] = "approval"
    order_id: Hex32
    invoice: SignedInvoice
    reasons: tuple[int, ...]


class StatusCard(AcbModel):
    type: Literal["status"] = "status"
    vault: VaultState
    mandates: tuple[MandateView, ...]


Card = Annotated[
    Clarification | MandateDraft | DecisionCard | ApprovalRequest | StatusCard, Field(discriminator="type")
]


class ChatResponse(AcbModel):
    reply: str
    cards: tuple[Card, ...]
    llm: dict  # {"mode", "model", "fallback"}


# ---------------------------------------------------------------------------
# LLM client types (8.2)
# ---------------------------------------------------------------------------


class ToolSpec(AcbModel):
    name: str
    description: str
    parameters: dict  # JSON Schema


class ToolCall(AcbModel):
    name: str
    arguments: dict
    raw: str


class LLMResult(AcbModel):
    text: str | None
    tool_calls: list[ToolCall]
    model: str
    latency_ms: int
    error: str | None  # "timeout" | "http_<code>" | "bad_json" | None


# ---------------------------------------------------------------------------
# event envelope (10.3): the only model with snake_case JSON keys
# ---------------------------------------------------------------------------

EVENT_TYPES: frozenset[str] = frozenset(
    {
        "chat",
        "card",
        "decision",
        "tx",
        "violation",
        "tier",
        "grade",
        "report",
        "mandate",
        "approval",
        "llm_switch",
        "message",
        "info",
        "error",
    }
)


class Event(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    ts: str
    run_id: str
    service: str
    type: str
    step: str | None
    chain_id: int | None
    data: dict
    reason_codes: tuple[int, ...] = ()
    tx_hash: str | None = None
    explorer: str | None = None


__all__ = [
    "DECISION_TO_TOOL_ACTION",
    "DELIVERY_SCHEMA",
    "DEPLOYMENTS_SCHEMA",
    "EVENT_TYPES",
    "EVIDENCE_SCHEMA",
    "GRADE_RANK",
    "INVOICE_SCHEMA",
    "STRICTNESS",
    "TIER_FROZEN",
    "TOOL_ACTION_TO_DECISION",
    "AcbModel",
    "Address",
    "AgentEntry",
    "AgentId",
    "AgentInfo",
    "Amount",
    "ApprovalRequest",
    "AutonomyResult",
    "Bundle",
    "Card",
    "ChatResponse",
    "CheckResult",
    "Clarification",
    "ContractEntry",
    "Decision",
    "DecisionCard",
    "Deployments",
    "DetectorHit",
    "DetectorResult",
    "Event",
    "ExecuteRequest",
    "ExecuteResponse",
    "FundingPlan",
    "Grade",
    "GradeView",
    "Hex32",
    "HexStr",
    "Invoice",
    "InvoiceItem",
    "InvoiceRejection",
    "LLMResult",
    "MandateDraft",
    "MandateView",
    "NetworkProfile",
    "OrderRecord",
    "OrderState",
    "Outcome",
    "PayeeCandidate",
    "PayeeView",
    "PolicyContext",
    "PolicyDecision",
    "Proposal",
    "QuoteRequest",
    "ReportRecord",
    "ReportRequest",
    "RescoreResult",
    "ScoreInputs",
    "SignedDelivery",
    "SignedInvoice",
    "StatusCard",
    "TierConfig",
    "TierProfile",
    "TiersConfig",
    "ToolAction",
    "ToolCall",
    "ToolSpec",
    "TxResult",
    "ValidatedInvoice",
    "ValidationContext",
    "VaultEntry",
    "VaultState",
    "Violation",
    "ViolationKind",
    "ViolationOut",
    "ViolationReport",
    "grade_mask",
    "mask_has",
    "stricter",
]
