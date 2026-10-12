// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {Test} from "forge-std/Test.sol";
import {IdentityRegistryLite} from "../src/erc8004/IdentityRegistryLite.sol";
import {ReputationRegistryLite} from "../src/erc8004/ReputationRegistryLite.sol";
import {StubProxy} from "./mocks/StubProxy.sol";

/// @notice ERC-1271 smart wallet that accepts exactly one digest.
contract Wallet1271 {
    bytes32 public accepted;

    function accept(bytes32 digest) external {
        accepted = digest;
    }

    function isValidSignature(bytes32 hash, bytes memory) external view returns (bytes4) {
        return hash == accepted ? bytes4(0x1626ba7e) : bytes4(0xffffffff);
    }
}

/// @notice Selector and topic parity with the official ERC-8004 v2.0.0 signatures (spec 0.4), and the
///         official behaviours our code relies on (spec 3.4, 3.6 Erc8004Parity.t.sol).
contract Erc8004ParityTest is Test {
    IdentityRegistryLite internal id;
    ReputationRegistryLite internal rep;
    address internal alice = makeAddr("alice");
    address internal bob = makeAddr("bob");
    address internal carol = makeAddr("carol");

    function setUp() public {
        vm.warp(1_760_000_000);
        id = new IdentityRegistryLite();
        rep = new ReputationRegistryLite(address(id));
    }

    function _sig(string memory s) internal pure returns (bytes4) {
        return bytes4(keccak256(bytes(s)));
    }

    // ------------------------------------------------------------------ selectors and topics

    function test_identity_selectors() public view {
        assertEq(id.getMetadata.selector, _sig("getMetadata(uint256,string)"));
        assertEq(id.setMetadata.selector, _sig("setMetadata(uint256,string,bytes)"));
        assertEq(id.setAgentURI.selector, _sig("setAgentURI(uint256,string)"));
        assertEq(id.getAgentWallet.selector, _sig("getAgentWallet(uint256)"));
        assertEq(id.setAgentWallet.selector, _sig("setAgentWallet(uint256,address,uint256,bytes)"));
        assertEq(id.unsetAgentWallet.selector, _sig("unsetAgentWallet(uint256)"));
        assertEq(id.isAuthorizedOrOwner.selector, _sig("isAuthorizedOrOwner(address,uint256)"));
        assertEq(id.getVersion.selector, _sig("getVersion()"));
        assertEq(id.balanceOf.selector, _sig("balanceOf(address)"));
        assertEq(id.ownerOf.selector, _sig("ownerOf(uint256)"));
        assertEq(id.approve.selector, _sig("approve(address,uint256)"));
        assertEq(id.getApproved.selector, _sig("getApproved(uint256)"));
        assertEq(id.setApprovalForAll.selector, _sig("setApprovalForAll(address,bool)"));
        assertEq(id.isApprovedForAll.selector, _sig("isApprovedForAll(address,address)"));
        assertEq(id.transferFrom.selector, _sig("transferFrom(address,address,uint256)"));
        assertEq(id.tokenURI.selector, _sig("tokenURI(uint256)"));
        assertEq(id.supportsInterface.selector, _sig("supportsInterface(bytes4)"));
    }

    function test_identity_overloadedSelectorsExist() public {
        // no fallback: a call with an unknown selector would revert, so success proves the selector exists
        vm.prank(alice);
        (bool ok, bytes memory ret) = address(id).call(abi.encodeWithSignature("register()"));
        assertTrue(ok);
        assertEq(abi.decode(ret, (uint256)), 0);
        vm.prank(alice);
        (ok, ret) = address(id).call(abi.encodeWithSignature("register(string)", "ipfs://x"));
        assertTrue(ok);
        assertEq(abi.decode(ret, (uint256)), 1);
        IdentityRegistryLite.MetadataEntry[] memory md = new IdentityRegistryLite.MetadataEntry[](1);
        md[0] = IdentityRegistryLite.MetadataEntry("category", bytes("fx-data"));
        vm.prank(alice);
        (ok, ret) = address(id).call(abi.encodeWithSignature("register(string,(string,bytes)[])", "ipfs://y", md));
        assertTrue(ok);
        assertEq(abi.decode(ret, (uint256)), 2);
        vm.prank(alice);
        (ok,) = address(id).call(abi.encodeWithSignature("safeTransferFrom(address,address,uint256)", alice, bob, 0));
        assertTrue(ok);
        vm.prank(alice);
        (ok,) = address(id)
            .call(abi.encodeWithSignature("safeTransferFrom(address,address,uint256,bytes)", alice, bob, 1, ""));
        assertTrue(ok);
        (ok,) = address(id).call(abi.encodeWithSignature("notAFunction()"));
        assertFalse(ok);
    }

    function test_identity_eventTopics() public pure {
        assertEq(IdentityRegistryLite.Registered.selector, keccak256("Registered(uint256,string,address)"));
        assertEq(IdentityRegistryLite.MetadataSet.selector, keccak256("MetadataSet(uint256,string,string,bytes)"));
        assertEq(IdentityRegistryLite.URIUpdated.selector, keccak256("URIUpdated(uint256,string,address)"));
        assertEq(IdentityRegistryLite.Transfer.selector, keccak256("Transfer(address,address,uint256)"));
        assertEq(IdentityRegistryLite.Approval.selector, keccak256("Approval(address,address,uint256)"));
        assertEq(IdentityRegistryLite.ApprovalForAll.selector, keccak256("ApprovalForAll(address,address,bool)"));
    }

    function test_reputation_selectorsAndTopics() public view {
        assertEq(rep.getIdentityRegistry.selector, _sig("getIdentityRegistry()"));
        assertEq(
            rep.giveFeedback.selector, _sig("giveFeedback(uint256,int128,uint8,string,string,string,string,bytes32)")
        );
        assertEq(rep.revokeFeedback.selector, _sig("revokeFeedback(uint256,uint64)"));
        assertEq(rep.appendResponse.selector, _sig("appendResponse(uint256,address,uint64,string,bytes32)"));
        assertEq(rep.getLastIndex.selector, _sig("getLastIndex(uint256,address)"));
        assertEq(rep.readFeedback.selector, _sig("readFeedback(uint256,address,uint64)"));
        assertEq(rep.getSummary.selector, _sig("getSummary(uint256,address[],string,string)"));
        assertEq(rep.readAllFeedback.selector, _sig("readAllFeedback(uint256,address[],string,string,bool)"));
        assertEq(rep.getResponseCount.selector, _sig("getResponseCount(uint256,address,uint64,address[])"));
        assertEq(rep.getClients.selector, _sig("getClients(uint256)"));
        assertEq(rep.getVersion.selector, _sig("getVersion()"));
        assertEq(
            ReputationRegistryLite.NewFeedback.selector,
            keccak256("NewFeedback(uint256,address,uint64,int128,uint8,string,string,string,string,string,bytes32)")
        );
        assertEq(ReputationRegistryLite.FeedbackRevoked.selector, keccak256("FeedbackRevoked(uint256,address,uint64)"));
        assertEq(
            ReputationRegistryLite.ResponseAppended.selector,
            keccak256("ResponseAppended(uint256,address,uint64,address,string,bytes32)")
        );
    }

    // ------------------------------------------------------------------ identity behaviour

    function test_agentIdsStartAtZero_andWalletIsSender() public {
        vm.expectEmit(true, true, false, true, address(id));
        emit IdentityRegistryLite.Registered(0, "", alice);
        vm.expectEmit(true, true, false, true, address(id));
        emit IdentityRegistryLite.MetadataSet(0, "agentWallet", "agentWallet", abi.encodePacked(alice));
        vm.prank(alice);
        uint256 a = id.register();
        vm.prank(bob);
        uint256 b = id.register("ipfs://bob");
        assertEq(a, 0);
        assertEq(b, 1);
        assertEq(id.ownerOf(0), alice);
        assertEq(id.getAgentWallet(0), alice);
        assertEq(id.getAgentWallet(1), bob);
        assertEq(id.tokenURI(1), "ipfs://bob");
        assertEq(id.balanceOf(alice), 1);
        assertEq(id.getVersion(), "2.0.0-lite");
        assertEq(id.name(), "AgentIdentity");
        assertEq(id.symbol(), "AGENT");
        assertEq(id.getAgentWallet(77), address(0));
    }

    function test_register_withMetadata_reservedKey() public {
        IdentityRegistryLite.MetadataEntry[] memory md = new IdentityRegistryLite.MetadataEntry[](1);
        md[0] = IdentityRegistryLite.MetadataEntry("agentWallet", abi.encodePacked(bob));
        vm.prank(alice);
        vm.expectRevert(bytes("reserved key"));
        id.register("ipfs://a", md);
        md[0] = IdentityRegistryLite.MetadataEntry("category", bytes("fx"));
        vm.prank(alice);
        uint256 a = id.register("ipfs://a", md);
        assertEq(id.getMetadata(a, "category"), bytes("fx"));
    }

    function test_register_safeMintToContractWithoutReceiverReverts() public {
        Wallet1271 noReceiver = new Wallet1271(); // a contract without onERC721Received
        vm.prank(address(noReceiver));
        vm.expectRevert(
            abi.encodeWithSelector(IdentityRegistryLite.ERC721InvalidReceiver.selector, address(noReceiver))
        );
        id.register();
    }

    function test_setMetadata_and_setAgentURI() public {
        vm.prank(alice);
        uint256 a = id.register();
        vm.prank(bob);
        vm.expectRevert(bytes("Not authorized"));
        id.setMetadata(a, "k", "v");
        vm.prank(alice);
        vm.expectRevert(bytes("reserved key"));
        id.setMetadata(a, "agentWallet", abi.encodePacked(bob));
        vm.prank(alice);
        id.setMetadata(a, "k", "v");
        assertEq(id.getMetadata(a, "k"), bytes("v"));
        vm.prank(bob);
        vm.expectRevert(bytes("Not authorized"));
        id.setAgentURI(a, "x");
        vm.expectEmit(true, true, false, true, address(id));
        emit IdentityRegistryLite.URIUpdated(a, "ipfs://new", alice);
        vm.prank(alice);
        id.setAgentURI(a, "ipfs://new");
        assertEq(id.tokenURI(a), "ipfs://new");
        // a nonexistent agent: getApproved reverts like the official contract
        vm.prank(bob);
        vm.expectRevert(abi.encodeWithSelector(IdentityRegistryLite.ERC721NonexistentToken.selector, 99));
        id.setMetadata(99, "k", "v");
    }

    function test_transferClearsAgentWallet_andEmitsMetadataSet() public {
        vm.prank(alice);
        uint256 a = id.register();
        vm.expectEmit(true, true, false, true, address(id));
        emit IdentityRegistryLite.MetadataSet(a, "agentWallet", "agentWallet", "");
        vm.expectEmit(true, true, true, false, address(id));
        emit IdentityRegistryLite.Transfer(alice, bob, a);
        vm.prank(alice);
        id.transferFrom(alice, bob, a);
        assertEq(id.ownerOf(a), bob);
        assertEq(id.getAgentWallet(a), address(0));
        assertEq(id.getMetadata(a, "agentWallet").length, 0);
        // safeTransferFrom to an EOA clears it as well
        vm.prank(bob);
        id.safeTransferFrom(bob, carol, a);
        assertEq(id.getAgentWallet(a), address(0));
    }

    function test_isAuthorizedOrOwner() public {
        vm.prank(alice);
        uint256 a = id.register();
        assertTrue(id.isAuthorizedOrOwner(alice, a));
        assertFalse(id.isAuthorizedOrOwner(bob, a));
        assertFalse(id.isAuthorizedOrOwner(address(0), a));
        vm.prank(alice);
        id.approve(bob, a);
        assertTrue(id.isAuthorizedOrOwner(bob, a));
        vm.prank(alice);
        id.setApprovalForAll(carol, true);
        assertTrue(id.isAuthorizedOrOwner(carol, a));
        vm.expectRevert(abi.encodeWithSelector(IdentityRegistryLite.ERC721NonexistentToken.selector, 5));
        id.isAuthorizedOrOwner(alice, 5);
        vm.expectRevert(abi.encodeWithSelector(IdentityRegistryLite.ERC721NonexistentToken.selector, 5));
        id.ownerOf(5);
    }

    function test_erc721_errorsAndInterfaces() public {
        vm.prank(alice);
        uint256 a = id.register();
        vm.prank(bob);
        vm.expectRevert(abi.encodeWithSelector(IdentityRegistryLite.ERC721InsufficientApproval.selector, bob, a));
        id.transferFrom(alice, bob, a);
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(IdentityRegistryLite.ERC721IncorrectOwner.selector, bob, a, alice));
        id.transferFrom(bob, carol, a);
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(IdentityRegistryLite.ERC721InvalidReceiver.selector, address(0)));
        id.transferFrom(alice, address(0), a);
        vm.prank(bob);
        vm.expectRevert(abi.encodeWithSelector(IdentityRegistryLite.ERC721InvalidApprover.selector, bob));
        id.approve(carol, a);
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(IdentityRegistryLite.ERC721InvalidOperator.selector, address(0)));
        id.setApprovalForAll(address(0), true);
        vm.expectRevert(abi.encodeWithSelector(IdentityRegistryLite.ERC721InvalidOwner.selector, address(0)));
        id.balanceOf(address(0));
        assertTrue(id.supportsInterface(0x80ac58cd));
        assertTrue(id.supportsInterface(0x5b5e139f));
        assertTrue(id.supportsInterface(0x01ffc9a7));
        assertFalse(id.supportsInterface(0xffffffff));
    }

    function _walletDigest(uint256 agentId, address newWallet, address owner_, uint256 deadline)
        internal
        view
        returns (bytes32)
    {
        bytes32 structHash = keccak256(
            abi.encode(
                keccak256("AgentWalletSet(uint256 agentId,address newWallet,address owner,uint256 deadline)"),
                agentId,
                newWallet,
                owner_,
                deadline
            )
        );
        bytes32 domain = keccak256(
            abi.encode(
                keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
                keccak256("ERC8004IdentityRegistry"),
                keccak256("1"),
                block.chainid,
                address(id)
            )
        );
        return keccak256(abi.encodePacked("\x19\x01", domain, structHash));
    }

    function test_setAgentWallet_eoaSignature() public {
        vm.prank(alice);
        uint256 a = id.register();
        (address w, uint256 key) = makeAddrAndKey("newWallet");
        uint256 deadline = vm.getBlockTimestamp() + 300;
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(key, _walletDigest(a, w, alice, deadline));
        bytes memory sig = abi.encodePacked(r, s, v);

        vm.prank(bob);
        vm.expectRevert(bytes("Not authorized"));
        id.setAgentWallet(a, w, deadline, sig);
        vm.prank(alice);
        vm.expectRevert(bytes("bad wallet"));
        id.setAgentWallet(a, address(0), deadline, sig);
        vm.prank(alice);
        vm.expectRevert(bytes("expired"));
        id.setAgentWallet(a, w, vm.getBlockTimestamp() - 1, sig);
        vm.prank(alice);
        vm.expectRevert(bytes("deadline too far"));
        id.setAgentWallet(a, w, vm.getBlockTimestamp() + 301, sig);
        vm.prank(alice);
        vm.expectRevert(bytes("invalid wallet sig"));
        id.setAgentWallet(a, bob, deadline, sig); // signed for w, not bob
        // high-s malleated signature is rejected
        bytes32 highS =
            bytes32(uint256(0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141) - uint256(s));
        vm.prank(alice);
        vm.expectRevert(bytes("invalid wallet sig"));
        id.setAgentWallet(a, w, deadline, abi.encodePacked(r, highS, v == 27 ? uint8(28) : uint8(27)));

        vm.expectEmit(true, true, false, true, address(id));
        emit IdentityRegistryLite.MetadataSet(a, "agentWallet", "agentWallet", abi.encodePacked(w));
        vm.prank(alice);
        id.setAgentWallet(a, w, deadline, sig);
        assertEq(id.getAgentWallet(a), w);

        vm.expectEmit(true, true, false, true, address(id));
        emit IdentityRegistryLite.MetadataSet(a, "agentWallet", "agentWallet", "");
        vm.prank(alice);
        id.unsetAgentWallet(a);
        assertEq(id.getAgentWallet(a), address(0));
        vm.prank(bob);
        vm.expectRevert(bytes("Not authorized"));
        id.unsetAgentWallet(a);
    }

    function test_setAgentWallet_erc1271() public {
        vm.prank(alice);
        uint256 a = id.register();
        Wallet1271 sw = new Wallet1271();
        uint256 deadline = vm.getBlockTimestamp() + 60;
        bytes32 digest = _walletDigest(a, address(sw), alice, deadline);
        vm.prank(alice);
        vm.expectRevert(bytes("invalid wallet sig"));
        id.setAgentWallet(a, address(sw), deadline, hex"00");
        sw.accept(digest);
        vm.prank(alice);
        id.setAgentWallet(a, address(sw), deadline, hex"00");
        assertEq(id.getAgentWallet(a), address(sw));
    }

    function test_eip712Domain() public view {
        (bytes1 fields, string memory name_, string memory version, uint256 chainId, address vc,,) = id.eip712Domain();
        assertEq(fields, hex"0f");
        assertEq(name_, "ERC8004IdentityRegistry");
        assertEq(version, "1");
        assertEq(chainId, block.chainid);
        assertEq(vc, address(id));
    }

    // ------------------------------------------------------------------ reputation behaviour

    function _agent() internal returns (uint256 a) {
        vm.prank(alice);
        a = id.register();
    }

    function _give(address from, uint256 a, int128 value, uint8 dec, string memory tag1) internal {
        vm.prank(from);
        rep.giveFeedback(a, value, dec, tag1, "", "", "ipfs://f", keccak256("f"));
    }

    function test_selfFeedbackReverts() public {
        uint256 a = _agent();
        vm.prank(alice);
        vm.expectRevert(bytes("Self-feedback not allowed"));
        rep.giveFeedback(a, 1, 0, "", "", "", "", 0);
        vm.prank(alice);
        id.setApprovalForAll(bob, true);
        vm.prank(bob);
        vm.expectRevert(bytes("Self-feedback not allowed"));
        rep.giveFeedback(a, 1, 0, "", "", "", "", 0);
        vm.prank(carol);
        vm.expectRevert(abi.encodeWithSelector(IdentityRegistryLite.ERC721NonexistentToken.selector, 42));
        rep.giveFeedback(42, 1, 0, "", "", "", "", 0);
    }

    function test_giveFeedback_validationAndEvent() public {
        uint256 a = _agent();
        vm.prank(bob);
        vm.expectRevert(bytes("too many decimals"));
        rep.giveFeedback(a, 1, 19, "", "", "", "", 0);
        vm.prank(bob);
        vm.expectRevert(bytes("value too large"));
        rep.giveFeedback(a, 1e38 + 1, 0, "", "", "", "", 0);
        vm.prank(bob);
        vm.expectRevert(bytes("value too large"));
        rep.giveFeedback(a, -1e38 - 1, 0, "", "", "", "", 0);
        vm.expectEmit(true, true, true, true, address(rep));
        emit ReputationRegistryLite.NewFeedback(
            a, bob, 1, 80, 0, "grade", "grade", "x", "ep", "ipfs://f", keccak256("f")
        );
        vm.prank(bob);
        rep.giveFeedback(a, 80, 0, "grade", "x", "ep", "ipfs://f", keccak256("f"));
        assertEq(rep.getLastIndex(a, bob), 1);
        (int128 value, uint8 dec, string memory t1, string memory t2, bool revoked) = rep.readFeedback(a, bob, 1);
        assertEq(value, 80);
        assertEq(dec, 0);
        assertEq(t1, "grade");
        assertEq(t2, "x");
        assertFalse(revoked);
        address[] memory clients = rep.getClients(a);
        assertEq(clients.length, 1);
        assertEq(clients[0], bob);
        assertEq(rep.getIdentityRegistry(), address(id));
        assertEq(rep.getVersion(), "2.0.0-lite");
    }

    function test_getSummary_emptyListReverts_andAverages() public {
        uint256 a = _agent();
        address[] memory none = new address[](0);
        vm.expectRevert(bytes("clientAddresses required"));
        rep.getSummary(a, none, "", "");

        _give(bob, a, 80, 0, "grade");
        _give(bob, a, 60, 0, "grade");
        _give(carol, a, 455, 1, "other"); // 45.5
        address[] memory both = new address[](2);
        (both[0], both[1]) = (bob, carol);
        (uint64 count, int128 v, uint8 d) = rep.getSummary(a, both, "", "");
        assertEq(count, 3);
        // average (80 + 60 + 45.5) / 3 = 61.8333..., mode decimals 0 -> 61
        assertEq(v, 61);
        assertEq(d, 0);
        (count, v, d) = rep.getSummary(a, both, "grade", "");
        assertEq(count, 2);
        assertEq(v, 70);
        (count, v, d) = rep.getSummary(a, both, "missing", "");
        assertEq(count, 0);
        assertEq(v, 0);
        assertEq(d, 0);

        vm.prank(bob);
        rep.revokeFeedback(a, 1);
        (count, v,) = rep.getSummary(a, both, "grade", "");
        assertEq(count, 1);
        assertEq(v, 60);
    }

    function test_revoke_and_responses() public {
        uint256 a = _agent();
        _give(bob, a, 10, 0, "t");
        vm.prank(bob);
        vm.expectRevert(bytes("index must be > 0"));
        rep.revokeFeedback(a, 0);
        vm.prank(bob);
        vm.expectRevert(bytes("index out of bounds"));
        rep.revokeFeedback(a, 2);
        vm.expectEmit(true, true, true, false, address(rep));
        emit ReputationRegistryLite.FeedbackRevoked(a, bob, 1);
        vm.prank(bob);
        rep.revokeFeedback(a, 1);
        vm.prank(bob);
        vm.expectRevert(bytes("Already revoked"));
        rep.revokeFeedback(a, 1);

        vm.prank(alice);
        vm.expectRevert(bytes("index must be > 0"));
        rep.appendResponse(a, bob, 0, "ipfs://r", 0);
        vm.prank(alice);
        vm.expectRevert(bytes("Empty URI"));
        rep.appendResponse(a, bob, 1, "", 0);
        vm.prank(alice);
        vm.expectRevert(bytes("index out of bounds"));
        rep.appendResponse(a, bob, 2, "ipfs://r", 0);
        vm.expectEmit(true, true, true, true, address(rep));
        emit ReputationRegistryLite.ResponseAppended(a, bob, 1, alice, "ipfs://r", keccak256("r"));
        vm.prank(alice);
        rep.appendResponse(a, bob, 1, "ipfs://r", keccak256("r"));
        vm.prank(carol);
        rep.appendResponse(a, bob, 1, "ipfs://r2", 0);
        vm.prank(alice);
        rep.appendResponse(a, bob, 1, "ipfs://r3", 0);
        address[] memory all = new address[](0);
        assertEq(rep.getResponseCount(a, bob, 1, all), 3);
        assertEq(rep.getResponseCount(a, bob, 0, all), 3);
        assertEq(rep.getResponseCount(a, address(0), 0, all), 3);
        address[] memory onlyAlice = new address[](1);
        onlyAlice[0] = alice;
        assertEq(rep.getResponseCount(a, bob, 1, onlyAlice), 2);

        vm.expectRevert(bytes("index must be > 0"));
        rep.readFeedback(a, bob, 0);
        vm.expectRevert(bytes("index out of bounds"));
        rep.readFeedback(a, bob, 2);
    }

    function test_readAllFeedback() public {
        uint256 a = _agent();
        _give(bob, a, 1, 0, "x");
        _give(carol, a, 2, 2, "y");
        _give(bob, a, 3, 0, "y");
        vm.prank(bob);
        rep.revokeFeedback(a, 1);
        address[] memory none = new address[](0);
        (address[] memory c, uint64[] memory idx, int128[] memory vals,,,, bool[] memory rev) =
            rep.readAllFeedback(a, none, "", "", false);
        assertEq(c.length, 2);
        assertEq(c[0], bob);
        assertEq(idx[0], 2);
        assertEq(vals[0], 3);
        assertEq(c[1], carol);
        assertFalse(rev[1]);
        (c,,,,,, rev) = rep.readAllFeedback(a, none, "", "", true);
        assertEq(c.length, 3);
        assertTrue(rev[0]);
        (c,,,,,,) = rep.readAllFeedback(a, none, "y", "", true);
        assertEq(c.length, 2);
    }

    // ------------------------------------------------------------------ proxy stub (attack A60)

    function test_stubProxy_delegatesAndUpgrades() public {
        IdentityRegistryLite impl = new IdentityRegistryLite();
        StubProxy proxy = new StubProxy(address(impl), address(this));
        IdentityRegistryLite viaProxy = IdentityRegistryLite(address(proxy));
        vm.prank(alice);
        uint256 a = viaProxy.register();
        assertEq(viaProxy.getAgentWallet(a), alice);
        // an "upgrade" to a different implementation changes behaviour behind the same address
        ReputationRegistryLite other = new ReputationRegistryLite(address(id));
        vm.prank(alice);
        vm.expectRevert(StubProxy.NotProxyAdmin.selector);
        proxy.stubUpgradeTo(address(other));
        proxy.stubUpgradeTo(address(other));
        bytes32 slot = vm.load(address(proxy), 0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc);
        assertEq(address(uint160(uint256(slot))), address(other));
    }
}
