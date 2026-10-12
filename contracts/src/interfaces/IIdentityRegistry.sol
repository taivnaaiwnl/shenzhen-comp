// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

/// @notice The subset of the ERC-8004 Identity Registry (v2.0.0) used by our contracts (all `view`).
interface IIdentityRegistry {
    /// @dev May return address(0) (never set, unset, or cleared by an ERC-721 transfer).
    function getAgentWallet(uint256 agentId) external view returns (address);

    /// @dev Reverts for a nonexistent agent (ERC721NonexistentToken).
    function isAuthorizedOrOwner(address spender, uint256 agentId) external view returns (bool);

    function ownerOf(uint256 tokenId) external view returns (address);
}
