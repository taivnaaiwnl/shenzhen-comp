// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {TierVault} from "../../src/TierVault.sol";
import {AcbTypes} from "../../src/lib/AcbTypes.sol";
import {VaultHandlerBase} from "./VaultHandlerBase.sol";

/// @notice Invariant handler for TierVault (spec 3.2 TV-I1..TV-I11), part 1 of 2: agent outflows (pay, escrow,
///         owner-approved execution), the escrow lifecycle (claim, release, refund) and recording (deliveries,
///         decisions). Callers are every role, an outsider and the payees. Properties that are judged "at the time
///         of" an action are checked right after it and recorded as broken flags in the shared VaultGhost; the
///         invariant test asserts that no flag is set. Part 2 is VaultAdminHandler.
/// @dev Uses vm.getBlockTimestamp() instead of block.timestamp: with via_ir the optimizer may reuse a stale
///      block.timestamp across vm.warp calls inside one function.
contract VaultHandler is VaultHandlerBase {
    constructor(Refs memory r, uint256[] memory ids, address[] memory wallets) VaultHandlerBase(r, ids, wallets) {}

    /// @dev Bounded amount, mostly within the small tiers' caps, sometimes above them.
    function _amount(uint256 seed) internal pure returns (uint128) {
        if (seed % 10 == 0) return uint128(bound(seed >> 8, 1, 5_000_000));
        return uint128(bound(seed >> 8, 1, 1_000_000));
    }

    function _cfgWindowCap(uint8 t) internal view returns (uint128) {
        if (t > 3) return 0;
        return vault.tierConfig(t).windowCap;
    }

    function _params(bytes32 oid, uint256 pid, uint128 amount, bytes32 mid, uint8 decision)
        internal
        view
        returns (TierVault.OutflowParams memory)
    {
        return TierVault.OutflowParams({
            orderId: oid,
            payeeAgentId: pid,
            amount: amount,
            deadline: uint64(vm.getBlockTimestamp() + 600),
            mandateId: mid,
            invoiceHash: bytes32(0),
            decisionHash: bytes32(0),
            decision: decision,
            reasonCode: 2000
        });
    }

    // ------------------------------------------------------------------ agent outflows

    function pay(uint256 payeeSeed, uint256 mandateSeed, uint256 amountSeed) external track(K_OTHER) {
        (uint256 pid,) = _pickPayee(payeeSeed);
        bytes32 mid = ghost.pickMandate(mandateSeed);
        uint128 amount = _amount(amountSeed);
        bytes32 oid = ghost.newId("inv-order");
        address pinned = vault.mandatePayee(mid, pid);
        uint8 g = _grade(pid);
        uint8 t = vault.tier();
        uint128 cap = _cfgWindowCap(t);
        uint256 before = usdc.balanceOf(pinned);
        uint256 vaultBefore = usdc.balanceOf(address(vault));
        TierVault.OutflowParams memory p = _params(oid, pid, amount, mid, AcbTypes.DECISION_PAY);
        address agent = vault.agent();

        vm.prank(agent);
        vault.pay(p);

        ghost.agentOutflow(t, g, pinned, cap, false, amount);
        if (pinned == address(0) || vault.getOrder(oid).payee != pinned) {
            ghost.fail(10, "TV-I10: pay not to pinned wallet");
        }
        if (usdc.balanceOf(pinned) != before + amount) ghost.fail(10, "TV-I10: pinned wallet not credited");
        if (usdc.balanceOf(address(vault)) != vaultBefore - amount) ghost.fail(10, "TV-I10: vault debit differs");
        ghost.transition(oid, AcbTypes.STATE_NONE, AcbTypes.STATE_PAID, true);
        ghost.success("pay");
    }

    function escrow(uint256 payeeSeed, uint256 mandateSeed, uint256 amountSeed) external track(K_OTHER) {
        (uint256 pid,) = _pickPayee(payeeSeed);
        bytes32 mid = ghost.pickMandate(mandateSeed);
        uint128 amount = _amount(amountSeed);
        bytes32 oid = ghost.newId("inv-order");
        address pinned = vault.mandatePayee(mid, pid);
        uint8 g = _grade(pid);
        uint8 t = vault.tier();
        uint128 cap = _cfgWindowCap(t);
        uint256 vaultBefore = usdc.balanceOf(address(vault));
        TierVault.OutflowParams memory p = _params(oid, pid, amount, mid, AcbTypes.DECISION_ESCROW);
        address agent = vault.agent();

        vm.prank(agent);
        vault.escrow(p);

        ghost.agentOutflow(t, g, pinned, cap, false, amount);
        if (usdc.balanceOf(address(vault)) != vaultBefore) ghost.fail(10, "TV-I10: escrow moved funds");
        if (vault.getOrder(oid).payee != pinned) ghost.fail(10, "TV-I10: escrow payee not pinned");
        ghost.transition(oid, AcbTypes.STATE_NONE, AcbTypes.STATE_ESCROWED, false);
        ghost.success("escrow");
    }

    function approveAndExecute(uint256 payeeSeed, uint256 amountSeed, bool asEscrow, bool wrongPayee)
        external
        track(K_OTHER)
    {
        (uint256 pid, uint256 idx) = _pickPayee(payeeSeed);
        uint128 amount = uint128(bound(amountSeed, 1, 4_000_000));
        bytes32 oid = ghost.newId("inv-order");
        address expected = wrongPayee ? payeeWallets[(idx + 1) % payeeWallets.length] : payeeWallets[idx];
        uint64 nowTs = uint64(vm.getBlockTimestamp());
        vm.prank(owner);
        vault.ownerApprove(oid, pid, expected, amount, nowTs + 600, asEscrow, nowTs + 900);
        ghost.success("ownerApprove");

        uint8 g = _grade(pid);
        uint8 t = vault.tier();
        uint256 before = usdc.balanceOf(expected);
        address agent = vault.agent();
        vm.prank(agent);
        vault.executeApproved(oid, bytes32(0), bytes32(0));

        ghost.agentOutflow(t, g, expected, 0, true, amount);
        if (vault.getOrder(oid).payee != expected) ghost.fail(10, "TV-I10: approved payee differs");
        if (asEscrow) {
            ghost.transition(oid, AcbTypes.STATE_NONE, AcbTypes.STATE_ESCROWED, false);
        } else {
            if (usdc.balanceOf(expected) != before + amount) ghost.fail(10, "TV-I10: expectedPayee not credited");
            ghost.transition(oid, AcbTypes.STATE_NONE, AcbTypes.STATE_PAID, true);
        }
        ghost.success("executeApproved");
    }

    // ------------------------------------------------------------------ escrow lifecycle

    function claim(uint256 orderSeed) external track(K_OTHER) {
        bytes32 oid = ghost.pickOrderIn(orderSeed, AcbTypes.STATE_ESCROWED);
        TierVault.Order memory o = vault.getOrder(oid);
        vm.prank(o.payee);
        vault.claimDelivered(oid, keccak256(abi.encode("delivery", oid)));
        ghost.success("claim");
    }

    function release(uint256 orderSeed, uint256 callerSeed) external track(K_RELEASE) {
        bytes32 oid = ghost.pickOrderIn(orderSeed, AcbTypes.STATE_ESCROWED);
        TierVault.Order memory o = vault.getOrder(oid);
        address agent = vault.agent();
        address[5] memory callers = [owner, agent, outsider, o.payee, autonomy];
        address caller = callers[callerSeed % 5];
        bool frozen = vault.isFrozen();
        uint8 g = _grade(o.payeeAgentId);
        uint256 readyAt = uint256(o.claimedAt) + vault.disputeWindow();
        uint256 before = usdc.balanceOf(o.payee);

        vm.prank(caller);
        vault.release(oid);

        if (frozen && caller != owner && vm.getBlockTimestamp() < readyAt) {
            ghost.fail(8, "TV-I8: release while frozen before the dispute window");
        }
        if (caller == agent && caller != owner) {
            if (g == AcbTypes.GRADE_D) ghost.fail(6, "TV-I6: agent release to grade D");
            if (frozen) ghost.fail(8, "TV-I8: agent release while frozen");
        }
        if (usdc.balanceOf(o.payee) != before + o.amount) ghost.fail(10, "TV-I10: release not to stored payee");
        ghost.transition(oid, AcbTypes.STATE_ESCROWED, AcbTypes.STATE_RELEASED, true);
        ghost.success("release");
    }

    function refund(uint256 orderSeed, uint256 callerSeed, uint256 reasonSeed) external track(K_OTHER) {
        bytes32 oid = ghost.pickOrderIn(orderSeed, AcbTypes.STATE_ESCROWED);
        TierVault.Order memory o = vault.getOrder(oid);
        address[5] memory callers = [owner, vault.agent(), outsider, o.payee, autonomy];
        address caller = callers[callerSeed % 5];
        uint16[5] memory reasons = [uint16(2400), 2401, 2402, 2403, 1];
        uint16 reason = reasons[reasonSeed % 5];
        uint256 vaultBefore = usdc.balanceOf(address(vault));

        vm.prank(caller);
        vault.refund(oid, reason);

        if (o.claimedAt != 0 && (caller != owner || reason != 2403)) {
            ghost.fail(11, "TV-I11: claimed escrow refunded other than by owner 2403");
        }
        if (usdc.balanceOf(address(vault)) != vaultBefore) ghost.fail(10, "TV-I10: refund moved funds");
        ghost.transition(oid, AcbTypes.STATE_ESCROWED, AcbTypes.STATE_REFUNDED, false);
        ghost.success("refund");
    }

    // ------------------------------------------------------------------ recording

    function recordDelivery(uint256 orderSeed, uint8 outcomeSeed, bool byOwner) external track(K_OTHER) {
        bytes32 oid = ghost.pickOrderIn(orderSeed, AcbTypes.STATE_PAID);
        uint8 outcome = uint8(bound(outcomeSeed, 1, 3));
        address caller = byOwner ? owner : vault.agent();
        vm.prank(caller);
        vault.recordDelivery(oid, outcome, keccak256("d"));
        ghost.success("recordDelivery");
    }

    function recordDecision(uint256 payeeSeed, bool refuse) external track(K_OTHER) {
        (uint256 pid,) = _pickPayee(payeeSeed);
        bytes32 oid = ghost.newId("inv-order");
        uint8 d = refuse ? AcbTypes.DECISION_REFUSE : AcbTypes.DECISION_NEEDS_OWNER_APPROVAL;
        address agent = vault.agent();
        vm.prank(agent);
        vault.recordDecision(TierVault.DecisionParams(oid, pid, 1_000_000, d, 2100, bytes32(0), bytes32(0)));
        if (refuse) ghost.transition(oid, AcbTypes.STATE_NONE, AcbTypes.STATE_REFUSED, false);
        ghost.success("recordDecision");
    }
}
