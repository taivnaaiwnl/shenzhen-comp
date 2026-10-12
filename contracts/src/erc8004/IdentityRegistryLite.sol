// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

// Derived from IdentityRegistryUpgradeable.sol of the official ERC-8004 reference contracts v2.0.0
// (https://github.com/erc-8004/erc-8004-contracts, MIT License). The external functions, events and revert
// strings below match the official contract; the OpenZeppelin upgradeable ERC-721, EIP-712 and ECDSA helpers
// are replaced by a minimal non-upgradeable implementation with the same behaviour (OpenZeppelin v5 custom
// errors included). For local Anvil runs only; Base Sepolia uses the official registries.

interface IERC721ReceiverLite {
    function onERC721Received(address operator, address from, uint256 tokenId, bytes calldata data)
        external
        returns (bytes4);
}

interface IERC1271Lite {
    function isValidSignature(bytes32 hash, bytes memory signature) external view returns (bytes4);
}

/// @title IdentityRegistryLite
/// @notice ERC-8004 Identity Registry ("AgentIdentity", "AGENT") with the official v2.0.0 interface.
contract IdentityRegistryLite {
    struct MetadataEntry {
        string metadataKey;
        bytes metadataValue;
    }

    // ---- ERC-8004 events (official signatures) ----
    event Registered(uint256 indexed agentId, string agentURI, address indexed owner);
    event MetadataSet(
        uint256 indexed agentId, string indexed indexedMetadataKey, string metadataKey, bytes metadataValue
    );
    event URIUpdated(uint256 indexed agentId, string newURI, address indexed updatedBy);

    // ---- ERC-721 / ERC-4906 events ----
    event Transfer(address indexed from, address indexed to, uint256 indexed tokenId);
    event Approval(address indexed owner, address indexed approved, uint256 indexed tokenId);
    event ApprovalForAll(address indexed owner, address indexed operator, bool approved);
    event MetadataUpdate(uint256 _tokenId);

    // ---- OpenZeppelin v5 ERC-721 errors (IERC6093) ----
    error ERC721InvalidOwner(address owner);
    error ERC721NonexistentToken(uint256 tokenId);
    error ERC721IncorrectOwner(address sender, uint256 tokenId, address owner);
    error ERC721InvalidSender(address sender);
    error ERC721InvalidReceiver(address receiver);
    error ERC721InsufficientApproval(address operator, uint256 tokenId);
    error ERC721InvalidApprover(address approver);
    error ERC721InvalidOperator(address operator);

    string public constant name = "AgentIdentity";
    string public constant symbol = "AGENT";

    bytes32 private constant AGENT_WALLET_SET_TYPEHASH =
        keccak256("AgentWalletSet(uint256 agentId,address newWallet,address owner,uint256 deadline)");
    bytes32 private constant EIP712_DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    bytes32 private constant EIP712_NAME_HASH = keccak256("ERC8004IdentityRegistry");
    bytes32 private constant EIP712_VERSION_HASH = keccak256("1");
    bytes4 private constant ERC1271_MAGICVALUE = 0x1626ba7e;
    uint256 private constant MAX_DEADLINE_DELAY = 5 minutes;
    bytes32 private constant RESERVED_AGENT_WALLET_KEY_HASH = keccak256("agentWallet");
    uint256 private constant SECP256K1_HALF_N = 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0;

    uint256 private _lastId;
    mapping(uint256 => mapping(string => bytes)) private _metadata;
    mapping(uint256 => address) private _owners;
    mapping(address => uint256) private _balances;
    mapping(uint256 => address) private _tokenApprovals;
    mapping(address => mapping(address => bool)) private _operatorApprovals;
    mapping(uint256 => string) private _tokenURIs;

    // ------------------------------------------------------------------ ERC-8004

    function register() external returns (uint256 agentId) {
        agentId = _lastId++;
        _metadata[agentId]["agentWallet"] = abi.encodePacked(msg.sender);
        _safeMint(msg.sender, agentId);
        emit Registered(agentId, "", msg.sender);
        emit MetadataSet(agentId, "agentWallet", "agentWallet", abi.encodePacked(msg.sender));
    }

    function register(string memory agentURI) external returns (uint256 agentId) {
        agentId = _lastId++;
        _metadata[agentId]["agentWallet"] = abi.encodePacked(msg.sender);
        _safeMint(msg.sender, agentId);
        _setTokenURI(agentId, agentURI);
        emit Registered(agentId, agentURI, msg.sender);
        emit MetadataSet(agentId, "agentWallet", "agentWallet", abi.encodePacked(msg.sender));
    }

    function register(string memory agentURI, MetadataEntry[] memory metadata) external returns (uint256 agentId) {
        agentId = _lastId++;
        _metadata[agentId]["agentWallet"] = abi.encodePacked(msg.sender);
        _safeMint(msg.sender, agentId);
        _setTokenURI(agentId, agentURI);
        emit Registered(agentId, agentURI, msg.sender);
        emit MetadataSet(agentId, "agentWallet", "agentWallet", abi.encodePacked(msg.sender));

        for (uint256 i; i < metadata.length; i++) {
            require(keccak256(bytes(metadata[i].metadataKey)) != RESERVED_AGENT_WALLET_KEY_HASH, "reserved key");
            _metadata[agentId][metadata[i].metadataKey] = metadata[i].metadataValue;
            emit MetadataSet(agentId, metadata[i].metadataKey, metadata[i].metadataKey, metadata[i].metadataValue);
        }
    }

    function getMetadata(uint256 agentId, string memory metadataKey) external view returns (bytes memory) {
        return _metadata[agentId][metadataKey];
    }

    function setMetadata(uint256 agentId, string memory metadataKey, bytes memory metadataValue) external {
        address agentOwner = _owners[agentId];
        require(
            msg.sender == agentOwner || isApprovedForAll(agentOwner, msg.sender) || msg.sender == getApproved(agentId),
            "Not authorized"
        );
        require(keccak256(bytes(metadataKey)) != RESERVED_AGENT_WALLET_KEY_HASH, "reserved key");
        _metadata[agentId][metadataKey] = metadataValue;
        emit MetadataSet(agentId, metadataKey, metadataKey, metadataValue);
    }

    function setAgentURI(uint256 agentId, string calldata newURI) external {
        address owner = ownerOf(agentId);
        require(
            msg.sender == owner || isApprovedForAll(owner, msg.sender) || msg.sender == getApproved(agentId),
            "Not authorized"
        );
        _setTokenURI(agentId, newURI);
        emit URIUpdated(agentId, newURI, msg.sender);
    }

    function getAgentWallet(uint256 agentId) external view returns (address) {
        bytes memory walletData = _metadata[agentId]["agentWallet"];
        return address(bytes20(walletData));
    }

    function setAgentWallet(uint256 agentId, address newWallet, uint256 deadline, bytes calldata signature) external {
        address owner = ownerOf(agentId);
        require(
            msg.sender == owner || isApprovedForAll(owner, msg.sender) || msg.sender == getApproved(agentId),
            "Not authorized"
        );
        require(newWallet != address(0), "bad wallet");
        require(block.timestamp <= deadline, "expired");
        require(deadline <= block.timestamp + MAX_DEADLINE_DELAY, "deadline too far");

        bytes32 structHash = keccak256(abi.encode(AGENT_WALLET_SET_TYPEHASH, agentId, newWallet, owner, deadline));
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", _domainSeparatorV4(), structHash));

        // Try ECDSA first (EOAs), then ERC-1271 (smart contract wallets), like the official contract.
        (address recovered, bool valid) = _tryRecover(digest, signature);
        if (!valid || recovered != newWallet) {
            (bool ok, bytes memory res) =
                newWallet.staticcall(abi.encodeCall(IERC1271Lite.isValidSignature, (digest, signature)));
            require(ok && res.length >= 32 && abi.decode(res, (bytes4)) == ERC1271_MAGICVALUE, "invalid wallet sig");
        }

        _metadata[agentId]["agentWallet"] = abi.encodePacked(newWallet);
        emit MetadataSet(agentId, "agentWallet", "agentWallet", abi.encodePacked(newWallet));
    }

    function unsetAgentWallet(uint256 agentId) external {
        address owner = ownerOf(agentId);
        require(
            msg.sender == owner || isApprovedForAll(owner, msg.sender) || msg.sender == getApproved(agentId),
            "Not authorized"
        );
        _metadata[agentId]["agentWallet"] = "";
        emit MetadataSet(agentId, "agentWallet", "agentWallet", "");
    }

    /// @notice Checks if spender is owner or approved for the agent. Reverts for a nonexistent agent.
    function isAuthorizedOrOwner(address spender, uint256 agentId) external view returns (bool) {
        address owner = ownerOf(agentId);
        return _isAuthorized(owner, spender, agentId);
    }

    function getVersion() external pure returns (string memory) {
        return "2.0.0-lite";
    }

    /// @notice ERC-5267 domain of the EIP-712 signatures (as exposed by the official contract).
    function eip712Domain()
        external
        view
        returns (
            bytes1 fields,
            string memory name_,
            string memory version,
            uint256 chainId,
            address verifyingContract,
            bytes32 salt,
            uint256[] memory extensions
        )
    {
        return (hex"0f", "ERC8004IdentityRegistry", "1", block.chainid, address(this), bytes32(0), new uint256[](0));
    }

    // ------------------------------------------------------------------ ERC-721

    function supportsInterface(bytes4 interfaceId) external pure returns (bool) {
        return interfaceId == 0x80ac58cd // ERC-721
            || interfaceId == 0x5b5e139f // ERC-721 Metadata
            || interfaceId == 0x01ffc9a7 // ERC-165
            || interfaceId == 0x49064906; // ERC-4906 (OpenZeppelin ERC721URIStorage)
    }

    function balanceOf(address owner) external view returns (uint256) {
        if (owner == address(0)) revert ERC721InvalidOwner(address(0));
        return _balances[owner];
    }

    function ownerOf(uint256 tokenId) public view returns (address) {
        return _requireOwned(tokenId);
    }

    function tokenURI(uint256 tokenId) external view returns (string memory) {
        _requireOwned(tokenId);
        return _tokenURIs[tokenId];
    }

    function approve(address to, uint256 tokenId) external {
        address owner = _requireOwned(tokenId);
        if (owner != msg.sender && !isApprovedForAll(owner, msg.sender)) revert ERC721InvalidApprover(msg.sender);
        _tokenApprovals[tokenId] = to;
        emit Approval(owner, to, tokenId);
    }

    function getApproved(uint256 tokenId) public view returns (address) {
        _requireOwned(tokenId);
        return _tokenApprovals[tokenId];
    }

    function setApprovalForAll(address operator, bool approved) external {
        if (operator == address(0)) revert ERC721InvalidOperator(operator);
        _operatorApprovals[msg.sender][operator] = approved;
        emit ApprovalForAll(msg.sender, operator, approved);
    }

    function isApprovedForAll(address owner, address operator) public view returns (bool) {
        return _operatorApprovals[owner][operator];
    }

    function transferFrom(address from, address to, uint256 tokenId) public {
        if (to == address(0)) revert ERC721InvalidReceiver(address(0));
        address previousOwner = _update(to, tokenId, msg.sender);
        if (previousOwner != from) revert ERC721IncorrectOwner(from, tokenId, previousOwner);
    }

    function safeTransferFrom(address from, address to, uint256 tokenId) external {
        safeTransferFrom(from, to, tokenId, "");
    }

    function safeTransferFrom(address from, address to, uint256 tokenId, bytes memory data) public {
        transferFrom(from, to, tokenId);
        _checkOnERC721Received(msg.sender, from, to, tokenId, data);
    }

    // ------------------------------------------------------------------ internals

    function _requireOwned(uint256 tokenId) internal view returns (address owner) {
        owner = _owners[tokenId];
        if (owner == address(0)) revert ERC721NonexistentToken(tokenId);
    }

    function _isAuthorized(address owner, address spender, uint256 tokenId) internal view returns (bool) {
        return spender != address(0)
            && (owner == spender || isApprovedForAll(owner, spender) || _tokenApprovals[tokenId] == spender);
    }

    /// @dev OpenZeppelin v5 `_update` with the official override: a transfer (not a mint) clears agentWallet.
    function _update(address to, uint256 tokenId, address auth) internal returns (address from) {
        from = _owners[tokenId];
        if (from != address(0) && to != address(0)) {
            _metadata[tokenId]["agentWallet"] = "";
            emit MetadataSet(tokenId, "agentWallet", "agentWallet", "");
        }
        if (auth != address(0) && !_isAuthorized(from, auth, tokenId)) {
            if (from == address(0)) revert ERC721NonexistentToken(tokenId);
            revert ERC721InsufficientApproval(auth, tokenId);
        }
        if (from != address(0)) {
            delete _tokenApprovals[tokenId];
            unchecked {
                _balances[from] -= 1;
            }
        }
        if (to != address(0)) {
            unchecked {
                _balances[to] += 1;
            }
        }
        _owners[tokenId] = to;
        emit Transfer(from, to, tokenId);
    }

    function _safeMint(address to, uint256 tokenId) internal {
        if (to == address(0)) revert ERC721InvalidReceiver(address(0));
        address previousOwner = _update(to, tokenId, address(0));
        if (previousOwner != address(0)) revert ERC721InvalidSender(address(0));
        _checkOnERC721Received(msg.sender, address(0), to, tokenId, "");
    }

    function _setTokenURI(uint256 tokenId, string memory uri) internal {
        _tokenURIs[tokenId] = uri;
        emit MetadataUpdate(tokenId);
    }

    function _checkOnERC721Received(address operator, address from, address to, uint256 tokenId, bytes memory data)
        internal
    {
        if (to.code.length == 0) return;
        try IERC721ReceiverLite(to).onERC721Received(operator, from, tokenId, data) returns (bytes4 retval) {
            if (retval != IERC721ReceiverLite.onERC721Received.selector) revert ERC721InvalidReceiver(to);
        } catch (bytes memory reason) {
            if (reason.length == 0) revert ERC721InvalidReceiver(to);
            assembly ("memory-safe") {
                revert(add(32, reason), mload(reason))
            }
        }
    }

    function _domainSeparatorV4() internal view returns (bytes32) {
        return keccak256(
            abi.encode(EIP712_DOMAIN_TYPEHASH, EIP712_NAME_HASH, EIP712_VERSION_HASH, block.chainid, address(this))
        );
    }

    /// @dev ECDSA recovery of a 65-byte signature with the low-s rule (OpenZeppelin `tryRecover`).
    function _tryRecover(bytes32 digest, bytes calldata signature) internal pure returns (address, bool) {
        if (signature.length != 65) return (address(0), false);
        bytes32 r = bytes32(signature[0:32]);
        bytes32 s = bytes32(signature[32:64]);
        uint8 v = uint8(signature[64]);
        if (uint256(s) > SECP256K1_HALF_N) return (address(0), false);
        address signer = ecrecover(digest, v, r, s);
        if (signer == address(0)) return (address(0), false);
        return (signer, true);
    }
}
