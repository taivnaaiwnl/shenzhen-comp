// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {VaultTestBase} from "./utils/VaultTestBase.sol";
import {TierVault} from "../src/TierVault.sol";
import {AcbTypes} from "../src/lib/AcbTypes.sol";
import {FalseToken} from "./mocks/FalseToken.sol";

/// @notice `pay` happy paths per tier and grade, and every revert in check order (spec 3.2, 3.6).
contract TierVaultPayTest is VaultTestBase {
    uint8 internal constant AB = AcbTypes.MASK_A | AcbTypes.MASK_B;

    function _expectPayRevert(TierVault.OutflowParams memory p, bytes memory err) internal {
        vm.prank(agent);
        vm.expectRevert(err);
        vault.pay(p);
    }

    // ------------------------------------------------------------------ happy paths

    function test_pay_tier1_gradeA_emitsAndTransfers() public {
        bytes32 oid = _oid();
        TierVault.OutflowParams memory p = _out(oid, idA, USDC, M1, AcbTypes.DECISION_PAY);
        vm.expectEmit(true, true, true, true, address(vault));
        emit TierVault.Paid(
            oid, idA, walA, USDC, AcbTypes.DECISION_PAY, 2000, M1, p.invoiceHash, p.decisionHash, p.deadline, 1
        );
        vm.prank(agent);
        vault.pay(p);
        assertEq(usdc.balanceOf(walA), USDC);
        assertEq(usdc.balanceOf(address(vault)), 14 * USDC);
        TierVault.Order memory o = vault.getOrder(oid);
        assertEq(o.state, AcbTypes.STATE_PAID);
        assertEq(o.outcome, AcbTypes.OUTCOME_NONE);
        assertEq(o.decision, AcbTypes.DECISION_PAY);
        assertEq(o.amount, USDC);
        assertEq(o.payee, walA);
        assertEq(o.payeeAgentId, idA);
        assertEq(o.mandateId, M1);
        assertEq(o.deadline, p.deadline);
        assertEq(o.createdAt, vm.getBlockTimestamp());
        assertEq(vault.getMandate(M1).spent, USDC);
        (uint128 spent, uint8 count) = vault.spentInWindow();
        assertEq(spent, USDC);
        assertEq(count, 1);
    }

    function test_pay_payCappedDecisionAccepted() public {
        bytes32 oid = _oid();
        vm.prank(agent);
        vault.pay(_out(oid, idB, USDC / 2, M1, AcbTypes.DECISION_PAY_CAPPED));
        assertEq(vault.getOrder(oid).decision, AcbTypes.DECISION_PAY_CAPPED);
    }

    function test_pay_tier1_grades() public {
        _pay(idA, USDC, MALL);
        _pay(idB, USDC, MALL);
        bytes32 oid = _oid();
        _expectPayRevert(
            _out(oid, idC, USDC, MALL, AcbTypes.DECISION_PAY),
            abi.encodeWithSelector(TierVault.GradeNotAllowed.selector, idC, AcbTypes.GRADE_C, AB)
        );
        _expectPayRevert(
            _out(oid, idD, USDC, MALL, AcbTypes.DECISION_PAY),
            abi.encodeWithSelector(TierVault.GradeNotAllowed.selector, idD, AcbTypes.GRADE_D, AB)
        );
        _expectPayRevert(
            _out(oid, idNew, USDC / 4, MALL, AcbTypes.DECISION_PAY),
            abi.encodeWithSelector(TierVault.GradeNotAllowed.selector, idNew, AcbTypes.GRADE_NEW, AB)
        );
        _expectPayRevert(
            _out(oid, idNone, USDC / 4, MALL, AcbTypes.DECISION_PAY),
            abi.encodeWithSelector(TierVault.GradeNotAllowed.selector, idNone, AcbTypes.GRADE_NONE, AB)
        );
    }

    function test_pay_tier0_isMakerChecker() public {
        _setTier(0);
        bytes32 oid = _oid();
        _expectPayRevert(
            _out(oid, idA, 1, M1, AcbTypes.DECISION_PAY),
            abi.encodeWithSelector(TierVault.PerTxCapExceeded.selector, 1, 0)
        );
    }

    function test_pay_tier2_caps() public {
        _setTier(2);
        _pay(idA, 2 * USDC, MALL);
        bytes32 oid = _oid();
        _expectPayRevert(
            _out(oid, idA, 2 * USDC + 1, MALL, AcbTypes.DECISION_PAY),
            abi.encodeWithSelector(TierVault.PerTxCapExceeded.selector, 2 * USDC + 1, 2 * USDC)
        );
        _expectPayRevert(
            _out(oid, idNew, USDC / 4, MALL, AcbTypes.DECISION_PAY),
            abi.encodeWithSelector(TierVault.GradeNotAllowed.selector, idNew, AcbTypes.GRADE_NEW, AB)
        );
    }

    function test_pay_tier3_newPayeeWithinNewCap() public {
        _setTier(3);
        assertEq(vault.tier(), 3);
        _pay(idNew, USDC, MALL);
        assertEq(usdc.balanceOf(walNew), USDC);
        _pay(idA, 4 * USDC, MALL);
        bytes32 oid = _oid();
        _expectPayRevert(
            _out(oid, idNew, USDC + 1, MALL, AcbTypes.DECISION_PAY),
            abi.encodeWithSelector(TierVault.NewPayeeCapExceeded.selector, USDC + 1, USDC)
        );
        _expectPayRevert(
            _out(oid, idNone, USDC / 2, MALL, AcbTypes.DECISION_PAY),
            abi.encodeWithSelector(
                TierVault.GradeNotAllowed.selector, idNone, AcbTypes.GRADE_NONE, AB | AcbTypes.MASK_NEW
            )
        );
    }

    function test_pay_staleGradeBecomesNone() public {
        vm.warp(vm.getBlockTimestamp() + 7 days + 1);
        bytes32 mf = keccak256("fresh");
        uint256[] memory ids = new uint256[](1);
        address[] memory wals = new address[](1);
        (ids[0], wals[0]) = (idA, walA);
        vm.prank(owner);
        vault.registerMandate(mf, ids, wals, USDC, USDC, uint64(vm.getBlockTimestamp() + 1 days), false, 0);
        bytes32 oid = _oid();
        _expectPayRevert(
            _out(oid, idA, USDC, mf, AcbTypes.DECISION_PAY),
            abi.encodeWithSelector(TierVault.GradeNotAllowed.selector, idA, AcbTypes.GRADE_NONE, AB)
        );
        _post(idA, AcbTypes.GRADE_A);
        _pay(idA, USDC, mf);
    }

    // ------------------------------------------------------------------ each revert, in order

    function test_pay_onlyAgent() public {
        bytes32 oid = _oid();
        vm.prank(owner);
        vm.expectRevert(TierVault.NotAgent.selector);
        vault.pay(_out(oid, idA, USDC, M1, AcbTypes.DECISION_PAY));
    }

    function test_pay_frozen() public {
        vm.prank(owner);
        vault.freeze(3202, 0);
        bytes32 oid = _oid();
        _expectPayRevert(_out(oid, idA, USDC, M1, AcbTypes.DECISION_PAY), _sel(TierVault.Frozen.selector));
    }

    function test_pay_order1_badDecisionFirst() public {
        bytes32 oid = _pay(idA, USDC, M1);
        TierVault.OutflowParams memory p = _out(oid, idA, 0, bytes32(0), AcbTypes.DECISION_ESCROW);
        p.deadline = 0;
        _expectPayRevert(p, abi.encodeWithSelector(TierVault.BadDecision.selector, AcbTypes.DECISION_ESCROW));
        p.decision = AcbTypes.DECISION_REFUSE;
        _expectPayRevert(p, abi.encodeWithSelector(TierVault.BadDecision.selector, AcbTypes.DECISION_REFUSE));
    }

    function test_pay_order2_orderExists() public {
        bytes32 oid = _pay(idA, USDC, M1);
        TierVault.OutflowParams memory p = _out(oid, idA, 0, bytes32(0), AcbTypes.DECISION_PAY);
        p.deadline = 0;
        _expectPayRevert(p, abi.encodeWithSelector(TierVault.OrderExists.selector, oid));
    }

    function test_pay_order2_refusedOrderCanNeverBePaid() public {
        bytes32 oid = _oid();
        vm.prank(agent);
        vault.recordDecision(
            TierVault.DecisionParams(oid, idA, USDC, AcbTypes.DECISION_REFUSE, 2100, bytes32(0), bytes32(0))
        );
        _expectPayRevert(
            _out(oid, idA, USDC, M1, AcbTypes.DECISION_PAY), abi.encodeWithSelector(TierVault.OrderExists.selector, oid)
        );
    }

    function test_pay_order3_zeroAmount() public {
        TierVault.OutflowParams memory p = _out(_oid(), idA, 0, bytes32(0), AcbTypes.DECISION_PAY);
        p.deadline = 0;
        _expectPayRevert(p, _sel(TierVault.ZeroAmount.selector));
    }

    function test_pay_order4_deadlineInPast() public {
        TierVault.OutflowParams memory p = _out(_oid(), idA, USDC, bytes32(0), AcbTypes.DECISION_PAY);
        p.deadline = uint64(vm.getBlockTimestamp());
        _expectPayRevert(p, _sel(TierVault.BadDeadline.selector));
        p.deadline = uint64(vm.getBlockTimestamp() - 1);
        _expectPayRevert(p, _sel(TierVault.BadDeadline.selector));
    }

    function test_pay_order5_mandateBeforeTierCap() public {
        // tier cap (1.00) and mandate both fail: mandate check (1) wins
        bytes32 oid = _oid();
        _expectPayRevert(
            _out(oid, idA, 2 * USDC, bytes32(0), AcbTypes.DECISION_PAY),
            abi.encodeWithSelector(TierVault.MandateInactive.selector, bytes32(0))
        );
    }

    function test_pay_order6_tierCapBeforeGrade() public {
        bytes32 oid = _oid();
        _expectPayRevert(
            _out(oid, idD, 2 * USDC, MALL, AcbTypes.DECISION_PAY),
            abi.encodeWithSelector(TierVault.PerTxCapExceeded.selector, 2 * USDC, USDC)
        );
    }

    function test_pay_order7_gradeBeforePayeeCheck() public {
        _rotate(idC, walC);
        bytes32 oid = _oid();
        _expectPayRevert(
            _out(oid, idC, USDC, MALL, AcbTypes.DECISION_PAY),
            abi.encodeWithSelector(TierVault.GradeNotAllowed.selector, idC, AcbTypes.GRADE_C, AB)
        );
    }

    function test_pay_order8_payeeBeforeReserve() public {
        _drainTo(3 * USDC);
        _rotate(idA, walA);
        bytes32 oid = _oid();
        _expectPayRevert(
            _out(oid, idA, USDC, M1, AcbTypes.DECISION_PAY),
            abi.encodeWithSelector(TierVault.PayeeWalletChanged.selector, idA, address(0), walA)
        );
    }

    function test_pay_order9_reserveBeforeWindow() public {
        for (uint256 i = 0; i < 5; i++) {
            _pay(idA, USDC, MALL);
        }
        _drainTo(3 * USDC);
        bytes32 oid = _oid();
        _expectPayRevert(
            _out(oid, idA, USDC, MALL, AcbTypes.DECISION_PAY),
            abi.encodeWithSelector(TierVault.ReserveFloorBreached.selector, 3 * USDC, 0, 3 * USDC, USDC)
        );
    }

    function test_pay_reserveFloor_countsEscrowLocked() public {
        _drainTo(5 * USDC);
        _escrow(idC, USDC, MALL); // locks 1.00: free = 5 - 1 - 3 = 1.00
        _pay(idA, USDC, MALL);
        bytes32 oid = _oid();
        _expectPayRevert(
            _out(oid, idA, 1, MALL, AcbTypes.DECISION_PAY),
            abi.encodeWithSelector(TierVault.ReserveFloorBreached.selector, 4 * USDC, USDC, 3 * USDC, 1)
        );
        assertEq(vault.freeBalance(), 0);
    }

    function test_pay_newPayeeCap_escrowTier1() public {
        bytes32 oid = _oid();
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.NewPayeeCapExceeded.selector, USDC / 2 + 1, USDC / 2));
        vault.escrow(_out(oid, idNone, USDC / 2 + 1, MALL, AcbTypes.DECISION_ESCROW));
        vm.prank(agent);
        vault.escrow(_out(oid, idNone, USDC / 2, MALL, AcbTypes.DECISION_ESCROW));
    }

    // ------------------------------------------------------------------ self-dealing

    function test_selfDealing_agentRotatedOntoPinnedWallet() public {
        vm.prank(owner);
        vault.setAgent(walA);
        bytes32 oid = _oid();
        vm.prank(walA);
        vm.expectRevert(abi.encodeWithSelector(TierVault.SelfDealing.selector, walA));
        vault.pay(_out(oid, idA, USDC, M1, AcbTypes.DECISION_PAY));
    }

    function test_selfDealing_ownerIsPinnedWallet() public {
        vm.prank(owner);
        vault.transferOwnership(walA);
        vm.prank(walA);
        vault.acceptOwnership();
        bytes32 oid = _oid();
        _expectPayRevert(
            _out(oid, idA, USDC, M1, AcbTypes.DECISION_PAY),
            abi.encodeWithSelector(TierVault.SelfDealing.selector, walA)
        );
    }

    function test_selfDealing_autonomyIsPinnedWallet() public {
        vm.prank(owner);
        vault.setAutonomyOperator(walB);
        bytes32 oid = _oid();
        _expectPayRevert(
            _out(oid, idB, USDC, M1, AcbTypes.DECISION_PAY),
            abi.encodeWithSelector(TierVault.SelfDealing.selector, walB)
        );
    }

    function test_selfDealing_vaultAndRolesViaApproval() public {
        _setTier(0);
        address[4] memory roles = [address(vault), owner, agent, autonomy];
        for (uint256 i = 0; i < roles.length; i++) {
            bytes32 oid = _oid();
            vm.prank(owner);
            vault.ownerApprove(oid, idA, roles[i], USDC, uint64(vm.getBlockTimestamp() + 600), false, 0);
            // make the live registry answer the role address so only the self-dealing rule can fail
            vm.mockCall(
                address(identity), abi.encodeWithSelector(identity.getAgentWallet.selector, idA), abi.encode(roles[i])
            );
            vm.prank(agent);
            vm.expectRevert(abi.encodeWithSelector(TierVault.SelfDealing.selector, roles[i]));
            vault.executeApproved(oid, bytes32(0), bytes32(0));
            vm.clearMockedCalls();
        }
    }

    function test_selfDealing_ownAgentId() public {
        bytes32 oid = _oid();
        vm.prank(owner);
        vault.ownerApprove(oid, idWallet, owner, USDC, uint64(vm.getBlockTimestamp() + 600), false, 0);
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.SelfDealing.selector, address(0)));
        vault.executeApproved(oid, bytes32(0), bytes32(0));
    }

    // ------------------------------------------------------------------ token failures

    function test_transferFailed_falseToken() public {
        FalseToken ft = new FalseToken();
        TierVault.VaultInit memory init = _init(1);
        init.token = address(ft);
        TierVault v = new TierVault(init);
        ft.mint(address(v), 15 * USDC);
        uint256[] memory ids = new uint256[](1);
        address[] memory wals = new address[](1);
        (ids[0], wals[0]) = (idA, walA);
        vm.prank(owner);
        v.registerMandate(M1, ids, wals, USDC, 4 * USDC, uint64(vm.getBlockTimestamp() + 1 days), false, 0);

        FalseToken.Mode[3] memory modes =
            [FalseToken.Mode.ReturnFalse, FalseToken.Mode.Revert, FalseToken.Mode.ReturnNothing];
        for (uint256 i = 0; i < 3; i++) {
            ft.setMode(modes[i]);
            bytes32 oid = _oid();
            vm.prank(agent);
            if (modes[i] == FalseToken.Mode.ReturnNothing) {
                v.pay(_out(oid, idA, USDC, M1, AcbTypes.DECISION_PAY)); // USDT-style tokens are accepted
            } else {
                vm.expectRevert(TierVault.TransferFailed.selector);
                v.pay(_out(oid, idA, USDC, M1, AcbTypes.DECISION_PAY));
            }
        }
        // withdraw goes through the same safe transfer
        ft.setMode(FalseToken.Mode.ReturnFalse);
        vm.prank(owner);
        vm.expectRevert(TierVault.TransferFailed.selector);
        v.withdraw(owner, USDC);
    }

    function test_transferFailed_tokenWithoutCode() public {
        TierVault.VaultInit memory init = _init(1);
        init.token = makeAddr("eoaToken");
        TierVault v = new TierVault(init);
        vm.mockCall(init.token, abi.encodeWithSignature("balanceOf(address)", address(v)), abi.encode(15 * USDC));
        vm.etch(init.token, ""); // mockCall gives the address placeholder code; remove it again
        vm.prank(owner);
        vm.expectRevert(TierVault.TransferFailed.selector);
        v.withdraw(owner, USDC);
    }

    // ------------------------------------------------------------------ helpers

    function _rotate(uint256 id, address holder) internal {
        vm.prank(holder);
        identity.unsetAgentWallet(id);
    }

    function _drainTo(uint256 balance) internal {
        uint256 bal = usdc.balanceOf(address(vault));
        vm.prank(owner);
        vault.withdraw(owner, uint128(bal - balance));
    }
}
