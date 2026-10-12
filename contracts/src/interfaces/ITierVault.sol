// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

/// @notice The configuration getters the AgentCreditBureau checks in `registerVault` (all `view`).
interface ITierVault {
    function agentId() external view returns (uint256);

    function token() external view returns (address);

    function bureau() external view returns (address);

    function identity() external view returns (address);
}
