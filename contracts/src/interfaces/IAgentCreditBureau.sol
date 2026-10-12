// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

/// @notice The reads a TierVault makes on the AgentCreditBureau (all `view`, compiled to STATICCALL).
interface IAgentCreditBureau {
    function effectiveGrade(uint256 agentId, uint64 maxAge) external view returns (uint8);

    function gradeOf(uint256 agentId) external view returns (uint8 grade, uint64 postedAt);
}
