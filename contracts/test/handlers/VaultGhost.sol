// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {CommonBase} from "forge-std/Base.sol";
import {TierVault} from "../../src/TierVault.sol";
import {MockUSDC} from "../../src/MockUSDC.sol";
import {AcbTypes} from "../../src/lib/AcbTypes.sol";

/// @notice Shared ghost state and checks for the TierVault invariant handlers (spec 3.2 TV-I1..TV-I11).
///         VaultHandler (agent outflows, escrow lifecycle, recording) and VaultAdminHandler (tiers, owner
///         administration, outside world) both report here, so one record covers every action. The handlers are
///         split in two only to keep each contract under the EIP-170 size limit.
/// @dev Not a fuzz target: the invariant test names its targets explicitly. Uses vm.getBlockTimestamp() because
///      with via_ir the optimizer may reuse a stale block.timestamp across vm.warp calls.
contract VaultGhost is CommonBase {
    // action kinds for the generic before/after checks
    uint8 internal constant K_SET_TIER = 1;
    uint8 internal constant K_UNFREEZE = 2;
    uint8 internal constant K_WITHDRAW = 3;
    uint8 internal constant K_RELEASE = 4;

    TierVault public immutable vault;
    MockUSDC public immutable usdc;

    bytes32[] public mandateIds;
    bytes32[] public orderIds;
    mapping(bytes32 => uint8) public ghostState;
    mapping(bytes32 => uint256) public ghostTransfers;

    // agent-initiated, non-approved outflows (pay and escrow) for TV-I3
    uint256[] internal _winTs;
    uint256[] internal _winAmt;

    /// @notice bit i set = invariant TV-I<i> was broken at some point; `failure` holds the first message.
    uint256 public broken;
    string public failure;
    mapping(string => uint256) public successes;
    uint256 internal _nonce;

    constructor(TierVault vault_, MockUSDC usdc_) {
        vault = vault_;
        usdc = usdc_;
    }

    // ------------------------------------------------------------------ bookkeeping

    function fail(uint256 inv, string memory why) public {
        if (broken == 0) failure = why;
        broken |= (1 << inv);
    }

    function success(string calldata name) external {
        successes[name]++;
    }

    function addMandate(bytes32 id) external {
        mandateIds.push(id);
    }

    function newId(string calldata tag) external returns (bytes32) {
        return keccak256(abi.encode(tag, ++_nonce));
    }

    function orderCount() external view returns (uint256) {
        return orderIds.length;
    }

    function mandateCount() external view returns (uint256) {
        return mandateIds.length;
    }

    // ------------------------------------------------------------------ picking

    /// @dev An order in `state` (searching from a random start), or any order if none is in that state.
    function pickOrderIn(uint256 seed, uint8 state) external view returns (bytes32) {
        uint256 n = orderIds.length;
        if (n == 0) return bytes32(0);
        uint256 start = seed % n;
        for (uint256 i = 0; i < n; i++) {
            bytes32 oid = orderIds[(start + i) % n];
            if (vault.getOrder(oid).state == state) return oid;
        }
        return orderIds[start];
    }

    /// @dev Mostly one of the three most recent mandates (older ones expire as time moves on).
    function pickMandate(uint256 seed) external view returns (bytes32) {
        uint256 n = mandateIds.length;
        if (seed % 5 == 0) return mandateIds[(seed >> 8) % n];
        uint256 k = n < 3 ? n : 3;
        return mandateIds[n - 1 - (seed >> 8) % k];
    }

    // ------------------------------------------------------------------ checks

    /// @dev Generic checks after every action (TV-I1, I4, I8, I9); `tierBefore`/`balBefore` taken before it.
    function afterAction(uint8 kind, uint8 tierBefore, uint256 balBefore) external {
        uint8 tierAfter = vault.tier();
        if (tierAfter > 3 && tierAfter != AcbTypes.TIER_FROZEN) fail(4, "TV-I4: tier outside {0,1,2,3,255}");
        if (tierBefore == AcbTypes.TIER_FROZEN && tierAfter != AcbTypes.TIER_FROZEN && kind != K_UNFREEZE) {
            fail(9, "TV-I9: left 255 without unfreeze");
        }
        if (
            tierBefore != AcbTypes.TIER_FROZEN && tierAfter != AcbTypes.TIER_FROZEN && tierAfter > tierBefore
                && kind != K_SET_TIER && kind != K_UNFREEZE
        ) fail(4, "TV-I4: tier increased outside setTier and unfreeze");
        uint256 balAfter = usdc.balanceOf(address(vault));
        if (tierBefore == AcbTypes.TIER_FROZEN && balAfter < balBefore && kind != K_WITHDRAW && kind != K_RELEASE) {
            fail(8, "TV-I8: outflow while frozen");
        }
        if (balAfter < vault.escrowLocked()) fail(1, "TV-I1: balance below escrowLocked");
    }

    function _windowSum() internal view returns (uint256 sum) {
        uint256 nowTs = vm.getBlockTimestamp();
        uint256 w = vault.windowSeconds();
        for (uint256 i = 0; i < _winTs.length; i++) {
            if (_winTs[i] + w > nowTs) sum += _winAmt[i];
        }
    }

    function _isRole(address a) internal view returns (bool) {
        return a == address(vault) || a == vault.owner() || a == vault.agent() || a == vault.autonomyOperator();
    }

    /// @dev Checks for every successful agent-initiated outflow (TV-I2, I3, I6, I8).
    function agentOutflow(
        uint8 tierAtTime,
        uint8 gradeAtTime,
        address recipient,
        uint128 windowCap,
        bool approved,
        uint128 amount
    ) external {
        if (usdc.balanceOf(address(vault)) < uint256(vault.escrowLocked()) + vault.reserveFloor()) {
            fail(2, "TV-I2: below reserve after agent outflow");
        }
        if (!approved) {
            _winTs.push(vm.getBlockTimestamp());
            _winAmt.push(amount);
            if (_windowSum() > windowCap) fail(3, "TV-I3: window cap exceeded");
        }
        if (gradeAtTime == AcbTypes.GRADE_D) fail(6, "TV-I6: agent outflow to grade D");
        if (_isRole(recipient)) fail(6, "TV-I6: agent outflow to a role or the vault");
        if (tierAtTime == AcbTypes.TIER_FROZEN) fail(8, "TV-I8: agent outflow while frozen");
    }

    /// @dev TV-I7: records an order state transition and checks it against the vault.
    function transition(bytes32 oid, uint8 from, uint8 to, bool transfer) external {
        if (ghostState[oid] != from) fail(7, "TV-I7: invalid order state transition");
        if (from == AcbTypes.STATE_NONE) orderIds.push(oid);
        ghostState[oid] = to;
        if (transfer) {
            ghostTransfers[oid]++;
            if (ghostTransfers[oid] > 1) fail(7, "TV-I7: order paid twice");
        }
        if (vault.getOrder(oid).state != to) fail(7, "TV-I7: vault state differs from ghost");
    }
}
