// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {CommonBase} from "forge-std/Base.sol";
import {StdCheats} from "forge-std/StdCheats.sol";
import {StdUtils} from "forge-std/StdUtils.sol";
import {TierVault} from "../../src/TierVault.sol";
import {AgentCreditBureau} from "../../src/AgentCreditBureau.sol";
import {MockUSDC} from "../../src/MockUSDC.sol";
import {IdentityRegistryLite} from "../../src/erc8004/IdentityRegistryLite.sol";
import {VaultGhost} from "./VaultGhost.sol";

/// @notice Common references and helpers of the TierVault invariant handlers. Every action runs under `track`,
///         which hands the state before the action to the shared VaultGhost for the generic checks.
/// @dev The current agent is always read from the vault (`vault.agent()`), since `unfreeze` may rotate it.
abstract contract VaultHandlerBase is CommonBase, StdCheats, StdUtils {
    struct Refs {
        TierVault vault;
        MockUSDC usdc;
        IdentityRegistryLite identity;
        AgentCreditBureau acb;
        VaultGhost ghost;
        address owner;
        address autonomy;
        address poster;
    }

    // action kinds for VaultGhost.afterAction
    uint8 internal constant K_OTHER = 0;
    uint8 internal constant K_SET_TIER = 1;
    uint8 internal constant K_UNFREEZE = 2;
    uint8 internal constant K_WITHDRAW = 3;
    uint8 internal constant K_RELEASE = 4;

    TierVault public vault;
    MockUSDC public usdc;
    IdentityRegistryLite public identity;
    AgentCreditBureau public acb;
    VaultGhost public ghost;
    address public owner;
    address public autonomy;
    address public poster;
    address public outsider = makeAddr("inv-outsider");

    uint256[] public payeeIds;
    address[] public payeeWallets; // original (identity owner) wallets

    constructor(Refs memory r, uint256[] memory ids, address[] memory wallets) {
        vault = r.vault;
        usdc = r.usdc;
        identity = r.identity;
        acb = r.acb;
        ghost = r.ghost;
        owner = r.owner;
        autonomy = r.autonomy;
        poster = r.poster;
        payeeIds = ids;
        payeeWallets = wallets;
    }

    modifier track(uint8 kind) {
        uint8 tierBefore = vault.tier();
        uint256 balBefore = usdc.balanceOf(address(vault));
        _;
        ghost.afterAction(kind, tierBefore, balBefore);
    }

    /// @dev Biased towards the A and B payees (indexes 0 and 1) so that more outflows succeed.
    function _pickPayee(uint256 seed) internal view returns (uint256 id, uint256 idx) {
        uint256 r = seed % 8;
        idx = r < 3 ? 0 : (r < 5 ? 1 : (seed >> 8) % payeeIds.length);
        id = payeeIds[idx];
    }

    function _grade(uint256 id) internal view returns (uint8) {
        return acb.effectiveGrade(id, vault.maxGradeAge());
    }
}
