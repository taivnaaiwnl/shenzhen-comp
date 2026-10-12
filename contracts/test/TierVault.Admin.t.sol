// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {VaultTestBase} from "./utils/VaultTestBase.sol";
import {TierVault} from "../src/TierVault.sol";
import {AcbTypes} from "../src/lib/AcbTypes.sol";

/// @notice Token that calls back into a hook on every transfer (reentrancy probe).
contract ReentrantToken {
    mapping(address => uint256) public balanceOf;
    address public hook;

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
    }

    function setHook(address h) external {
        hook = h;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        if (hook != address(0)) OwnerHook(hook).reenter();
        return true;
    }
}

/// @notice A contract vault owner that tries to re-enter `withdraw` from inside the token transfer.
contract OwnerHook {
    TierVault public vault;
    bytes4 public innerError;
    bool internal _entered;

    function setVault(TierVault v) external {
        vault = v;
    }

    function withdraw(uint128 amount) external {
        vault.withdraw(address(this), amount);
    }

    function reenter() external {
        if (_entered) return;
        _entered = true;
        try vault.withdraw(address(this), 1) {
            innerError = bytes4(0xffffffff);
        } catch (bytes memory err) {
            innerError = bytes4(err);
        }
    }

    function acceptOwnership(TierVault v) external {
        v.acceptOwnership();
    }
}

/// @notice Role checks, configuration validation, withdraw, key rotation and ownership (spec 3.2, 3.6).
contract TierVaultAdminTest is VaultTestBase {
    // ------------------------------------------------------------------ constructor

    function test_constructor_state() public view {
        assertEq(address(vault.token()), address(usdc));
        assertEq(address(vault.bureau()), address(acb));
        assertEq(address(vault.identity()), address(identity));
        assertEq(vault.owner(), owner);
        assertEq(vault.agent(), agent);
        assertEq(vault.autonomyOperator(), autonomy);
        assertEq(vault.agentId(), idWallet);
        assertEq(vault.tier(), 1);
        assertEq(vault.maxAutoTier(), 2);
        assertEq(vault.reserveFloor(), 3 * USDC);
        assertEq(vault.minCleanAmount(), USDC / 10);
        assertEq(vault.windowSeconds(), 86_400);
        assertEq(vault.promotionCooldown(), 60);
        assertEq(vault.maxGradeAge(), 7 days);
        assertEq(vault.disputeWindow(), 120);
        assertEq(vault.lastTierChangeAt(), T0);
        assertEq(vault.RING_SIZE(), 32);
        assertEq(vault.MAX_MANDATE_PAYEES(), 6);
        assertEq(vault.MAX_MANDATE_DURATION(), 7 days);
        TierVault.TierConfig memory c = vault.tierConfig(2);
        assertEq(c.perTxCap, 2 * USDC);
        assertEq(c.windowCap, 10 * USDC);
        assertEq(c.newPayeeCap, USDC);
        assertEq(c.maxTxPerWindow, 20);
        assertEq(c.minCleanToPromote, 6);
    }

    function test_constructor_emits() public {
        TierVault.VaultInit memory init = _init(0);
        TierVault.TierConfig[4] memory c = _walletConfigs();
        vm.expectEmit(true, true, true, true);
        emit TierVault.VaultInitialized(owner, agent, autonomy, idWallet, 0);
        for (uint8 t = 0; t < 4; t++) {
            vm.expectEmit(true, false, false, true);
            emit TierVault.TierConfigSet(
                t,
                c[t].perTxCap,
                c[t].windowCap,
                c[t].newPayeeCap,
                c[t].maxTxPerWindow,
                c[t].payGradeMask,
                c[t].escrowGradeMask,
                c[t].minCleanToPromote
            );
        }
        new TierVault(init);
    }

    function test_constructor_zeroAddresses() public {
        for (uint256 i = 0; i < 6; i++) {
            TierVault.VaultInit memory init = _init(1);
            if (i == 0) init.token = address(0);
            if (i == 1) init.bureau = address(0);
            if (i == 2) init.identity = address(0);
            if (i == 3) init.owner = address(0);
            if (i == 4) init.agent = address(0);
            if (i == 5) init.autonomyOperator = address(0);
            vm.expectRevert(TierVault.ZeroAddress.selector);
            new TierVault(init);
        }
    }

    function test_constructor_rolesDistinct() public {
        for (uint256 i = 0; i < 3; i++) {
            TierVault.VaultInit memory init = _init(1);
            if (i == 0) init.agent = owner;
            if (i == 1) init.autonomyOperator = owner;
            if (i == 2) init.autonomyOperator = agent;
            vm.expectRevert(TierVault.RolesNotDistinct.selector);
            new TierVault(init);
        }
    }

    function test_constructor_tiersAndWindow() public {
        TierVault.VaultInit memory init = _init(2);
        vm.expectRevert(abi.encodeWithSelector(TierVault.BadTier.selector, 2));
        new TierVault(init);
        init = _init(1);
        init.maxAutoTier = 4;
        vm.expectRevert(abi.encodeWithSelector(TierVault.BadTier.selector, 4));
        new TierVault(init);
        init = _init(1);
        init.windowSeconds = 0;
        vm.expectRevert(TierVault.BadWindow.selector);
        new TierVault(init);
        init = _init(1);
        init.disputeWindow = 30 days + 1;
        vm.expectRevert(TierVault.BadWindow.selector);
        new TierVault(init);
        init = _init(1);
        init.configs[1].payGradeMask |= AcbTypes.MASK_D;
        vm.expectRevert(TierVault.MaskIncludesD.selector);
        new TierVault(init);
        init = _init(1);
        init.configs[0].perTxCap = 1;
        init.configs[0].windowCap = 1;
        vm.expectRevert(abi.encodeWithSelector(TierVault.BadTierConfig.selector, 0));
        new TierVault(init);
    }

    // ------------------------------------------------------------------ config validation

    function _cfgExpect(uint8 t, TierVault.TierConfig memory c, bytes memory err) internal {
        vm.prank(owner);
        vm.expectRevert(err);
        vault.setTierConfig(t, c);
    }

    function test_setTierConfig_validation() public {
        TierVault.TierConfig memory c = vault.tierConfig(1);
        TierVault.TierConfig memory bad = vault.tierConfig(1);
        bad.payGradeMask = AcbTypes.MASK_A | AcbTypes.MASK_D;
        _cfgExpect(1, bad, _sel(TierVault.MaskIncludesD.selector));
        bad = vault.tierConfig(1);
        bad.escrowGradeMask = AcbTypes.MASK_D;
        _cfgExpect(1, bad, _sel(TierVault.MaskIncludesD.selector));
        bad = vault.tierConfig(1);
        bad.maxTxPerWindow = 33;
        _cfgExpect(1, bad, abi.encodeWithSelector(TierVault.BadTierConfig.selector, 1));
        bad = vault.tierConfig(1);
        bad.perTxCap = bad.windowCap + 1;
        _cfgExpect(1, bad, abi.encodeWithSelector(TierVault.BadTierConfig.selector, 1));
        bad = vault.tierConfig(1);
        bad.newPayeeCap = bad.perTxCap + 1;
        _cfgExpect(1, bad, abi.encodeWithSelector(TierVault.BadTierConfig.selector, 1));
        bad = vault.tierConfig(1);
        bad.payGradeMask = 0x40;
        _cfgExpect(1, bad, abi.encodeWithSelector(TierVault.BadTierConfig.selector, 1));
        _cfgExpect(4, c, abi.encodeWithSelector(TierVault.BadTier.selector, 4));
    }

    function test_setTierConfig_tier0MustBeInert() public {
        TierVault.TierConfig memory z;
        TierVault.TierConfig memory bad = vault.tierConfig(0);
        bad.perTxCap = 1;
        bad.windowCap = 1;
        _cfgExpect(0, bad, abi.encodeWithSelector(TierVault.BadTierConfig.selector, 0));
        bad = vault.tierConfig(0);
        bad.payGradeMask = AcbTypes.MASK_A;
        _cfgExpect(0, bad, abi.encodeWithSelector(TierVault.BadTierConfig.selector, 0));
        bad = vault.tierConfig(0);
        bad.escrowGradeMask = AcbTypes.MASK_A;
        _cfgExpect(0, bad, abi.encodeWithSelector(TierVault.BadTierConfig.selector, 0));
        // promotion settings may change (perTx 0 keeps the tier inert)
        z = vault.tierConfig(0);
        z.minCleanToPromote = 5;
        vm.prank(owner);
        vault.setTierConfig(0, z);
        assertEq(vault.tierConfig(0).minCleanToPromote, 5);
    }

    function test_setTierConfig_appliesAndEmits() public {
        TierVault.TierConfig memory c = vault.tierConfig(1);
        c.perTxCap = USDC / 2;
        c.newPayeeCap = USDC / 4;
        vm.expectEmit(true, false, false, true, address(vault));
        emit TierVault.TierConfigSet(
            1,
            c.perTxCap,
            c.windowCap,
            c.newPayeeCap,
            c.maxTxPerWindow,
            c.payGradeMask,
            c.escrowGradeMask,
            c.minCleanToPromote
        );
        vm.prank(owner);
        vault.setTierConfig(1, c);
        bytes32 oid = _oid();
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(TierVault.PerTxCapExceeded.selector, USDC, USDC / 2));
        vault.pay(_out(oid, idA, USDC, M1, AcbTypes.DECISION_PAY));
    }

    // ------------------------------------------------------------------ role checks for every admin function

    function test_adminFunctions_onlyOwner() public {
        address[4] memory notOwners = [agent, autonomy, outsider, walA];
        TierVault.TierConfig memory c = vault.tierConfig(1);
        for (uint256 i = 0; i < notOwners.length; i++) {
            vm.startPrank(notOwners[i]);
            vm.expectRevert(TierVault.NotOwner.selector);
            vault.setTierConfig(1, c);
            vm.expectRevert(TierVault.NotOwner.selector);
            vault.setReserveFloor(0);
            vm.expectRevert(TierVault.NotOwner.selector);
            vault.setMinCleanAmount(0);
            vm.expectRevert(TierVault.NotOwner.selector);
            vault.setMaxAutoTier(3);
            vm.expectRevert(TierVault.NotOwner.selector);
            vault.setPromotionCooldown(0);
            vm.expectRevert(TierVault.NotOwner.selector);
            vault.setMaxGradeAge(0);
            vm.expectRevert(TierVault.NotOwner.selector);
            vault.setWindowSeconds(1);
            vm.expectRevert(TierVault.NotOwner.selector);
            vault.setDisputeWindow(0);
            vm.expectRevert(TierVault.NotOwner.selector);
            vault.setAgent(makeAddr("x"));
            vm.expectRevert(TierVault.NotOwner.selector);
            vault.setAutonomyOperator(makeAddr("y"));
            vm.expectRevert(TierVault.NotOwner.selector);
            vault.withdraw(notOwners[i], 1);
            vm.expectRevert(TierVault.NotOwner.selector);
            vault.transferOwnership(notOwners[i]);
            vm.expectRevert(TierVault.NotOwner.selector);
            vault.unfreeze(1, 3300, address(0));
            vm.expectRevert(TierVault.NotOwner.selector);
            vault.ownerApprove(bytes32(uint256(1)), idA, walA, 1, uint64(vm.getBlockTimestamp() + 1), false, 0);
            vm.expectRevert(TierVault.NotOwner.selector);
            vault.revokeApproval(bytes32(uint256(1)));
            vm.expectRevert(TierVault.NotOwner.selector);
            vault.revokeMandate(M1);
            vm.stopPrank();
        }
    }

    function test_setters_emitConfigChanged() public {
        vm.startPrank(owner);
        vm.expectEmit(true, false, false, true, address(vault));
        emit TierVault.ConfigChanged("reserveFloor", 5 * USDC);
        vault.setReserveFloor(5 * USDC);
        assertEq(vault.reserveFloor(), 5 * USDC);
        vm.expectEmit(true, false, false, true, address(vault));
        emit TierVault.ConfigChanged("minCleanAmount", 7);
        vault.setMinCleanAmount(7);
        assertEq(vault.minCleanAmount(), 7);
        vm.expectEmit(true, false, false, true, address(vault));
        emit TierVault.ConfigChanged("promotionCooldown", 86_400);
        vault.setPromotionCooldown(86_400);
        assertEq(vault.promotionCooldown(), 86_400);
        vm.expectEmit(true, false, false, true, address(vault));
        emit TierVault.ConfigChanged("maxGradeAge", 1 days);
        vault.setMaxGradeAge(1 days);
        assertEq(vault.maxGradeAge(), 1 days);
        vm.expectEmit(true, false, false, true, address(vault));
        emit TierVault.ConfigChanged("windowSeconds", 3600);
        vault.setWindowSeconds(3600);
        assertEq(vault.windowSeconds(), 3600);
        vm.expectRevert(TierVault.BadWindow.selector);
        vault.setWindowSeconds(0);
        vm.expectEmit(true, false, false, true, address(vault));
        emit TierVault.ConfigChanged("disputeWindow", 3 days);
        vault.setDisputeWindow(3 days);
        assertEq(vault.disputeWindow(), 3 days);
        vm.expectRevert(TierVault.BadWindow.selector);
        vault.setDisputeWindow(30 days + 1);
        vault.setDisputeWindow(30 days);
        vm.expectEmit(true, false, false, true, address(vault));
        emit TierVault.ConfigChanged("maxAutoTier", 3);
        vault.setMaxAutoTier(3);
        vm.stopPrank();
    }

    // ------------------------------------------------------------------ withdraw

    function test_withdraw_freeFundsOnly() public {
        _escrow(idA, USDC, M1);
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(TierVault.InsufficientUnlocked.selector, 14 * USDC, 14 * USDC + 1));
        vault.withdraw(owner, 14 * USDC + 1);
        vm.prank(owner);
        vm.expectRevert(TierVault.ZeroAddress.selector);
        vault.withdraw(address(0), 1);
        vm.expectEmit(true, false, false, true, address(vault));
        emit TierVault.Withdrawn(outsider, 14 * USDC);
        vm.prank(owner);
        vault.withdraw(outsider, 14 * USDC);
        assertEq(usdc.balanceOf(outsider), 14 * USDC);
        assertEq(usdc.balanceOf(address(vault)), USDC);
        assertEq(vault.freeBalance(), 0);
    }

    function test_withdraw_worksWhileFrozen() public {
        vm.prank(agent);
        vault.freeze(3203, 0);
        vm.prank(owner);
        vault.withdraw(owner, 15 * USDC);
        assertEq(usdc.balanceOf(owner), 15 * USDC);
    }

    function test_withdraw_nonReentrant() public {
        ReentrantToken rt = new ReentrantToken();
        OwnerHook hook = new OwnerHook();
        TierVault.VaultInit memory init = _init(1);
        init.token = address(rt);
        init.owner = address(hook);
        TierVault v = new TierVault(init);
        hook.setVault(v);
        rt.mint(address(v), 10 * USDC);
        rt.setHook(address(hook));
        hook.withdraw(USDC);
        assertEq(hook.innerError(), TierVault.Reentrancy.selector);
        assertEq(rt.balanceOf(address(hook)), USDC);
    }

    function test_lockedGuardBlocksEveryTransferFunction() public {
        vm.store(address(vault), bytes32(uint256(58)), bytes32(uint256(2))); // _lock = 2 (locked)
        bytes32 oid = _oid();
        vm.prank(owner);
        vm.expectRevert(TierVault.Reentrancy.selector);
        vault.withdraw(owner, 1);
        vm.prank(agent);
        vm.expectRevert(TierVault.Reentrancy.selector);
        vault.pay(_out(oid, idA, USDC, M1, AcbTypes.DECISION_PAY));
        vm.prank(agent);
        vm.expectRevert(TierVault.Reentrancy.selector);
        vault.escrow(_out(oid, idA, USDC, M1, AcbTypes.DECISION_ESCROW));
        vm.prank(agent);
        vm.expectRevert(TierVault.Reentrancy.selector);
        vault.executeApproved(oid, 0, 0);
        vm.prank(owner);
        vm.expectRevert(TierVault.Reentrancy.selector);
        vault.release(oid);
        vm.prank(owner);
        vm.expectRevert(TierVault.Reentrancy.selector);
        vault.refund(oid, 2401);
        vm.prank(walA);
        vm.expectRevert(TierVault.Reentrancy.selector);
        vault.claimDelivered(oid, keccak256("d"));
    }

    // ------------------------------------------------------------------ key rotation

    function test_setAgent_rotation() public {
        address newAgent = makeAddr("newAgent");
        vm.prank(owner);
        vm.expectRevert(TierVault.ZeroAddress.selector);
        vault.setAgent(address(0));
        vm.prank(owner);
        vm.expectRevert(TierVault.RolesNotDistinct.selector);
        vault.setAgent(owner);
        vm.prank(owner);
        vm.expectRevert(TierVault.RolesNotDistinct.selector);
        vault.setAgent(autonomy);
        vm.expectEmit(true, true, false, false, address(vault));
        emit TierVault.AgentChanged(agent, newAgent);
        vm.prank(owner);
        vault.setAgent(newAgent);
        assertEq(vault.agent(), newAgent);
        vm.prank(agent);
        vm.expectRevert(TierVault.NotAuthorized.selector);
        vault.freeze(3203, 0);
    }

    function test_setAutonomyOperator() public {
        address op = makeAddr("op2");
        vm.prank(owner);
        vm.expectRevert(TierVault.ZeroAddress.selector);
        vault.setAutonomyOperator(address(0));
        vm.prank(owner);
        vm.expectRevert(TierVault.RolesNotDistinct.selector);
        vault.setAutonomyOperator(owner);
        vm.prank(owner);
        vm.expectRevert(TierVault.RolesNotDistinct.selector);
        vault.setAutonomyOperator(agent);
        vm.expectEmit(true, true, false, false, address(vault));
        emit TierVault.AutonomyOperatorChanged(autonomy, op);
        vm.prank(owner);
        vault.setAutonomyOperator(op);
        vm.prank(autonomy);
        vm.expectRevert(TierVault.NotAutonomy.selector);
        vault.setTier(0, 3100, 0);
        vm.prank(op);
        vault.setTier(0, 3100, 0);
    }

    // ------------------------------------------------------------------ two-step ownership

    function test_ownership_twoStep() public {
        address newOwner = makeAddr("newOwner");
        vm.prank(owner);
        vm.expectRevert(TierVault.ZeroAddress.selector);
        vault.transferOwnership(address(0));
        vm.expectEmit(true, true, false, false, address(vault));
        emit TierVault.OwnershipTransferStarted(owner, newOwner);
        vm.prank(owner);
        vault.transferOwnership(newOwner);
        assertEq(vault.pendingOwner(), newOwner);
        assertEq(vault.owner(), owner);
        vm.prank(outsider);
        vm.expectRevert(TierVault.NotPendingOwner.selector);
        vault.acceptOwnership();
        vm.expectEmit(true, true, false, false, address(vault));
        emit TierVault.OwnershipTransferred(owner, newOwner);
        vm.prank(newOwner);
        vault.acceptOwnership();
        assertEq(vault.owner(), newOwner);
        assertEq(vault.pendingOwner(), address(0));
        vm.prank(owner);
        vm.expectRevert(TierVault.NotOwner.selector);
        vault.setReserveFloor(0);
    }

    function test_ownership_cannotMergeRoles() public {
        vm.prank(owner);
        vault.transferOwnership(agent);
        vm.prank(agent);
        vm.expectRevert(TierVault.RolesNotDistinct.selector);
        vault.acceptOwnership();
        vm.prank(owner);
        vault.transferOwnership(autonomy);
        vm.prank(autonomy);
        vm.expectRevert(TierVault.RolesNotDistinct.selector);
        vault.acceptOwnership();
    }

    // ------------------------------------------------------------------ views

    function test_views() public {
        _escrow(idC, USDC, M1);
        _pay(idA, USDC / 2, M1);
        TierVault.VaultStatus memory s = vault.status();
        assertEq(s.tier, 1);
        assertFalse(s.frozen);
        assertEq(s.tierBeforeFreeze, 1);
        assertEq(s.maxAutoTier, 2);
        assertEq(s.balance, 15 * USDC - USDC / 2);
        assertEq(s.escrowLocked, USDC);
        assertEq(s.reserveFloor, 3 * USDC);
        assertEq(s.spent, USDC + USDC / 2);
        assertEq(s.count, 2);
        assertEq(s.clean, 0);
        assertEq(s.points, 0);
        assertEq(s.lastTierChangeAt, T0);
        assertEq(vault.freeBalance(), 15 * USDC - USDC / 2 - USDC - 3 * USDC);
        vm.prank(owner);
        vault.freeze(3202, 0);
        s = vault.status();
        assertTrue(s.frozen);
        assertEq(s.tier, 255);
        assertTrue(vault.isFrozen());
        vm.expectRevert(abi.encodeWithSelector(TierVault.BadTier.selector, 4));
        vault.tierConfig(4);
    }

    function test_noReceive_noFallback() public {
        vm.deal(outsider, 1 ether);
        vm.prank(outsider);
        (bool ok,) = address(vault).call{value: 1}("");
        assertFalse(ok);
        vm.prank(outsider);
        (ok,) = address(vault)
            .call(abi.encodeWithSignature("onERC721Received(address,address,uint256,bytes)", outsider, outsider, 1, ""));
        assertFalse(ok);
    }
}
