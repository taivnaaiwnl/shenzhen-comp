// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {AcbTypes} from "./lib/AcbTypes.sol";
import {IAgentCreditBureau} from "./interfaces/IAgentCreditBureau.sol";
import {IIdentityRegistry} from "./interfaces/IIdentityRegistry.sol";
import {IERC20Minimal} from "./interfaces/IERC20Minimal.sol";
import {DeliveryClaimed as DeliveryClaimedError} from "./lib/VaultErrors.sol";

/// @title TierVault
/// @notice Holds USDC for one agent. Owner-registered mandates (with pinned payout wallets) bound every agent
///         outflow; the agent key spends only within the current tier's caps and grade masks, above the reserve
///         floor and inside a rolling window. Escrow with provider delivery claims, owner approvals
///         (maker-checker), on-chain recording of refusals, deliveries and violations, the Earned Autonomy
///         state machine, freeze and human reset with key rotation. Spec section 3.2.
/// @dev No immutables: every instance has identical runtime code, so the bureau can check its EXTCODEHASH.
///      Configuration that matters to the bureau (token, bureau, identity) has no setter.
contract TierVault {
    // ------------------------------------------------------------------ types

    struct TierConfig {
        uint128 perTxCap; // max single outflow (pay, escrow)
        uint128 windowCap; // max sum of agent outflows in the rolling window
        uint128 newPayeeCap; // max single outflow to a NEW or NONE payee
        uint8 maxTxPerWindow; // <= 32
        uint8 payGradeMask; // grades allowed for pay()
        uint8 escrowGradeMask; // grades allowed for escrow()
        uint8 minCleanToPromote; // clean settled orders needed to promote FROM this tier; 0 = cannot promote
    }

    struct VaultInit {
        address token;
        address bureau;
        address identity;
        address owner;
        address agent;
        address autonomyOperator;
        uint256 agentId;
        TierConfig[4] configs;
        uint128 reserveFloor;
        uint128 minCleanAmount;
        uint64 windowSeconds;
        uint64 promotionCooldown;
        uint64 maxGradeAge;
        uint64 disputeWindow;
        uint8 maxAutoTier;
        uint8 initialTier;
    }

    struct Mandate {
        uint128 maxPerTx;
        uint128 maxTotal;
        uint128 spent;
        uint64 validUntil;
        bool recurring;
        bool active;
    }

    struct Order {
        uint8 state; // OrderState
        uint8 outcome; // Outcome
        uint8 decision; // Decision that created it
        uint64 createdAt;
        uint64 deadline; // delivery deadline (pay and escrow)
        uint64 claimedAt; // escrow only: time of the payee's delivery claim, 0 = none
        uint128 amount;
        uint256 payeeAgentId;
        address payee; // the pinned wallet paid or to be paid
        bytes32 mandateId; // 0 for owner-approved orders
        bytes32 deliveryHash; // from the claim (escrow) or recordDelivery (pay)
    }

    struct Approval {
        uint256 payeeAgentId;
        address expectedPayee;
        uint128 amount;
        uint64 expiry;
        uint64 escrowDeadline;
        bool asEscrow;
        bool used;
    }

    struct OutflowParams {
        bytes32 orderId;
        uint256 payeeAgentId;
        uint128 amount;
        uint64 deadline;
        bytes32 mandateId;
        bytes32 invoiceHash;
        bytes32 decisionHash;
        uint8 decision;
        uint16 reasonCode;
    }

    struct DecisionParams {
        bytes32 orderId;
        uint256 payeeAgentId;
        uint128 amount;
        uint8 decision;
        uint16 reasonCode;
        bytes32 invoiceHash;
        bytes32 decisionHash;
    }

    struct VaultStatus {
        uint8 tier;
        bool frozen;
        uint8 tierBeforeFreeze;
        uint8 maxAutoTier;
        uint256 balance;
        uint128 escrowLocked;
        uint128 reserveFloor;
        uint128 spent;
        uint8 count;
        uint16 clean;
        uint16 points;
        uint64 lastTierChangeAt;
    }

    // ------------------------------------------------------------------ storage

    IERC20Minimal public token;
    IAgentCreditBureau public bureau;
    IIdentityRegistry public identity;
    address public owner;
    address public pendingOwner;
    address public agent;
    address public autonomyOperator;
    uint256 public agentId;
    uint8 public tier;
    uint8 public tierBeforeFreeze;
    uint8 public maxAutoTier;
    uint64 public lastTierChangeAt;
    uint64 public promotionCooldown;
    uint64 public windowSeconds;
    uint64 public maxGradeAge;
    uint64 public disputeWindow;
    uint16 public cleanSinceChange;
    uint16 public violationPointsSinceChange;
    uint128 public reserveFloor;
    uint128 public escrowLocked;
    uint128 public minCleanAmount;
    TierConfig[4] internal _tierConfig;
    mapping(bytes32 => Mandate) internal _mandates;
    mapping(bytes32 => mapping(uint256 => address)) public mandatePayee; // mandateId -> payeeAgentId -> pinned wallet
    mapping(bytes32 => Order) internal _orders;
    mapping(bytes32 => Approval) internal _approvals;
    mapping(uint256 => uint32) public completedWith; // payeeAgentId -> settled orders (saturating)
    uint256[32] internal _ring; // rolling-window spends: (uint64 ts << 128) | uint128 amount
    uint8 internal _ringHead;
    uint256 private _lock;

    uint8 public constant RING_SIZE = 32;
    uint8 public constant MAX_MANDATE_PAYEES = 6;
    uint64 public constant MAX_MANDATE_DURATION = 7 days;
    uint64 public constant MAX_DISPUTE_WINDOW = 30 days;

    // ------------------------------------------------------------------ events

    event VaultInitialized(
        address indexed owner,
        address indexed agent,
        address indexed autonomyOperator,
        uint256 agentId,
        uint8 initialTier
    );
    event MandateRegistered(
        bytes32 indexed mandateId,
        uint256[] payeeAgentIds,
        address[] payeeWallets,
        uint128 maxPerTx,
        uint128 maxTotal,
        uint64 validUntil,
        bool recurring,
        bytes32 purposeHash
    );
    event MandateRevoked(bytes32 indexed mandateId);
    event Paid(
        bytes32 indexed orderId,
        uint256 indexed payeeAgentId,
        address indexed payee,
        uint128 amount,
        uint8 decision,
        uint16 reasonCode,
        bytes32 mandateId,
        bytes32 invoiceHash,
        bytes32 decisionHash,
        uint64 deadline,
        uint8 tier
    );
    event EscrowOpened(
        bytes32 indexed orderId,
        uint256 indexed payeeAgentId,
        address indexed payee,
        uint128 amount,
        uint64 deadline,
        bytes32 mandateId,
        bytes32 invoiceHash,
        bytes32 decisionHash,
        uint16 reasonCode,
        uint8 tier
    );
    event DeliveryClaimed(
        bytes32 indexed orderId, uint256 indexed payeeAgentId, bytes32 deliveryHash, uint64 claimedAt, bool late
    );
    event EscrowReleased(
        bytes32 indexed orderId,
        uint256 indexed payeeAgentId,
        address indexed by,
        uint128 amount,
        uint8 outcome,
        bytes32 deliveryHash
    );
    event EscrowRefunded(
        bytes32 indexed orderId,
        uint256 indexed payeeAgentId,
        address indexed by,
        uint128 amount,
        uint16 reasonCode,
        uint8 outcome
    );
    event DeliveryRecorded(
        bytes32 indexed orderId, uint256 indexed payeeAgentId, address indexed by, uint8 outcome, bytes32 deliveryHash
    );
    event DecisionRecorded(
        bytes32 indexed orderId,
        uint256 indexed payeeAgentId,
        uint8 indexed decision,
        uint128 amount,
        uint16 reasonCode,
        bytes32 invoiceHash,
        bytes32 decisionHash,
        uint8 tier
    );
    event OwnerApproved(
        bytes32 indexed orderId,
        uint256 indexed payeeAgentId,
        address indexed expectedPayee,
        uint128 amount,
        uint64 expiry,
        bool asEscrow,
        uint64 escrowDeadline
    );
    event ApprovalRevoked(bytes32 indexed orderId);
    event ViolationRecorded(
        uint8 indexed kind, uint8 severity, bytes32 evidenceHash, address indexed by, uint8 tierAfter
    );
    event TierChanged(
        uint8 indexed fromTier, uint8 indexed toTier, uint16 reasonCode, bytes32 evidenceHash, address indexed by
    );
    event TierConfigSet(
        uint8 indexed tier,
        uint128 perTxCap,
        uint128 windowCap,
        uint128 newPayeeCap,
        uint8 maxTxPerWindow,
        uint8 payGradeMask,
        uint8 escrowGradeMask,
        uint8 minCleanToPromote
    );
    event ConfigChanged(bytes32 indexed what, uint256 value);
    event AgentChanged(address indexed oldAgent, address indexed newAgent);
    event AutonomyOperatorChanged(address indexed oldOperator, address indexed newOperator);
    event OwnershipTransferStarted(address indexed from, address indexed to);
    event OwnershipTransferred(address indexed from, address indexed to);
    event Withdrawn(address indexed to, uint128 amount);

    // ------------------------------------------------------------------ errors

    error NotOwner();
    error NotPendingOwner();
    error NotAgent();
    error NotAutonomy();
    error NotAuthorized();
    error NotPayee();
    error Frozen();
    error NotFrozen();
    error Reentrancy();
    error ZeroAddress();
    error ZeroHash();
    error RolesNotDistinct();
    error BadWindow();
    error BadTier(uint8 tier);
    error SameTier();
    error BadTierConfig(uint8 tier);
    error MaskIncludesD();
    error BadDecision(uint8 decision);
    error BadOutcome(uint8 outcome);
    error BadReason(uint16 reasonCode);
    error OutcomeAlreadySet(bytes32 orderId);
    error OrderExists(bytes32 orderId);
    error BadOrderState(bytes32 orderId, uint8 state);
    error ZeroAmount();
    error BadDeadline();
    error DeadlineNotPassed(uint64 deadline);
    error MandateExists(bytes32 mandateId);
    error BadMandate();
    error MandateInactive(bytes32 mandateId);
    error MandateExpired(bytes32 mandateId);
    error NotInMandate(bytes32 mandateId, uint256 agentId);
    error MandatePerTxExceeded(uint256 amount, uint256 max);
    error MandateBudgetExceeded(uint256 spent, uint256 amount, uint256 max);
    error RecurringNeedsTier2();
    error PayeeWalletMismatch(uint256 agentId, address live, address wallet);
    error PayeeWalletChanged(uint256 agentId, address live, address pinned);
    error PerTxCapExceeded(uint256 amount, uint256 cap);
    error NewPayeeCapExceeded(uint256 amount, uint256 cap);
    error WindowCapExceeded(uint256 spent, uint256 amount, uint256 cap);
    error TooManyTx(uint8 count, uint8 max);
    error RingFull();
    error GradeNotAllowed(uint256 agentId, uint8 grade, uint8 mask);
    error SelfDealing(address payee);
    error ReserveFloorBreached(uint256 balance, uint256 escrowLocked, uint256 floor, uint256 amount);
    error InsufficientUnlocked(uint256 free, uint256 amount);
    error EscrowNotExpired(uint64 deadline);
    error NoClaim(bytes32 orderId);
    error AlreadyClaimed(bytes32 orderId);
    // error DeliveryClaimed(bytes32 orderId): declared in lib/VaultErrors.sol (name shared with the event).
    error DisputeWindowOpen(uint64 readyAt);
    error ApprovalMissing(bytes32 orderId);
    error ApprovalUsed(bytes32 orderId);
    error ApprovalExpired(bytes32 orderId);
    error BadViolationKind(uint8 kind);
    error BadSeverity(uint8 severity);
    error PromotionTooLarge(uint8 fromTier, uint8 toTier);
    error AboveMaxAutoTier(uint8 tier, uint8 maxAutoTier);
    error PromotionCooldown(uint64 readyAt);
    error PromotionNotEarned(uint16 clean, uint16 needed, uint16 points);
    error TransferFailed();

    // ------------------------------------------------------------------ modifiers

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier onlyAgent() {
        if (msg.sender != agent) revert NotAgent();
        _;
    }

    modifier onlyAutonomy() {
        if (msg.sender != autonomyOperator) revert NotAutonomy();
        _;
    }

    modifier notFrozen() {
        if (tier == AcbTypes.TIER_FROZEN) revert Frozen();
        _;
    }

    modifier nonReentrant() {
        if (_lock != 1) revert Reentrancy();
        _lock = 2;
        _;
        _lock = 1;
    }

    // ------------------------------------------------------------------ constructor

    constructor(VaultInit memory init) {
        if (
            init.token == address(0) || init.bureau == address(0) || init.identity == address(0)
                || init.owner == address(0) || init.agent == address(0) || init.autonomyOperator == address(0)
        ) revert ZeroAddress();
        if (init.owner == init.agent || init.owner == init.autonomyOperator || init.agent == init.autonomyOperator) {
            revert RolesNotDistinct();
        }
        if (init.initialTier > AcbTypes.TIER_SMALL) revert BadTier(init.initialTier);
        if (init.maxAutoTier > AcbTypes.TIER_MAX) revert BadTier(init.maxAutoTier);
        if (init.windowSeconds == 0 || init.disputeWindow > MAX_DISPUTE_WINDOW) revert BadWindow();
        for (uint8 t = 0; t < 4; t++) {
            _validateConfig(t, init.configs[t]);
        }

        token = IERC20Minimal(init.token);
        bureau = IAgentCreditBureau(init.bureau);
        identity = IIdentityRegistry(init.identity);
        owner = init.owner;
        agent = init.agent;
        autonomyOperator = init.autonomyOperator;
        agentId = init.agentId;
        tier = init.initialTier;
        tierBeforeFreeze = init.initialTier;
        maxAutoTier = init.maxAutoTier;
        lastTierChangeAt = uint64(block.timestamp);
        promotionCooldown = init.promotionCooldown;
        windowSeconds = init.windowSeconds;
        maxGradeAge = init.maxGradeAge;
        disputeWindow = init.disputeWindow;
        reserveFloor = init.reserveFloor;
        minCleanAmount = init.minCleanAmount;
        _lock = 1;

        emit VaultInitialized(init.owner, init.agent, init.autonomyOperator, init.agentId, init.initialTier);
        for (uint8 t = 0; t < 4; t++) {
            _tierConfig[t] = init.configs[t];
            _emitTierConfig(t, init.configs[t]);
        }
    }

    // ------------------------------------------------------------------ mandates

    /// @notice Register a standing payment authority with pinned payout wallets (owner, also while frozen).
    function registerMandate(
        bytes32 mandateId,
        uint256[] calldata payeeAgentIds,
        address[] calldata payeeWallets,
        uint128 maxPerTx,
        uint128 maxTotal,
        uint64 validUntil,
        bool recurring,
        bytes32 purposeHash
    ) external onlyOwner {
        if (_mandates[mandateId].validUntil != 0) revert MandateExists(mandateId);
        if (mandateId == bytes32(0)) revert BadMandate();
        uint256 n = payeeAgentIds.length;
        if (n == 0 || n > MAX_MANDATE_PAYEES || n != payeeWallets.length) revert BadMandate();
        if (maxPerTx == 0 || maxPerTx > maxTotal) revert BadMandate();
        if (validUntil <= block.timestamp || uint256(validUntil) > block.timestamp + MAX_MANDATE_DURATION) {
            revert BadMandate();
        }
        for (uint256 i = 0; i < n; i++) {
            uint256 id = payeeAgentIds[i];
            address wallet = payeeWallets[i];
            for (uint256 j = 0; j < i; j++) {
                if (payeeAgentIds[j] == id) revert BadMandate();
            }
            if (id == agentId) revert BadMandate();
            if (
                wallet == address(0) || wallet == address(this) || wallet == owner || wallet == agent
                    || wallet == autonomyOperator
            ) revert BadMandate();
            address live = identity.getAgentWallet(id);
            if (live != wallet) revert PayeeWalletMismatch(id, live, wallet);
            mandatePayee[mandateId][id] = wallet;
        }
        _mandates[mandateId] = Mandate({
            maxPerTx: maxPerTx, maxTotal: maxTotal, spent: 0, validUntil: validUntil, recurring: recurring, active: true
        });
        emit MandateRegistered(
            mandateId, payeeAgentIds, payeeWallets, maxPerTx, maxTotal, validUntil, recurring, purposeHash
        );
    }

    /// @notice Revoke a mandate (owner, also while frozen).
    function revokeMandate(bytes32 mandateId) external onlyOwner {
        Mandate storage m = _mandates[mandateId];
        if (!m.active) revert MandateInactive(mandateId);
        m.active = false;
        emit MandateRevoked(mandateId);
    }

    function getMandate(bytes32 mandateId) external view returns (Mandate memory) {
        return _mandates[mandateId];
    }

    // ------------------------------------------------------------------ outflows

    /// @notice Direct payment under a mandate, to the pinned wallet (agent).
    function pay(OutflowParams calldata p) external onlyAgent notFrozen nonReentrant {
        if (p.decision != AcbTypes.DECISION_PAY && p.decision != AcbTypes.DECISION_PAY_CAPPED) {
            revert BadDecision(p.decision);
        }
        address pinned = _openOutflow(p, false);
        _orders[p.orderId] = Order({
            state: AcbTypes.STATE_PAID,
            outcome: AcbTypes.OUTCOME_NONE,
            decision: p.decision,
            createdAt: uint64(block.timestamp),
            deadline: p.deadline,
            claimedAt: 0,
            amount: p.amount,
            payeeAgentId: p.payeeAgentId,
            payee: pinned,
            mandateId: p.mandateId,
            deliveryHash: bytes32(0)
        });
        _safeTransfer(pinned, p.amount);
        emit Paid(
            p.orderId,
            p.payeeAgentId,
            pinned,
            p.amount,
            p.decision,
            p.reasonCode,
            p.mandateId,
            p.invoiceHash,
            p.decisionHash,
            p.deadline,
            tier
        );
    }

    /// @notice Open an escrow under a mandate; funds stay locked in the vault until release or refund (agent).
    function escrow(OutflowParams calldata p) external onlyAgent notFrozen nonReentrant {
        if (p.decision != AcbTypes.DECISION_ESCROW) revert BadDecision(p.decision);
        address pinned = _openOutflow(p, true);
        escrowLocked += p.amount;
        _orders[p.orderId] = Order({
            state: AcbTypes.STATE_ESCROWED,
            outcome: AcbTypes.OUTCOME_NONE,
            decision: p.decision,
            createdAt: uint64(block.timestamp),
            deadline: p.deadline,
            claimedAt: 0,
            amount: p.amount,
            payeeAgentId: p.payeeAgentId,
            payee: pinned,
            mandateId: p.mandateId,
            deliveryHash: bytes32(0)
        });
        emit EscrowOpened(
            p.orderId,
            p.payeeAgentId,
            pinned,
            p.amount,
            p.deadline,
            p.mandateId,
            p.invoiceHash,
            p.decisionHash,
            p.reasonCode,
            tier
        );
    }

    /// @notice The payee claims delivery of an escrow before handing over the data (also while frozen).
    ///         After a claim the agent can no longer refund.
    function claimDelivered(bytes32 orderId, bytes32 deliveryHash) external nonReentrant {
        Order storage o = _orders[orderId];
        if (o.state != AcbTypes.STATE_ESCROWED) revert BadOrderState(orderId, o.state);
        if (msg.sender != o.payee) revert NotPayee();
        if (o.claimedAt != 0) revert AlreadyClaimed(orderId);
        if (deliveryHash == bytes32(0)) revert ZeroHash();
        uint64 nowTs = uint64(block.timestamp);
        o.claimedAt = nowTs;
        o.deliveryHash = deliveryHash;
        emit DeliveryClaimed(orderId, o.payeeAgentId, deliveryHash, nowTs, nowTs > o.deadline);
    }

    /// @notice Release a claimed escrow to the stored payee. Agent (not frozen, payee not D), owner (any time),
    ///         anyone from `claimedAt + disputeWindow`. Outcome is computed from `claimedAt` vs the deadline.
    function release(bytes32 orderId) external nonReentrant {
        Order storage o = _orders[orderId];
        if (o.state != AcbTypes.STATE_ESCROWED) revert BadOrderState(orderId, o.state);
        if (o.claimedAt == 0) revert NoClaim(orderId);
        if (msg.sender == agent) {
            if (tier == AcbTypes.TIER_FROZEN) revert Frozen();
            uint8 g = _grade(o.payeeAgentId);
            if (g == AcbTypes.GRADE_D) revert GradeNotAllowed(o.payeeAgentId, g, AcbTypes.MASK_NOT_D);
        } else if (msg.sender != owner) {
            uint64 readyAt = o.claimedAt + disputeWindow;
            if (block.timestamp < readyAt) revert DisputeWindowOpen(readyAt);
        }
        uint8 outcome = o.claimedAt <= o.deadline ? AcbTypes.OUTCOME_ON_TIME : AcbTypes.OUTCOME_LATE;
        uint128 amount = o.amount;
        escrowLocked -= amount;
        o.state = AcbTypes.STATE_RELEASED;
        o.outcome = outcome;
        _cleanRecord(o.payeeAgentId, amount);
        _safeTransfer(o.payee, amount);
        emit EscrowReleased(orderId, o.payeeAgentId, msg.sender, amount, outcome, o.deliveryHash);
    }

    /// @notice Refund an escrow into the vault. Rules by caller and claim state (spec 3.2):
    ///         no claim: agent 2401 any time, 2400 after the deadline and not frozen; owner 2400 (after the
    ///         deadline) or 2401; anyone else only after `deadline + disputeWindow`, reason forced to 2402.
    ///         After a claim: only the owner, with 2403.
    function refund(bytes32 orderId, uint16 reasonCode) external nonReentrant {
        Order storage o = _orders[orderId];
        if (o.state != AcbTypes.STATE_ESCROWED) revert BadOrderState(orderId, o.state);
        uint16 reason = reasonCode;
        if (o.claimedAt == 0) {
            if (msg.sender == agent) {
                if (reasonCode == AcbTypes.R_REFUND_NON_DELIVERY) {
                    if (block.timestamp <= o.deadline) revert EscrowNotExpired(o.deadline);
                    if (tier == AcbTypes.TIER_FROZEN) revert Frozen();
                } else if (reasonCode != AcbTypes.R_REFUND_RISK_ROLLBACK) {
                    revert BadReason(reasonCode);
                }
            } else if (msg.sender == owner) {
                if (reasonCode == AcbTypes.R_REFUND_NON_DELIVERY) {
                    if (block.timestamp <= o.deadline) revert EscrowNotExpired(o.deadline);
                } else if (reasonCode != AcbTypes.R_REFUND_RISK_ROLLBACK) {
                    revert BadReason(reasonCode);
                }
            } else {
                // first second after deadline + disputeWindow (uint256: a far deadline cannot overflow)
                uint256 readyAt = uint256(o.deadline) + disputeWindow + 1;
                if (block.timestamp < readyAt) {
                    revert DisputeWindowOpen(readyAt > type(uint64).max ? type(uint64).max : uint64(readyAt));
                }
                reason = AcbTypes.R_REFUND_TIMEOUT;
            }
        } else {
            if (msg.sender != owner) revert DeliveryClaimedError(orderId);
            if (reasonCode != AcbTypes.R_REFUND_DISPUTED_AFTER_CLAIM) revert BadReason(reasonCode);
        }
        uint128 amount = o.amount;
        escrowLocked -= amount;
        if (o.mandateId != bytes32(0)) {
            Mandate storage m = _mandates[o.mandateId];
            m.spent = m.spent > amount ? m.spent - amount : 0;
        }
        uint8 outcome = reason == AcbTypes.R_REFUND_NON_DELIVERY ? AcbTypes.OUTCOME_FAILED : AcbTypes.OUTCOME_NONE;
        o.state = AcbTypes.STATE_REFUNDED;
        o.outcome = outcome;
        emit EscrowRefunded(orderId, o.payeeAgentId, msg.sender, amount, reason, outcome);
    }

    /// @notice Owner approval for one order (maker-checker), paid to `expectedPayee` only.
    function ownerApprove(
        bytes32 orderId,
        uint256 payeeAgentId,
        address expectedPayee,
        uint128 amount,
        uint64 expiry,
        bool asEscrow,
        uint64 escrowDeadline
    ) external onlyOwner {
        uint8 s = _orders[orderId].state;
        if (s != AcbTypes.STATE_NONE) revert OrderExists(orderId);
        if (expiry <= block.timestamp) revert BadDeadline();
        if (amount == 0) revert ZeroAmount();
        if (expectedPayee == address(0)) revert ZeroAddress();
        if (asEscrow && escrowDeadline <= block.timestamp) revert BadDeadline();
        _approvals[orderId] = Approval({
            payeeAgentId: payeeAgentId,
            expectedPayee: expectedPayee,
            amount: amount,
            expiry: expiry,
            escrowDeadline: asEscrow ? escrowDeadline : 0,
            asEscrow: asEscrow,
            used: false
        });
        emit OwnerApproved(
            orderId, payeeAgentId, expectedPayee, amount, expiry, asEscrow, asEscrow ? escrowDeadline : 0
        );
    }

    function revokeApproval(bytes32 orderId) external onlyOwner {
        Approval storage a = _approvals[orderId];
        if (a.expectedPayee == address(0)) revert ApprovalMissing(orderId);
        if (a.used) revert ApprovalUsed(orderId);
        delete _approvals[orderId];
        emit ApprovalRevoked(orderId);
    }

    /// @notice Execute an owner-approved order beyond the tier caps (agent). Still blocks D payees, a frozen
    ///         vault, a changed payee wallet and the reserve floor; counts in the rolling window.
    function executeApproved(bytes32 orderId, bytes32 invoiceHash, bytes32 decisionHash)
        external
        onlyAgent
        notFrozen
        nonReentrant
    {
        Approval storage a = _approvals[orderId];
        if (a.expectedPayee == address(0)) revert ApprovalMissing(orderId);
        if (a.used) revert ApprovalUsed(orderId);
        if (block.timestamp > a.expiry) revert ApprovalExpired(orderId);
        // An escrow must not open already past its deadline (it could be refunded as FAILED at once).
        if (a.asEscrow && a.escrowDeadline <= block.timestamp) revert BadDeadline();
        if (_orders[orderId].state != AcbTypes.STATE_NONE) revert OrderExists(orderId);
        uint256 payeeId = a.payeeAgentId;
        uint128 amount = a.amount;
        address payee = a.expectedPayee;
        uint8 g = _grade(payeeId);
        if (g == AcbTypes.GRADE_D) revert GradeNotAllowed(payeeId, g, AcbTypes.MASK_NOT_D);
        _checkPayee(payeeId, payee);
        _checkReserve(amount);
        _recordSpend(amount, false, _tierConfig[tier]);
        a.used = true;

        uint8 t = tier;
        if (a.asEscrow) {
            uint64 deadline = a.escrowDeadline;
            escrowLocked += amount;
            _orders[orderId] = Order({
                state: AcbTypes.STATE_ESCROWED,
                outcome: AcbTypes.OUTCOME_NONE,
                decision: AcbTypes.DECISION_ESCROW,
                createdAt: uint64(block.timestamp),
                deadline: deadline,
                claimedAt: 0,
                amount: amount,
                payeeAgentId: payeeId,
                payee: payee,
                mandateId: bytes32(0),
                deliveryHash: bytes32(0)
            });
            emit EscrowOpened(
                orderId,
                payeeId,
                payee,
                amount,
                deadline,
                bytes32(0),
                invoiceHash,
                decisionHash,
                AcbTypes.R_OWNER_APPROVED,
                t
            );
        } else {
            uint64 deadline = a.expiry;
            _orders[orderId] = Order({
                state: AcbTypes.STATE_PAID,
                outcome: AcbTypes.OUTCOME_NONE,
                decision: AcbTypes.DECISION_PAY,
                createdAt: uint64(block.timestamp),
                deadline: deadline,
                claimedAt: 0,
                amount: amount,
                payeeAgentId: payeeId,
                payee: payee,
                mandateId: bytes32(0),
                deliveryHash: bytes32(0)
            });
            _safeTransfer(payee, amount);
            emit Paid(
                orderId,
                payeeId,
                payee,
                amount,
                AcbTypes.DECISION_PAY,
                AcbTypes.R_OWNER_APPROVED,
                bytes32(0),
                invoiceHash,
                decisionHash,
                deadline,
                t
            );
        }
    }

    // ------------------------------------------------------------------ recording

    /// @notice Record a REFUSE (the orderId can never be paid) or NEEDS_OWNER_APPROVAL decision (agent, also
    ///         while frozen).
    function recordDecision(DecisionParams calldata p) external onlyAgent {
        if (p.decision != AcbTypes.DECISION_REFUSE && p.decision != AcbTypes.DECISION_NEEDS_OWNER_APPROVAL) {
            revert BadDecision(p.decision);
        }
        Order storage o = _orders[p.orderId];
        if (o.state != AcbTypes.STATE_NONE) revert OrderExists(p.orderId);
        if (p.decision == AcbTypes.DECISION_REFUSE) {
            o.state = AcbTypes.STATE_REFUSED;
            o.decision = p.decision;
            o.createdAt = uint64(block.timestamp);
            o.amount = p.amount;
            o.payeeAgentId = p.payeeAgentId;
        }
        emit DecisionRecorded(
            p.orderId, p.payeeAgentId, p.decision, p.amount, p.reasonCode, p.invoiceHash, p.decisionHash, tier
        );
    }

    /// @notice Record the delivery outcome of a direct payment (agent while not frozen, owner any time).
    function recordDelivery(bytes32 orderId, uint8 outcome, bytes32 deliveryHash) external {
        if (msg.sender == agent) {
            if (tier == AcbTypes.TIER_FROZEN) revert Frozen();
        } else if (msg.sender != owner) {
            revert NotAuthorized();
        }
        Order storage o = _orders[orderId];
        if (o.state != AcbTypes.STATE_PAID) revert BadOrderState(orderId, o.state);
        if (o.outcome != AcbTypes.OUTCOME_NONE) revert OutcomeAlreadySet(orderId);
        if (outcome < AcbTypes.OUTCOME_ON_TIME || outcome > AcbTypes.OUTCOME_FAILED) revert BadOutcome(outcome);
        if (outcome == AcbTypes.OUTCOME_FAILED && block.timestamp <= o.deadline) {
            revert DeadlineNotPassed(o.deadline);
        }
        o.outcome = outcome;
        o.deliveryHash = deliveryHash;
        if (outcome != AcbTypes.OUTCOME_FAILED) _cleanRecord(o.payeeAgentId, o.amount);
        emit DeliveryRecorded(orderId, o.payeeAgentId, msg.sender, outcome, deliveryHash);
    }

    /// @notice Record a violation by the wallet agent and apply the on-chain autonomy rule (spec 5.3):
    ///         severity 3 freezes, severity 2 demotes one tier, two minor points demote one tier.
    function recordViolation(uint8 kind, uint8 severity, bytes32 evidenceHash) external {
        if (msg.sender != agent && msg.sender != autonomyOperator) revert NotAuthorized();
        if (kind < AcbTypes.VK_LIMIT_PROPOSAL || kind > AcbTypes.VK_REPEATED_VIOLATIONS) revert BadViolationKind(kind);
        if (severity < AcbTypes.SEV_MINOR || severity > AcbTypes.SEV_SEVERE) revert BadSeverity(severity);
        uint8 t = tier;
        if (t != AcbTypes.TIER_FROZEN) {
            if (severity == AcbTypes.SEV_SEVERE) {
                _freeze(AcbTypes.R_FREEZE_SEVERE_VIOLATION, evidenceHash);
            } else if (severity == AcbTypes.SEV_SERIOUS && t > 0) {
                _changeTier(t - 1, AcbTypes.R_DEMOTE_SERIOUS_VIOLATION, evidenceHash);
            } else {
                // Points are the sum of severities since the last tier change (spec 5.3), saturating.
                uint16 pts = _satAdd16(violationPointsSinceChange, severity);
                violationPointsSinceChange = pts;
                if (severity == AcbTypes.SEV_MINOR && pts >= 2 && t > 0) {
                    _changeTier(t - 1, AcbTypes.R_DEMOTE_MINOR_VIOLATIONS, evidenceHash);
                }
            }
        }
        emit ViolationRecorded(kind, severity, evidenceHash, msg.sender, tier);
    }

    // ------------------------------------------------------------------ tier management

    /// @notice Promote by one (earned record, cooldown, `maxAutoTier`) or demote to any lower tier.
    function setTier(uint8 newTier, uint16 reasonCode, bytes32 evidenceHash) external onlyAutonomy notFrozen {
        if (newTier > AcbTypes.TIER_MAX) revert BadTier(newTier);
        uint8 t = tier;
        if (newTier == t) revert SameTier();
        if (newTier > t) {
            if (newTier != t + 1) revert PromotionTooLarge(t, newTier);
            if (newTier > maxAutoTier) revert AboveMaxAutoTier(newTier, maxAutoTier);
            uint256 readyAt = uint256(lastTierChangeAt) + promotionCooldown;
            if (block.timestamp < readyAt) revert PromotionCooldown(uint64(readyAt));
            uint16 needed = _tierConfig[t].minCleanToPromote;
            uint16 clean = cleanSinceChange;
            uint16 points = violationPointsSinceChange;
            if (needed == 0 || clean < needed || points != 0) revert PromotionNotEarned(clean, needed, points);
        }
        _changeTier(newTier, reasonCode, evidenceHash);
    }

    /// @notice Circuit breaker (owner, autonomy operator, or the agent itself).
    function freeze(uint16 reasonCode, bytes32 evidenceHash) external {
        if (msg.sender != owner && msg.sender != autonomyOperator && msg.sender != agent) revert NotAuthorized();
        if (tier == AcbTypes.TIER_FROZEN) revert Frozen();
        _freeze(reasonCode, evidenceHash);
    }

    /// @notice Human reset to tier 0 or 1 (probation), optionally rotating the agent key in the same tx.
    function unfreeze(uint8 toTier, uint16 reasonCode, address newAgent) external onlyOwner {
        if (tier != AcbTypes.TIER_FROZEN) revert NotFrozen();
        if (toTier > AcbTypes.TIER_SMALL) revert BadTier(toTier);
        if (newAgent != address(0)) _setAgent(newAgent);
        _changeTier(toTier, reasonCode, bytes32(0));
    }

    // ------------------------------------------------------------------ owner administration

    function setTierConfig(uint8 t, TierConfig calldata c) external onlyOwner {
        if (t > AcbTypes.TIER_MAX) revert BadTier(t);
        _validateConfig(t, c);
        _tierConfig[t] = c;
        _emitTierConfig(t, c);
    }

    function setReserveFloor(uint128 v) external onlyOwner {
        reserveFloor = v;
        emit ConfigChanged("reserveFloor", v);
    }

    function setMinCleanAmount(uint128 v) external onlyOwner {
        minCleanAmount = v;
        emit ConfigChanged("minCleanAmount", v);
    }

    /// @notice Lower or raise the autonomy ceiling. While frozen it only stores the ceiling and clamps
    ///         `tierBeforeFreeze`; it never unfreezes. Otherwise a tier above the new ceiling is demoted (3104).
    function setMaxAutoTier(uint8 m) external onlyOwner {
        if (m > AcbTypes.TIER_MAX) revert BadTier(m);
        maxAutoTier = m;
        emit ConfigChanged("maxAutoTier", m);
        uint8 t = tier;
        if (t == AcbTypes.TIER_FROZEN) {
            if (tierBeforeFreeze > m) tierBeforeFreeze = m;
        } else if (t > m) {
            _changeTier(m, AcbTypes.R_DEMOTE_OWNER_LIMIT, bytes32(0));
        }
    }

    function setPromotionCooldown(uint64 v) external onlyOwner {
        promotionCooldown = v;
        emit ConfigChanged("promotionCooldown", v);
    }

    function setMaxGradeAge(uint64 v) external onlyOwner {
        maxGradeAge = v;
        emit ConfigChanged("maxGradeAge", v);
    }

    function setWindowSeconds(uint64 v) external onlyOwner {
        if (v == 0) revert BadWindow();
        windowSeconds = v;
        emit ConfigChanged("windowSeconds", v);
    }

    function setDisputeWindow(uint64 v) external onlyOwner {
        if (v > MAX_DISPUTE_WINDOW) revert BadWindow();
        disputeWindow = v;
        emit ConfigChanged("disputeWindow", v);
    }

    /// @notice Rotate the agent key (must stay distinct from owner and autonomy operator).
    function setAgent(address newAgent) external onlyOwner {
        _setAgent(newAgent);
    }

    function setAutonomyOperator(address newOperator) external onlyOwner {
        if (newOperator == address(0)) revert ZeroAddress();
        if (newOperator == owner || newOperator == agent) revert RolesNotDistinct();
        address old = autonomyOperator;
        autonomyOperator = newOperator;
        emit AutonomyOperatorChanged(old, newOperator);
    }

    /// @notice Withdraw free funds (balance minus escrow-locked; the owner may go below the reserve floor).
    function withdraw(address to, uint128 amount) external onlyOwner nonReentrant {
        if (to == address(0)) revert ZeroAddress();
        uint256 bal = token.balanceOf(address(this));
        uint256 free = bal > escrowLocked ? bal - escrowLocked : 0;
        if (free < amount) revert InsufficientUnlocked(free, amount);
        _safeTransfer(to, amount);
        emit Withdrawn(to, amount);
    }

    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert ZeroAddress();
        pendingOwner = newOwner;
        emit OwnershipTransferStarted(msg.sender, newOwner);
    }

    function acceptOwnership() external {
        if (msg.sender != pendingOwner) revert NotPendingOwner();
        if (msg.sender == agent || msg.sender == autonomyOperator) revert RolesNotDistinct();
        address old = owner;
        owner = msg.sender;
        pendingOwner = address(0);
        emit OwnershipTransferred(old, msg.sender);
    }

    // ------------------------------------------------------------------ views

    function tierConfig(uint8 t) external view returns (TierConfig memory) {
        if (t > AcbTypes.TIER_MAX) revert BadTier(t);
        return _tierConfig[t];
    }

    function getOrder(bytes32 orderId) external view returns (Order memory) {
        return _orders[orderId];
    }

    function getApproval(bytes32 orderId) external view returns (Approval memory) {
        return _approvals[orderId];
    }

    /// @notice Sum and count of outflows inside the rolling window (all 32 ring slots).
    function spentInWindow() public view returns (uint128 spent, uint8 count) {
        uint256 w = windowSeconds;
        for (uint256 i = 0; i < RING_SIZE; i++) {
            uint256 slot = _ring[i];
            if (_inWindow(slot, w)) {
                spent += uint128(slot);
                count++;
            }
        }
    }

    function isFrozen() public view returns (bool) {
        return tier == AcbTypes.TIER_FROZEN;
    }

    /// @notice Balance minus escrow-locked minus reserve floor, floored at 0.
    function freeBalance() public view returns (uint256) {
        uint256 bal = token.balanceOf(address(this));
        uint256 locked = uint256(escrowLocked) + reserveFloor;
        return bal > locked ? bal - locked : 0;
    }

    function status() external view returns (VaultStatus memory s) {
        (uint128 spent, uint8 count) = spentInWindow();
        s.tier = tier;
        s.frozen = tier == AcbTypes.TIER_FROZEN;
        s.tierBeforeFreeze = tierBeforeFreeze;
        s.maxAutoTier = maxAutoTier;
        s.balance = token.balanceOf(address(this));
        s.escrowLocked = escrowLocked;
        s.reserveFloor = reserveFloor;
        s.spent = spent;
        s.count = count;
        s.clean = cleanSinceChange;
        s.points = violationPointsSinceChange;
        s.lastTierChangeAt = lastTierChangeAt;
    }

    // ------------------------------------------------------------------ internals

    /// @dev Common checks of pay and escrow (spec 3.2 "Checks in order"), then the mandate spend.
    function _openOutflow(OutflowParams calldata p, bool isEscrow) internal returns (address pinned) {
        if (_orders[p.orderId].state != AcbTypes.STATE_NONE) revert OrderExists(p.orderId);
        if (p.amount == 0) revert ZeroAmount();
        if (p.deadline <= block.timestamp) revert BadDeadline();
        TierConfig memory cfg = _tierConfig[tier];
        // 1. mandate
        Mandate storage m = _checkMandate(p.mandateId, p.payeeAgentId, p.amount);
        pinned = mandatePayee[p.mandateId][p.payeeAgentId];
        // 2. tier per-tx cap
        if (p.amount > cfg.perTxCap) revert PerTxCapExceeded(p.amount, cfg.perTxCap);
        // 3. grade mask and new-payee cap
        uint8 g = _grade(p.payeeAgentId);
        uint8 mask = isEscrow ? cfg.escrowGradeMask : cfg.payGradeMask;
        if ((mask >> g) & 1 != 1) revert GradeNotAllowed(p.payeeAgentId, g, mask);
        if ((g == AcbTypes.GRADE_NEW || g == AcbTypes.GRADE_NONE) && p.amount > cfg.newPayeeCap) {
            revert NewPayeeCapExceeded(p.amount, cfg.newPayeeCap);
        }
        // 4. payee identity
        _checkPayee(p.payeeAgentId, pinned);
        // 5. reserve floor
        _checkReserve(p.amount);
        // 6. rolling window
        _recordSpend(p.amount, true, cfg);
        m.spent += p.amount;
    }

    function _checkMandate(bytes32 mandateId, uint256 payeeAgentId, uint128 amount)
        internal
        view
        returns (Mandate storage m)
    {
        m = _mandates[mandateId];
        if (!m.active) revert MandateInactive(mandateId);
        if (block.timestamp > m.validUntil) revert MandateExpired(mandateId);
        if (mandatePayee[mandateId][payeeAgentId] == address(0)) revert NotInMandate(mandateId, payeeAgentId);
        if (amount > m.maxPerTx) revert MandatePerTxExceeded(amount, m.maxPerTx);
        if (uint256(m.spent) + amount > m.maxTotal) revert MandateBudgetExceeded(m.spent, amount, m.maxTotal);
        if (m.recurring && tier < AcbTypes.TIER_STANDARD) revert RecurringNeedsTier2();
    }

    function _grade(uint256 payeeAgentId) internal view returns (uint8) {
        return bureau.effectiveGrade(payeeAgentId, maxGradeAge);
    }

    function _checkPayee(uint256 payeeAgentId, address pinned) internal view {
        if (payeeAgentId == agentId) revert SelfDealing(address(0));
        address live = identity.getAgentWallet(payeeAgentId);
        if (live != pinned) revert PayeeWalletChanged(payeeAgentId, live, pinned);
        if (pinned == address(this) || pinned == owner || pinned == agent || pinned == autonomyOperator) {
            revert SelfDealing(pinned);
        }
    }

    function _checkReserve(uint128 amount) internal view {
        uint256 bal = token.balanceOf(address(this));
        if (bal < uint256(escrowLocked) + reserveFloor + amount) {
            revert ReserveFloorBreached(bal, escrowLocked, reserveFloor, amount);
        }
    }

    /// @dev Rolling window over the last 32 outflows. `enforce` applies the tier's count and sum caps;
    ///      the ring itself is a hard velocity stop of 32 outflows per window for every outflow.
    function _recordSpend(uint128 amount, bool enforce, TierConfig memory cfg) internal {
        if (enforce) {
            (uint128 spent, uint8 count) = spentInWindow();
            if (uint256(count) + 1 > cfg.maxTxPerWindow) revert TooManyTx(count, cfg.maxTxPerWindow);
            if (uint256(spent) + amount > cfg.windowCap) revert WindowCapExceeded(spent, amount, cfg.windowCap);
        }
        uint8 head = _ringHead;
        if (_inWindow(_ring[head], windowSeconds)) revert RingFull();
        _ring[head] = (uint256(uint64(block.timestamp)) << 128) | uint256(amount);
        _ringHead = (head + 1) % RING_SIZE;
    }

    /// @dev A slot is inside the window if it was written (non-zero) and `ts > now - windowSeconds`.
    function _inWindow(uint256 slot, uint256 w) internal view returns (bool) {
        if (slot == 0) return false;
        return (slot >> 128) + w > block.timestamp;
    }

    /// @dev Clean-record rule (spec 3.2): count only if amount >= minCleanAmount and payee graded A or B now.
    function _cleanRecord(uint256 payeeAgentId, uint128 amount) internal {
        if (amount < minCleanAmount) return;
        uint8 g = _grade(payeeAgentId);
        if (g != AcbTypes.GRADE_A && g != AcbTypes.GRADE_B) return;
        uint32 c = completedWith[payeeAgentId];
        if (c != type(uint32).max) completedWith[payeeAgentId] = c + 1;
        cleanSinceChange = _satAdd16(cleanSinceChange, 1);
    }

    function _freeze(uint16 reasonCode, bytes32 evidenceHash) internal {
        tierBeforeFreeze = tier;
        _changeTier(AcbTypes.TIER_FROZEN, reasonCode, evidenceHash);
    }

    function _changeTier(uint8 to, uint16 reason, bytes32 evidence) internal {
        uint8 from = tier;
        tier = to;
        lastTierChangeAt = uint64(block.timestamp);
        cleanSinceChange = 0;
        violationPointsSinceChange = 0;
        emit TierChanged(from, to, reason, evidence, msg.sender);
    }

    function _setAgent(address newAgent) internal {
        if (newAgent == address(0)) revert ZeroAddress();
        if (newAgent == owner || newAgent == autonomyOperator) revert RolesNotDistinct();
        address old = agent;
        agent = newAgent;
        emit AgentChanged(old, newAgent);
    }

    function _validateConfig(uint8 t, TierConfig memory c) internal pure {
        if ((c.payGradeMask & AcbTypes.MASK_D) != 0 || (c.escrowGradeMask & AcbTypes.MASK_D) != 0) {
            revert MaskIncludesD();
        }
        if (c.maxTxPerWindow > RING_SIZE || c.perTxCap > c.windowCap || c.newPayeeCap > c.perTxCap) {
            revert BadTierConfig(t);
        }
        if (t == 0 && (c.perTxCap != 0 || c.payGradeMask != 0 || c.escrowGradeMask != 0)) revert BadTierConfig(t);
        // Masks may only use the six defined grade bits.
        if ((c.payGradeMask | c.escrowGradeMask) > 0x3F) revert BadTierConfig(t);
    }

    function _emitTierConfig(uint8 t, TierConfig memory c) internal {
        emit TierConfigSet(
            t,
            c.perTxCap,
            c.windowCap,
            c.newPayeeCap,
            c.maxTxPerWindow,
            c.payGradeMask,
            c.escrowGradeMask,
            c.minCleanToPromote
        );
    }

    function _safeTransfer(address to, uint256 amount) internal {
        (bool ok, bytes memory ret) = address(token).call(abi.encodeCall(IERC20Minimal.transfer, (to, amount)));
        if (!ok) revert TransferFailed();
        if (ret.length == 0) {
            // No return value is accepted (USDT style) only from a contract: a call to an address without code
            // "succeeds" with empty return data.
            if (address(token).code.length == 0) revert TransferFailed();
        } else if (ret.length < 32 || !abi.decode(ret, (bool))) {
            revert TransferFailed();
        }
    }

    function _satAdd16(uint16 a, uint16 b) internal pure returns (uint16) {
        uint256 s = uint256(a) + b;
        return s > type(uint16).max ? type(uint16).max : uint16(s);
    }
}
