// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

/// @notice The subset of the ERC-8004 Reputation Registry (v2.0.0) used off-chain by the bureau key.
interface IReputationRegistry {
    function giveFeedback(
        uint256 agentId,
        int128 value,
        uint8 valueDecimals,
        string calldata tag1,
        string calldata tag2,
        string calldata endpoint,
        string calldata feedbackURI,
        bytes32 feedbackHash
    ) external;
}
