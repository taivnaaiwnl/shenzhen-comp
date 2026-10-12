// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {VaultTestBase} from "./utils/VaultTestBase.sol";
import {AgentCreditBureau} from "../src/AgentCreditBureau.sol";
import {TierVault} from "../src/TierVault.sol";
import {MockUSDC} from "../src/MockUSDC.sol";
import {IdentityRegistryLite} from "../src/erc8004/IdentityRegistryLite.sol";
import {AcbTypes} from "../src/lib/AcbTypes.sol";

/// @notice Grade registry, upgrade delay, vault registry and appeals (spec 3.1, 3.6).
contract AgentCreditBureauTest is VaultTestBase {
    bytes32 internal constant EV = keccak256("evidence");
    bytes32 internal constant CFG = keccak256("config");

    function _reasons() internal pure returns (uint16[4] memory r) {
        r = [uint16(1006), 1007, 0, 0];
    }

    function _postAs(address who, uint256 id, uint8 grade, uint16 score, uint64 asOf) internal {
        vm.prank(who);
        acb.postGrade(id, grade, score, _reasons(), MODEL, CFG, EV, asOf, "ipfs://e");
    }

    function _postExpect(
        uint256 id,
        uint8 grade,
        uint16 score,
        bytes32 model,
        bytes32 ev,
        uint64 asOf,
        bytes memory err
    ) internal {
        vm.prank(poster);
        vm.expectRevert(err);
        acb.postGrade(id, grade, score, _reasons(), model, CFG, ev, asOf, "");
    }

    function _asOf() internal view returns (uint64) {
        return uint64(vm.getBlockNumber() - 1);
    }

    // ------------------------------------------------------------------ constructor and admin

    function test_constructor() public {
        assertEq(acb.admin(), admin);
        assertEq(address(acb.identity()), address(identity));
        assertEq(acb.token(), address(usdc));
        assertEq(acb.vaultCodeHash(), keccak256(type(TierVault).runtimeCode));
        assertEq(acb.upgradeDelay(), 0);
        vm.expectRevert(AgentCreditBureau.ZeroAddress.selector);
        new AgentCreditBureau(address(0), address(identity), address(usdc), bytes32(0), 0);
        vm.expectRevert(AgentCreditBureau.ZeroAddress.selector);
        new AgentCreditBureau(admin, address(0), address(usdc), bytes32(0), 0);
        vm.expectRevert(AgentCreditBureau.ZeroAddress.selector);
        new AgentCreditBureau(admin, address(identity), address(0), bytes32(0), 0);
        vm.expectRevert(AgentCreditBureau.BadDelay.selector);
        new AgentCreditBureau(admin, address(identity), address(usdc), bytes32(0), 7 days + 1);
    }

    function test_admin_twoStep() public {
        address next = makeAddr("nextAdmin");
        vm.prank(outsider);
        vm.expectRevert(AgentCreditBureau.NotAdmin.selector);
        acb.transferAdmin(next);
        vm.prank(admin);
        vm.expectRevert(AgentCreditBureau.ZeroAddress.selector);
        acb.transferAdmin(address(0));
        vm.expectEmit(true, true, false, false, address(acb));
        emit AgentCreditBureau.AdminTransferStarted(admin, next);
        vm.prank(admin);
        acb.transferAdmin(next);
        assertEq(acb.pendingAdmin(), next);
        vm.prank(outsider);
        vm.expectRevert(AgentCreditBureau.NotPendingAdmin.selector);
        acb.acceptAdmin();
        vm.expectEmit(true, true, false, false, address(acb));
        emit AgentCreditBureau.AdminTransferred(admin, next);
        vm.prank(next);
        acb.acceptAdmin();
        assertEq(acb.admin(), next);
        assertEq(acb.pendingAdmin(), address(0));
        vm.prank(admin);
        vm.expectRevert(AgentCreditBureau.NotAdmin.selector);
        acb.setPoster(outsider, true);
    }

    function test_adminSetters() public {
        vm.startPrank(outsider);
        vm.expectRevert(AgentCreditBureau.NotAdmin.selector);
        acb.setPoster(outsider, true);
        vm.expectRevert(AgentCreditBureau.NotAdmin.selector);
        acb.setVaultCodeHash(bytes32(0));
        vm.expectRevert(AgentCreditBureau.NotAdmin.selector);
        acb.setUpgradeDelay(1);
        vm.stopPrank();

        vm.startPrank(admin);
        vm.expectEmit(true, false, false, true, address(acb));
        emit AgentCreditBureau.PosterSet(outsider, true);
        acb.setPoster(outsider, true);
        assertTrue(acb.isPoster(outsider));
        acb.setPoster(outsider, false);
        assertFalse(acb.isPoster(outsider));
        vm.expectRevert(AgentCreditBureau.ZeroAddress.selector);
        acb.setPoster(address(0), true);
        vm.expectEmit(false, false, false, true, address(acb));
        emit AgentCreditBureau.VaultCodeHashSet(keccak256("x"));
        acb.setVaultCodeHash(keccak256("x"));
        assertEq(acb.vaultCodeHash(), keccak256("x"));
        vm.expectEmit(false, false, false, true, address(acb));
        emit AgentCreditBureau.UpgradeDelaySet(7 days);
        acb.setUpgradeDelay(7 days);
        vm.expectRevert(AgentCreditBureau.BadDelay.selector);
        acb.setUpgradeDelay(7 days + 1);
        vm.stopPrank();
    }

    // ------------------------------------------------------------------ postGrade error paths (in order)

    function test_post_notPoster() public {
        vm.prank(outsider);
        vm.expectRevert(AgentCreditBureau.NotPoster.selector);
        acb.postGrade(idA, 5, 700, _reasons(), MODEL, CFG, EV, _asOf(), "");
        // a revoked poster cannot post
        vm.prank(admin);
        acb.setPoster(poster, false);
        vm.prank(poster);
        vm.expectRevert(AgentCreditBureau.NotPoster.selector);
        acb.postGrade(idA, 5, 700, _reasons(), MODEL, CFG, EV, _asOf(), "");
    }

    function test_post_notPosterBeforeEverythingElse() public {
        vm.prank(outsider);
        vm.expectRevert(AgentCreditBureau.NotPoster.selector);
        acb.postGrade(idA, 0, 0, _reasons(), 0, 0, 0, type(uint64).max, "");
    }

    function test_post_badGrade() public {
        _postExpect(idA, 0, 0, 0, 0, type(uint64).max, abi.encodeWithSelector(AgentCreditBureau.BadGrade.selector, 0));
        _postExpect(idA, 6, 700, MODEL, EV, _asOf(), abi.encodeWithSelector(AgentCreditBureau.BadGrade.selector, 6));
    }

    function test_post_badScore() public {
        _postExpect(idA, 5, 299, 0, 0, 0, abi.encodeWithSelector(AgentCreditBureau.BadScore.selector, 299));
        _postExpect(idA, 5, 851, MODEL, EV, _asOf(), abi.encodeWithSelector(AgentCreditBureau.BadScore.selector, 851));
        _postExpect(idA, 5, 0, MODEL, EV, _asOf(), abi.encodeWithSelector(AgentCreditBureau.BadScore.selector, 0));
        _postAs(poster, idA, 5, 300, _asOf());
        _postAs(poster, idA, 5, 850, _asOf());
    }

    function test_post_zeroHashes() public {
        _postExpect(idA, 5, 700, MODEL, 0, type(uint64).max, _sel(AgentCreditBureau.ZeroHash.selector));
        _postExpect(idA, 5, 700, 0, EV, type(uint64).max, _sel(AgentCreditBureau.ZeroHash.selector));
    }

    function test_post_asOfInFuture() public {
        uint64 bn = uint64(vm.getBlockNumber());
        _postExpect(idA, 5, 700, MODEL, EV, bn, abi.encodeWithSelector(AgentCreditBureau.AsOfInFuture.selector, bn));
        _postExpect(
            idA, 5, 700, MODEL, EV, bn + 5, abi.encodeWithSelector(AgentCreditBureau.AsOfInFuture.selector, bn + 5)
        );
    }

    function test_post_staleAsOf() public {
        vm.roll(vm.getBlockNumber() + 10);
        uint64 asOf = _asOf();
        _postAs(poster, idA, 5, 700, asOf);
        _postExpect(
            idA,
            4,
            650,
            MODEL,
            EV,
            asOf - 1,
            abi.encodeWithSelector(AgentCreditBureau.StaleAsOf.selector, asOf - 1, asOf)
        );
        _postAs(poster, idA, 4, 650, asOf); // equal as-of block is allowed
    }

    function test_post_selfGrading_ownerApprovedOperator() public {
        // poster owns the agent
        vm.prank(poster);
        uint256 own = identity.register();
        _postExpect(
            own, 5, 700, MODEL, EV, _asOf(), abi.encodeWithSelector(AgentCreditBureau.SelfGrading.selector, own)
        );
        // poster approved for one token
        vm.prank(walB);
        identity.approve(poster, idB);
        _postExpect(
            idB, 5, 700, MODEL, EV, _asOf(), abi.encodeWithSelector(AgentCreditBureau.SelfGrading.selector, idB)
        );
        // poster is an operator of the owner
        vm.prank(walC);
        identity.setApprovalForAll(poster, true);
        _postExpect(
            idC, 5, 700, MODEL, EV, _asOf(), abi.encodeWithSelector(AgentCreditBureau.SelfGrading.selector, idC)
        );
    }

    function test_post_nonexistentAgentReverts() public {
        _postExpect(
            999,
            5,
            700,
            MODEL,
            EV,
            _asOf(),
            abi.encodeWithSelector(IdentityRegistryLite.ERC721NonexistentToken.selector, 999)
        );
    }

    // ------------------------------------------------------------------ record contents

    function test_post_updatesRecordAndEmits() public {
        uint256 id = idNone; // never graded
        assertEq(acb.getRecord(id).grade, 0);
        (uint8 g0, uint64 p0) = acb.gradeOf(id);
        assertEq(g0, 0);
        assertEq(p0, 0);
        vm.roll(vm.getBlockNumber() + 5);
        uint64 asOf = _asOf();
        vm.expectEmit(true, true, true, true, address(acb));
        emit AgentCreditBureau.GradePosted(id, 4, 0, 660, _reasons(), MODEL, CFG, EV, asOf, poster, "ipfs://e");
        _postAs(poster, id, 4, 660, asOf);
        uint64 firstAt = uint64(vm.getBlockTimestamp());
        AgentCreditBureau.GradeRecord memory r = acb.getRecord(id);
        assertEq(r.grade, 4);
        assertEq(r.prevGrade, 0);
        assertEq(r.score, 660);
        assertEq(r.postedAt, firstAt);
        assertEq(r.prevPostedAt, 0);
        assertEq(r.asOfBlock, asOf);
        assertEq(r.reasons, uint64(1006) | (uint64(1007) << 16));
        assertEq(r.poster, poster);
        assertEq(r.modelId, MODEL);
        assertEq(r.configHash, CFG);
        assertEq(r.evidenceHash, EV);
        uint16[4] memory rs = acb.reasonsOf(id);
        assertEq(rs[0], 1006);
        assertEq(rs[1], 1007);
        assertEq(rs[2], 0);
        assertEq(rs[3], 0);

        vm.warp(vm.getBlockTimestamp() + 100);
        vm.roll(vm.getBlockNumber() + 1);
        vm.expectEmit(true, true, true, true, address(acb));
        emit AgentCreditBureau.GradePosted(id, 2, 4, 400, _reasons(), MODEL, CFG, EV, _asOf(), poster, "ipfs://e");
        _postAs(poster, id, 2, 400, _asOf());
        r = acb.getRecord(id);
        assertEq(r.grade, 2);
        assertEq(r.prevGrade, 4);
        assertEq(r.prevPostedAt, firstAt);
        assertEq(r.postedAt, firstAt + 100);
        (uint8 g, uint64 p) = acb.gradeOf(id);
        assertEq(g, 2);
        assertEq(p, firstAt + 100);
    }

    // ------------------------------------------------------------------ effectiveGrade: staleness

    function test_effective_neverPosted() public view {
        assertEq(acb.effectiveGrade(idNone, 0), AcbTypes.GRADE_NONE);
        assertEq(acb.effectiveGrade(idNone, 1 days), AcbTypes.GRADE_NONE);
    }

    function test_effective_stalenessForEachGrade() public {
        uint8[5] memory grades =
            [AcbTypes.GRADE_A, AcbTypes.GRADE_B, AcbTypes.GRADE_NEW, AcbTypes.GRADE_C, AcbTypes.GRADE_D];
        uint8[5] memory stale =
            [AcbTypes.GRADE_NONE, AcbTypes.GRADE_NONE, AcbTypes.GRADE_NONE, AcbTypes.GRADE_C, AcbTypes.GRADE_D];
        uint256[5] memory ids;
        for (uint256 i = 0; i < 5; i++) {
            (ids[i],) = _registerPayee(string.concat("stale", vm.toString(i)));
            _postAs(poster, ids[i], grades[i], 600, _asOf());
        }
        uint256 postedAt = vm.getBlockTimestamp();
        vm.warp(postedAt + 1 days); // exactly at maxAge: still fresh
        for (uint256 i = 0; i < 5; i++) {
            assertEq(acb.effectiveGrade(ids[i], 1 days), grades[i], "fresh at maxAge");
            assertEq(acb.effectiveGrade(ids[i], 0), grades[i], "maxAge 0 never stale");
        }
        vm.warp(postedAt + 1 days + 1);
        for (uint256 i = 0; i < 5; i++) {
            assertEq(acb.effectiveGrade(ids[i], 1 days), stale[i], "stale");
            assertEq(acb.effectiveGrade(ids[i], 0), grades[i], "maxAge 0 never stale");
        }
    }

    // ------------------------------------------------------------------ effectiveGrade: upgrade delay

    function _acbWithDelay(uint64 delay) internal returns (AgentCreditBureau b) {
        b = new AgentCreditBureau(admin, address(identity), address(usdc), bytes32(0), delay);
        vm.prank(admin);
        b.setPoster(poster, true);
    }

    function _postOn(AgentCreditBureau b, uint256 id, uint8 grade) internal {
        vm.prank(poster);
        b.postGrade(id, grade, 600, _reasons(), MODEL, CFG, EV, _asOf(), "");
    }

    function test_upgradeDelay_upgradeHiddenUntilDelay() public {
        AgentCreditBureau b = _acbWithDelay(60);
        _postOn(b, idA, AcbTypes.GRADE_B);
        // first posting B (rank 3) is an upgrade over NONE (rank 2): hidden
        assertEq(b.effectiveGrade(idA, 0), AcbTypes.GRADE_NONE);
        vm.warp(vm.getBlockTimestamp() + 60);
        assertEq(b.effectiveGrade(idA, 0), AcbTypes.GRADE_B);
        _postOn(b, idA, AcbTypes.GRADE_A);
        uint256 at = vm.getBlockTimestamp();
        assertEq(b.effectiveGrade(idA, 0), AcbTypes.GRADE_B);
        vm.warp(at + 59);
        assertEq(b.effectiveGrade(idA, 0), AcbTypes.GRADE_B);
        vm.warp(at + 60);
        assertEq(b.effectiveGrade(idA, 0), AcbTypes.GRADE_A);
    }

    function test_upgradeDelay_downgradeImmediate() public {
        AgentCreditBureau b = _acbWithDelay(60);
        _postOn(b, idA, AcbTypes.GRADE_A);
        vm.warp(vm.getBlockTimestamp() + 60);
        assertEq(b.effectiveGrade(idA, 0), AcbTypes.GRADE_A);
        _postOn(b, idA, AcbTypes.GRADE_D);
        assertEq(b.effectiveGrade(idA, 0), AcbTypes.GRADE_D);
        // first posting D is immediate too
        _postOn(b, idB, AcbTypes.GRADE_D);
        assertEq(b.effectiveGrade(idB, 0), AcbTypes.GRADE_D);
        // NEW over NONE has the same rank: immediate
        _postOn(b, idC, AcbTypes.GRADE_NEW);
        assertEq(b.effectiveGrade(idC, 0), AcbTypes.GRADE_NEW);
        // D -> C is an upgrade: hidden
        _postOn(b, idB, AcbTypes.GRADE_C);
        assertEq(b.effectiveGrade(idB, 0), AcbTypes.GRADE_D);
    }

    function test_upgradeDelay_repostOfPendingUpgradeStaysHidden() public {
        AgentCreditBureau b = _acbWithDelay(1 days);
        _postOn(b, idD, AcbTypes.GRADE_D);
        uint64 dAt = uint64(vm.getBlockTimestamp());
        vm.warp(dAt + 10);
        vm.roll(vm.getBlockNumber() + 1);
        _postOn(b, idD, AcbTypes.GRADE_A);
        assertEq(b.effectiveGrade(idD, 0), AcbTypes.GRADE_D, "first upgrade hidden");
        // D -> A -> A within the delay: the repost keeps D as the baseline and restarts the delay
        vm.warp(dAt + 20);
        vm.roll(vm.getBlockNumber() + 1);
        _postOn(b, idD, AcbTypes.GRADE_A);
        uint256 at = vm.getBlockTimestamp();
        AgentCreditBureau.GradeRecord memory r = b.getRecord(idD);
        assertEq(r.prevGrade, AcbTypes.GRADE_D, "baseline carried forward");
        assertEq(r.prevPostedAt, dAt, "baseline postedAt carried forward");
        assertEq(b.effectiveGrade(idD, 0), AcbTypes.GRADE_D, "repost does not reveal the upgrade");
        vm.warp(at + 1 days - 1);
        assertEq(b.effectiveGrade(idD, 0), AcbTypes.GRADE_D);
        vm.warp(at + 1 days);
        assertEq(b.effectiveGrade(idD, 0), AcbTypes.GRADE_A);
    }

    function test_upgradeDelay_lowerRepostOfPendingUpgradeStaysHidden() public {
        AgentCreditBureau b = _acbWithDelay(1 days);
        _postOn(b, idD, AcbTypes.GRADE_D);
        vm.roll(vm.getBlockNumber() + 1);
        _postOn(b, idD, AcbTypes.GRADE_A);
        // D -> A -> B within the delay: B ranks below A but above the D in effect, so it is an upgrade
        vm.warp(vm.getBlockTimestamp() + 10);
        vm.roll(vm.getBlockNumber() + 1);
        _postOn(b, idD, AcbTypes.GRADE_B);
        uint256 at = vm.getBlockTimestamp();
        assertEq(b.getRecord(idD).prevGrade, AcbTypes.GRADE_D);
        assertEq(b.effectiveGrade(idD, 0), AcbTypes.GRADE_D, "B hidden");
        vm.warp(at + 1 days);
        assertEq(b.effectiveGrade(idD, 0), AcbTypes.GRADE_B);
        // D -> A -> D within the delay: a downgrade relative to the grade in effect is immediate
        vm.roll(vm.getBlockNumber() + 1);
        _postOn(b, idD, AcbTypes.GRADE_D);
        vm.roll(vm.getBlockNumber() + 1);
        _postOn(b, idD, AcbTypes.GRADE_A);
        vm.roll(vm.getBlockNumber() + 1);
        _postOn(b, idD, AcbTypes.GRADE_D);
        assertEq(b.effectiveGrade(idD, 0), AcbTypes.GRADE_D);
        assertEq(b.getRecord(idD).prevGrade, AcbTypes.GRADE_D);
    }

    function test_upgradeDelay_effectiveUpgradeBecomesBaseline() public {
        AgentCreditBureau b = _acbWithDelay(60);
        _postOn(b, idD, AcbTypes.GRADE_D);
        _postOn(b, idD, AcbTypes.GRADE_B);
        vm.warp(vm.getBlockTimestamp() + 60);
        uint64 bAt = b.getRecord(idD).postedAt;
        // B is in effect now, so a later posting uses B (and its postedAt) as the baseline
        _postOn(b, idD, AcbTypes.GRADE_A);
        AgentCreditBureau.GradeRecord memory r = b.getRecord(idD);
        assertEq(r.prevGrade, AcbTypes.GRADE_B);
        assertEq(r.prevPostedAt, bAt);
        assertEq(b.effectiveGrade(idD, 0), AcbTypes.GRADE_B);
    }

    function test_upgradeDelay_zeroBehavesAsNoDelay() public {
        AgentCreditBureau b = _acbWithDelay(0);
        _postOn(b, idA, AcbTypes.GRADE_D);
        _postOn(b, idA, AcbTypes.GRADE_A);
        assertEq(b.effectiveGrade(idA, 0), AcbTypes.GRADE_A);
    }

    function test_upgradeDelay_previousRecordStaleness() public {
        AgentCreditBureau b = _acbWithDelay(1 days);
        _postOn(b, idA, AcbTypes.GRADE_B);
        uint256 first = vm.getBlockTimestamp();
        vm.warp(first + 2 days);
        _postOn(b, idA, AcbTypes.GRADE_A);
        // during the delay the previous B is evaluated with its own (old) postedAt
        assertEq(b.effectiveGrade(idA, 0), AcbTypes.GRADE_B);
        assertEq(b.effectiveGrade(idA, 1 days), AcbTypes.GRADE_NONE);
        vm.warp(first + 3 days);
        assertEq(b.effectiveGrade(idA, 1 days), AcbTypes.GRADE_A);
    }

    // ------------------------------------------------------------------ registerVault

    function test_registerVault_happyPath() public {
        vm.expectEmit(true, true, true, false, address(acb));
        emit AgentCreditBureau.VaultRegistered(address(vault), idWallet, owner);
        vm.prank(owner);
        acb.registerVault(address(vault));
        assertTrue(acb.isVault(address(vault)));
        assertEq(acb.vaultAgentId(address(vault)), idWallet);
        vm.prank(owner);
        vm.expectRevert(AgentCreditBureau.VaultExists.selector);
        acb.registerVault(address(vault));
    }

    function test_registerVault_approvedOperatorMayRegister() public {
        address op = makeAddr("operator");
        vm.prank(owner);
        identity.setApprovalForAll(op, true);
        vm.prank(op);
        acb.registerVault(address(vault));
        assertTrue(acb.isVault(address(vault)));
    }

    function test_registerVault_wrongCodehash() public {
        MockUSDC notAVault = new MockUSDC(address(this));
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(AgentCreditBureau.BadVaultCode.selector, address(notAVault).codehash));
        acb.registerVault(address(notAVault));
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(AgentCreditBureau.BadVaultCode.selector, bytes32(0)));
        acb.registerVault(makeAddr("emptyAccount"));
    }

    function test_registerVault_fakeTokenBureauIdentity() public {
        for (uint256 i = 0; i < 3; i++) {
            TierVault.VaultInit memory init = _init(1);
            if (i == 0) init.token = address(new MockUSDC(address(this)));
            if (i == 1) init.bureau = address(_acbWithDelay(0));
            if (i == 2) init.identity = address(new IdentityRegistryLite());
            TierVault fake = new TierVault(init);
            assertEq(address(fake).codehash, address(vault).codehash, "same code hash");
            vm.prank(owner);
            vm.expectRevert(AgentCreditBureau.BadVaultConfig.selector);
            acb.registerVault(address(fake));
        }
    }

    function test_registerVault_notAgentOwner() public {
        vm.prank(outsider);
        vm.expectRevert(abi.encodeWithSelector(AgentCreditBureau.NotAgentOwner.selector, idWallet));
        acb.registerVault(address(vault));
        vm.prank(agent); // the vault's agent key does not own the identity
        vm.expectRevert(abi.encodeWithSelector(AgentCreditBureau.NotAgentOwner.selector, idWallet));
        acb.registerVault(address(vault));
    }

    function test_registerVault_nonexistentAgent() public {
        TierVault.VaultInit memory init = _init(1);
        init.agentId = 4242;
        TierVault v = new TierVault(init);
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(IdentityRegistryLite.ERC721NonexistentToken.selector, 4242));
        acb.registerVault(address(v));
    }

    // ------------------------------------------------------------------ appeals

    function test_appeals() public {
        vm.prank(outsider);
        vm.expectRevert(abi.encodeWithSelector(AgentCreditBureau.NotAgentOwner.selector, idA));
        acb.fileAppeal(idA, keccak256("a"), "ipfs://appeal");
        vm.expectEmit(true, true, true, true, address(acb));
        emit AgentCreditBureau.AppealFiled(idA, 1, walA, keccak256("a"), "ipfs://appeal");
        vm.prank(walA);
        uint256 appealId = acb.fileAppeal(idA, keccak256("a"), "ipfs://appeal");
        assertEq(appealId, 1);
        assertEq(acb.appealCount(), 1);
        assertEq(acb.appealAgent(1), idA);
        // agent 0 can appeal too (ids start at 0)
        vm.prank(owner);
        uint256 second = acb.fileAppeal(idWallet, keccak256("b"), "");
        assertEq(second, 2);
        assertEq(acb.appealAgent(2), idWallet);

        vm.prank(outsider);
        vm.expectRevert(AgentCreditBureau.NotPoster.selector);
        acb.resolveAppeal(1, true, keccak256("r"));
        vm.prank(poster);
        vm.expectRevert(abi.encodeWithSelector(AgentCreditBureau.UnknownAppeal.selector, 0));
        acb.resolveAppeal(0, true, keccak256("r"));
        vm.prank(poster);
        vm.expectRevert(abi.encodeWithSelector(AgentCreditBureau.UnknownAppeal.selector, 3));
        acb.resolveAppeal(3, true, keccak256("r"));
        vm.expectEmit(true, false, false, true, address(acb));
        emit AgentCreditBureau.AppealResolved(1, true, keccak256("r"));
        vm.prank(poster);
        acb.resolveAppeal(1, true, keccak256("r"));
    }
}
