// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {VaultTestBase} from "./utils/VaultTestBase.sol";
import {TierVault} from "../src/TierVault.sol";
import {AcbTypes} from "../src/lib/AcbTypes.sol";

/// @notice On-chain mandates with pinned payout wallets (spec 3.2 "Mandates", 3.6 TierVault.Mandate.t.sol).
contract TierVaultMandateTest is VaultTestBase {
    bytes32 internal constant MX = keccak256("MX");

    function _one(uint256 id) internal pure returns (uint256[] memory a) {
        a = new uint256[](1);
        a[0] = id;
    }

    function _oneW(address w) internal pure returns (address[] memory a) {
        a = new address[](1);
        a[0] = w;
    }

    function _reg(bytes32 id, uint256[] memory ids, address[] memory wals, uint128 perTx, uint128 total, uint64 until)
        internal
    {
        vm.prank(owner);
        vault.registerMandate(id, ids, wals, perTx, total, until, false, bytes32("purpose"));
    }

    function _until() internal view returns (uint64) {
        return uint64(vm.getBlockTimestamp() + 1 days);
    }

    // ------------------------------------------------------------------ registerMandate

    function test_register_storesMandateAndPinnedWallets() public {
        vm.expectEmit(true, false, false, true, address(vault));
        emit TierVault.MandateRegistered(
            MX, _one(idA), _oneW(walA), USDC, 2 * USDC, _until(), false, bytes32("purpose")
        );
        _reg(MX, _one(idA), _oneW(walA), USDC, 2 * USDC, _until());
        TierVault.Mandate memory m = vault.getMandate(MX);
        assertEq(m.maxPerTx, USDC);
        assertEq(m.maxTotal, 2 * USDC);
        assertEq(m.spent, 0);
        assertEq(m.validUntil, _until());
        assertTrue(m.active);
        assertFalse(m.recurring);
        assertEq(vault.mandatePayee(MX, idA), walA);
        assertEq(vault.mandatePayee(MX, idB), address(0));
    }

    function test_register_onlyOwner() public {
        vm.prank(agent);
        vm.expectRevert(TierVault.NotOwner.selector);
        vault.registerMandate(MX, _one(idA), _oneW(walA), USDC, USDC, _until(), false, 0);
    }

    function test_register_worksWhileFrozen() public {
        vm.prank(owner);
        vault.freeze(3202, 0);
        _reg(MX, _one(idA), _oneW(walA), USDC, USDC, _until());
        assertTrue(vault.getMandate(MX).active);
    }

    function test_register_duplicateId() public {
        _reg(MX, _one(idA), _oneW(walA), USDC, USDC, _until());
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(TierVault.MandateExists.selector, MX));
        vault.registerMandate(MX, _one(idB), _oneW(walB), USDC, USDC, _until(), false, 0);
    }

    function test_register_revokedIdCannotBeReused() public {
        _reg(MX, _one(idA), _oneW(walA), USDC, USDC, _until());
        vm.prank(owner);
        vault.revokeMandate(MX);
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(TierVault.MandateExists.selector, MX));
        vault.registerMandate(MX, _one(idA), _oneW(walA), USDC, USDC, _until(), false, 0);
    }

    function test_register_badMandate_zeroId() public {
        vm.prank(owner);
        vm.expectRevert(TierVault.BadMandate.selector);
        vault.registerMandate(bytes32(0), _one(idA), _oneW(walA), USDC, USDC, _until(), false, 0);
    }

    function test_register_badMandate_emptyPayees() public {
        vm.prank(owner);
        vm.expectRevert(TierVault.BadMandate.selector);
        vault.registerMandate(MX, new uint256[](0), new address[](0), USDC, USDC, _until(), false, 0);
    }

    function test_register_badMandate_lengthMismatch() public {
        address[] memory wals = new address[](2);
        (wals[0], wals[1]) = (walA, walB);
        vm.prank(owner);
        vm.expectRevert(TierVault.BadMandate.selector);
        vault.registerMandate(MX, _one(idA), wals, USDC, USDC, _until(), false, 0);
    }

    function test_register_badMandate_tooManyPayees() public {
        uint256[] memory ids = new uint256[](7);
        address[] memory wals = new address[](7);
        for (uint256 i = 0; i < 7; i++) {
            (ids[i], wals[i]) = _registerPayee(string.concat("extra", vm.toString(i)));
        }
        vm.prank(owner);
        vm.expectRevert(TierVault.BadMandate.selector);
        vault.registerMandate(MX, ids, wals, USDC, USDC, _until(), false, 0);
    }

    function test_register_badMandate_duplicatePayee() public {
        uint256[] memory ids = new uint256[](2);
        address[] memory wals = new address[](2);
        (ids[0], ids[1]) = (idA, idA);
        (wals[0], wals[1]) = (walA, walA);
        vm.prank(owner);
        vm.expectRevert(TierVault.BadMandate.selector);
        vault.registerMandate(MX, ids, wals, USDC, USDC, _until(), false, 0);
    }

    function test_register_badMandate_zeroPerTx() public {
        vm.prank(owner);
        vm.expectRevert(TierVault.BadMandate.selector);
        vault.registerMandate(MX, _one(idA), _oneW(walA), 0, USDC, _until(), false, 0);
    }

    function test_register_badMandate_perTxAboveTotal() public {
        vm.prank(owner);
        vm.expectRevert(TierVault.BadMandate.selector);
        vault.registerMandate(MX, _one(idA), _oneW(walA), 2 * USDC, USDC, _until(), false, 0);
    }

    function test_register_badMandate_validUntilNotInFuture() public {
        vm.prank(owner);
        vm.expectRevert(TierVault.BadMandate.selector);
        vault.registerMandate(MX, _one(idA), _oneW(walA), USDC, USDC, uint64(vm.getBlockTimestamp()), false, 0);
    }

    function test_register_badMandate_tooLongValidity() public {
        uint64 tooLong = uint64(vm.getBlockTimestamp() + 7 days + 1);
        vm.prank(owner);
        vm.expectRevert(TierVault.BadMandate.selector);
        vault.registerMandate(MX, _one(idA), _oneW(walA), USDC, USDC, tooLong, false, 0);
        // exactly the maximum is accepted
        _reg(MX, _one(idA), _oneW(walA), USDC, USDC, uint64(vm.getBlockTimestamp() + 7 days));
    }

    function test_register_badMandate_selfAgentId() public {
        vm.prank(owner);
        vm.expectRevert(TierVault.BadMandate.selector);
        vault.registerMandate(MX, _one(idWallet), _oneW(owner), USDC, USDC, _until(), false, 0);
    }

    function test_register_badMandate_zeroWallet() public {
        vm.prank(owner);
        vm.expectRevert(TierVault.BadMandate.selector);
        vault.registerMandate(MX, _one(idA), _oneW(address(0)), USDC, USDC, _until(), false, 0);
    }

    function test_register_badMandate_roleWallets() public {
        address[4] memory roles = [address(vault), owner, agent, autonomy];
        for (uint256 i = 0; i < roles.length; i++) {
            // the role rule is checked before the live-wallet lookup
            vm.prank(owner);
            vm.expectRevert(TierVault.BadMandate.selector);
            vault.registerMandate(MX, _one(idA), _oneW(roles[i]), USDC, USDC, _until(), false, 0);
        }
    }

    function test_register_walletMismatch() public {
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(TierVault.PayeeWalletMismatch.selector, idA, walA, walB));
        vault.registerMandate(MX, _one(idA), _oneW(walB), USDC, USDC, _until(), false, 0);
    }

    function test_register_walletMismatch_nonexistentAgent() public {
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(TierVault.PayeeWalletMismatch.selector, 999, address(0), walA));
        vault.registerMandate(MX, _one(999), _oneW(walA), USDC, USDC, _until(), false, 0);
    }

    // ------------------------------------------------------------------ revokeMandate

    function test_revoke() public {
        vm.expectEmit(true, false, false, false, address(vault));
        emit TierVault.MandateRevoked(M1);
        vm.prank(owner);
        vault.revokeMandate(M1);
        assertFalse(vault.getMandate(M1).active);
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(TierVault.MandateInactive.selector, M1));
        vault.revokeMandate(M1);
    }

    function test_revoke_unknown() public {
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(TierVault.MandateInactive.selector, MX));
        vault.revokeMandate(MX);
    }

    function test_revoke_onlyOwner_andWhileFrozen() public {
        vm.prank(agent);
        vm.expectRevert(TierVault.NotOwner.selector);
        vault.revokeMandate(M1);
        vm.prank(agent);
        vault.freeze(3203, 0);
        vm.prank(owner);
        vault.revokeMandate(M1);
        assertFalse(vault.getMandate(M1).active);
    }

    // ------------------------------------------------------------------ outflow checks (check 1)

    function test_pay_inactiveMandate() public {
        vm.prank(owner);
        vault.revokeMandate(M1);
        bytes32 oid = _oid();
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.MandateInactive.selector, M1));
        vault.pay(_out(oid, idA, USDC, M1, AcbTypes.DECISION_PAY));
    }

    function test_pay_unknownMandate() public {
        bytes32 oid = _oid();
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.MandateInactive.selector, MX));
        vault.pay(_out(oid, idA, USDC, MX, AcbTypes.DECISION_PAY));
    }

    function test_pay_expiredMandate() public {
        vm.warp(vm.getBlockTimestamp() + 1 days + 1);
        bytes32 oid = _oid();
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.MandateExpired.selector, M1));
        vault.pay(_out(oid, idA, USDC, M1, AcbTypes.DECISION_PAY));
    }

    function test_pay_atExactValidUntil() public {
        vm.warp(vault.getMandate(M1).validUntil);
        _pay(idA, USDC, M1);
    }

    function test_pay_payeeNotInMandate() public {
        bytes32 oid = _oid();
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.NotInMandate.selector, M1, idD));
        vault.pay(_out(oid, idD, USDC, M1, AcbTypes.DECISION_PAY));
    }

    function test_pay_mandatePerTx() public {
        _setTier(2);
        bytes32 oid = _oid();
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.MandatePerTxExceeded.selector, USDC + 1, USDC));
        vault.pay(_out(oid, idA, USDC + 1, M1, AcbTypes.DECISION_PAY));
    }

    function test_pay_mandateBudget() public {
        _pay(idA, USDC, M1);
        _pay(idA, USDC, M1);
        _pay(idB, USDC, M1);
        _pay(idB, USDC / 2, M1);
        bytes32 oid = _oid();
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.MandateBudgetExceeded.selector, 7 * USDC / 2, USDC, 4 * USDC));
        vault.pay(_out(oid, idA, USDC, M1, AcbTypes.DECISION_PAY));
        assertEq(vault.getMandate(M1).spent, 7 * USDC / 2);
    }

    function test_escrow_mandateBudgetCountsEscrows() public {
        _escrow(idC, USDC, M1);
        _escrow(idC, USDC, M1);
        _escrow(idC, USDC, M1);
        _escrow(idC, USDC, M1);
        bytes32 oid = _oid();
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.MandateBudgetExceeded.selector, 4 * USDC, 1, 4 * USDC));
        vault.escrow(_out(oid, idC, 1, M1, AcbTypes.DECISION_ESCROW));
    }

    function test_recurringNeedsTier2() public {
        bytes32 mr = keccak256("recurring");
        vm.prank(owner);
        vault.registerMandate(mr, _one(idA), _oneW(walA), USDC, 4 * USDC, _until(), true, 0);
        bytes32 oid = _oid();
        vm.prank(agent);
        vm.expectRevert(TierVault.RecurringNeedsTier2.selector);
        vault.pay(_out(oid, idA, USDC, mr, AcbTypes.DECISION_PAY));
        vm.prank(agent);
        vm.expectRevert(TierVault.RecurringNeedsTier2.selector);
        vault.escrow(_out(oid, idA, USDC, mr, AcbTypes.DECISION_ESCROW));
        _setTier(2);
        _pay(idA, USDC, mr);
    }

    function test_mandateChecksComeBeforeTierCaps() public {
        // amount above both the mandate per-tx (1.00) and the tier cap (1.00 at tier 1): mandate reported
        bytes32 oid = _oid();
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.MandatePerTxExceeded.selector, 2 * USDC, USDC));
        vault.pay(_out(oid, idA, 2 * USDC, M1, AcbTypes.DECISION_PAY));
    }

    // ------------------------------------------------------------------ budget restored on refund

    function test_budgetRestoredOnRefund() public {
        bytes32 oid = _escrow(idC, USDC, M1);
        assertEq(vault.getMandate(M1).spent, USDC);
        vm.prank(agent);
        vault.refund(oid, 2401);
        assertEq(vault.getMandate(M1).spent, 0);
    }

    // ------------------------------------------------------------------ payee wallet rotation

    function test_walletRotation_nftTransfer_blocksPay() public {
        address newOwner = makeAddr("newOwnerOfA");
        vm.prank(walA);
        identity.transferFrom(walA, newOwner, idA);
        assertEq(identity.getAgentWallet(idA), address(0));
        bytes32 oid = _oid();
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.PayeeWalletChanged.selector, idA, address(0), walA));
        vault.pay(_out(oid, idA, USDC, M1, AcbTypes.DECISION_PAY));

        // the new owner sets a new wallet: still blocked, and funds never reach it
        (address newWallet, uint256 key) = makeAddrAndKey("rotatedWalletA");
        _setAgentWallet(newOwner, idA, newWallet, key);
        assertEq(identity.getAgentWallet(idA), newWallet);
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.PayeeWalletChanged.selector, idA, newWallet, walA));
        vault.pay(_out(oid, idA, USDC, M1, AcbTypes.DECISION_PAY));
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.PayeeWalletChanged.selector, idA, newWallet, walA));
        vault.escrow(_out(oid, idA, USDC, M1, AcbTypes.DECISION_ESCROW));
        assertEq(usdc.balanceOf(newWallet), 0);
    }

    function test_walletRotation_setAgentWallet_blocksPay() public {
        (address newWallet, uint256 key) = makeAddrAndKey("rotatedWalletB");
        _setAgentWallet(walB, idB, newWallet, key);
        bytes32 oid = _oid();
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.PayeeWalletChanged.selector, idB, newWallet, walB));
        vault.pay(_out(oid, idB, USDC, M1, AcbTypes.DECISION_PAY));
        assertEq(usdc.balanceOf(newWallet), 0);
        // a new mandate pinned to the new wallet is needed
        _reg(MX, _one(idB), _oneW(newWallet), USDC, USDC, _until());
        _pay(idB, USDC, MX);
        assertEq(usdc.balanceOf(newWallet), USDC);
    }

    function test_walletRotation_fundsGoToPinnedWallet() public {
        uint256 before = usdc.balanceOf(walA);
        bytes32 oid = _pay(idA, USDC, M1);
        assertEq(usdc.balanceOf(walA), before + USDC);
        assertEq(vault.getOrder(oid).payee, walA);
    }

    // ------------------------------------------------------------------ helpers

    function _setAgentWallet(address idOwner, uint256 id, address newWallet, uint256 key) internal {
        uint256 deadline = vm.getBlockTimestamp() + 60;
        bytes32 structHash = keccak256(
            abi.encode(
                keccak256("AgentWalletSet(uint256 agentId,address newWallet,address owner,uint256 deadline)"),
                id,
                newWallet,
                idOwner,
                deadline
            )
        );
        bytes32 domain = keccak256(
            abi.encode(
                keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
                keccak256("ERC8004IdentityRegistry"),
                keccak256("1"),
                block.chainid,
                address(identity)
            )
        );
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(key, keccak256(abi.encodePacked("\x19\x01", domain, structHash)));
        vm.prank(idOwner);
        identity.setAgentWallet(id, newWallet, deadline, abi.encodePacked(r, s, v));
    }
}
