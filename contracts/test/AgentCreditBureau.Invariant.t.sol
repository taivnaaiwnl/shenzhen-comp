// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {StdInvariant} from "forge-std/StdInvariant.sol";
import {VaultTestBase} from "./utils/VaultTestBase.sol";
import {AgentCreditBureau} from "../src/AgentCreditBureau.sol";
import {TierVault} from "../src/TierVault.sol";
import {MockUSDC} from "../src/MockUSDC.sol";
import {IdentityRegistryLite} from "../src/erc8004/IdentityRegistryLite.sol";
import {AcbTypes} from "../src/lib/AcbTypes.sol";
import {BureauHandler} from "./handlers/BureauHandler.sol";

/// @notice AgentCreditBureau invariants ACB-I1..ACB-I5 (spec 3.1).
contract AgentCreditBureauInvariantTest is StdInvariant, VaultTestBase {
    BureauHandler internal handler;
    address internal poster2 = makeAddr("poster2");
    address internal nonPoster = makeAddr("nonPoster");

    function setUp() public override {
        super.setUp();
        vm.prank(admin);
        acb.setPoster(poster2, true);
        vm.prank(admin);
        acb.setUpgradeDelay(60);

        // agents 0..7: the fixture's wallet (0) and payees (1..6), plus one owned by a poster (self-grading)
        vm.prank(poster);
        uint256 posterOwned = identity.register();
        uint256[] memory ids = new uint256[](8);
        address[] memory owners = new address[](8);
        (ids[0], ids[1], ids[2], ids[3], ids[4], ids[5], ids[6], ids[7]) =
        (idWallet, idA, idB, idC, idD, idNew, idNone, posterOwned);
        (owners[0], owners[1], owners[2], owners[3], owners[4], owners[5], owners[6], owners[7]) =
        (owner, walA, walB, walC, walD, walNew, walNone, poster);
        for (uint256 i = 0; i < 8; i++) {
            assertEq(ids[i], i, "agent ids are 0..7");
        }

        // vault candidates: genuine ones and fakes with the same code but foreign configuration
        address[] memory vaults = new address[](7);
        vaults[0] = address(vault);
        TierVault.VaultInit memory init = _init(1);
        init.agentId = idA;
        vaults[1] = address(new TierVault(init)); // genuine vault of agent A
        init = _init(1);
        init.token = address(new MockUSDC(address(this)));
        vaults[2] = address(new TierVault(init)); // fake token
        init = _init(1);
        init.bureau = address(new AgentCreditBureau(admin, address(identity), address(usdc), bytes32(0), 0));
        vaults[3] = address(new TierVault(init)); // fake bureau
        init = _init(1);
        init.identity = address(new IdentityRegistryLite());
        vaults[4] = address(new TierVault(init)); // fake identity
        vaults[5] = address(new MockUSDC(address(this))); // wrong code
        init = _init(1);
        init.agentId = 999;
        vaults[6] = address(new TierVault(init)); // nonexistent agent

        handler = new BureauHandler(acb, identity, admin, [poster, poster2, nonPoster], ids, owners, vaults);
        targetContract(address(handler));
    }

    function _noFlag(uint256 inv) internal view {
        assertEq((handler.broken() >> inv) & 1, 0, handler.failure());
    }

    /// ACB-I1: for every agentId, the record's asOfBlock never decreases.
    function invariant_ACB_I1_asOfMonotonic() public view {
        uint256 n = handler.agentCount();
        for (uint256 i = 0; i < n; i++) {
            uint256 id = handler.agentIds(i);
            assertEq(acb.getRecord(id).asOfBlock, handler.ghostAsOf(id), "asOfBlock differs from ghost");
        }
        _noFlag(1);
    }

    /// ACB-I2: a record changes only through postGrade by an address that is a poster at that time.
    function invariant_ACB_I2_onlyPostersWrite() public view {
        _noFlag(2);
    }

    /// ACB-I3: a written record always has a grade in NEW..A and a score in 300..850.
    function invariant_ACB_I3_recordRanges() public view {
        uint256 n = handler.agentCount();
        for (uint256 i = 0; i < n; i++) {
            AgentCreditBureau.GradeRecord memory r = acb.getRecord(handler.agentIds(i));
            if (r.grade == AcbTypes.GRADE_NONE && r.postedAt == 0) continue;
            assertGe(r.grade, AcbTypes.GRADE_NEW);
            assertLe(r.grade, AcbTypes.GRADE_A);
            assertGe(r.score, 300);
            assertLe(r.score, 850);
            assertTrue(r.evidenceHash != bytes32(0) && r.modelId != bytes32(0));
        }
        _noFlag(3);
    }

    /// ACB-I4: isVault[v] implies that at registration v had the vault code hash and this bureau's token,
    ///         bureau and identity.
    function invariant_ACB_I4_vaultRegistry() public view {
        uint256 n = handler.vaultCount();
        for (uint256 i = 0; i < n; i++) {
            address v = handler.vaults(i);
            if (acb.isVault(v)) assertTrue(handler.validAtRegistration(v), "invalid vault registered");
        }
        // the fakes can never be registered
        for (uint256 i = 2; i < n; i++) {
            assertFalse(acb.isVault(handler.vaults(i)), "fake vault registered");
        }
        _noFlag(4);
    }

    /// ACB-I5: before postedAt + upgradeDelay, effectiveGrade never ranks above the previous grade. The record's
    ///         prevGrade is the grade in effect at posting time; the handler's ghost also checks it against the
    ///         effective grade observed just before the last raising posting, so a repost cannot reveal an upgrade.
    function invariant_ACB_I5_upgradeDelay() public view {
        uint256 n = handler.agentCount();
        uint64[3] memory ages = [uint64(0), 1 hours, 7 days];
        uint256 delay = acb.upgradeDelay();
        for (uint256 i = 0; i < n; i++) {
            uint256 id = handler.agentIds(i);
            AgentCreditBureau.GradeRecord memory r = acb.getRecord(id);
            if (r.grade == AcbTypes.GRADE_NONE) continue;
            bool recordPending = vm.getBlockTimestamp() < uint256(r.postedAt) + delay;
            bool ghostPending =
                handler.ceilActive(id) && vm.getBlockTimestamp() < uint256(handler.ceilPostedAt(id)) + delay;
            for (uint256 k = 0; k < 3; k++) {
                uint8 rk = AcbTypes.rank(acb.effectiveGrade(id, ages[k]));
                if (recordPending) assertLe(rk, AcbTypes.rank(r.prevGrade), "upgrade visible before the delay");
                if (ghostPending) assertLe(rk, handler.ceilRank(id), "repost revealed a pending upgrade");
            }
        }
    }

    function afterInvariant() external view {
        assertEq(handler.broken(), 0, handler.failure());
    }
}
