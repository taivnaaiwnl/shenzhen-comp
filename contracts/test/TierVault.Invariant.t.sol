// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {StdInvariant} from "forge-std/StdInvariant.sol";
import {console2} from "forge-std/console2.sol";
import {VaultTestBase} from "./utils/VaultTestBase.sol";
import {TierVault} from "../src/TierVault.sol";
import {AcbTypes} from "../src/lib/AcbTypes.sol";
import {VaultHandler} from "./handlers/VaultHandler.sol";
import {VaultAdminHandler} from "./handlers/VaultAdminHandler.sol";
import {VaultHandlerBase} from "./handlers/VaultHandlerBase.sol";
import {VaultGhost} from "./handlers/VaultGhost.sol";

/// @notice TierVault invariants TV-I1..TV-I11 (spec 3.2) over random sequences of every external function
///         (512 runs, depth 64 from foundry.toml), driven by two handlers that share one VaultGhost.
contract TierVaultInvariantTest is StdInvariant, VaultTestBase {
    VaultGhost internal ghost;
    VaultHandler internal handler; // outflows, escrow lifecycle, recording
    VaultAdminHandler internal adminHandler; // tiers, owner administration, outside world

    function setUp() public override {
        super.setUp();
        uint256[] memory ids = new uint256[](6);
        address[] memory wals = new address[](6);
        uint256[] memory keys = new uint256[](6);
        string[6] memory labels = ["payeeA", "payeeB", "payeeC", "payeeD", "payeeNew", "payeeNone"];
        (ids[0], ids[1], ids[2], ids[3], ids[4], ids[5]) = (idA, idB, idC, idD, idNew, idNone);
        for (uint256 i = 0; i < 6; i++) {
            (wals[i], keys[i]) = makeAddrAndKey(labels[i]);
        }
        ghost = new VaultGhost(vault, usdc);
        VaultHandlerBase.Refs memory refs =
            VaultHandlerBase.Refs(vault, usdc, identity, acb, ghost, owner, autonomy, poster);
        handler = new VaultHandler(refs, ids, wals);
        adminHandler = new VaultAdminHandler(refs, ids, wals, keys);
        ghost.addMandate(M1);
        ghost.addMandate(MALL);
        usdc.mint(adminHandler.donor(), 1_000 * USDC);
        // the handlers drive the bureau through the poster; the vault starts at tier 1 with 15 USDC

        // outflows and the escrow lifecycle are listed several times to weight them
        bytes4[] memory flows = new bytes4[](15);
        flows[0] = VaultHandler.pay.selector;
        flows[1] = VaultHandler.pay.selector;
        flows[2] = VaultHandler.pay.selector;
        flows[3] = VaultHandler.escrow.selector;
        flows[4] = VaultHandler.escrow.selector;
        flows[5] = VaultHandler.approveAndExecute.selector;
        flows[6] = VaultHandler.claim.selector;
        flows[7] = VaultHandler.claim.selector;
        flows[8] = VaultHandler.release.selector;
        flows[9] = VaultHandler.release.selector;
        flows[10] = VaultHandler.refund.selector;
        flows[11] = VaultHandler.refund.selector;
        flows[12] = VaultHandler.recordDelivery.selector;
        flows[13] = VaultHandler.recordDelivery.selector;
        flows[14] = VaultHandler.recordDecision.selector;
        targetSelector(FuzzSelector({addr: address(handler), selectors: flows}));
        targetContract(address(handler));

        bytes4[] memory ops = new bytes4[](19);
        ops[0] = VaultAdminHandler.recordViolation.selector;
        ops[1] = VaultAdminHandler.setTier.selector;
        ops[2] = VaultAdminHandler.setTier.selector;
        ops[3] = VaultAdminHandler.freeze.selector;
        ops[4] = VaultAdminHandler.unfreeze.selector;
        ops[5] = VaultAdminHandler.setMaxAutoTier.selector;
        ops[6] = VaultAdminHandler.donate.selector;
        ops[7] = VaultAdminHandler.setReserveFloor.selector;
        ops[8] = VaultAdminHandler.registerMandate.selector;
        ops[9] = VaultAdminHandler.registerMandate.selector;
        ops[10] = VaultAdminHandler.donate.selector;
        ops[11] = VaultAdminHandler.giftNft.selector;
        ops[12] = VaultAdminHandler.warp.selector;
        ops[13] = VaultAdminHandler.regrade.selector;
        ops[14] = VaultAdminHandler.rotatePayeeWallet.selector;
        ops[15] = VaultAdminHandler.withdraw.selector;
        ops[16] = VaultAdminHandler.revokeMandate.selector;
        ops[17] = VaultAdminHandler.resetToTier1.selector;
        ops[18] = VaultAdminHandler.resetToTier1.selector;
        targetSelector(FuzzSelector({addr: address(adminHandler), selectors: ops}));
        targetContract(address(adminHandler));
    }

    /// Smoke test: each handler action works on its own (guards against a handler that only reverts).
    function test_handlerSmoke() public {
        handler.pay(0, 1, 500_000); // payee A under MALL
        handler.escrow(2, 1, 400_000); // payee C under MALL
        handler.approveAndExecute(1, 2_000_000, false, false);
        handler.approveAndExecute(0, 300_000, true, false);
        assertEq(ghost.orderCount(), 4);
        handler.claim(1); // the escrow
        handler.release(1, 0);
        handler.recordDelivery(0, 1, false);
        handler.recordDecision(0, true);
        adminHandler.donate(1_000_000);
        adminHandler.giftNft(true);
        adminHandler.giftNft(false);
        adminHandler.registerMandate(3, 1_000_000, 2_000_000, 3600);
        adminHandler.rotatePayeeWallet(0, false);
        adminHandler.rotatePayeeWallet(0, true);
        adminHandler.regrade(1, 5);
        adminHandler.warp(100);
        adminHandler.withdraw(1_000_000);
        adminHandler.setReserveFloor(2_000_000);
        adminHandler.recordViolation(1, 1, false);
        adminHandler.freeze(0);
        adminHandler.unfreeze(1, 0);
        adminHandler.setMaxAutoTier(3);
        adminHandler.setTier(0);
        assertEq(ghost.broken(), 0, ghost.failure());
        assertEq(ghost.successes("pay"), 1);
        assertEq(ghost.successes("release"), 1);
        assertEq(ghost.successes("unfreeze"), 1);
    }

    function _noFlag(uint256 inv) internal view {
        assertEq((ghost.broken() >> inv) & 1, 0, ghost.failure());
    }

    /// TV-I1: token.balanceOf(vault) >= escrowLocked always.
    function invariant_TV_I1_balanceCoversEscrow() public view {
        assertGe(usdc.balanceOf(address(vault)), vault.escrowLocked());
        _noFlag(1);
    }

    /// TV-I2: after any agent-initiated outflow, balanceOf(vault) >= escrowLocked + reserveFloor.
    function invariant_TV_I2_reserveAfterAgentOutflow() public view {
        _noFlag(2);
    }

    /// TV-I3: each agent outflow keeps non-approved agent outflows in the window <= the tier's cap at that time.
    function invariant_TV_I3_windowCap() public view {
        _noFlag(3);
    }

    /// TV-I4: tier in {0,1,2,3,255}; increases (other than unfreeze) are earned +1 steps by the operator.
    function invariant_TV_I4_tierChanges() public view {
        uint8 t = vault.tier();
        assertTrue(t <= 3 || t == AcbTypes.TIER_FROZEN, "tier outside the set");
        assertLe(vault.tierBeforeFreeze(), 3);
        _noFlag(4);
    }

    /// TV-I5: inbound token transfers and NFT transfer attempts never change tier or counters.
    function invariant_TV_I5_inboundAssets() public view {
        _noFlag(5);
    }

    /// TV-I6: no agent outflow to a grade-D payee or to owner, agent, autonomy operator or the vault.
    function invariant_TV_I6_noDOrSelfDealing() public view {
        _noFlag(6);
    }

    /// TV-I7: order states move only along the allowed paths; no order pays twice.
    function invariant_TV_I7_orderStates() public view {
        uint256 n = ghost.orderCount();
        for (uint256 i = 0; i < n; i++) {
            bytes32 oid = ghost.orderIds(i);
            assertEq(vault.getOrder(oid).state, ghost.ghostState(oid), "vault state differs from ghost");
            assertLe(ghost.ghostTransfers(oid), 1, "paid twice");
        }
        _noFlag(7);
    }

    /// TV-I8: while frozen the only outflows are owner withdraw, owner release and release after the window.
    function invariant_TV_I8_frozenOutflows() public view {
        _noFlag(8);
    }

    /// TV-I9: tier leaves 255 only through unfreeze.
    function invariant_TV_I9_leaveFrozenOnlyByUnfreeze() public view {
        _noFlag(9);
    }

    /// TV-I10: mandate spent <= maxTotal; outflows go to the pinned wallet or the approval's expectedPayee.
    function invariant_TV_I10_mandates() public view {
        uint256 n = ghost.mandateCount();
        for (uint256 i = 0; i < n; i++) {
            TierVault.Mandate memory m = vault.getMandate(ghost.mandateIds(i));
            assertLe(m.spent, m.maxTotal, "mandate overspent");
        }
        _noFlag(10);
    }

    /// TV-I11: an escrow with a claim is never refunded except by the owner with reason 2403.
    function invariant_TV_I11_claimedEscrowRefunds() public view {
        _noFlag(11);
    }

    /// After each run: no flag at all, and a coverage line (shown with -vvv) of the successful actions.
    function afterInvariant() external view {
        assertEq(ghost.broken(), 0, ghost.failure());
        string[13] memory names = [
            "pay",
            "escrow",
            "executeApproved",
            "claim",
            "release",
            "refund",
            "recordDelivery",
            "recordViolation",
            "setTier",
            "unfreeze",
            "withdraw",
            "registerMandate",
            "rotatePayeeWallet"
        ];
        // (resetToTier1 and the other owner actions are counted too; see VaultGhost.successes)
        string memory line = "successes:";
        for (uint256 i = 0; i < names.length; i++) {
            line = string.concat(line, " ", names[i], "=", vm.toString(ghost.successes(names[i])));
        }
        console2.log(line);
    }
}
