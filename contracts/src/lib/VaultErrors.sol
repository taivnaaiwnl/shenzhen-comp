// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

/// @notice Spec 3.2 names both an event and an error `DeliveryClaimed`. Solidity does not allow two
///         declarations with one name in a contract, so the error is declared at file level here and imported
///         under an alias by TierVault. The ABI keeps the spec name and selector `DeliveryClaimed(bytes32)`.
error DeliveryClaimed(bytes32 orderId);
