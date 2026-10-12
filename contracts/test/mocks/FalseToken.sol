// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

/// @notice Test-only ERC-20 whose `transfer` returns false (or reverts, or returns nothing) without moving
///         funds, to prove TierVault's `_safeTransfer` reverts `TransferFailed()`.
contract FalseToken {
    enum Mode {
        ReturnFalse,
        Revert,
        ReturnNothing
    }

    mapping(address => uint256) public balanceOf;
    Mode public mode;

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
    }

    function setMode(Mode m) external {
        mode = m;
    }

    function transfer(address, uint256) external view returns (bool) {
        if (mode == Mode.Revert) revert("FalseToken: transfer reverted");
        if (mode == Mode.ReturnNothing) {
            assembly ("memory-safe") {
                return(0, 0)
            }
        }
        return false;
    }

    function transferFrom(address, address, uint256) external pure returns (bool) {
        return false;
    }
}
