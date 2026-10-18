# Agent Credit Bureau - headline transactions (base_sepolia)

Run `r-20261017-083443` on chain 84532, 2026-10-17T08:34:43Z to 2026-10-17T12:27:55Z (git 15f2cc8b7da6).
Each row is one judge-facing claim of the recorded run (spec 10.2) and the transaction that proves it.
"Agent key" marks transactions signed by the wallet agent's own key (the signer process); the others are
signed by the owner, the bureau, the autonomy operator, a provider or, for the stolen-key rows, by the runner
holding a copy of the agent key outside the signer to show that the vault reverts the call.

| # | Claim | Step | Function | Status | Agent key | Reasons | Transaction |
|---|---|---|---|---|---|---|---|
| 1 | Owner's mandate M1 registered on-chain (payees, pinned wallets, caps) | S5 | `registerMandate` | success | no | - | [`0x0fd2d2a3a96cf245d4e6538a20f2f1caf0230924bffd6327ce7dbb05b4088fe1`](https://sepolia.basescan.org/tx/0x0fd2d2a3a96cf245d4e6538a20f2f1caf0230924bffd6327ce7dbb05b4088fe1) |
| 2 | Autonomous payment: grade A provider within tier limits [2000] | S6 | `pay` | success | yes | 2000 | [`0xdd46a8b398f7ef0920fd2af91117e3432c2238e60e7648a07312ea759f2a902e`](https://sepolia.basescan.org/tx/0xdd46a8b398f7ef0920fd2af91117e3432c2238e60e7648a07312ea759f2a902e) |
| 3 | Capped payment: B-grade invoice re-quoted to the cap [2002] | S7 | `pay` | success | yes | 2002 | [`0x7fb6ed88ef348765db8d14a3dd0d969f20fa71a427c211001b3bad3d9caa80bb`](https://sepolia.basescan.org/tx/0x7fb6ed88ef348765db8d14a3dd0d969f20fa71a427c211001b3bad3d9caa80bb) |
| 4 | Escrow opened for a new, unrated provider [2004] | S8 | `escrow` | success | yes | 2004 | [`0x36bdc123e8a7b0f2ed32baebb39a23a75a8ab14e457e9430f5314c41f1314e7d`](https://sepolia.basescan.org/tx/0x36bdc123e8a7b0f2ed32baebb39a23a75a8ab14e457e9430f5314c41f1314e7d) |
| 5 | Provider's on-chain delivery claim before hand-over | S8 | `claimDelivered` | success | no | - | [`0x3c0165cf3775fae668030d550b30f1147d04546520e77e4ff3695563458376af`](https://sepolia.basescan.org/tx/0x3c0165cf3775fae668030d550b30f1147d04546520e77e4ff3695563458376af) |
| 6 | Escrow released after the provider's claim [2008] | S8 | `release` | success | yes | 2008 | [`0x89d612fce24f2001007a25d09ac9237d36f328f1cab9eb067854c5d64aca6bac`](https://sepolia.basescan.org/tx/0x89d612fce24f2001007a25d09ac9237d36f328f1cab9eb067854c5d64aca6bac) |
| 7 | Bureau grades the wallet A with an evidence hash | S10 | `postGrade` | success | no | 1006, 1007 | [`0x0b2b280b8b677e707219836928638d6f30915cda3aa16fb229ad2041adc8a3f3`](https://sepolia.basescan.org/tx/0x0b2b280b8b677e707219836928638d6f30915cda3aa16fb229ad2041adc8a3f3) |
| 8 | Earned promotion from tier 1 to tier 2 [3000] | S10 | `setTier` | success | no | 3000 | [`0x2923c9a0d72912458b688045c1e1193514ec6ac18fc8c663ce4f85250fbbe4b6`](https://sepolia.basescan.org/tx/0x2923c9a0d72912458b688045c1e1193514ec6ac18fc8c663ce4f85250fbbe4b6) |
| 9 | Beacon refunds Ghost's undelivered escrow [2400] | S11 | `refund` | success | no | 2400 | [`0xfb0b1c2fcc6f3ecb1ff0bcf7c86598bd30d8123b95dcb97699a2bb9c8080ed30`](https://sepolia.basescan.org/tx/0xfb0b1c2fcc6f3ecb1ff0bcf7c86598bd30d8123b95dcb97699a2bb9c8080ed30) |
| 10 | Grade collapse: Ghost Feeds A -> D after defaults | S11 | `postGrade` | success | no | 1002, 1001, 1006, 1007 | [`0x58d9b3fceb244a5a656f63756fc2ebcbc1cca5603538d7923c86f21adfd5254f`](https://sepolia.basescan.org/tx/0x58d9b3fceb244a5a656f63756fc2ebcbc1cca5603538d7923c86f21adfd5254f) |
| 11 | On-chain refusal after a grade drop [2100] | S12 | `recordDecision` | success | yes | 2100 | [`0xae242fbeae9eee131a34fd7ee6e26af55503bfb5a581116649435a13dd0d12a0`](https://sepolia.basescan.org/tx/0xae242fbeae9eee131a34fd7ee6e26af55503bfb5a581116649435a13dd0d12a0) |
| 12 | Stolen key pays a grade-D payee: reverts GradeNotAllowed | S13 | `pay` | reverted (expected) | yes | - | [`0x024356fc2a84bd355780bf987d3d311891103d1d3df212dc29a0ab3be1c06bc4`](https://sepolia.basescan.org/tx/0x024356fc2a84bd355780bf987d3d311891103d1d3df212dc29a0ab3be1c06bc4) |
| 13 | Stolen key replays an order id: reverts OrderExists | S13 | `pay` | reverted (expected) | yes | - | [`0xa078d02c4b008e24b0214045ae4d50ca4658100148bbd635a01a743b44a1c5c6`](https://sepolia.basescan.org/tx/0xa078d02c4b008e24b0214045ae4d50ca4658100148bbd635a01a743b44a1c5c6) |
| 14 | Agent refunds an undelivered escrow [2400] | S14 | `refund` | success | yes | 2400 | [`0x186a10051ef8140bf2a7db12570d4b3704cc77e5f4644c4c7912d9e1eeec5587`](https://sepolia.basescan.org/tx/0x186a10051ef8140bf2a7db12570d4b3704cc77e5f4644c4c7912d9e1eeec5587) |
| 15 | Injection followed by the LLM: violation recorded, wallet demoted [3100] | S15 | `recordViolation` | success | yes | 3100 | [`0x8e8dc70a0b13b3e1913a8d02fe222c8b2844b8622f80a3625cbd19f05e4db700`](https://sepolia.basescan.org/tx/0x8e8dc70a0b13b3e1913a8d02fe222c8b2844b8622f80a3625cbd19f05e4db700) |
| 16 | Verified INJECTION_ATTEMPT report filed by code | S15 | `giveFeedback` | success | yes | - | [`0x3891c75fdb04315061a87ce0f942b004d9c20a7cbf2e10d639c14a02e00cd006`](https://sepolia.basescan.org/tx/0x3891c75fdb04315061a87ce0f942b004d9c20a7cbf2e10d639c14a02e00cd006) |
| 17 | Drain attempt: severe violation freezes the vault [3200] | S16 | `recordViolation` | success | yes | 3200 | [`0x2a829dc4a7e368d74f53edf7ef82057b96e74ef27b2acda70313fb577f589de0`](https://sepolia.basescan.org/tx/0x2a829dc4a7e368d74f53edf7ef82057b96e74ef27b2acda70313fb577f589de0) |
| 18 | Hijacked provider Orion graded D (verified injection report) | S16 | `postGrade` | success | no | 1004, 1006, 1007 | [`0x2aa7789a6d633ad549641423ed866ad58587c44414b1188a53913c8f602887c0`](https://sepolia.basescan.org/tx/0x2aa7789a6d633ad549641423ed866ad58587c44414b1188a53913c8f602887c0) |
| 19 | Stolen key while frozen: reverts Frozen | S17 | `pay` | reverted (expected) | yes | - | [`0x18e325a4d30f4e7a63504030888e902c75c99fa5fedeaf6e08145901b0095e44`](https://sepolia.basescan.org/tx/0x18e325a4d30f4e7a63504030888e902c75c99fa5fedeaf6e08145901b0095e44) |
| 20 | Owner rotates the agent key and unfreezes to tier 0 [3300] | S18 | `unfreeze` | success | no | 3300 | [`0x555966ad7138c2fc1b639e961990793f53b2df57022972bc28306c784229c636`](https://sepolia.basescan.org/tx/0x555966ad7138c2fc1b639e961990793f53b2df57022972bc28306c784229c636) |
| 21 | Maker-checker: owner approves the verified invoice | S18 | `ownerApprove` | success | no | - | [`0x6fa18f8936f8ddeaba808a2da217bb02a4853deb3b752191bc57db50feeafc07`](https://sepolia.basescan.org/tx/0x6fa18f8936f8ddeaba808a2da217bb02a4853deb3b752191bc57db50feeafc07) |
| 22 | Approved payment executed [2007] | S18 | `executeApproved` | success | yes | 2007 | [`0xc77acded49ef4419399b62b32977de041fe41d83a8789d8dd8265cad14f70ca6`](https://sepolia.basescan.org/tx/0xc77acded49ef4419399b62b32977de041fe41d83a8789d8dd8265cad14f70ca6) |
| 23 | Verified SPAM report for unsolicited invoices | S19 | `giveFeedback` | success | yes | - | [`0x3de4cf94c6324e3f2e0c61ab6517c18afd2e38ac7c164a90d8a515f4463874ec`](https://sepolia.basescan.org/tx/0x3de4cf94c6324e3f2e0c61ab6517c18afd2e38ac7c164a90d8a515f4463874ec) |
| 24 | Ghost files an appeal | S20 | `fileAppeal` | success | no | - | [`0x895b6b48d2c1bdba54c9bf761a670e5dcd1fcd3d7e2ef3af8645d2e977a4305e`](https://sepolia.basescan.org/tx/0x895b6b48d2c1bdba54c9bf761a670e5dcd1fcd3d7e2ef3af8645d2e977a4305e) |
| 25 | Bureau rejects the appeal after checking the events [1103] | S20 | `resolveAppeal` | success | no | 1103 | [`0xa9d6f90973d9c8e62f100137e409a76ca399758ac64beae95003ec198cd5be6c`](https://sepolia.basescan.org/tx/0xa9d6f90973d9c8e62f100137e409a76ca399758ac64beae95003ec198cd5be6c) |

## Contracts

| Contract | Address |
|---|---|
| AgentCreditBureau | `0xd88f1f951bace92b331d2a94eaf2075352594374` |
| IdentityRegistry | `0x8004a818bfb912233c491871b3d84c89a494bd9e` |
| ReputationRegistry | `0x8004b663056a597dffe9eccc1965a193b7388713` |
| USDC | `0x036cbd53842c5426634e7929541ec2318f3dcf7e` |
| TierVault (wallet) | `0x16c7a9f608bf6d02788749a1134aed0eeddc12ad` |
| TierVault (beacon) | `0xde6b53b66d34daefaa3ce526468b4b58f5db9ff1` |

## Verify

```
python scripts/verify_evidence.py evidence/base_sepolia/evidence.json
```

All 214 transactions of the run are in `evidence.json` and `evidence.csv`; the bootstrap and simulator transactions are listed there as supporting evidence.
