// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {VaultTestBase} from "./utils/VaultTestBase.sol";
import {TierVault} from "../src/TierVault.sol";
import {AcbTypes} from "../src/lib/AcbTypes.sol";

/// @notice Rolling 24 h window over the last 32 outflows (spec 3.2 check 6, 3.6 TierVault.Window.t.sol).
/// @dev An outflow at time `ts` counts while `ts > now - windowSeconds`, i.e. during [ts, ts + windowSeconds).
contract TierVaultWindowTest is VaultTestBase {
    uint64 internal constant W = 86_400;
    bytes32 internal constant MBIG = keccak256("MBIG");

    function setUp() public override {
        super.setUp();
        usdc.mint(address(vault), 1_000 * USDC);
        uint256[] memory ids = new uint256[](2);
        address[] memory wals = new address[](2);
        (ids[0], ids[1]) = (idA, idB);
        (wals[0], wals[1]) = (walA, walB);
        vm.prank(owner);
        vault.registerMandate(
            MBIG, ids, wals, 4 * USDC, type(uint128).max, uint64(vm.getBlockTimestamp() + 7 days), false, bytes32("big")
        );
    }

    function _payExpect(uint128 amount, bytes memory err) internal {
        bytes32 oid = _oid();
        vm.prank(agent);
        vm.expectRevert(err);
        vault.pay(_out(oid, idA, amount, MBIG, AcbTypes.DECISION_PAY));
    }

    function test_spendUpToCap_thenRevert() public {
        for (uint256 i = 0; i < 5; i++) {
            _pay(idA, USDC, MBIG);
        }
        (uint128 spent, uint8 count) = vault.spentInWindow();
        assertEq(spent, 5 * USDC);
        assertEq(count, 5);
        _payExpect(1, abi.encodeWithSelector(TierVault.WindowCapExceeded.selector, 5 * USDC, 1, 5 * USDC));
    }

    function test_windowFreesExactlyAtWindowEnd() public {
        uint256 t0 = vm.getBlockTimestamp();
        for (uint256 i = 0; i < 5; i++) {
            _pay(idA, USDC, MBIG);
        }
        vm.warp(t0 + W - 1);
        _payExpect(1, abi.encodeWithSelector(TierVault.WindowCapExceeded.selector, 5 * USDC, 1, 5 * USDC));
        (uint128 spent,) = vault.spentInWindow();
        assertEq(spent, 5 * USDC);
        vm.warp(t0 + W);
        (spent,) = vault.spentInWindow();
        assertEq(spent, 0);
        for (uint256 i = 0; i < 5; i++) {
            _pay(idA, USDC, MBIG);
        }
        vm.warp(t0 + W + 1);
        _payExpect(1, abi.encodeWithSelector(TierVault.WindowCapExceeded.selector, 5 * USDC, 1, 5 * USDC));
    }

    function test_partialExpiry() public {
        uint256 t0 = vm.getBlockTimestamp();
        _pay(idA, 2 * USDC / 2, MBIG); // 1.00 at t0
        vm.warp(t0 + 3600);
        for (uint256 i = 0; i < 4; i++) {
            _pay(idA, USDC, MBIG); // 4.00 at t0 + 1h
        }
        vm.warp(t0 + W); // the first payment leaves the window
        _pay(idA, USDC, MBIG);
        _payExpect(1, abi.encodeWithSelector(TierVault.WindowCapExceeded.selector, 5 * USDC, 1, 5 * USDC));
    }

    function test_fixedBoundaryDoubleSpendImpossible() public {
        // A fixed daily bucket would reset at a day boundary; the rolling window does not.
        uint256 boundary = (vm.getBlockTimestamp() / W + 1) * W;
        vm.warp(boundary - 1);
        for (uint256 i = 0; i < 5; i++) {
            _pay(idA, USDC, MBIG);
        }
        vm.warp(boundary + 1);
        _payExpect(1, abi.encodeWithSelector(TierVault.WindowCapExceeded.selector, 5 * USDC, 1, 5 * USDC));
        vm.warp(boundary + W / 2);
        _payExpect(1, abi.encodeWithSelector(TierVault.WindowCapExceeded.selector, 5 * USDC, 1, 5 * USDC));
        vm.warp(boundary - 1 + W);
        _pay(idA, USDC, MBIG);
    }

    function test_tooManyTx() public {
        for (uint256 i = 0; i < 10; i++) {
            _pay(idA, USDC / 10, MBIG);
        }
        _payExpect(1, abi.encodeWithSelector(TierVault.TooManyTx.selector, 10, 10));
    }

    function test_tooManyTxBeforeWindowCap() public {
        for (uint256 i = 0; i < 5; i++) {
            _pay(idA, USDC, MBIG);
        }
        for (uint256 i = 0; i < 5; i++) {
            bytes32 oid = _oid();
            vm.prank(owner);
            vault.ownerApprove(oid, idA, walA, USDC / 10, uint64(vm.getBlockTimestamp() + 600), false, 0);
            vm.prank(agent);
            vault.executeApproved(oid, bytes32(0), bytes32(0));
        }
        _payExpect(USDC, abi.encodeWithSelector(TierVault.TooManyTx.selector, 10, 10));
    }

    function test_escrowCountsInWindow() public {
        uint256[] memory ids = new uint256[](1);
        address[] memory wals = new address[](1);
        (ids[0], wals[0]) = (idC, walC);
        bytes32 mc = keccak256("mc");
        vm.prank(owner);
        vault.registerMandate(mc, ids, wals, USDC, 100 * USDC, uint64(vm.getBlockTimestamp() + 1 days), false, 0);
        for (uint256 i = 0; i < 5; i++) {
            bytes32 oid = _escrow(idC, USDC, mc);
            vm.prank(agent);
            vault.refund(oid, 2401); // refunds do not give window capacity back
        }
        _payExpect(1, abi.encodeWithSelector(TierVault.WindowCapExceeded.selector, 5 * USDC, 1, 5 * USDC));
    }

    function test_approvedSpendsCountButAreNotCapped() public {
        for (uint256 i = 0; i < 3; i++) {
            bytes32 oid = _oid();
            vm.prank(owner);
            vault.ownerApprove(oid, idA, walA, 3 * USDC, uint64(vm.getBlockTimestamp() + 600), false, 0);
            vm.prank(agent);
            vault.executeApproved(oid, bytes32(0), bytes32(0)); // 9.00 total, above the 5.00 cap
        }
        (uint128 spent, uint8 count) = vault.spentInWindow();
        assertEq(spent, 9 * USDC);
        assertEq(count, 3);
        _payExpect(1, abi.encodeWithSelector(TierVault.WindowCapExceeded.selector, 9 * USDC, 1, 5 * USDC));
    }

    function test_ringFull_withOwnerApprovedSpends() public {
        for (uint256 i = 0; i < 32; i++) {
            bytes32 oid = _oid();
            vm.prank(owner);
            vault.ownerApprove(oid, idA, walA, USDC / 10, uint64(vm.getBlockTimestamp() + 600), false, 0);
            vm.prank(agent);
            vault.executeApproved(oid, bytes32(0), bytes32(0));
        }
        bytes32 last = _oid();
        vm.prank(owner);
        vault.ownerApprove(last, idA, walA, USDC / 10, uint64(vm.getBlockTimestamp() + 2 days), false, 0);
        vm.prank(agent);
        vm.expectRevert(TierVault.RingFull.selector);
        vault.executeApproved(last, bytes32(0), bytes32(0));
        (, uint8 count) = vault.spentInWindow();
        assertEq(count, 32);
        // capacity returns when the oldest slot leaves the window
        vm.warp(vm.getBlockTimestamp() + W);
        vm.prank(agent);
        vault.executeApproved(last, bytes32(0), bytes32(0));
    }

    /// @dev Fuzz: random amounts and warps; after every accepted payment the sum of payments made in the
    ///      rolling window (computed independently here) never exceeds the tier's window cap, and the count
    ///      never exceeds maxTxPerWindow.
    function testFuzz_windowNeverExceedsCap(uint256 seed) public {
        uint256[] memory ts = new uint256[](48);
        uint256[] memory amt = new uint256[](48);
        uint256 n;
        for (uint256 step = 0; step < 48; step++) {
            seed = uint256(keccak256(abi.encode(seed, step)));
            uint128 amount = uint128(bound(seed % 1e12, 1, USDC));
            uint256 dt = bound((seed >> 64) % 1e12, 0, W / 3);
            vm.warp(vm.getBlockTimestamp() + dt);
            if (vm.getBlockTimestamp() >= vault.getMandate(MBIG).validUntil) break;
            bytes32 oid = _oid();
            vm.prank(agent);
            try vault.pay(_out(oid, idA, amount, MBIG, AcbTypes.DECISION_PAY)) {
                ts[n] = vm.getBlockTimestamp();
                amt[n] = amount;
                n++;
                uint256 sum;
                uint256 cnt;
                for (uint256 i = 0; i < n; i++) {
                    if (ts[i] + W > vm.getBlockTimestamp()) {
                        sum += amt[i];
                        cnt++;
                    }
                }
                assertLe(sum, 5 * USDC, "window cap exceeded");
                assertLe(cnt, 10, "tx count exceeded");
                (uint128 spent, uint8 count) = vault.spentInWindow();
                assertEq(spent, sum, "vault window sum differs");
                assertEq(count, cnt, "vault window count differs");
            } catch (bytes memory err) {
                bytes4 sel = bytes4(err);
                assertTrue(
                    sel == TierVault.WindowCapExceeded.selector || sel == TierVault.TooManyTx.selector,
                    "unexpected revert"
                );
            }
        }
    }
}
