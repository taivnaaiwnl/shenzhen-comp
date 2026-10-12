// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {CommonBase} from "forge-std/Base.sol";
import {StdCheats} from "forge-std/StdCheats.sol";
import {StdUtils} from "forge-std/StdUtils.sol";
import {AgentCreditBureau} from "../../src/AgentCreditBureau.sol";
import {TierVault} from "../../src/TierVault.sol";
import {IdentityRegistryLite} from "../../src/erc8004/IdentityRegistryLite.sol";
import {AcbTypes} from "../../src/lib/AcbTypes.sol";

/// @notice Invariant handler for AgentCreditBureau (spec 3.1 ACB-I1..ACB-I5): posters, a non-poster, the admin,
///         agent owners, time, the upgrade delay and vault registrations with genuine and fake vaults.
contract BureauHandler is CommonBase, StdCheats, StdUtils {
    AgentCreditBureau public acb;
    IdentityRegistryLite public identity;
    address public admin;
    address[3] public callers; // poster1, poster2, nonPoster
    uint256[] public agentIds;
    address[] public agentOwners;
    address[] public vaults; // candidates for registerVault
    bytes32 public realCodeHash;

    mapping(uint256 => uint64) public ghostAsOf;
    mapping(address => bool) public validAtRegistration;
    /// @dev ACB-I5 ghost: after a posting that raises an agent's grade, the rank of the grade in effect just
    ///      before it must not be exceeded until that posting's postedAt + upgradeDelay.
    mapping(uint256 => bool) public ceilActive;
    mapping(uint256 => uint8) public ceilRank;
    mapping(uint256 => uint64) public ceilPostedAt;

    uint256 public broken;
    string public failure;
    mapping(string => uint256) public successes;

    constructor(
        AgentCreditBureau acb_,
        IdentityRegistryLite identity_,
        address admin_,
        address[3] memory callers_,
        uint256[] memory ids,
        address[] memory owners,
        address[] memory vaults_
    ) {
        acb = acb_;
        identity = identity_;
        admin = admin_;
        callers = callers_;
        agentIds = ids;
        agentOwners = owners;
        vaults = vaults_;
        realCodeHash = acb_.vaultCodeHash();
        for (uint256 i = 0; i < ids.length; i++) {
            ghostAsOf[ids[i]] = acb_.getRecord(ids[i]).asOfBlock;
        }
    }

    function agentCount() external view returns (uint256) {
        return agentIds.length;
    }

    function vaultCount() external view returns (uint256) {
        return vaults.length;
    }

    function _fail(uint256 inv, string memory why) internal {
        if (broken == 0) failure = why;
        broken |= (1 << inv);
    }

    function _hashRecords() internal view returns (bytes32 h) {
        for (uint256 i = 0; i < agentIds.length; i++) {
            h = keccak256(abi.encode(h, acb.getRecord(agentIds[i])));
        }
    }

    /// @dev ACB-I2 for every action other than a successful postGrade: no record changes.
    modifier noRecordChange() {
        bytes32 before = _hashRecords();
        _;
        if (_hashRecords() != before) _fail(2, "ACB-I2: a record changed outside postGrade");
    }

    // ------------------------------------------------------------------ actions

    function postGrade(uint256 callerSeed, uint256 agentSeed, uint8 gradeSeed, uint16 scoreSeed, uint8 asOfSeed)
        external
    {
        address caller = callers[callerSeed % 3];
        uint256 idx = agentSeed % agentIds.length;
        uint256 id = agentIds[idx];
        // mostly valid inputs, sometimes invalid ones
        uint8 grade = gradeSeed % 8 == 0 ? gradeSeed % 7 : uint8(bound(gradeSeed, 1, 5));
        uint16 score = scoreSeed % 8 == 0 ? uint16(bound(scoreSeed, 0, 1000)) : uint16(bound(scoreSeed, 300, 850));
        uint64 last = acb.getRecord(id).asOfBlock;
        uint64 bn = uint64(vm.getBlockNumber());
        uint64[4] memory choices = [bn - 1, last, last > 0 ? last - 1 : 0, bn];
        uint64 asOf = choices[asOfSeed % 4];
        bool wasPoster = acb.isPoster(caller);
        bytes32 othersBefore = _hashOthers(id);
        uint16[4] memory reasons = [uint16(1100), 0, 0, 0];
        uint8 effBefore = acb.effectiveGrade(id, 0);

        vm.prank(caller);
        acb.postGrade(id, grade, score, reasons, bytes32("acb-sc-1.0.0"), bytes32(uint256(7)), keccak256("e"), asOf, "");

        if (!wasPoster) _fail(2, "ACB-I2: record written by a non-poster");
        if (_hashOthers(id) != othersBefore) _fail(2, "ACB-I2: postGrade changed another agent's record");
        AgentCreditBureau.GradeRecord memory r = acb.getRecord(id);
        if (r.asOfBlock < ghostAsOf[id]) _fail(1, "ACB-I1: asOfBlock decreased");
        ghostAsOf[id] = r.asOfBlock;
        if (r.grade < 1 || r.grade > 5 || r.score < 300 || r.score > 850) _fail(3, "ACB-I3: bad record written");
        if (AcbTypes.rank(r.grade) > AcbTypes.rank(effBefore)) {
            ceilActive[id] = true;
            ceilRank[id] = AcbTypes.rank(effBefore);
            ceilPostedAt[id] = r.postedAt;
        } else {
            ceilActive[id] = false;
        }
        successes["postGrade"]++;
    }

    function setPoster(uint256 seed, bool allowed) external noRecordChange {
        vm.prank(admin);
        acb.setPoster(callers[seed % 3], allowed);
        successes["setPoster"]++;
    }

    function setUpgradeDelay(uint256 seed) external noRecordChange {
        vm.prank(admin);
        acb.setUpgradeDelay(uint64(bound(seed, 0, 2 hours)));
        successes["setUpgradeDelay"]++;
    }

    function setVaultCodeHash(bool real) external noRecordChange {
        vm.prank(admin);
        acb.setVaultCodeHash(real ? realCodeHash : keccak256("not a vault"));
        successes["setVaultCodeHash"]++;
    }

    function registerVault(uint256 vaultSeed, uint256 callerSeed) external noRecordChange {
        address v = vaults[vaultSeed % vaults.length];
        bool codeOk = v.codehash == acb.vaultCodeHash();
        bool configOk;
        uint256 id;
        if (v.code.length > 0 && codeOk) {
            TierVault tv = TierVault(v);
            configOk = address(tv.token()) == acb.token() && address(tv.bureau()) == address(acb)
                && address(tv.identity()) == address(acb.identity());
            id = tv.agentId();
        }
        address caller = callerSeed % 2 == 0 && id < agentOwners.length ? agentOwners[id] : callers[callerSeed % 3];

        vm.prank(caller);
        acb.registerVault(v);

        if (!codeOk || !configOk) _fail(4, "ACB-I4: vault registered with wrong code or configuration");
        validAtRegistration[v] = codeOk && configOk;
        successes["registerVault"]++;
    }

    function fileAppeal(uint256 agentSeed) external noRecordChange {
        uint256 idx = agentSeed % agentIds.length;
        vm.prank(agentOwners[idx]);
        acb.fileAppeal(agentIds[idx], keccak256("appeal"), "");
        successes["fileAppeal"]++;
    }

    function resolveAppeal(uint256 seed, bool upheld) external noRecordChange {
        uint256 count = acb.appealCount();
        uint256 appealId = count == 0 ? 1 : bound(seed, 1, count);
        vm.prank(callers[seed % 3]);
        acb.resolveAppeal(appealId, upheld, keccak256("r"));
        successes["resolveAppeal"]++;
    }

    function warp(uint256 dtSeed) external noRecordChange {
        vm.warp(vm.getBlockTimestamp() + bound(dtSeed, 1, 3 hours));
        vm.roll(vm.getBlockNumber() + 1 + dtSeed % 3);
    }

    function _hashOthers(uint256 skip) internal view returns (bytes32 h) {
        for (uint256 i = 0; i < agentIds.length; i++) {
            if (agentIds[i] == skip) continue;
            h = keccak256(abi.encode(h, acb.getRecord(agentIds[i])));
        }
    }
}
