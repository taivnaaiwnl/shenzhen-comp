// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {Test} from "forge-std/Test.sol";
import {MockUSDC} from "../src/MockUSDC.sol";

/// @notice ERC-20 basics and mint access of the local mock USDC (spec 3.3).
contract MockUSDCTest is Test {
    MockUSDC internal usdc;
    address internal minter = makeAddr("minter");
    address internal alice = makeAddr("alice");
    address internal bob = makeAddr("bob");

    function setUp() public {
        usdc = new MockUSDC(minter);
    }

    function test_metadata() public view {
        assertEq(usdc.name(), "Mock USD Coin");
        assertEq(usdc.symbol(), "mUSDC");
        assertEq(usdc.decimals(), 6);
        assertEq(usdc.totalSupply(), 0);
        assertEq(usdc.minter(), minter);
    }

    function test_mint_onlyMinter() public {
        vm.prank(alice);
        vm.expectRevert(MockUSDC.NotMinter.selector);
        usdc.mint(alice, 1);
        vm.expectEmit(true, true, false, true, address(usdc));
        emit MockUSDC.Transfer(address(0), alice, 5e6);
        vm.prank(minter);
        usdc.mint(alice, 5e6);
        assertEq(usdc.balanceOf(alice), 5e6);
        assertEq(usdc.totalSupply(), 5e6);
    }

    function test_transfer() public {
        vm.prank(minter);
        usdc.mint(alice, 5e6);
        vm.expectEmit(true, true, false, true, address(usdc));
        emit MockUSDC.Transfer(alice, bob, 2e6);
        vm.prank(alice);
        assertTrue(usdc.transfer(bob, 2e6));
        assertEq(usdc.balanceOf(alice), 3e6);
        assertEq(usdc.balanceOf(bob), 2e6);
        vm.prank(alice);
        vm.expectRevert(MockUSDC.InsufficientBalance.selector);
        usdc.transfer(bob, 3e6 + 1);
        assertEq(usdc.totalSupply(), 5e6);
    }

    function test_approveAndTransferFrom() public {
        vm.prank(minter);
        usdc.mint(alice, 5e6);
        vm.expectEmit(true, true, false, true, address(usdc));
        emit MockUSDC.Approval(alice, bob, 2e6);
        vm.prank(alice);
        assertTrue(usdc.approve(bob, 2e6));
        assertEq(usdc.allowance(alice, bob), 2e6);
        vm.prank(bob);
        vm.expectRevert(MockUSDC.InsufficientAllowance.selector);
        usdc.transferFrom(alice, bob, 2e6 + 1);
        vm.prank(bob);
        assertTrue(usdc.transferFrom(alice, bob, 15e5));
        assertEq(usdc.allowance(alice, bob), 5e5);
        assertEq(usdc.balanceOf(bob), 15e5);
        // infinite allowance is not decremented
        vm.prank(alice);
        usdc.approve(bob, type(uint256).max);
        vm.prank(bob);
        usdc.transferFrom(alice, bob, 1e6);
        assertEq(usdc.allowance(alice, bob), type(uint256).max);
        // allowance does not bypass the balance check
        vm.prank(bob);
        vm.expectRevert(MockUSDC.InsufficientBalance.selector);
        usdc.transferFrom(alice, bob, 10e6);
    }

    function testFuzz_transferConservesSupply(uint96 minted, uint96 sent) public {
        vm.prank(minter);
        usdc.mint(alice, minted);
        vm.prank(alice);
        if (sent > minted) {
            vm.expectRevert(MockUSDC.InsufficientBalance.selector);
            usdc.transfer(bob, sent);
        } else {
            usdc.transfer(bob, sent);
        }
        assertEq(usdc.balanceOf(alice) + usdc.balanceOf(bob), minted);
        assertEq(usdc.totalSupply(), minted);
    }
}
