// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {VaultTestBase} from "./utils/VaultTestBase.sol";
import {TierVault} from "../src/TierVault.sol";
import {AcbTypes} from "../src/lib/AcbTypes.sol";
import {DeliveryClaimed} from "../src/lib/VaultErrors.sol";

/// @notice Escrow with provider delivery claims, release and the refund matrix (spec 3.2, 3.6).
contract TierVaultEscrowTest is VaultTestBase {
    uint64 internal constant DW = 120; // dispute window of the fixture

    function _open() internal returns (bytes32 oid, uint64 deadline) {
        oid = _escrow(idA, USDC, M1);
        deadline = vault.getOrder(oid).deadline;
    }

    function _claim(bytes32 oid) internal {
        vm.prank(walA);
        vault.claimDelivered(oid, keccak256("delivery"));
    }

    function _refundExpect(address caller, bytes32 oid, uint16 reason, bytes memory err) internal {
        vm.prank(caller);
        vm.expectRevert(err);
        vault.refund(oid, reason);
    }

    // ------------------------------------------------------------------ open

    function test_open_locksFundsWithoutTransfer() public {
        bytes32 oid = _oid();
        TierVault.OutflowParams memory p = _out(oid, idA, USDC, M1, AcbTypes.DECISION_ESCROW);
        p.reasonCode = 2004;
        vm.expectEmit(true, true, true, true, address(vault));
        emit TierVault.EscrowOpened(oid, idA, walA, USDC, p.deadline, M1, p.invoiceHash, p.decisionHash, 2004, 1);
        vm.prank(agent);
        vault.escrow(p);
        assertEq(usdc.balanceOf(address(vault)), 15 * USDC);
        assertEq(usdc.balanceOf(walA), 0);
        assertEq(vault.escrowLocked(), USDC);
        assertEq(vault.getMandate(M1).spent, USDC);
        TierVault.Order memory o = vault.getOrder(oid);
        assertEq(o.state, AcbTypes.STATE_ESCROWED);
        assertEq(o.decision, AcbTypes.DECISION_ESCROW);
        assertEq(o.claimedAt, 0);
        assertEq(o.payee, walA);
        assertEq(vault.freeBalance(), 15 * USDC - USDC - 3 * USDC);
    }

    function test_open_requiresEscrowDecision() public {
        bytes32 oid = _oid();
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.BadDecision.selector, AcbTypes.DECISION_PAY));
        vault.escrow(_out(oid, idA, USDC, M1, AcbTypes.DECISION_PAY));
    }

    function test_open_gradeMaskBlocksD() public {
        bytes32 oid = _oid();
        uint8 esc = AcbTypes.MASK_A | AcbTypes.MASK_B | AcbTypes.MASK_C | AcbTypes.MASK_NEW | AcbTypes.MASK_NONE;
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.GradeNotAllowed.selector, idD, AcbTypes.GRADE_D, esc));
        vault.escrow(_out(oid, idD, USDC, MALL, AcbTypes.DECISION_ESCROW));
        // grade C may be escrowed at tier 1, but not paid
        _escrow(idC, USDC, M1);
    }

    function test_open_checksInOrder() public {
        bytes32 oid = _escrow(idA, USDC, M1);
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.OrderExists.selector, oid));
        vault.escrow(_out(oid, idA, 0, M1, AcbTypes.DECISION_ESCROW));
        TierVault.OutflowParams memory p = _out(_oid(), idA, 0, M1, AcbTypes.DECISION_ESCROW);
        vm.prank(agent);
        vm.expectRevert(TierVault.ZeroAmount.selector);
        vault.escrow(p);
        p.amount = USDC;
        p.deadline = uint64(vm.getBlockTimestamp());
        vm.prank(agent);
        vm.expectRevert(TierVault.BadDeadline.selector);
        vault.escrow(p);
    }

    function test_open_frozenAndRoles() public {
        bytes32 oid = _oid();
        vm.prank(outsider);
        vm.expectRevert(TierVault.NotAgent.selector);
        vault.escrow(_out(oid, idA, USDC, M1, AcbTypes.DECISION_ESCROW));
        vm.prank(agent);
        vault.freeze(3203, 0);
        vm.prank(agent);
        vm.expectRevert(TierVault.Frozen.selector);
        vault.escrow(_out(oid, idA, USDC, M1, AcbTypes.DECISION_ESCROW));
    }

    // ------------------------------------------------------------------ claim

    function test_claim_onlyStoredPayee_once() public {
        (bytes32 oid, uint64 deadline) = _open();
        address[4] memory others = [owner, agent, autonomy, outsider];
        for (uint256 i = 0; i < 4; i++) {
            vm.prank(others[i]);
            vm.expectRevert(TierVault.NotPayee.selector);
            vault.claimDelivered(oid, keccak256("x"));
        }
        vm.prank(walA);
        vm.expectRevert(TierVault.ZeroHash.selector);
        vault.claimDelivered(oid, bytes32(0));

        vm.expectEmit(true, true, false, true, address(vault));
        emit TierVault.DeliveryClaimed(oid, idA, keccak256("delivery"), uint64(vm.getBlockTimestamp()), false);
        _claim(oid);
        TierVault.Order memory o = vault.getOrder(oid);
        assertEq(o.claimedAt, vm.getBlockTimestamp());
        assertEq(o.deliveryHash, keccak256("delivery"));
        assertLt(o.claimedAt, deadline);

        vm.prank(walA);
        vm.expectRevert(abi.encodeWithSelector(TierVault.AlreadyClaimed.selector, oid));
        vault.claimDelivered(oid, keccak256("again"));
    }

    function test_claim_payeeIsPinnedWalletEvenAfterRotation() public {
        (bytes32 oid,) = _open();
        // the payee rotates its registry wallet: the claim still belongs to the stored (pinned) wallet
        address newOwner = makeAddr("newOwner");
        vm.prank(walA);
        identity.transferFrom(walA, newOwner, idA);
        vm.prank(newOwner);
        vm.expectRevert(TierVault.NotPayee.selector);
        vault.claimDelivered(oid, keccak256("x"));
        _claim(oid);
    }

    function test_claim_whileFrozen() public {
        (bytes32 oid,) = _open();
        vm.prank(owner);
        vault.freeze(3202, 0);
        _claim(oid);
        assertGt(vault.getOrder(oid).claimedAt, 0);
    }

    function test_claim_lateFlag() public {
        (bytes32 oid, uint64 deadline) = _open();
        vm.warp(deadline + 1);
        vm.expectEmit(true, true, false, true, address(vault));
        emit TierVault.DeliveryClaimed(oid, idA, keccak256("delivery"), deadline + 1, true);
        _claim(oid);
    }

    function test_claim_wrongState() public {
        bytes32 paid = _pay(idA, USDC, M1);
        vm.prank(walA);
        vm.expectRevert(abi.encodeWithSelector(TierVault.BadOrderState.selector, paid, AcbTypes.STATE_PAID));
        vault.claimDelivered(paid, keccak256("x"));
        bytes32 none = _oid();
        vm.prank(walA);
        vm.expectRevert(abi.encodeWithSelector(TierVault.BadOrderState.selector, none, AcbTypes.STATE_NONE));
        vault.claimDelivered(none, keccak256("x"));
    }

    // ------------------------------------------------------------------ release

    function test_release_requiresClaim() public {
        (bytes32 oid,) = _open();
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(TierVault.NoClaim.selector, oid));
        vault.release(oid);
    }

    function test_release_byAgent_onTime() public {
        (bytes32 oid, uint64 deadline) = _open();
        vm.warp(deadline); // claim exactly at the deadline is on time
        _claim(oid);
        vm.expectEmit(true, true, true, true, address(vault));
        emit TierVault.EscrowReleased(oid, idA, agent, USDC, AcbTypes.OUTCOME_ON_TIME, keccak256("delivery"));
        vm.prank(agent);
        vault.release(oid);
        TierVault.Order memory o = vault.getOrder(oid);
        assertEq(o.state, AcbTypes.STATE_RELEASED);
        assertEq(o.outcome, AcbTypes.OUTCOME_ON_TIME);
        assertEq(usdc.balanceOf(walA), USDC);
        assertEq(vault.escrowLocked(), 0);
        assertEq(vault.getMandate(M1).spent, USDC);
        assertEq(vault.cleanSinceChange(), 1);
        assertEq(vault.completedWith(idA), 1);
    }

    function test_release_lateOutcomeFromClaimTime() public {
        (bytes32 oid, uint64 deadline) = _open();
        vm.warp(deadline + 1);
        _claim(oid);
        vm.prank(agent);
        vault.release(oid);
        assertEq(vault.getOrder(oid).outcome, AcbTypes.OUTCOME_LATE);
        // a late settlement with an A payee is still a clean record
        assertEq(vault.cleanSinceChange(), 1);
    }

    function test_release_outcomeUsesClaimNotReleaseTime() public {
        (bytes32 oid, uint64 deadline) = _open();
        _claim(oid);
        vm.warp(deadline + 1 days);
        vm.prank(outsider);
        vault.release(oid);
        assertEq(vault.getOrder(oid).outcome, AcbTypes.OUTCOME_ON_TIME);
    }

    function test_release_agentBlockedWhenFrozen() public {
        (bytes32 oid,) = _open();
        _claim(oid);
        vm.prank(owner);
        vault.freeze(3202, 0);
        vm.prank(agent);
        vm.expectRevert(TierVault.Frozen.selector);
        vault.release(oid);
        // the owner may release while frozen
        vm.prank(owner);
        vault.release(oid);
        assertEq(usdc.balanceOf(walA), USDC);
    }

    function test_release_agentBlockedWhenPayeeBecameD() public {
        (bytes32 oid,) = _open();
        _claim(oid);
        _post(idA, AcbTypes.GRADE_D);
        vm.prank(agent);
        vm.expectRevert(
            abi.encodeWithSelector(TierVault.GradeNotAllowed.selector, idA, AcbTypes.GRADE_D, AcbTypes.MASK_NOT_D)
        );
        vault.release(oid);
        // owner any time
        vm.prank(owner);
        vault.release(oid);
        assertEq(vault.getOrder(oid).state, AcbTypes.STATE_RELEASED);
        // D payee: not a clean record
        assertEq(vault.cleanSinceChange(), 0);
    }

    function test_release_anyoneOnlyAfterDisputeWindow() public {
        (bytes32 oid,) = _open();
        _claim(oid);
        uint64 readyAt = uint64(vm.getBlockTimestamp()) + DW;
        vm.prank(outsider);
        vm.expectRevert(abi.encodeWithSelector(TierVault.DisputeWindowOpen.selector, readyAt));
        vault.release(oid);
        vm.warp(readyAt - 1);
        vm.prank(walA);
        vm.expectRevert(abi.encodeWithSelector(TierVault.DisputeWindowOpen.selector, readyAt));
        vault.release(oid);
        vm.warp(readyAt);
        vm.prank(owner);
        vault.freeze(3202, 0); // also while frozen
        vm.expectEmit(true, true, true, true, address(vault));
        emit TierVault.EscrowReleased(oid, idA, walA, USDC, AcbTypes.OUTCOME_ON_TIME, keccak256("delivery"));
        vm.prank(walA);
        vault.release(oid);
        assertEq(usdc.balanceOf(walA), USDC);
    }

    function test_release_onlyOnce() public {
        (bytes32 oid,) = _open();
        _claim(oid);
        vm.prank(owner);
        vault.release(oid);
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(TierVault.BadOrderState.selector, oid, AcbTypes.STATE_RELEASED));
        vault.release(oid);
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(TierVault.BadOrderState.selector, oid, AcbTypes.STATE_RELEASED));
        vault.refund(oid, 2403);
    }

    // ------------------------------------------------------------------ refund matrix (no claim)

    function test_refund_agent2401_anyTime_alsoFrozen() public {
        (bytes32 oid,) = _open();
        vm.prank(agent);
        vault.freeze(3203, 0);
        vm.expectEmit(true, true, true, true, address(vault));
        emit TierVault.EscrowRefunded(oid, idA, agent, USDC, 2401, AcbTypes.OUTCOME_NONE);
        vm.prank(agent);
        vault.refund(oid, 2401);
        TierVault.Order memory o = vault.getOrder(oid);
        assertEq(o.state, AcbTypes.STATE_REFUNDED);
        assertEq(o.outcome, AcbTypes.OUTCOME_NONE);
        assertEq(vault.escrowLocked(), 0);
        assertEq(vault.getMandate(M1).spent, 0);
        assertEq(usdc.balanceOf(address(vault)), 15 * USDC);
    }

    function test_refund_agent2400_onlyAfterDeadline_notFrozen() public {
        (bytes32 oid, uint64 deadline) = _open();
        _refundExpect(agent, oid, 2400, abi.encodeWithSelector(TierVault.EscrowNotExpired.selector, deadline));
        vm.warp(deadline);
        _refundExpect(agent, oid, 2400, abi.encodeWithSelector(TierVault.EscrowNotExpired.selector, deadline));
        vm.warp(deadline + 1);
        vm.prank(owner);
        vault.freeze(3202, 0);
        _refundExpect(agent, oid, 2400, _sel(TierVault.Frozen.selector));
        vm.prank(owner);
        vault.unfreeze(1, 3300, address(0));
        vm.prank(agent);
        vault.refund(oid, 2400);
        assertEq(vault.getOrder(oid).outcome, AcbTypes.OUTCOME_FAILED);
        assertEq(vault.getMandate(M1).spent, 0);
    }

    function test_refund_agentOtherReasons() public {
        (bytes32 oid,) = _open();
        uint16[4] memory bad = [uint16(2402), 2403, 0, 2007];
        for (uint256 i = 0; i < 4; i++) {
            _refundExpect(agent, oid, bad[i], abi.encodeWithSelector(TierVault.BadReason.selector, bad[i]));
        }
    }

    function test_refund_owner() public {
        (bytes32 oid, uint64 deadline) = _open();
        _refundExpect(owner, oid, 2400, abi.encodeWithSelector(TierVault.EscrowNotExpired.selector, deadline));
        _refundExpect(owner, oid, 2403, abi.encodeWithSelector(TierVault.BadReason.selector, 2403));
        _refundExpect(owner, oid, 2402, abi.encodeWithSelector(TierVault.BadReason.selector, 2402));
        vm.prank(owner);
        vault.refund(oid, 2401);
        assertEq(vault.getOrder(oid).outcome, AcbTypes.OUTCOME_NONE);

        (bytes32 oid2, uint64 deadline2) = _open();
        vm.warp(deadline2 + 1);
        vm.prank(owner);
        vault.freeze(3202, 0); // owner may refund while frozen
        vm.prank(owner);
        vault.refund(oid2, 2400);
        assertEq(vault.getOrder(oid2).outcome, AcbTypes.OUTCOME_FAILED);
    }

    function test_refund_thirdParty_onlyAfterDeadlinePlusDisputeWindow_forced2402() public {
        (bytes32 oid, uint64 deadline) = _open();
        uint64 readyAt = deadline + DW + 1;
        _refundExpect(outsider, oid, 2400, abi.encodeWithSelector(TierVault.DisputeWindowOpen.selector, readyAt));
        vm.warp(deadline + DW);
        _refundExpect(walA, oid, 2400, abi.encodeWithSelector(TierVault.DisputeWindowOpen.selector, readyAt));
        _refundExpect(autonomy, oid, 2401, abi.encodeWithSelector(TierVault.DisputeWindowOpen.selector, readyAt));
        vm.warp(readyAt);
        vm.expectEmit(true, true, true, true, address(vault));
        emit TierVault.EscrowRefunded(oid, idA, outsider, USDC, 2402, AcbTypes.OUTCOME_NONE);
        vm.prank(outsider);
        vault.refund(oid, 2400); // the reason is forced to 2402: never FAILED
        assertEq(vault.getOrder(oid).outcome, AcbTypes.OUTCOME_NONE);
        assertEq(vault.getMandate(M1).spent, 0);
    }

    function test_refund_thirdParty_farDeadlineDoesNotOverflow() public {
        bytes32 oid = _oid();
        TierVault.OutflowParams memory p = _out(oid, idA, USDC, M1, AcbTypes.DECISION_ESCROW);
        p.deadline = type(uint64).max;
        vm.prank(agent);
        vault.escrow(p);
        _refundExpect(
            outsider, oid, 2402, abi.encodeWithSelector(TierVault.DisputeWindowOpen.selector, type(uint64).max)
        );
        vm.prank(owner);
        vault.refund(oid, 2401); // the owner can always roll back an unclaimed escrow
        assertEq(vault.escrowLocked(), 0);
    }

    // ------------------------------------------------------------------ refund matrix (after a claim)

    function test_refund_afterClaim_onlyOwner2403() public {
        (bytes32 oid, uint64 deadline) = _open();
        _claim(oid);
        vm.warp(deadline + DW + 10);
        uint16[3] memory reasons = [uint16(2400), 2401, 2402];
        address[4] memory callers = [agent, outsider, walA, autonomy];
        for (uint256 c = 0; c < 4; c++) {
            for (uint256 r = 0; r < 3; r++) {
                _refundExpect(callers[c], oid, reasons[r], abi.encodeWithSelector(DeliveryClaimed.selector, oid));
            }
            _refundExpect(callers[c], oid, 2403, abi.encodeWithSelector(DeliveryClaimed.selector, oid));
        }
        for (uint256 r = 0; r < 3; r++) {
            _refundExpect(owner, oid, reasons[r], abi.encodeWithSelector(TierVault.BadReason.selector, reasons[r]));
        }
        vm.expectEmit(true, true, true, true, address(vault));
        emit TierVault.EscrowRefunded(oid, idA, owner, USDC, 2403, AcbTypes.OUTCOME_NONE);
        vm.prank(owner);
        vault.refund(oid, 2403);
        assertEq(vault.escrowLocked(), 0);
        assertEq(vault.getMandate(M1).spent, 0);
    }

    function test_refund_wrongState() public {
        bytes32 paid = _pay(idA, USDC, M1);
        _refundExpect(owner, paid, 2401, abi.encodeWithSelector(TierVault.BadOrderState.selector, paid, 1));
        (bytes32 oid,) = _open();
        vm.prank(agent);
        vault.refund(oid, 2401);
        _refundExpect(owner, oid, 2401, abi.encodeWithSelector(TierVault.BadOrderState.selector, oid, 4));
    }

    // ------------------------------------------------------------------ accounting

    function test_accounting_severalEscrows() public {
        bytes32 a = _escrow(idA, USDC, M1);
        bytes32 b = _escrow(idB, USDC / 2, M1);
        bytes32 c = _escrow(idC, USDC / 4, M1);
        assertEq(vault.escrowLocked(), USDC + USDC / 2 + USDC / 4);
        assertEq(vault.getMandate(M1).spent, USDC + USDC / 2 + USDC / 4);
        vm.prank(walA);
        vault.claimDelivered(a, keccak256("a"));
        vm.prank(agent);
        vault.release(a);
        vm.prank(agent);
        vault.refund(b, 2401);
        assertEq(vault.escrowLocked(), USDC / 4);
        assertEq(vault.getMandate(M1).spent, USDC + USDC / 4);
        assertEq(usdc.balanceOf(address(vault)), 14 * USDC);
        vm.prank(walC);
        vault.claimDelivered(c, keccak256("c"));
        vm.prank(owner);
        vault.refund(c, 2403);
        assertEq(vault.escrowLocked(), 0);
        assertEq(vault.getMandate(M1).spent, USDC);
        // grade C payee does not count as a clean record; grade A does
        assertEq(vault.cleanSinceChange(), 1);
    }

    function test_withdrawCannotTouchEscrow() public {
        _escrow(idA, USDC, M1);
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(TierVault.InsufficientUnlocked.selector, 14 * USDC, 15 * USDC));
        vault.withdraw(owner, 15 * USDC);
        vm.prank(owner);
        vault.withdraw(owner, 14 * USDC); // below the reserve floor is allowed for the owner
        assertEq(usdc.balanceOf(address(vault)), USDC);
    }
}
