// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {AcbTypes} from "./lib/AcbTypes.sol";
import {IIdentityRegistry} from "./interfaces/IIdentityRegistry.sol";
import {ITierVault} from "./interfaces/ITierVault.sol";

/// @title AgentCreditBureau
/// @notice Grade registry for ERC-8004 agents (latest grade per agentId with score, reason codes, model id,
///         config hash, evidence hash and as-of block; history via events), registry of legitimate
///         TierVaults (code hash and configuration bound), and a dispute/appeal stub. Spec section 3.1.
/// @dev Grade upgrades take effect only after `upgradeDelay`; downgrades are immediate. A posting made while an
///      upgrade is still hidden keeps that upgrade's baseline, so reposting cannot shorten the delay.
contract AgentCreditBureau {
    struct GradeRecord {
        uint8 grade; // AcbTypes grade, never NONE once written
        uint8 prevGrade; // grade in effect before this posting (NONE if first); a still-hidden upgrade's baseline
        uint16 score; // 300..850
        uint64 postedAt; // block.timestamp
        uint64 prevPostedAt; // postedAt of that baseline grade (0 if first)
        uint64 asOfBlock; // block the evidence was computed at (< posting block)
        uint64 reasons; // 4 x uint16 packed, reasons[0] in the low 16 bits; 0 = empty slot
        address poster;
        bytes32 modelId; // e.g. bytes32("acb-sc-1.0.0")
        bytes32 configHash; // keccak of canonical scorecard config
        bytes32 evidenceHash; // keccak of canonical evidence bundle
    }

    uint16 public constant MIN_SCORE = 300;
    uint16 public constant MAX_SCORE = 850;
    uint64 public constant MAX_UPGRADE_DELAY = 7 days;

    address public admin;
    address public pendingAdmin;
    IIdentityRegistry public identity;
    address public token; // the canonical USDC for this deployment
    bytes32 public vaultCodeHash;
    uint64 public upgradeDelay; // seconds before a better grade takes effect
    mapping(address => bool) public isPoster;
    mapping(uint256 => GradeRecord) internal _records;
    mapping(address => bool) public isVault;
    mapping(address => uint256) public vaultAgentId;
    uint256 public appealCount;
    mapping(uint256 => uint256) public appealAgent;

    event GradePosted(
        uint256 indexed agentId,
        uint8 grade,
        uint8 previousGrade,
        uint16 score,
        uint16[4] reasons,
        bytes32 indexed modelId,
        bytes32 configHash,
        bytes32 evidenceHash,
        uint64 asOfBlock,
        address indexed poster,
        string evidenceURI
    );
    event PosterSet(address indexed poster, bool allowed);
    event VaultCodeHashSet(bytes32 codeHash);
    event UpgradeDelaySet(uint64 delay);
    event VaultRegistered(address indexed vault, uint256 indexed agentId, address indexed registrant);
    event AppealFiled(
        uint256 indexed agentId, uint256 indexed appealId, address indexed filer, bytes32 appealHash, string uri
    );
    event AppealResolved(uint256 indexed appealId, bool upheld, bytes32 evidenceHash);
    event AdminTransferStarted(address indexed from, address indexed to);
    event AdminTransferred(address indexed from, address indexed to);

    error NotAdmin();
    error NotPendingAdmin();
    error NotPoster();
    error ZeroAddress();
    error ZeroHash();
    error BadGrade(uint8 grade);
    error BadScore(uint16 score);
    error AsOfInFuture(uint64 asOfBlock);
    error StaleAsOf(uint64 asOfBlock, uint64 lastAsOfBlock);
    error SelfGrading(uint256 agentId);
    error BadVaultCode(bytes32 codehash);
    error BadVaultConfig();
    error VaultExists();
    error NotAgentOwner(uint256 agentId);
    error UnknownAppeal(uint256 appealId);
    error BadDelay();

    modifier onlyAdmin() {
        if (msg.sender != admin) revert NotAdmin();
        _;
    }

    modifier onlyPoster() {
        if (!isPoster[msg.sender]) revert NotPoster();
        _;
    }

    constructor(address admin_, address identity_, address token_, bytes32 vaultCodeHash_, uint64 upgradeDelay_) {
        if (admin_ == address(0) || identity_ == address(0) || token_ == address(0)) revert ZeroAddress();
        if (upgradeDelay_ > MAX_UPGRADE_DELAY) revert BadDelay();
        admin = admin_;
        identity = IIdentityRegistry(identity_);
        token = token_;
        vaultCodeHash = vaultCodeHash_;
        upgradeDelay = upgradeDelay_;
        emit AdminTransferred(address(0), admin_);
        emit VaultCodeHashSet(vaultCodeHash_);
        emit UpgradeDelaySet(upgradeDelay_);
    }

    // ------------------------------------------------------------------ grades

    /// @notice Post the latest grade of `agentId`. Checks run in the order of spec 3.1.
    function postGrade(
        uint256 agentId,
        uint8 grade,
        uint16 score,
        uint16[4] calldata reasons,
        bytes32 modelId,
        bytes32 configHash,
        bytes32 evidenceHash,
        uint64 asOfBlock,
        string calldata evidenceURI
    ) external {
        if (!isPoster[msg.sender]) revert NotPoster();
        if (grade < AcbTypes.GRADE_NEW || grade > AcbTypes.GRADE_A) revert BadGrade(grade);
        if (score < MIN_SCORE || score > MAX_SCORE) revert BadScore(score);
        if (evidenceHash == bytes32(0) || modelId == bytes32(0)) revert ZeroHash();
        if (asOfBlock >= block.number) revert AsOfInFuture(asOfBlock);
        GradeRecord storage r = _records[agentId];
        uint64 last = r.asOfBlock;
        if (asOfBlock < last) revert StaleAsOf(asOfBlock, last);
        // Also reverts for a nonexistent agent (ERC721NonexistentToken).
        if (identity.isAuthorizedOrOwner(msg.sender, agentId)) revert SelfGrading(agentId);

        uint8 previous = r.grade;
        // Baseline for the upgrade delay: the grade in effect now. If the old record is itself an upgrade that
        // is still hidden, its own baseline stays in effect and is carried forward; otherwise a repost of the
        // pending grade (or a lower one above the baseline) would make it effective at once.
        uint8 baseGrade = previous;
        uint64 basePostedAt = r.postedAt;
        if (
            AcbTypes.rank(previous) > AcbTypes.rank(r.prevGrade)
                && block.timestamp < uint256(r.postedAt) + uint256(upgradeDelay)
        ) {
            baseGrade = r.prevGrade;
            basePostedAt = r.prevPostedAt;
        }
        uint64 packed =
            uint64(reasons[0]) | (uint64(reasons[1]) << 16) | (uint64(reasons[2]) << 32) | (uint64(reasons[3]) << 48);
        _records[agentId] = GradeRecord({
            grade: grade,
            prevGrade: baseGrade,
            score: score,
            postedAt: uint64(block.timestamp),
            prevPostedAt: basePostedAt,
            asOfBlock: asOfBlock,
            reasons: packed,
            poster: msg.sender,
            modelId: modelId,
            configHash: configHash,
            evidenceHash: evidenceHash
        });
        emit GradePosted(
            agentId,
            grade,
            previous,
            score,
            reasons,
            modelId,
            configHash,
            evidenceHash,
            asOfBlock,
            msg.sender,
            evidenceURI
        );
    }

    /// @notice Raw latest grade; `(NONE, 0)` if never posted.
    function gradeOf(uint256 agentId) external view returns (uint8 grade, uint64 postedAt) {
        GradeRecord storage r = _records[agentId];
        return (r.grade, r.postedAt);
    }

    /// @notice Grade a vault should act on: upgrade delay first, then staleness (spec 3.1).
    /// @param maxAge 0 = no staleness rule; otherwise a grade older than `maxAge` seconds is NONE unless D or C.
    function effectiveGrade(uint256 agentId, uint64 maxAge) external view returns (uint8) {
        GradeRecord storage r = _records[agentId];
        uint8 g = r.grade;
        if (g == AcbTypes.GRADE_NONE) return AcbTypes.GRADE_NONE;
        uint256 postedAt = r.postedAt;
        if (AcbTypes.rank(g) > AcbTypes.rank(r.prevGrade) && block.timestamp < postedAt + uint256(upgradeDelay)) {
            g = r.prevGrade;
            postedAt = r.prevPostedAt;
            if (g == AcbTypes.GRADE_NONE) return AcbTypes.GRADE_NONE;
        }
        if (maxAge != 0 && block.timestamp > postedAt + uint256(maxAge)) {
            return (g == AcbTypes.GRADE_D || g == AcbTypes.GRADE_C) ? g : AcbTypes.GRADE_NONE;
        }
        return g;
    }

    function getRecord(uint256 agentId) external view returns (GradeRecord memory) {
        return _records[agentId];
    }

    function reasonsOf(uint256 agentId) external view returns (uint16[4] memory out) {
        uint64 packed = _records[agentId].reasons;
        for (uint256 i = 0; i < 4; i++) {
            out[i] = uint16(packed >> (16 * i));
        }
    }

    // ------------------------------------------------------------------ admin

    function setPoster(address poster, bool allowed) external onlyAdmin {
        if (poster == address(0)) revert ZeroAddress();
        isPoster[poster] = allowed;
        emit PosterSet(poster, allowed);
    }

    function setVaultCodeHash(bytes32 codeHash) external onlyAdmin {
        vaultCodeHash = codeHash;
        emit VaultCodeHashSet(codeHash);
    }

    function setUpgradeDelay(uint64 delay) external onlyAdmin {
        if (delay > MAX_UPGRADE_DELAY) revert BadDelay();
        upgradeDelay = delay;
        emit UpgradeDelaySet(delay);
    }

    function transferAdmin(address newAdmin) external onlyAdmin {
        if (newAdmin == address(0)) revert ZeroAddress();
        pendingAdmin = newAdmin;
        emit AdminTransferStarted(msg.sender, newAdmin);
    }

    function acceptAdmin() external {
        if (msg.sender != pendingAdmin) revert NotPendingAdmin();
        address previous = admin;
        admin = msg.sender;
        pendingAdmin = address(0);
        emit AdminTransferred(previous, msg.sender);
    }

    // ------------------------------------------------------------------ vaults

    /// @notice Register a legitimate vault: same runtime code as the canonical TierVault AND bound to this
    ///         bureau's token, bureau and identity registry. Caller must be authorised for the vault's agent.
    function registerVault(address vault) external {
        bytes32 codehash = vault.codehash;
        if (codehash != vaultCodeHash) revert BadVaultCode(codehash);
        if (isVault[vault]) revert VaultExists();
        ITierVault v = ITierVault(vault);
        if (v.token() != token || v.bureau() != address(this) || v.identity() != address(identity)) {
            revert BadVaultConfig();
        }
        uint256 id = v.agentId();
        // Reverts for a nonexistent agent (ERC721NonexistentToken).
        if (!identity.isAuthorizedOrOwner(msg.sender, id)) revert NotAgentOwner(id);
        isVault[vault] = true;
        vaultAgentId[vault] = id;
        emit VaultRegistered(vault, id, msg.sender);
    }

    // ------------------------------------------------------------------ appeals

    /// @notice File an appeal against the grade of `agentId` (owner or operator of the agent only).
    function fileAppeal(uint256 agentId, bytes32 appealHash, string calldata uri) external returns (uint256 appealId) {
        if (!identity.isAuthorizedOrOwner(msg.sender, agentId)) revert NotAgentOwner(agentId);
        appealId = ++appealCount;
        appealAgent[appealId] = agentId;
        emit AppealFiled(agentId, appealId, msg.sender, appealHash, uri);
    }

    /// @notice Resolve an appeal. A grade change after an upheld appeal is a normal `postGrade` (reason 1102).
    function resolveAppeal(uint256 appealId, bool upheld, bytes32 evidenceHash) external onlyPoster {
        if (appealId == 0 || appealId > appealCount) revert UnknownAppeal(appealId);
        emit AppealResolved(appealId, upheld, evidenceHash);
    }
}
