// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {Test} from "forge-std/Test.sol";
import {AgentCreditBureau} from "../../src/AgentCreditBureau.sol";
import {TierVault} from "../../src/TierVault.sol";
import {MockUSDC} from "../../src/MockUSDC.sol";
import {IdentityRegistryLite} from "../../src/erc8004/IdentityRegistryLite.sol";
import {ReputationRegistryLite} from "../../src/erc8004/ReputationRegistryLite.sol";
import {AcbTypes} from "../../src/lib/AcbTypes.sol";

/// @notice Shared fixture: ERC-8004 Lite registries, MockUSDC, the bureau, graded payees (A, B, C, D, NEW,
///         never graded) and one wallet-profile vault (spec 5.1) funded with 15 USDC.
abstract contract VaultTestBase is Test {
    uint128 internal constant USDC = 1_000_000;
    uint64 internal constant T0 = 1_760_000_000;

    MockUSDC internal usdc;
    IdentityRegistryLite internal identity;
    ReputationRegistryLite internal reputation;
    AgentCreditBureau internal acb;
    TierVault internal vault;

    address internal admin = makeAddr("admin");
    address internal poster = makeAddr("poster");
    address internal owner = makeAddr("owner");
    address internal agent = makeAddr("agent");
    address internal autonomy = makeAddr("autonomy");
    address internal outsider = makeAddr("outsider");

    // payees: identity owner == registered wallet (register() sets agentWallet = msg.sender)
    address internal walA;
    address internal walB;
    address internal walC;
    address internal walD;
    address internal walNew;
    address internal walNone;
    uint256 internal idWallet; // the vault's own agentId (owned by `owner`)
    uint256 internal idA;
    uint256 internal idB;
    uint256 internal idC;
    uint256 internal idD;
    uint256 internal idNew;
    uint256 internal idNone;

    bytes32 internal constant M1 = keccak256("M1"); // demo mandate: A, B, C, NEW, NONE; 1.00 per tx, 4.00 total
    bytes32 internal constant MALL = keccak256("MALL"); // every payee incl. D; 4.00 per tx, 20.00 total
    bytes32 internal constant MODEL = bytes32("acb-sc-1.0.0");

    uint256 internal orderNonce;

    function setUp() public virtual {
        vm.warp(T0);
        vm.roll(100);
        usdc = new MockUSDC(address(this));
        identity = new IdentityRegistryLite();
        reputation = new ReputationRegistryLite(address(identity));
        acb = new AgentCreditBureau(admin, address(identity), address(usdc), keccak256(type(TierVault).runtimeCode), 0);
        vm.prank(admin);
        acb.setPoster(poster, true);

        vm.prank(owner);
        idWallet = identity.register("ipfs://wallet");
        (idA, walA) = _registerPayee("payeeA");
        (idB, walB) = _registerPayee("payeeB");
        (idC, walC) = _registerPayee("payeeC");
        (idD, walD) = _registerPayee("payeeD");
        (idNew, walNew) = _registerPayee("payeeNew");
        (idNone, walNone) = _registerPayee("payeeNone");

        _post(idA, AcbTypes.GRADE_A);
        _post(idB, AcbTypes.GRADE_B);
        _post(idC, AcbTypes.GRADE_C);
        _post(idD, AcbTypes.GRADE_D);
        _post(idNew, AcbTypes.GRADE_NEW);

        vault = _deployVault(1);
        usdc.mint(address(vault), 15 * USDC);
        _registerDefaultMandates();
    }

    // ------------------------------------------------------------------ helpers: setup

    function _registerPayee(string memory label) internal returns (uint256 id, address wallet) {
        wallet = makeAddr(label);
        vm.prank(wallet);
        id = identity.register(string.concat("ipfs://", label));
    }

    function _post(uint256 id, uint8 grade) internal {
        _postScore(id, grade, 700);
    }

    function _postScore(uint256 id, uint8 grade, uint16 score) internal {
        uint16[4] memory reasons = [uint16(1100), 0, 0, 0];
        vm.prank(poster);
        acb.postGrade(
            id,
            grade,
            score,
            reasons,
            MODEL,
            keccak256("cfg"),
            keccak256(abi.encode(id, grade, vm.getBlockTimestamp())),
            uint64(vm.getBlockNumber() - 1),
            "ipfs://bundle"
        );
    }

    function _walletConfigs() internal pure returns (TierVault.TierConfig[4] memory c) {
        uint8 ab = AcbTypes.MASK_A | AcbTypes.MASK_B;
        uint8 esc = AcbTypes.MASK_A | AcbTypes.MASK_B | AcbTypes.MASK_C | AcbTypes.MASK_NEW | AcbTypes.MASK_NONE;
        c[0] = TierVault.TierConfig(0, 0, 0, 0, 0, 0, 3);
        c[1] = TierVault.TierConfig(1 * USDC, 5 * USDC, USDC / 2, 10, ab, esc, 3);
        c[2] = TierVault.TierConfig(2 * USDC, 10 * USDC, 1 * USDC, 20, ab, esc, 6);
        c[3] = TierVault.TierConfig(4 * USDC, 20 * USDC, 1 * USDC, 32, ab | AcbTypes.MASK_NEW, esc, 0);
    }

    function _init(uint8 initialTier) internal view returns (TierVault.VaultInit memory init) {
        init.token = address(usdc);
        init.bureau = address(acb);
        init.identity = address(identity);
        init.owner = owner;
        init.agent = agent;
        init.autonomyOperator = autonomy;
        init.agentId = idWallet;
        init.configs = _walletConfigs();
        init.reserveFloor = 3 * USDC;
        init.minCleanAmount = USDC / 10;
        init.windowSeconds = 86_400;
        init.promotionCooldown = 60;
        init.maxGradeAge = 7 days;
        init.disputeWindow = 120;
        init.maxAutoTier = 2;
        init.initialTier = initialTier;
    }

    function _deployVault(uint8 initialTier) internal returns (TierVault) {
        return new TierVault(_init(initialTier));
    }

    function _registerDefaultMandates() internal {
        uint256[] memory ids = new uint256[](5);
        address[] memory wals = new address[](5);
        (ids[0], ids[1], ids[2], ids[3], ids[4]) = (idA, idB, idC, idNew, idNone);
        (wals[0], wals[1], wals[2], wals[3], wals[4]) = (walA, walB, walC, walNew, walNone);
        vm.prank(owner);
        vault.registerMandate(
            M1, ids, wals, 1 * USDC, 4 * USDC, uint64(vm.getBlockTimestamp() + 1 days), false, bytes32("p1")
        );

        uint256[] memory ids6 = new uint256[](6);
        address[] memory wals6 = new address[](6);
        (ids6[0], ids6[1], ids6[2], ids6[3], ids6[4], ids6[5]) = (idA, idB, idC, idD, idNew, idNone);
        (wals6[0], wals6[1], wals6[2], wals6[3], wals6[4], wals6[5]) = (walA, walB, walC, walD, walNew, walNone);
        vm.prank(owner);
        vault.registerMandate(
            MALL, ids6, wals6, 4 * USDC, 20 * USDC, uint64(vm.getBlockTimestamp() + 7 days), false, bytes32("pall")
        );
    }

    // ------------------------------------------------------------------ helpers: actions

    function _oid() internal returns (bytes32) {
        return keccak256(abi.encode("order", ++orderNonce));
    }

    function _out(bytes32 orderId, uint256 payeeId, uint128 amount, bytes32 mandateId, uint8 decision)
        internal
        view
        returns (TierVault.OutflowParams memory p)
    {
        p = TierVault.OutflowParams({
            orderId: orderId,
            payeeAgentId: payeeId,
            amount: amount,
            deadline: uint64(vm.getBlockTimestamp() + 600),
            mandateId: mandateId,
            invoiceHash: keccak256(abi.encode("inv", orderId)),
            decisionHash: keccak256(abi.encode("dec", orderId)),
            decision: decision,
            reasonCode: 2000
        });
    }

    function _pay(uint256 payeeId, uint128 amount, bytes32 mandateId) internal returns (bytes32 orderId) {
        orderId = _oid();
        vm.prank(agent);
        vault.pay(_out(orderId, payeeId, amount, mandateId, AcbTypes.DECISION_PAY));
    }

    function _escrow(uint256 payeeId, uint128 amount, bytes32 mandateId) internal returns (bytes32 orderId) {
        orderId = _oid();
        vm.prank(agent);
        vault.escrow(_out(orderId, payeeId, amount, mandateId, AcbTypes.DECISION_ESCROW));
    }

    function _setTier(uint8 t) internal {
        // demote through the autonomy operator; promote through earned clean records (as on-chain)
        if (t < vault.tier()) {
            vm.prank(autonomy);
            vault.setTier(t, 3100, bytes32(0));
        } else if (t > vault.tier()) {
            vm.startPrank(owner);
            vault.setMaxAutoTier(3);
            vm.stopPrank();
            while (vault.tier() < t) {
                _earnPromotion();
            }
        }
    }

    /// @dev Make enough clean grade-A settlements, wait the cooldown and promote one tier.
    function _earnPromotion() internal {
        uint8 t = vault.tier();
        uint16 needed = vault.tierConfig(t).minCleanToPromote;
        for (uint256 i = 0; i < needed; i++) {
            if (t == 0) {
                bytes32 oid = _oid();
                vm.prank(owner);
                vault.ownerApprove(oid, idA, walA, USDC / 5, uint64(vm.getBlockTimestamp() + 600), false, 0);
                vm.prank(agent);
                vault.executeApproved(oid, bytes32(0), bytes32(0));
                vm.prank(agent);
                vault.recordDelivery(oid, AcbTypes.OUTCOME_ON_TIME, keccak256("d"));
            } else {
                bytes32 oid = _pay(idA, USDC / 5, MALL);
                vm.prank(agent);
                vault.recordDelivery(oid, AcbTypes.OUTCOME_ON_TIME, keccak256("d"));
            }
        }
        vm.warp(vm.getBlockTimestamp() + vault.promotionCooldown());
        vm.prank(autonomy);
        vault.setTier(t + 1, 3000, bytes32(0));
    }

    function _sel(bytes4 s) internal pure returns (bytes memory) {
        return abi.encodeWithSelector(s);
    }
}
