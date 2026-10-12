// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {VaultTestBase} from "./utils/VaultTestBase.sol";
import {TierVault} from "../src/TierVault.sol";
import {AcbTypes} from "../src/lib/AcbTypes.sol";

/// @notice Maker-checker: recorded decisions, owner approvals and their execution (spec 3.2, 3.6).
contract TierVaultApprovalTest is VaultTestBase {
    function setUp() public override {
        super.setUp();
        _setTier(0);
    }

    function _approve(bytes32 oid, uint256 id, address payee, uint128 amount) internal returns (uint64 expiry) {
        expiry = uint64(vm.getBlockTimestamp() + 600);
        vm.prank(owner);
        vault.ownerApprove(oid, id, payee, amount, expiry, false, 0);
    }

    function _execExpect(bytes32 oid, bytes memory err) internal {
        vm.prank(agent);
        vm.expectRevert(err);
        vault.executeApproved(oid, bytes32(0), bytes32(0));
    }

    // ------------------------------------------------------------------ tier 0 and decisions

    function test_tier0_payAndEscrowRevert() public {
        assertEq(vault.tier(), 0);
        bytes32 oid = _oid();
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.PerTxCapExceeded.selector, USDC / 10, 0));
        vault.pay(_out(oid, idA, USDC / 10, M1, AcbTypes.DECISION_PAY));
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.PerTxCapExceeded.selector, USDC / 10, 0));
        vault.escrow(_out(oid, idA, USDC / 10, M1, AcbTypes.DECISION_ESCROW));
    }

    function test_recordDecision_needsOwnerApproval_leavesStateNone() public {
        bytes32 oid = _oid();
        TierVault.DecisionParams memory p = TierVault.DecisionParams(
            oid, idA, 2 * USDC, AcbTypes.DECISION_NEEDS_OWNER_APPROVAL, 2200, keccak256("inv"), keccak256("dec")
        );
        vm.expectEmit(true, true, true, true, address(vault));
        emit TierVault.DecisionRecorded(
            oid, idA, AcbTypes.DECISION_NEEDS_OWNER_APPROVAL, 2 * USDC, 2200, keccak256("inv"), keccak256("dec"), 0
        );
        vm.prank(agent);
        vault.recordDecision(p);
        assertEq(vault.getOrder(oid).state, AcbTypes.STATE_NONE);
        // the owner can then approve the same order id
        _approve(oid, idA, walA, 2 * USDC);
        vm.prank(agent);
        vault.executeApproved(oid, keccak256("inv"), keccak256("dec"));
        assertEq(vault.getOrder(oid).state, AcbTypes.STATE_PAID);
    }

    function test_recordDecision_refuseIsFinal() public {
        bytes32 oid = _oid();
        vm.prank(agent);
        vault.recordDecision(
            TierVault.DecisionParams(oid, idD, USDC, AcbTypes.DECISION_REFUSE, 2100, bytes32(0), bytes32(0))
        );
        TierVault.Order memory o = vault.getOrder(oid);
        assertEq(o.state, AcbTypes.STATE_REFUSED);
        assertEq(o.decision, AcbTypes.DECISION_REFUSE);
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.OrderExists.selector, oid));
        vault.recordDecision(
            TierVault.DecisionParams(oid, idD, USDC, AcbTypes.DECISION_REFUSE, 2100, bytes32(0), bytes32(0))
        );
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(TierVault.OrderExists.selector, oid));
        vault.ownerApprove(oid, idA, walA, USDC, uint64(vm.getBlockTimestamp() + 600), false, 0);
    }

    function test_recordDecision_validation() public {
        bytes32 oid = _oid();
        uint8[4] memory bad = [AcbTypes.DECISION_NONE, AcbTypes.DECISION_PAY, AcbTypes.DECISION_ESCROW, 6];
        for (uint256 i = 0; i < 4; i++) {
            vm.prank(agent);
            vm.expectRevert(abi.encodeWithSelector(TierVault.BadDecision.selector, bad[i]));
            vault.recordDecision(TierVault.DecisionParams(oid, idA, USDC, bad[i], 2100, bytes32(0), bytes32(0)));
        }
        vm.prank(owner);
        vm.expectRevert(TierVault.NotAgent.selector);
        vault.recordDecision(
            TierVault.DecisionParams(oid, idA, USDC, AcbTypes.DECISION_REFUSE, 2100, bytes32(0), bytes32(0))
        );
    }

    function test_recordDecision_whileFrozen() public {
        vm.prank(agent);
        vault.freeze(3203, 0);
        bytes32 oid = _oid();
        vm.prank(agent);
        vault.recordDecision(
            TierVault.DecisionParams(oid, idA, USDC, AcbTypes.DECISION_REFUSE, 2101, bytes32(0), bytes32(0))
        );
        assertEq(vault.getOrder(oid).state, AcbTypes.STATE_REFUSED);
    }

    // ------------------------------------------------------------------ ownerApprove

    function test_ownerApprove_storesAndEmits() public {
        bytes32 oid = _oid();
        uint64 expiry = uint64(vm.getBlockTimestamp() + 600);
        uint64 escDl = uint64(vm.getBlockTimestamp() + 900);
        vm.expectEmit(true, true, true, true, address(vault));
        emit TierVault.OwnerApproved(oid, idA, walA, 3 * USDC, expiry, true, escDl);
        vm.prank(owner);
        vault.ownerApprove(oid, idA, walA, 3 * USDC, expiry, true, escDl);
        TierVault.Approval memory a = vault.getApproval(oid);
        assertEq(a.payeeAgentId, idA);
        assertEq(a.expectedPayee, walA);
        assertEq(a.amount, 3 * USDC);
        assertEq(a.expiry, expiry);
        assertEq(a.escrowDeadline, escDl);
        assertTrue(a.asEscrow);
        assertFalse(a.used);
    }

    function test_ownerApprove_validation() public {
        bytes32 oid = _oid();
        uint64 nowTs = uint64(vm.getBlockTimestamp());
        vm.prank(agent);
        vm.expectRevert(TierVault.NotOwner.selector);
        vault.ownerApprove(oid, idA, walA, USDC, nowTs + 600, false, 0);
        vm.startPrank(owner);
        vm.expectRevert(TierVault.BadDeadline.selector);
        vault.ownerApprove(oid, idA, walA, USDC, nowTs, false, 0);
        vm.expectRevert(TierVault.ZeroAmount.selector);
        vault.ownerApprove(oid, idA, walA, 0, nowTs + 600, false, 0);
        vm.expectRevert(TierVault.ZeroAddress.selector);
        vault.ownerApprove(oid, idA, address(0), USDC, nowTs + 600, false, 0);
        vm.expectRevert(TierVault.BadDeadline.selector);
        vault.ownerApprove(oid, idA, walA, USDC, nowTs + 600, true, nowTs);
        vm.stopPrank();
    }

    // ------------------------------------------------------------------ executeApproved

    function test_execute_paysBeyondCapsToExpectedPayee() public {
        bytes32 oid = _oid();
        uint64 expiry = _approve(oid, idA, walA, 5 * USDC);
        vm.expectEmit(true, true, true, true, address(vault));
        emit TierVault.Paid(
            oid, idA, walA, 5 * USDC, AcbTypes.DECISION_PAY, 2007, bytes32(0), keccak256("i"), keccak256("d"), expiry, 0
        );
        vm.prank(agent);
        vault.executeApproved(oid, keccak256("i"), keccak256("d"));
        assertEq(usdc.balanceOf(walA), 5 * USDC);
        TierVault.Order memory o = vault.getOrder(oid);
        assertEq(o.state, AcbTypes.STATE_PAID);
        assertEq(o.decision, AcbTypes.DECISION_PAY);
        assertEq(o.deadline, expiry);
        assertEq(o.mandateId, bytes32(0));
        assertTrue(vault.getApproval(oid).used);
        (uint128 spent, uint8 count) = vault.spentInWindow();
        assertEq(spent, 5 * USDC);
        assertEq(count, 1);
    }

    function test_execute_countsInWindowForLaterPayments() public {
        bytes32 oid = _oid();
        _approve(oid, idA, walA, 5 * USDC);
        vm.prank(agent);
        vault.executeApproved(oid, bytes32(0), bytes32(0));
        // back to tier 1 through a human reset: the approved 5.00 still fills the 5.00 window
        vm.prank(owner);
        vault.freeze(3202, 0);
        vm.prank(owner);
        vault.unfreeze(1, 3300, address(0));
        bytes32 next = _oid();
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.WindowCapExceeded.selector, 5 * USDC, USDC / 10, 5 * USDC));
        vault.pay(_out(next, idA, USDC / 10, M1, AcbTypes.DECISION_PAY));
    }

    function test_execute_asEscrow() public {
        bytes32 oid = _oid();
        uint64 escDl = uint64(vm.getBlockTimestamp() + 900);
        vm.prank(owner);
        vault.ownerApprove(oid, idC, walC, 2 * USDC, uint64(vm.getBlockTimestamp() + 600), true, escDl);
        vm.expectEmit(true, true, true, true, address(vault));
        emit TierVault.EscrowOpened(oid, idC, walC, 2 * USDC, escDl, bytes32(0), bytes32(0), bytes32(0), 2007, 0);
        vm.prank(agent);
        vault.executeApproved(oid, bytes32(0), bytes32(0));
        TierVault.Order memory o = vault.getOrder(oid);
        assertEq(o.state, AcbTypes.STATE_ESCROWED);
        assertEq(o.decision, AcbTypes.DECISION_ESCROW);
        assertEq(o.deadline, escDl);
        assertEq(vault.escrowLocked(), 2 * USDC);
        assertEq(usdc.balanceOf(walC), 0);
        vm.prank(walC);
        vault.claimDelivered(oid, keccak256("c"));
        vm.prank(agent);
        vault.release(oid);
        assertEq(usdc.balanceOf(walC), 2 * USDC);
        assertEq(vault.escrowLocked(), 0);
    }

    function test_execute_asEscrowRevertsAtOrAfterEscrowDeadline() public {
        bytes32 oid = _oid();
        uint64 nowTs = uint64(vm.getBlockTimestamp());
        // the approval outlives its escrow deadline: executing late must not open an escrow already expired
        vm.prank(owner);
        vault.ownerApprove(oid, idC, walC, 2 * USDC, nowTs + 600, true, nowTs + 10);
        vm.warp(nowTs + 10);
        _execExpect(oid, abi.encodeWithSelector(TierVault.BadDeadline.selector));
        vm.warp(nowTs + 100);
        _execExpect(oid, abi.encodeWithSelector(TierVault.BadDeadline.selector));
        assertEq(vault.getOrder(oid).state, AcbTypes.STATE_NONE);
        assertEq(vault.escrowLocked(), 0);
        // one second before the deadline it still opens
        bytes32 oid2 = _oid();
        nowTs = uint64(vm.getBlockTimestamp());
        vm.prank(owner);
        vault.ownerApprove(oid2, idC, walC, 2 * USDC, nowTs + 600, true, nowTs + 10);
        vm.warp(nowTs + 9);
        vm.prank(agent);
        vault.executeApproved(oid2, bytes32(0), bytes32(0));
        assertEq(vault.getOrder(oid2).state, AcbTypes.STATE_ESCROWED);
    }

    function test_execute_revertsAfterPayeeWalletRotation() public {
        bytes32 oid = _oid();
        _approve(oid, idA, walA, USDC);
        address newOwner = makeAddr("newOwner");
        vm.prank(walA);
        identity.transferFrom(walA, newOwner, idA);
        _execExpect(oid, abi.encodeWithSelector(TierVault.PayeeWalletChanged.selector, idA, address(0), walA));
    }

    function test_execute_expectedPayeeMustBeLiveWallet() public {
        bytes32 oid = _oid();
        _approve(oid, idA, walB, USDC); // owner typed B's wallet for agent A
        _execExpect(oid, abi.encodeWithSelector(TierVault.PayeeWalletChanged.selector, idA, walA, walB));
    }

    function test_execute_stillBlocksGradeD() public {
        bytes32 oid = _oid();
        _approve(oid, idD, walD, USDC);
        _execExpect(
            oid, abi.encodeWithSelector(TierVault.GradeNotAllowed.selector, idD, AcbTypes.GRADE_D, AcbTypes.MASK_NOT_D)
        );
    }

    function test_execute_stillBlocksFrozen() public {
        bytes32 oid = _oid();
        _approve(oid, idA, walA, USDC);
        vm.prank(autonomy);
        vault.freeze(3204, 0);
        _execExpect(oid, _sel(TierVault.Frozen.selector));
    }

    function test_execute_stillRespectsReserve() public {
        bytes32 oid = _oid();
        _approve(oid, idA, walA, 12 * USDC + 1);
        _execExpect(
            oid, abi.encodeWithSelector(TierVault.ReserveFloorBreached.selector, 15 * USDC, 0, 3 * USDC, 12 * USDC + 1)
        );
        bytes32 ok = _oid();
        _approve(ok, idA, walA, 12 * USDC);
        vm.prank(agent);
        vault.executeApproved(ok, bytes32(0), bytes32(0));
        assertEq(usdc.balanceOf(address(vault)), 3 * USDC);
    }

    function test_execute_expiryAndReuse() public {
        bytes32 oid = _oid();
        uint64 expiry = _approve(oid, idA, walA, USDC);
        vm.warp(expiry + 1);
        _execExpect(oid, abi.encodeWithSelector(TierVault.ApprovalExpired.selector, oid));
        vm.warp(expiry);
        vm.prank(agent);
        vault.executeApproved(oid, bytes32(0), bytes32(0)); // at expiry is still valid
        _execExpect(oid, abi.encodeWithSelector(TierVault.ApprovalUsed.selector, oid));
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(TierVault.ApprovalUsed.selector, oid));
        vault.revokeApproval(oid);
    }

    function test_execute_missingAndRevoked() public {
        bytes32 oid = _oid();
        _execExpect(oid, abi.encodeWithSelector(TierVault.ApprovalMissing.selector, oid));
        _approve(oid, idA, walA, USDC);
        vm.prank(agent);
        vm.expectRevert(TierVault.NotOwner.selector);
        vault.revokeApproval(oid);
        vm.expectEmit(true, false, false, false, address(vault));
        emit TierVault.ApprovalRevoked(oid);
        vm.prank(owner);
        vault.revokeApproval(oid);
        _execExpect(oid, abi.encodeWithSelector(TierVault.ApprovalMissing.selector, oid));
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(TierVault.ApprovalMissing.selector, oid));
        vault.revokeApproval(oid);
    }

    function test_execute_orderIdAlreadyUsedByPay() public {
        _setTier(1);
        bytes32 oid = _pay(idA, USDC, M1);
        // approval of an existing order is impossible, and so is execution
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(TierVault.OrderExists.selector, oid));
        vault.ownerApprove(oid, idA, walA, USDC, uint64(vm.getBlockTimestamp() + 600), false, 0);
    }

    function test_execute_onlyAgent() public {
        bytes32 oid = _oid();
        _approve(oid, idA, walA, USDC);
        vm.prank(owner);
        vm.expectRevert(TierVault.NotAgent.selector);
        vault.executeApproved(oid, bytes32(0), bytes32(0));
    }
}
