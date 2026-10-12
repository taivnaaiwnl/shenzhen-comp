// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {VaultTestBase} from "./utils/VaultTestBase.sol";
import {TierVault} from "../src/TierVault.sol";
import {AcbTypes} from "../src/lib/AcbTypes.sol";
import {IdentityRegistryLite} from "../src/erc8004/IdentityRegistryLite.sol";
import {ERC721Gift} from "./mocks/ERC721Gift.sol";

/// @notice Earned Autonomy: promotion, the clean-record rule, violations, freeze, unfreeze (spec 3.2, 5.3).
contract TierVaultAutonomyTest is VaultTestBase {
    uint256 internal constant SLOT_COUNTERS = 9; // maxGradeAge | disputeWindow | cleanSinceChange | points
    uint256 internal constant SLOT_COMPLETED_WITH = 24;

    function _clean(uint256 n) internal {
        for (uint256 i = 0; i < n; i++) {
            bytes32 oid = _pay(idA, USDC / 5, MALL);
            vm.prank(agent);
            vault.recordDelivery(oid, AcbTypes.OUTCOME_ON_TIME, keccak256("d"));
        }
    }

    function _promote(uint8 to) internal {
        vm.prank(autonomy);
        vault.setTier(to, 3000, keccak256("ev"));
    }

    function _promoteExpect(uint8 to, bytes memory err) internal {
        vm.prank(autonomy);
        vm.expectRevert(err);
        vault.setTier(to, 3000, bytes32(0));
    }

    function _violation(address by, uint8 kind, uint8 sev) internal {
        vm.prank(by);
        vault.recordViolation(kind, sev, keccak256("violation"));
    }

    function _assertCounters(uint16 clean, uint16 points) internal view {
        assertEq(vault.cleanSinceChange(), clean, "clean");
        assertEq(vault.violationPointsSinceChange(), points, "points");
    }

    // ------------------------------------------------------------------ promotion

    function test_promotion_happyPath() public {
        _clean(3);
        vm.warp(vm.getBlockTimestamp() + 60);
        vm.expectEmit(true, true, true, true, address(vault));
        emit TierVault.TierChanged(1, 2, 3000, keccak256("ev"), autonomy);
        _promote(2);
        assertEq(vault.tier(), 2);
        assertEq(vault.lastTierChangeAt(), vm.getBlockTimestamp());
        _assertCounters(0, 0);
    }

    function test_promotion_mustBePlusOne() public {
        vm.prank(owner);
        vault.setMaxAutoTier(3);
        _clean(3);
        vm.warp(vm.getBlockTimestamp() + 60);
        _promoteExpect(3, abi.encodeWithSelector(TierVault.PromotionTooLarge.selector, 1, 3));
    }

    function test_promotion_cooldown() public {
        uint64 changedAt = vault.lastTierChangeAt();
        _clean(3);
        vm.warp(changedAt + 59);
        _promoteExpect(2, abi.encodeWithSelector(TierVault.PromotionCooldown.selector, changedAt + 60));
        vm.warp(changedAt + 60);
        _promote(2);
    }

    function test_promotion_needsCleanCount() public {
        _clean(2);
        vm.warp(vm.getBlockTimestamp() + 60);
        _promoteExpect(2, abi.encodeWithSelector(TierVault.PromotionNotEarned.selector, 2, 3, 0));
        _clean(1);
        _promote(2);
    }

    function test_promotion_needsZeroPoints() public {
        _clean(3);
        _violation(agent, AcbTypes.VK_LIMIT_PROPOSAL, 1);
        vm.warp(vm.getBlockTimestamp() + 60);
        _promoteExpect(2, abi.encodeWithSelector(TierVault.PromotionNotEarned.selector, 3, 3, 1));
    }

    function test_promotion_maxAutoTier() public {
        _setTier(2);
        vm.prank(owner);
        vault.setMaxAutoTier(2);
        _clean(6);
        vm.warp(vm.getBlockTimestamp() + 60);
        _promoteExpect(3, abi.encodeWithSelector(TierVault.AboveMaxAutoTier.selector, 3, 2));
        vm.prank(owner);
        vault.setMaxAutoTier(3);
        _promote(3);
        _promoteExpect(4, abi.encodeWithSelector(TierVault.BadTier.selector, 4));
    }

    function test_promotion_zeroMinCleanCannotPromote() public {
        TierVault.TierConfig memory c = vault.tierConfig(1);
        c.minCleanToPromote = 0;
        vm.prank(owner);
        vault.setTierConfig(1, c);
        _clean(5);
        vm.warp(vm.getBlockTimestamp() + 60);
        _promoteExpect(2, abi.encodeWithSelector(TierVault.PromotionNotEarned.selector, 5, 0, 0));
    }

    function test_promotion_fromTier0_withApprovedOrders() public {
        _setTier(0);
        _earnPromotion();
        assertEq(vault.tier(), 1);
    }

    function test_setTier_rolesAndFrozen() public {
        vm.prank(owner);
        vm.expectRevert(TierVault.NotAutonomy.selector);
        vault.setTier(0, 3100, 0);
        vm.prank(agent);
        vm.expectRevert(TierVault.NotAutonomy.selector);
        vault.setTier(0, 3100, 0);
        _promoteExpect(1, _sel(TierVault.SameTier.selector));
        vm.prank(autonomy);
        vault.freeze(3204, 0);
        _promoteExpect(0, _sel(TierVault.Frozen.selector));
    }

    // ------------------------------------------------------------------ clean-record rule

    function test_cleanRecord_dustDoesNotCount() public {
        bytes32 oid = _pay(idA, USDC / 10 - 1, MALL);
        vm.prank(agent);
        vault.recordDelivery(oid, AcbTypes.OUTCOME_ON_TIME, keccak256("d"));
        _assertCounters(0, 0);
        assertEq(vault.completedWith(idA), 0);
        oid = _pay(idA, USDC / 10, MALL);
        vm.prank(agent);
        vault.recordDelivery(oid, AcbTypes.OUTCOME_ON_TIME, keccak256("d"));
        _assertCounters(1, 0);
        assertEq(vault.completedWith(idA), 1);
    }

    function test_cleanRecord_newNoneAndCPayeesDoNotCount() public {
        uint256[3] memory ids = [idNew, idNone, idC];
        address[3] memory wals = [walNew, walNone, walC];
        for (uint256 i = 0; i < 3; i++) {
            bytes32 oid = _escrow(ids[i], USDC / 2, MALL);
            vm.prank(wals[i]);
            vault.claimDelivered(oid, keccak256("d"));
            vm.prank(agent);
            vault.release(oid);
            assertEq(vault.completedWith(ids[i]), 0);
        }
        _assertCounters(0, 0);
    }

    function test_cleanRecord_gradeBCountsAndLateCounts() public {
        bytes32 oid = _pay(idB, USDC / 2, MALL);
        vm.warp(vm.getBlockTimestamp() + 601);
        vm.prank(agent);
        vault.recordDelivery(oid, AcbTypes.OUTCOME_LATE, keccak256("d"));
        _assertCounters(1, 0);
        assertEq(vault.completedWith(idB), 1);
    }

    function test_cleanRecord_failedDoesNotCount() public {
        bytes32 oid = _pay(idA, USDC / 2, MALL);
        vm.warp(vm.getBlockTimestamp() + 601);
        vm.prank(agent);
        vault.recordDelivery(oid, AcbTypes.OUTCOME_FAILED, bytes32(0));
        _assertCounters(0, 0);
        assertEq(vault.getOrder(oid).outcome, AcbTypes.OUTCOME_FAILED);
    }

    function test_cleanRecord_gradeAtSettlementTime() public {
        bytes32 oid = _pay(idA, USDC / 2, MALL);
        _post(idA, AcbTypes.GRADE_C); // downgraded before settlement
        vm.prank(agent);
        vault.recordDelivery(oid, AcbTypes.OUTCOME_ON_TIME, keccak256("d"));
        _assertCounters(0, 0);
    }

    // ------------------------------------------------------------------ recordDelivery

    function test_recordDelivery_rules() public {
        bytes32 oid = _pay(idA, USDC / 2, MALL);
        vm.prank(outsider);
        vm.expectRevert(TierVault.NotAuthorized.selector);
        vault.recordDelivery(oid, 1, 0);
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.BadOutcome.selector, 0));
        vault.recordDelivery(oid, 0, 0);
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.BadOutcome.selector, 4));
        vault.recordDelivery(oid, 4, 0);
        uint64 deadline = vault.getOrder(oid).deadline;
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.DeadlineNotPassed.selector, deadline));
        vault.recordDelivery(oid, AcbTypes.OUTCOME_FAILED, 0);
        vm.expectEmit(true, true, true, true, address(vault));
        emit TierVault.DeliveryRecorded(oid, idA, agent, AcbTypes.OUTCOME_ON_TIME, keccak256("d"));
        vm.prank(agent);
        vault.recordDelivery(oid, AcbTypes.OUTCOME_ON_TIME, keccak256("d"));
        assertEq(vault.getOrder(oid).deliveryHash, keccak256("d"));
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(TierVault.OutcomeAlreadySet.selector, oid));
        vault.recordDelivery(oid, AcbTypes.OUTCOME_LATE, 0);
        // only PAID orders
        bytes32 esc = _escrow(idA, USDC / 2, MALL);
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.BadOrderState.selector, esc, AcbTypes.STATE_ESCROWED));
        vault.recordDelivery(esc, 1, 0);
    }

    function test_recordDelivery_agentBlockedWhileFrozen_ownerAllowed() public {
        bytes32 oid = _pay(idA, USDC / 2, MALL);
        vm.warp(vm.getBlockTimestamp() + 601);
        vm.prank(owner);
        vault.freeze(3202, 0);
        vm.prank(agent);
        vm.expectRevert(TierVault.Frozen.selector);
        vault.recordDelivery(oid, AcbTypes.OUTCOME_FAILED, 0);
        vm.prank(owner);
        vault.recordDelivery(oid, AcbTypes.OUTCOME_ON_TIME, keccak256("d"));
    }

    function test_refund2400_agentBlockedWhileFrozen() public {
        bytes32 oid = _escrow(idA, USDC / 2, MALL);
        vm.warp(vm.getBlockTimestamp() + 601);
        vm.prank(autonomy);
        vault.freeze(3205, 0);
        vm.prank(agent);
        vm.expectRevert(TierVault.Frozen.selector);
        vault.refund(oid, 2400);
        vm.prank(agent);
        vault.refund(oid, 2401);
    }

    // ------------------------------------------------------------------ violations

    function test_violation_severe_freezesInSameTx() public {
        vm.expectEmit(true, true, true, true, address(vault));
        emit TierVault.TierChanged(1, 255, 3200, keccak256("violation"), agent);
        vm.expectEmit(true, true, false, true, address(vault));
        emit TierVault.ViolationRecorded(AcbTypes.VK_INJECTION_FOLLOWED, 3, keccak256("violation"), agent, 255);
        _violation(agent, AcbTypes.VK_INJECTION_FOLLOWED, 3);
        assertTrue(vault.isFrozen());
        assertEq(vault.tierBeforeFreeze(), 1);
    }

    function test_violation_serious_demotesOne() public {
        _setTier(2);
        vm.expectEmit(true, true, true, true, address(vault));
        emit TierVault.TierChanged(2, 1, 3100, keccak256("violation"), autonomy);
        _violation(autonomy, AcbTypes.VK_PROVENANCE_AMOUNT, 2);
        assertEq(vault.tier(), 1);
        _assertCounters(0, 0);
    }

    function test_violation_twoMinor_demoteOne() public {
        _clean(2);
        _violation(agent, AcbTypes.VK_LIMIT_PROPOSAL, 1);
        assertEq(vault.tier(), 1);
        _assertCounters(2, 1);
        vm.expectEmit(true, true, true, true, address(vault));
        emit TierVault.TierChanged(1, 0, 3101, keccak256("violation"), agent);
        _violation(agent, AcbTypes.VK_DUPLICATE_PROPOSAL, 1);
        assertEq(vault.tier(), 0);
        _assertCounters(0, 0);
    }

    function test_violation_atTier0_accumulatesPoints() public {
        _setTier(0);
        _violation(agent, AcbTypes.VK_LIMIT_PROPOSAL, 1);
        _violation(agent, AcbTypes.VK_LIMIT_PROPOSAL, 1);
        assertEq(vault.tier(), 0);
        _assertCounters(0, 2);
        _violation(agent, AcbTypes.VK_UNKNOWN_TOOL, 2); // no tier below 0: severity added to the points
        assertEq(vault.tier(), 0);
        _assertCounters(0, 4);
    }

    function test_violation_whileFrozen_onlyEmits() public {
        vm.prank(owner);
        vault.freeze(3202, 0);
        vm.expectEmit(true, true, false, true, address(vault));
        emit TierVault.ViolationRecorded(AcbTypes.VK_LIMIT_PROPOSAL, 1, keccak256("violation"), agent, 255);
        _violation(agent, AcbTypes.VK_LIMIT_PROPOSAL, 1);
        _violation(agent, AcbTypes.VK_DRAIN_ATTEMPT, 3);
        assertEq(vault.tier(), 255);
        assertEq(vault.tierBeforeFreeze(), 1);
        _assertCounters(0, 0);
    }

    function test_violation_validation() public {
        vm.prank(owner);
        vm.expectRevert(TierVault.NotAuthorized.selector);
        vault.recordViolation(1, 1, 0);
        vm.prank(outsider);
        vm.expectRevert(TierVault.NotAuthorized.selector);
        vault.recordViolation(1, 1, 0);
        vm.startPrank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.BadViolationKind.selector, 0));
        vault.recordViolation(0, 1, 0);
        vm.expectRevert(abi.encodeWithSelector(TierVault.BadViolationKind.selector, 12));
        vault.recordViolation(12, 1, 0);
        vm.expectRevert(abi.encodeWithSelector(TierVault.BadSeverity.selector, 0));
        vault.recordViolation(1, 0, 0);
        vm.expectRevert(abi.encodeWithSelector(TierVault.BadSeverity.selector, 4));
        vault.recordViolation(1, 4, 0);
        vm.stopPrank();
    }

    // ------------------------------------------------------------------ counters

    function test_counters_resetOnAnyChange() public {
        _clean(3);
        _violation(agent, AcbTypes.VK_LIMIT_PROPOSAL, 1);
        _assertCounters(3, 1);
        vm.warp(vm.getBlockTimestamp() + 5);
        vm.prank(autonomy);
        vault.setTier(0, 3103, 0);
        _assertCounters(0, 0);
        assertEq(vault.lastTierChangeAt(), vm.getBlockTimestamp());
    }

    function test_counters_saturate() public {
        // cleanSinceChange = 65535 (slot 9, bits 128..143) and completedWith[idA] = 2^32 - 1, set in storage
        uint256 v = uint256(vm.load(address(vault), bytes32(SLOT_COUNTERS)));
        v |= uint256(type(uint16).max) << 128;
        vm.store(address(vault), bytes32(SLOT_COUNTERS), bytes32(v));
        bytes32 slot = keccak256(abi.encode(idA, SLOT_COMPLETED_WITH));
        vm.store(address(vault), slot, bytes32(uint256(type(uint32).max)));
        assertEq(vault.cleanSinceChange(), type(uint16).max);
        assertEq(vault.completedWith(idA), type(uint32).max);
        _clean(1);
        assertEq(vault.cleanSinceChange(), type(uint16).max);
        assertEq(vault.completedWith(idA), type(uint32).max);

        // violation points (bits 144..159) at tier 0, where no demotion resets them
        vm.prank(autonomy);
        vault.setTier(0, 3103, 0);
        v = uint256(vm.load(address(vault), bytes32(SLOT_COUNTERS)));
        v = (v & ~(uint256(0xFFFF) << 144)) | (uint256(type(uint16).max - 1) << 144);
        vm.store(address(vault), bytes32(SLOT_COUNTERS), bytes32(v));
        assertEq(vault.violationPointsSinceChange(), type(uint16).max - 1);
        _violation(agent, AcbTypes.VK_UNKNOWN_TOOL, 2);
        assertEq(vault.violationPointsSinceChange(), type(uint16).max);
        _violation(agent, AcbTypes.VK_LIMIT_PROPOSAL, 1);
        assertEq(vault.violationPointsSinceChange(), type(uint16).max);
        assertEq(vault.tier(), 0);
    }

    // ------------------------------------------------------------------ freeze / unfreeze

    function test_selfFreezeByAgent() public {
        vm.expectEmit(true, true, true, true, address(vault));
        emit TierVault.TierChanged(1, 255, 3203, keccak256("cb"), agent);
        vm.prank(agent);
        vault.freeze(3203, keccak256("cb"));
        assertTrue(vault.isFrozen());
        vm.prank(owner);
        vm.expectRevert(TierVault.Frozen.selector);
        vault.freeze(3202, 0);
        vm.prank(outsider);
        vm.expectRevert(TierVault.NotAuthorized.selector);
        vault.freeze(3202, 0);
    }

    function test_unfreeze_onlyOwner_onlyTo0or1() public {
        vm.prank(owner);
        vm.expectRevert(TierVault.NotFrozen.selector);
        vault.unfreeze(1, 3300, address(0));
        _setTier(2);
        vm.prank(autonomy);
        vault.freeze(3204, 0);
        assertEq(vault.tierBeforeFreeze(), 2);
        vm.prank(autonomy);
        vm.expectRevert(TierVault.NotOwner.selector);
        vault.unfreeze(1, 3300, address(0));
        vm.prank(agent);
        vm.expectRevert(TierVault.NotOwner.selector);
        vault.unfreeze(1, 3300, address(0));
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(TierVault.BadTier.selector, 2));
        vault.unfreeze(2, 3300, address(0));
        vm.expectEmit(true, true, true, true, address(vault));
        emit TierVault.TierChanged(255, 0, 3300, bytes32(0), owner);
        vm.prank(owner);
        vault.unfreeze(0, 3300, address(0));
        assertEq(vault.tier(), 0);
    }

    function test_unfreeze_withKeyRotation() public {
        address newAgent = makeAddr("newAgent");
        vm.prank(agent);
        vault.freeze(3203, 0);
        vm.prank(owner);
        vm.expectRevert(TierVault.RolesNotDistinct.selector);
        vault.unfreeze(1, 3300, owner);
        vm.prank(owner);
        vm.expectRevert(TierVault.RolesNotDistinct.selector);
        vault.unfreeze(1, 3300, autonomy);
        vm.expectEmit(true, true, false, false, address(vault));
        emit TierVault.AgentChanged(agent, newAgent);
        vm.prank(owner);
        vault.unfreeze(1, 3300, newAgent);
        assertEq(vault.agent(), newAgent);
        assertEq(vault.tier(), 1);
        bytes32 oid = _oid();
        vm.prank(agent);
        vm.expectRevert(TierVault.NotAgent.selector);
        vault.pay(_out(oid, idA, USDC / 2, M1, AcbTypes.DECISION_PAY));
        vm.prank(newAgent);
        vault.pay(_out(oid, idA, USDC / 2, M1, AcbTypes.DECISION_PAY));
    }

    function test_setMaxAutoTier_whileFrozen_neverUnfreezes() public {
        _setTier(2);
        vm.prank(autonomy);
        vault.freeze(3204, 0);
        vm.prank(owner);
        vault.setMaxAutoTier(1);
        assertEq(vault.tier(), 255);
        assertEq(vault.tierBeforeFreeze(), 1);
        assertEq(vault.maxAutoTier(), 1);
        vm.prank(owner);
        vault.setMaxAutoTier(3);
        assertEq(vault.tier(), 255);
        assertEq(vault.tierBeforeFreeze(), 1);
        vm.prank(owner);
        vault.setMaxAutoTier(0);
        assertEq(vault.tier(), 255);
        assertEq(vault.tierBeforeFreeze(), 0);
    }

    function test_setMaxAutoTier_demotesWhenNotFrozen() public {
        _setTier(2);
        vm.expectEmit(true, true, true, true, address(vault));
        emit TierVault.TierChanged(2, 1, 3104, bytes32(0), owner);
        vm.prank(owner);
        vault.setMaxAutoTier(1);
        assertEq(vault.tier(), 1);
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(TierVault.BadTier.selector, 4));
        vault.setMaxAutoTier(4);
        // raising the ceiling never promotes
        vm.prank(owner);
        vault.setMaxAutoTier(3);
        assertEq(vault.tier(), 1);
    }

    // ------------------------------------------------------------------ inbound assets never move the tier

    function test_inboundAssetsNeverChangeTier() public {
        _clean(2);
        _violation(agent, AcbTypes.VK_LIMIT_PROPOSAL, 1);
        uint8 tierBefore = vault.tier();
        uint16 cleanBefore = vault.cleanSinceChange();
        uint16 pointsBefore = vault.violationPointsSinceChange();

        // inbound USDC (mint and transfer from a donor)
        usdc.mint(outsider, 100 * USDC);
        vm.prank(outsider);
        usdc.transfer(address(vault), 100 * USDC);
        usdc.mint(address(vault), 1);

        // NFT gifts: safeTransferFrom reverts (no onERC721Received), plain transferFrom lands silently
        ERC721Gift gift = new ERC721Gift();
        uint256 g1 = gift.mint(outsider);
        vm.prank(outsider);
        vm.expectRevert(abi.encodeWithSelector(ERC721Gift.InvalidReceiver.selector, address(vault)));
        gift.safeTransferFrom(outsider, address(vault), g1);
        vm.prank(outsider);
        gift.transferFrom(outsider, address(vault), g1);

        vm.prank(outsider);
        uint256 agentNft = identity.register();
        vm.prank(outsider);
        vm.expectRevert(abi.encodeWithSelector(IdentityRegistryLite.ERC721InvalidReceiver.selector, address(vault)));
        identity.safeTransferFrom(outsider, address(vault), agentNft);

        // ETH cannot be sent at all (no receive or fallback)
        vm.deal(outsider, 1 ether);
        vm.prank(outsider);
        (bool ok,) = address(vault).call{value: 1 ether}("");
        assertFalse(ok);

        assertEq(vault.tier(), tierBefore);
        assertEq(vault.cleanSinceChange(), cleanBefore);
        assertEq(vault.violationPointsSinceChange(), pointsBefore);
    }
}
