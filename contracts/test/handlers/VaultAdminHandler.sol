// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {AcbTypes} from "../../src/lib/AcbTypes.sol";
import {ERC721Gift} from "../mocks/ERC721Gift.sol";
import {VaultHandlerBase} from "./VaultHandlerBase.sol";

/// @notice Invariant handler for TierVault (spec 3.2 TV-I1..TV-I11), part 2 of 2: violations and tiers (setTier,
///         freeze, unfreeze with agent rotation, maxAutoTier), owner administration (withdraw, reserve floor,
///         mandates) and the outside world (inbound tokens and NFTs, time, regrading, payee wallet rotation).
///         Results go to the shared VaultGhost. Part 1 is VaultHandler.
/// @dev Uses vm.getBlockTimestamp() instead of block.timestamp: with via_ir the optimizer may reuse a stale
///      block.timestamp across vm.warp calls inside one function.
contract VaultAdminHandler is VaultHandlerBase {
    address public donor = makeAddr("inv-donor");
    ERC721Gift public gift;
    uint256[] internal _payeeKeys;
    address[] internal _agentPool;

    constructor(Refs memory r, uint256[] memory ids, address[] memory wallets, uint256[] memory keys)
        VaultHandlerBase(r, ids, wallets)
    {
        _payeeKeys = keys;
        _agentPool.push(r.vault.agent());
        _agentPool.push(makeAddr("inv-agent-2"));
        _agentPool.push(makeAddr("inv-agent-3"));
        gift = new ERC721Gift();
    }

    // ------------------------------------------------------------------ violations and tiers

    function recordViolation(uint8 kindSeed, uint8 sevSeed, bool byAutonomy) external track(K_OTHER) {
        uint8 kind = uint8(bound(kindSeed, 1, 11));
        // severe violations freeze; keep them rarer so the vault spends time in every tier
        uint8 sev = sevSeed % 8 == 0 ? 3 : (sevSeed % 4 == 0 ? 2 : 1);
        address caller = byAutonomy ? autonomy : vault.agent();
        vm.prank(caller);
        vault.recordViolation(kind, sev, keccak256("v"));
        ghost.success("recordViolation");
    }

    function setTier(uint8 tierSeed) external track(K_SET_TIER) {
        uint8 t = vault.tier();
        // mostly try the earned promotion t -> t + 1; otherwise any tier 0..3
        uint8 newTier = (tierSeed % 4 != 0 && t < 3) ? t + 1 : uint8(bound(tierSeed >> 2, 0, 3));
        uint256 readyAt = uint256(vault.lastTierChangeAt()) + vault.promotionCooldown();
        uint16 clean = vault.cleanSinceChange();
        uint16 points = vault.violationPointsSinceChange();
        uint8 maxAuto = vault.maxAutoTier();
        uint16 needed = t <= 3 ? vault.tierConfig(t).minCleanToPromote : 0;

        vm.prank(autonomy);
        vault.setTier(newTier, newTier > t ? 3000 : 3100, bytes32(0));

        if (newTier > t) {
            if (newTier != t + 1) ghost.fail(4, "TV-I4: promotion by more than one");
            if (vm.getBlockTimestamp() < readyAt) ghost.fail(4, "TV-I4: promotion before cooldown");
            if (needed == 0 || clean < needed) ghost.fail(4, "TV-I4: promotion without clean record");
            if (points != 0) ghost.fail(4, "TV-I4: promotion with violation points");
            if (newTier > maxAuto) ghost.fail(4, "TV-I4: promotion above maxAutoTier");
        }
        ghost.success("setTier");
    }

    /// @dev Owner reset to probation (freeze, then unfreeze to tier 1): keeps the vault out of tier 0.
    function resetToTier1() external track(K_UNFREEZE) {
        if (!vault.isFrozen()) {
            vm.prank(owner);
            vault.freeze(3202, bytes32(0));
        }
        vm.prank(owner);
        vault.unfreeze(1, 3300, address(0));
        ghost.success("resetToTier1");
    }

    function freeze(uint256 callerSeed) external track(K_OTHER) {
        address[3] memory callers = [owner, vault.agent(), autonomy];
        vm.prank(callers[callerSeed % 3]);
        vault.freeze(3202, bytes32(0));
        ghost.success("freeze");
    }

    function unfreeze(uint8 toSeed, uint256 rotateSeed) external track(K_UNFREEZE) {
        uint8 to = toSeed % 2;
        address newAgent = address(0);
        if (rotateSeed % 3 == 0) {
            newAgent = _agentPool[rotateSeed % _agentPool.length];
            if (newAgent == vault.agent()) newAgent = address(0);
        }
        vm.prank(owner);
        vault.unfreeze(to, 3300, newAgent);
        if (newAgent != address(0) && vault.agent() != newAgent) ghost.fail(9, "TV-I9: agent not rotated");
        if (vault.tier() > 1) ghost.fail(9, "TV-I9: unfreeze above probation");
        ghost.success("unfreeze");
    }

    function setMaxAutoTier(uint8 mSeed) external track(K_OTHER) {
        uint8 m = mSeed % 4;
        vm.prank(owner);
        vault.setMaxAutoTier(m);
        ghost.success("setMaxAutoTier");
    }

    // ------------------------------------------------------------------ owner administration

    function withdraw(uint256 amountSeed) external track(K_WITHDRAW) {
        uint256 bal = usdc.balanceOf(address(vault));
        uint256 free = bal - vault.escrowLocked();
        uint128 amount = uint128(bound(amountSeed, 0, free / 10));
        vm.prank(owner);
        vault.withdraw(owner, amount);
        ghost.success("withdraw");
    }

    function setReserveFloor(uint256 v) external track(K_OTHER) {
        vm.prank(owner);
        vault.setReserveFloor(uint128(bound(v, 0, 4_000_000)));
        ghost.success("setReserveFloor");
    }

    function registerMandate(uint256 payeeMask, uint256 perTxSeed, uint256 totalSeed, uint256 validSeed)
        external
        track(K_OTHER)
    {
        uint256 n;
        uint256[] memory ids = new uint256[](payeeIds.length);
        address[] memory wals = new address[](payeeIds.length);
        for (uint256 i = 0; i < payeeIds.length; i++) {
            if ((payeeMask >> i) & 1 == 1 && identity.getAgentWallet(payeeIds[i]) == payeeWallets[i]) {
                ids[n] = payeeIds[i];
                wals[n] = payeeWallets[i];
                n++;
            }
        }
        if (n == 0) return;
        assembly ("memory-safe") {
            mstore(ids, n)
            mstore(wals, n)
        }
        uint128 perTx = uint128(bound(perTxSeed, 1, 4_000_000));
        uint128 total = uint128(bound(totalSeed, perTx, 20_000_000));
        uint64 validUntil = uint64(vm.getBlockTimestamp() + bound(validSeed, 1 hours, 7 days));
        bytes32 id = ghost.newId("inv-mandate");
        vm.prank(owner);
        vault.registerMandate(id, ids, wals, perTx, total, validUntil, validSeed % 7 == 0, bytes32(0));
        ghost.addMandate(id);
        ghost.success("registerMandate");
    }

    function revokeMandate(uint256 mandateSeed) external track(K_OTHER) {
        bytes32 mid = ghost.pickMandate(mandateSeed);
        vm.prank(owner);
        vault.revokeMandate(mid);
        ghost.success("revokeMandate");
    }

    // ------------------------------------------------------------------ outside world

    /// @dev TV-I5: inbound tokens never change tier or counters.
    function donate(uint256 amountSeed) external track(K_OTHER) {
        uint8 t = vault.tier();
        uint16 c = vault.cleanSinceChange();
        uint16 p = vault.violationPointsSinceChange();
        uint256 amount = bound(amountSeed, 1, 5_000_000);
        if (usdc.balanceOf(donor) < amount) return;
        vm.prank(donor);
        usdc.transfer(address(vault), amount);
        if (vault.tier() != t || vault.cleanSinceChange() != c || vault.violationPointsSinceChange() != p) {
            ghost.fail(5, "TV-I5: inbound tokens changed tier or counters");
        }
        ghost.success("donate");
    }

    /// @dev TV-I5: NFT transfer attempts never change tier or counters (safe transfers revert).
    function giftNft(bool safe) external track(K_OTHER) {
        uint8 t = vault.tier();
        uint16 c = vault.cleanSinceChange();
        uint16 p = vault.violationPointsSinceChange();
        uint256 tokenId = gift.mint(outsider);
        vm.prank(outsider);
        if (safe) {
            try gift.safeTransferFrom(outsider, address(vault), tokenId) {
                ghost.fail(5, "TV-I5: vault accepted a safe NFT transfer");
            } catch {}
        } else {
            gift.transferFrom(outsider, address(vault), tokenId);
        }
        if (vault.tier() != t || vault.cleanSinceChange() != c || vault.violationPointsSinceChange() != p) {
            ghost.fail(5, "TV-I5: NFT transfer changed tier or counters");
        }
        ghost.success("giftNft");
    }

    function warp(uint256 dtSeed) external track(K_OTHER) {
        uint256 dt = dtSeed % 10 == 0 ? bound(dtSeed >> 8, 1, 2 days) : bound(dtSeed >> 8, 1, 6 hours);
        vm.warp(vm.getBlockTimestamp() + dt);
        vm.roll(vm.getBlockNumber() + 1);
    }

    function regrade(uint256 payeeSeed, uint8 gradeSeed) external track(K_OTHER) {
        (uint256 pid,) = _pickPayee(payeeSeed);
        // mostly A or B, sometimes any grade from NEW to A
        uint8 grade = gradeSeed % 4 == 0
            ? uint8(bound(gradeSeed >> 2, AcbTypes.GRADE_NEW, AcbTypes.GRADE_A))
            : (gradeSeed % 2 == 0 ? AcbTypes.GRADE_A : AcbTypes.GRADE_B);
        vm.roll(vm.getBlockNumber() + 1);
        uint16[4] memory reasons;
        uint64 asOf = uint64(vm.getBlockNumber() - 1);
        vm.prank(poster);
        acb.postGrade(
            pid, grade, 600, reasons, bytes32("acb-sc-1.0.0"), bytes32(uint256(1)), bytes32(uint256(2)), asOf, ""
        );
        ghost.success("regrade");
    }

    /// @dev The payee rotates its live wallet away from (or back to) the pinned one.
    function rotatePayeeWallet(uint256 payeeSeed, bool restore) external track(K_OTHER) {
        (uint256 pid, uint256 idx) = _pickPayee(payeeSeed);
        address holder = payeeWallets[idx];
        if (identity.ownerOf(pid) != holder) return;
        if (!restore) {
            vm.prank(holder);
            identity.unsetAgentWallet(pid);
        } else {
            uint256 deadline = vm.getBlockTimestamp() + 60;
            bytes32 digest = _walletDigest(pid, holder, holder, deadline);
            (uint8 v, bytes32 r, bytes32 s) = vm.sign(_payeeKeys[idx], digest);
            vm.prank(holder);
            identity.setAgentWallet(pid, holder, deadline, abi.encodePacked(r, s, v));
        }
        ghost.success("rotatePayeeWallet");
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
                address(identity)
            )
        );
        return keccak256(abi.encodePacked("\x19\x01", domain, structHash));
    }
}
