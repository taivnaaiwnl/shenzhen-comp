// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {VaultTestBase} from "./utils/VaultTestBase.sol";
import {TierVault} from "../src/TierVault.sol";
import {AcbTypes} from "../src/lib/AcbTypes.sol";

/// @notice Every TierVault instance has the same runtime code (no immutables), so the bureau can bind the
///         vault registry to one EXTCODEHASH (spec 3.0, 3.1 registerVault, 3.6 Codehash.t.sol).
contract CodehashTest is VaultTestBase {
    function test_twoVaultsWithDifferentArgumentsHaveEqualCodehash() public {
        TierVault.VaultInit memory init = _init(0);
        init.owner = makeAddr("otherOwner");
        init.agent = makeAddr("otherAgent");
        init.autonomyOperator = makeAddr("otherAutonomy");
        init.agentId = 12345;
        init.reserveFloor = 0;
        init.minCleanAmount = 1;
        init.windowSeconds = 3600;
        init.promotionCooldown = 1;
        init.maxGradeAge = 0;
        init.disputeWindow = 0;
        init.maxAutoTier = 1;
        init.configs[1].payGradeMask = AcbTypes.MASK_A;
        init.token = makeAddr("anyToken");
        TierVault other = new TierVault(init);
        assertEq(address(other).codehash, address(vault).codehash);
        assertEq(address(vault).codehash, keccak256(type(TierVault).runtimeCode));
        assertEq(acb.vaultCodeHash(), address(other).codehash);
    }

    function test_runtimeCodeHasNoMetadataHash() public pure {
        // bytecode_hash = "none": the CBOR tail only carries the solc version (0xa1 0x64 "solc" ...).
        bytes memory code = type(TierVault).runtimeCode;
        uint256 n = code.length;
        uint256 cborLen = (uint256(uint8(code[n - 2])) << 8) | uint256(uint8(code[n - 1]));
        assertEq(cborLen, 10, "CBOR metadata must only hold the compiler version");
        assertEq(uint8(code[n - 2 - cborLen]), 0xa1, "one CBOR map entry");
    }
}
