// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

interface IERC721ReceiverGift {
    function onERC721Received(address operator, address from, uint256 tokenId, bytes calldata data)
        external
        returns (bytes4);
}

/// @notice Test-only NFT used to "gift" a token to a vault: `safeTransferFrom` to a contract without
///         `onERC721Received` reverts, `transferFrom` succeeds silently. Either way the vault tier must not move.
contract ERC721Gift {
    mapping(uint256 => address) public ownerOf;
    uint256 public nextId;

    event Transfer(address indexed from, address indexed to, uint256 indexed tokenId);

    error NotOwner();
    error InvalidReceiver(address receiver);

    function mint(address to) external returns (uint256 id) {
        id = nextId++;
        ownerOf[id] = to;
        emit Transfer(address(0), to, id);
    }

    function transferFrom(address from, address to, uint256 id) public {
        if (ownerOf[id] != from || msg.sender != from) revert NotOwner();
        ownerOf[id] = to;
        emit Transfer(from, to, id);
    }

    function safeTransferFrom(address from, address to, uint256 id) external {
        transferFrom(from, to, id);
        if (to.code.length > 0) {
            try IERC721ReceiverGift(to).onERC721Received(msg.sender, from, id, "") returns (bytes4 ret) {
                if (ret != IERC721ReceiverGift.onERC721Received.selector) revert InvalidReceiver(to);
            } catch {
                revert InvalidReceiver(to);
            }
        }
    }
}
