// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

/// @notice Test-only minimal EIP-1967 proxy. Attack A60 uses it to simulate an implementation change of an
///         upgradeable registry (the official ERC-8004 registries are UUPS proxies, spec 0.4 item 9).
contract StubProxy {
    /// @dev bytes32(uint256(keccak256("eip1967.proxy.implementation")) - 1)
    bytes32 internal constant IMPLEMENTATION_SLOT = 0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc;
    /// @dev bytes32(uint256(keccak256("eip1967.proxy.admin")) - 1)
    bytes32 internal constant ADMIN_SLOT = 0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103;

    event Upgraded(address indexed implementation);

    error NotProxyAdmin();

    constructor(address implementation, address admin) {
        assembly ("memory-safe") {
            sstore(IMPLEMENTATION_SLOT, implementation)
            sstore(ADMIN_SLOT, admin)
        }
        emit Upgraded(implementation);
    }

    /// @notice Change the implementation (proxy admin only). Selector chosen not to clash with registries.
    function stubUpgradeTo(address implementation) external {
        address admin;
        assembly ("memory-safe") {
            admin := sload(ADMIN_SLOT)
        }
        if (msg.sender != admin) revert NotProxyAdmin();
        assembly ("memory-safe") {
            sstore(IMPLEMENTATION_SLOT, implementation)
        }
        emit Upgraded(implementation);
    }

    fallback() external payable {
        assembly {
            let impl := sload(IMPLEMENTATION_SLOT)
            calldatacopy(0, 0, calldatasize())
            let ok := delegatecall(gas(), impl, 0, calldatasize(), 0, 0)
            returndatacopy(0, 0, returndatasize())
            switch ok
            case 0 { revert(0, returndatasize()) }
            default { return(0, returndatasize()) }
        }
    }
}
