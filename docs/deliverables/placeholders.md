# Placeholder registry: measured values in the deliverables

This file is the single list of every measured value that a deliverable may cite. The recorded Base
Sepolia run (spec 10.1-10.2) has not happened yet, so no deliverable types a transaction hash, a count,
an attack total, a backtest or dialogue metric, a gas figure, a timing or a grade reached in the run by
hand. It writes a placeholder such as `{{tx_s6_pay_atlas}}` instead, and
`scripts/render_deliverables.py` fills it from the evidence files after the run.

Design constants (tier caps, score bands, weights, reason codes, mandate M1 terms) are not measured.
They are written as text and cite their file (see "Design constants are not placeholders" at the end).

## How to use a placeholder

- Write `{{name}}` with a name from the tables below. Names are lowercase snake case. A name that is not
  in this file is an error.
- Hashes and addresses take an optional filter: `{{name|short}}` (`0x1c34da...23d8`),
  `{{name|basescan}}` or `{{name|blockscout}}` (the explorer URL from `config/networks.yaml`), and
  `{{name|link}}` (a Markdown link with the short hash as text; on a network without an explorer, the
  full hash as code).
- Put code formatting around a bare hash yourself: `` `{{tx_s6_pay_atlas}}` ``.
- Do not use double braces for anything else in a deliverable (a Mermaid hexagon node included). The
  renderer treats any leftover double-brace text as an error.
- Need a value that is not here? Add a row to this file first (same columns), then use it.

## How to render

```
uv run python scripts/render_deliverables.py                  # Base Sepolia evidence -> docs/deliverables/out/*.md
uv run python scripts/render_deliverables.py --check          # resolve and print every row below
uv run python scripts/render_deliverables.py --network anvil  # dry run on the local Anvil evidence
uv run python scripts/render_deliverables.py --keep-going     # write drafts with the gaps left in (exit 1)
```

The renderer reads every `docs/deliverables/*.md` except this file and writes `docs/deliverables/out/`
with LF line endings. It stops with a list of problems, and writes nothing, when a name is unknown, a
source file is missing, a value is null, a hash or address is malformed, or a double-brace text is left
over. It also checks that `evidence.csv` has one row per transaction in `evidence.json` and that the
bureau address in `deployments/<network>.json` equals the one in `evidence.json` (a dress-rehearsal
deployment file next to recorded-run evidence fails here).

On Anvil some rows do not resolve by design: the `erc8004_*` rows (Anvil deploys local Lite registries),
the explorer filters (Anvil has no explorer), and the `deployments/anvil.json` rows, which every local
deploy overwrites. Only the Base Sepolia render is cited.

## Columns

- **Placeholder**: the name as written in a deliverable.
- **Meaning**: what the value is, in the words a deliverable would use.
- **Source**: the file it comes from, relative to the repository root. `<network>` is the `--network`
  argument (default `base_sepolia`). Files are those of spec 10.4 and 16: `evidence/<network>/` holds the
  curated run (`evidence.json`, `evidence.csv`, `HEADLINE.md`, `receipts/`, `dialogue/S5.json`); the
  suites write `evidence/attack_suite.json` (12.3), `evidence/benign_suite.json` (12.4),
  `evidence/backtest.json` (4.12) and `evidence/dialogue_eval.json` (8.9); the deployment writes
  `deployments/<network>.json` (13.5).
- **Extract**: how the value is computed, in a small path language:
  - `json:<path>` takes one value; `count:<path>` counts a list; `sum:`, `median:`, `min:`, `max:`
    aggregate a list of integers.
  - A path walks keys with dots (`metrics.aucBps`). A key applied to a list maps over it
    (`transactions.gasUsed`). `[0]` and `[-1]` index a list. `[k=v,k2=w]` keeps the objects whose field
    equals the text (`true`, `false`, numbers and strings compare as text); `!=` is "not equal", `~=` is
    "contains" and `[k?]` keeps objects where `k` is present and not null.
  - `fn:<name>(args)` is a computed value implemented in `scripts/render_deliverables.py`.
- **Format**: how the value is printed. `text` as is; `int` plain digits; `grouped` with thousands
  commas; `num` an integer or a decimal (medians); `usdc` 6-decimal base units as USDC (`800000` ->
  `0.80`); `eth` wei as ETH (6 significant digits); `gwei` wei as gwei; `bps_pct` basis points as a
  percentage (`9758` -> `97.58%`); `bps_dec` basis points (or x10000 values) as a decimal (`9758` ->
  `0.9758`); `rate_pct` a 4-digit rate string as a percentage (`"0.9500"` -> `95.0%`); `duration`
  seconds as minutes and seconds; `hash` a lowercase 32-byte hash; `address` a 20-byte address; `tier` a
  tier number (`255` -> `FROZEN`); `yesno` a boolean; `table` a generated Markdown table;
  `list` or `list(<format>)` a list joined by commas (`none` when empty).

## Run and chain

| Placeholder | Meaning | Source | Extract | Format |
|---|---|---|---|---|
| `{{run_id}}` | Run id of the recorded run | `evidence/<network>/evidence.json` | `json:runId` | `text` |
| `{{run_network}}` | Network name of the run | `evidence/<network>/evidence.json` | `json:network` | `text` |
| `{{chain_id}}` | Chain id the run used (84532 expected on Base Sepolia) | `evidence/<network>/evidence.json` | `json:chainId` | `int` |
| `{{run_started_at}}` | Start of the run, UTC ISO time | `evidence/<network>/evidence.json` | `json:startedAt` | `text` |
| `{{run_finished_at}}` | End of the run, UTC ISO time | `evidence/<network>/evidence.json` | `json:finishedAt` | `text` |
| `{{run_duration}}` | Wall-clock duration of S0-S21 | `evidence/<network>/evidence.json` | `fn:run_duration` | `duration` |
| `{{git_commit}}` | Git commit the run was made from | `evidence/<network>/evidence.json` | `json:gitCommit` | `text` |
| `{{steps_total}}` | Steps in the run (S0-S21) | `evidence/<network>/evidence.json` | `count:steps` | `int` |
| `{{steps_passed}}` | Steps whose assertions all passed | `evidence/<network>/evidence.json` | `count:steps[status=PASSED]` | `int` |
| `{{steps_table}}` | Table of steps: id, title, status, transactions | `evidence/<network>/evidence.json` | `fn:steps_table` | `table` |
| `{{block_first}}` | First block with a run transaction | `evidence/<network>/evidence.json` | `min:transactions.block` | `int` |
| `{{block_last}}` | Last block with a run transaction | `evidence/<network>/evidence.json` | `max:transactions.block` | `int` |
| `{{block_span}}` | Blocks covered by the run (last - first + 1) | `evidence/<network>/evidence.json` | `fn:block_span` | `grouped` |
| `{{erc8004_identity_impl}}` | ERC-8004 IdentityRegistry implementation address read in the S0 preflight | `evidence/<network>/evidence.json` | `json:erc8004.identityImpl` | `address` |
| `{{erc8004_reputation_impl}}` | ERC-8004 ReputationRegistry implementation address read in S0 | `evidence/<network>/evidence.json` | `json:erc8004.reputationImpl` | `address` |
| `{{erc8004_version}}` | `getVersion()` of the ERC-8004 registries read in S0 | `evidence/<network>/evidence.json` | `json:erc8004.version` | `text` |
| `{{erc8004_checked_block}}` | Block at which S0 checked the ERC-8004 implementations | `evidence/<network>/evidence.json` | `json:erc8004.checkedAtBlock` | `int` |

## Contracts and roles

| Placeholder | Meaning | Source | Extract | Format |
|---|---|---|---|---|
| `{{addr_acb}}` | AgentCreditBureau contract address | `evidence/<network>/evidence.json` | `json:contracts.AgentCreditBureau` | `address` |
| `{{addr_identity_registry}}` | ERC-8004 IdentityRegistry address used | `evidence/<network>/evidence.json` | `json:contracts.IdentityRegistry` | `address` |
| `{{addr_reputation_registry}}` | ERC-8004 ReputationRegistry address used | `evidence/<network>/evidence.json` | `json:contracts.ReputationRegistry` | `address` |
| `{{addr_usdc}}` | USDC token address used | `evidence/<network>/evidence.json` | `json:contracts.USDC` | `address` |
| `{{addr_vault_wallet}}` | TierVault of our wallet agent | `evidence/<network>/evidence.json` | `json:contracts.vaults.wallet` | `address` |
| `{{addr_vault_beacon}}` | TierVault of Beacon, the simulated anchor buyer | `evidence/<network>/evidence.json` | `json:contracts.vaults.beacon` | `address` |
| `{{vault_code_hash}}` | Runtime code hash every registered TierVault must have | `deployments/<network>.json` | `json:vaultCodeHash` | `hash` |
| `{{acb_upgrade_delay_s}}` | Upgrade delay set at deployment, in seconds | `deployments/<network>.json` | `json:contracts.AgentCreditBureau.upgradeDelay` | `int` |
| `{{deployed_at}}` | Deployment time, UTC ISO time | `deployments/<network>.json` | `json:deployedAt` | `text` |
| `{{deploy_start_block}}` | Block from which the indexer reads | `deployments/<network>.json` | `json:startBlock` | `int` |
| `{{abi_sha}}` | Hash of the ABI files the deployment used | `deployments/<network>.json` | `json:abi.sha` | `hash` |
| `{{addr_role_deployer}}` | Deployer key address | `deployments/<network>.json` | `json:roles.deployer` | `address` |
| `{{addr_role_owner}}` | Owner key address (wallet vault owner) | `deployments/<network>.json` | `json:roles.owner` | `address` |
| `{{addr_role_admin}}` | Bureau admin key address | `deployments/<network>.json` | `json:roles.admin` | `address` |
| `{{addr_role_agent_signer}}` | Wallet agent signer key address before the S18 rotation | `deployments/<network>.json` | `json:roles.agentSigner` | `address` |
| `{{addr_role_agent_signer_next}}` | Wallet agent signer key address after the S18 rotation | `deployments/<network>.json` | `json:roles.agentSignerNext` | `address` |
| `{{addr_role_bureau}}` | Bureau poster key address | `deployments/<network>.json` | `json:roles.bureau` | `address` |
| `{{addr_role_autonomy}}` | Autonomy operator key address | `deployments/<network>.json` | `json:roles.autonomy` | `address` |

## Deployment transactions (S1-S3)

| Placeholder | Meaning | Source | Extract | Format |
|---|---|---|---|---|
| `{{tx_deploy_acb}}` | S1: AgentCreditBureau deployment | `deployments/<network>.json` | `json:contracts.AgentCreditBureau.txHash` | `hash` |
| `{{deploy_block_acb}}` | S1: block of the AgentCreditBureau deployment | `deployments/<network>.json` | `json:contracts.AgentCreditBureau.block` | `int` |
| `{{tx_s1_set_poster}}` | S1: admin `setPoster(bureau)` | `evidence/<network>/evidence.json` | `json:transactions[step=S1,function=setPoster][0].txHash` | `hash` |
| `{{tx_register_identity_wallet}}` | S2: ERC-8004 registration of our wallet agent by the owner key | `deployments/<network>.json` | `json:agents.wallet.registerTx` | `hash` |
| `{{tx_deploy_vault_wallet}}` | S2: deployment of the wallet TierVault | `deployments/<network>.json` | `json:vaults.wallet.deployTx` | `hash` |
| `{{tx_register_vault_wallet}}` | S2: `registerVault` of the wallet TierVault | `deployments/<network>.json` | `json:vaults.wallet.registerVaultTx` | `hash` |
| `{{tx_deploy_vault_beacon}}` | S2: deployment of the Beacon TierVault | `deployments/<network>.json` | `json:vaults.beacon.deployTx` | `hash` |
| `{{tx_register_vault_beacon}}` | S2: `registerVault` of the Beacon TierVault | `deployments/<network>.json` | `json:vaults.beacon.registerVaultTx` | `hash` |
| `{{tx_s3_funding_count}}` | S3: funding transactions sent (ETH top-ups and USDC) | `evidence/<network>/evidence.json` | `count:transactions[step=S3]` | `int` |

## Agent ids (ERC-8004 identities registered in S2)

| Placeholder | Meaning | Source | Extract | Format |
|---|---|---|---|---|
| `{{agent_id_wallet}}` | agentId of our wallet agent (Research Wallet) | `deployments/<network>.json` | `json:agents.wallet.agentId` | `text` |
| `{{agent_id_atlas}}` | agentId of Atlas Data | `deployments/<network>.json` | `json:agents.atlas.agentId` | `text` |
| `{{agent_id_slowpoke}}` | agentId of Slowpoke Analytics | `deployments/<network>.json` | `json:agents.slowpoke.agentId` | `text` |
| `{{agent_id_ghost}}` | agentId of Ghost Feeds | `deployments/<network>.json` | `json:agents.ghost.agentId` | `text` |
| `{{agent_id_blaster}}` | agentId of Blaster Ads | `deployments/<network>.json` | `json:agents.blaster.agentId` | `text` |
| `{{agent_id_orion}}` | agentId of Orion API | `deployments/<network>.json` | `json:agents.orion.agentId` | `text` |
| `{{agent_id_nova}}` | agentId of Nova Labs | `deployments/<network>.json` | `json:agents.nova.agentId` | `text` |
| `{{agent_id_beacon}}` | agentId of Beacon (the anchor buyer) | `deployments/<network>.json` | `json:agents.beacon.agentId` | `text` |
| `{{agent_id_echo1}}` | agentId of the sybil identity echo1 | `deployments/<network>.json` | `json:agents.echo1.agentId` | `text` |
| `{{agent_id_echo2}}` | agentId of the sybil identity echo2 | `deployments/<network>.json` | `json:agents.echo2.agentId` | `text` |
| `{{agent_id_echo3}}` | agentId of the sybil identity echo3 | `deployments/<network>.json` | `json:agents.echo3.agentId` | `text` |
| `{{agent_id_attacker}}` | agentId of the attacker identity | `deployments/<network>.json` | `json:agents.attacker.agentId` | `text` |

## Headline transactions by step (HEADLINE.md)

The 25 judge-facing claims of `config/scenario.yaml` (`headline`), in order. Each row takes the
transaction the runner flagged `headline: true` for that step and function. S0, S1-S4 and S21 have no
headline row (S1-S2 are in the deployment table above; S21 sends no transaction).

| Placeholder | Meaning | Source | Extract | Format |
|---|---|---|---|---|
| `{{headline_table}}` | The claims table of HEADLINE.md as generated by the run | `evidence/<network>/HEADLINE.md` | `fn:headline_table` | `table` |
| `{{mandate_m1_id}}` | S5: mandateId of the owner's mandate M1 | `evidence/<network>/evidence.json` | `json:mandate.mandateId` | `hash` |
| `{{tx_s5_register_mandate}}` | 1, S5: owner registers mandate M1 on-chain | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S5,function=registerMandate][0].txHash` | `hash` |
| `{{tx_s6_pay_atlas}}` | 2, S6: autonomous payment to grade-A Atlas [2000] (agent key) | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S6,function=pay][0].txHash` | `hash` |
| `{{tx_s7_pay_capped}}` | 3, S7: capped payment to B-grade Slowpoke [2002] (agent key) | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S7,function=pay][0].txHash` | `hash` |
| `{{tx_s8_escrow_nova}}` | 4, S8: escrow for the new, unrated Nova Labs [2004] (agent key) | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S8,function=escrow][0].txHash` | `hash` |
| `{{tx_s8_claim_nova}}` | 5, S8: Nova's on-chain delivery claim | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S8,function=claimDelivered][0].txHash` | `hash` |
| `{{tx_s8_release_nova}}` | 6, S8: escrow released after the claim [2008] (agent key) | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S8,function=release][0].txHash` | `hash` |
| `{{tx_s10_grade_wallet}}` | 7, S10: bureau grades the wallet with an evidence hash | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S10,function=postGrade][0].txHash` | `hash` |
| `{{tx_s10_promotion}}` | 8, S10: earned promotion tier 1 -> 2 [3000] | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S10,function=setTier][0].txHash` | `hash` |
| `{{tx_s11_refund_ghost}}` | 9, S11: Beacon refunds Ghost's undelivered escrow [2400] | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S11,function=refund][0].txHash` | `hash` |
| `{{tx_s11_grade_ghost}}` | 10, S11: grade collapse of Ghost Feeds | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S11,function=postGrade][0].txHash` | `hash` |
| `{{tx_s12_refuse_ghost}}` | 11, S12: on-chain refusal after the grade drop [2100] (agent key) | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S12,function=recordDecision][0].txHash` | `hash` |
| `{{tx_s13_revert_grade}}` | 12, S13: stolen key pays a grade-D payee, reverts `GradeNotAllowed` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S13,function=pay][0].txHash` | `hash` |
| `{{tx_s13_revert_replay}}` | 13, S13: stolen key replays an order id, reverts `OrderExists` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S13,function=pay][1].txHash` | `hash` |
| `{{tx_s14_refund_nova}}` | 14, S14: agent refunds Nova's undelivered escrow [2400] (agent key) | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S14,function=refund][0].txHash` | `hash` |
| `{{tx_s15_violation}}` | 15, S15: injection followed by the scripted naive LLM; violation and demotion [3100] | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S15,function=recordViolation][0].txHash` | `hash` |
| `{{tx_s15_report_injection}}` | 16, S15: verified INJECTION_ATTEMPT report filed by code | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S15,function=giveFeedback][0].txHash` | `hash` |
| `{{tx_s16_violation_freeze}}` | 17, S16: drain attempt, severe violation freezes the vault [3200] | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S16,function=recordViolation][0].txHash` | `hash` |
| `{{tx_s16_grade_orion}}` | 18, S16: hijacked provider Orion graded down | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S16,function=postGrade][0].txHash` | `hash` |
| `{{tx_s17_revert_frozen}}` | 19, S17: stolen key while frozen, reverts `Frozen` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S17,function=pay][0].txHash` | `hash` |
| `{{tx_s18_unfreeze}}` | 20, S18: owner rotates the agent key and unfreezes to tier 0 [3300] | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S18,function=unfreeze][0].txHash` | `hash` |
| `{{tx_s18_owner_approve}}` | 21, S18: maker-checker, owner approves the verified invoice | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S18,function=ownerApprove][0].txHash` | `hash` |
| `{{tx_s18_execute_approved}}` | 22, S18: approved payment executed [2007] (agent key) | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S18,function=executeApproved][0].txHash` | `hash` |
| `{{tx_s19_report_spam}}` | 23, S19: verified SPAM report for unsolicited invoices | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S19,function=giveFeedback][0].txHash` | `hash` |
| `{{tx_s20_file_appeal}}` | 24, S20: Ghost files an appeal | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S20,function=fileAppeal][0].txHash` | `hash` |
| `{{tx_s20_resolve_appeal}}` | 25, S20: bureau rejects the appeal after checking the events [1103] | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S20,function=resolveAppeal][0].txHash` | `hash` |

## Headline transactions: time, wallet tier and recorded reason codes

Per headline row of the table above: the UTC time (`ts_`), the wallet vault's tier after the
transaction (`tier_`, `255` printed as `FROZEN`) and, for rows whose transaction carries reason codes,
the codes recorded on it (`rc_`). Each path is the path of the matching `tx_` row with another field.

| Placeholder | Meaning | Source | Extract | Format |
|---|---|---|---|---|
| `{{ts_s5_register_mandate}}` | 1, S5: UTC time of the headline transaction `tx_s5_register_mandate` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S5,function=registerMandate][0].timestampUtc` | `text` |
| `{{ts_s6_pay_atlas}}` | 2, S6: UTC time of the headline transaction `tx_s6_pay_atlas` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S6,function=pay][0].timestampUtc` | `text` |
| `{{ts_s7_pay_capped}}` | 3, S7: UTC time of the headline transaction `tx_s7_pay_capped` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S7,function=pay][0].timestampUtc` | `text` |
| `{{ts_s8_escrow_nova}}` | 4, S8: UTC time of the headline transaction `tx_s8_escrow_nova` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S8,function=escrow][0].timestampUtc` | `text` |
| `{{ts_s8_claim_nova}}` | 5, S8: UTC time of the headline transaction `tx_s8_claim_nova` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S8,function=claimDelivered][0].timestampUtc` | `text` |
| `{{ts_s8_release_nova}}` | 6, S8: UTC time of the headline transaction `tx_s8_release_nova` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S8,function=release][0].timestampUtc` | `text` |
| `{{ts_s10_grade_wallet}}` | 7, S10: UTC time of the headline transaction `tx_s10_grade_wallet` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S10,function=postGrade][0].timestampUtc` | `text` |
| `{{ts_s10_promotion}}` | 8, S10: UTC time of the headline transaction `tx_s10_promotion` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S10,function=setTier][0].timestampUtc` | `text` |
| `{{ts_s11_refund_ghost}}` | 9, S11: UTC time of the headline transaction `tx_s11_refund_ghost` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S11,function=refund][0].timestampUtc` | `text` |
| `{{ts_s11_grade_ghost}}` | 10, S11: UTC time of the headline transaction `tx_s11_grade_ghost` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S11,function=postGrade][0].timestampUtc` | `text` |
| `{{ts_s12_refuse_ghost}}` | 11, S12: UTC time of the headline transaction `tx_s12_refuse_ghost` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S12,function=recordDecision][0].timestampUtc` | `text` |
| `{{ts_s13_revert_grade}}` | 12, S13: UTC time of the headline transaction `tx_s13_revert_grade` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S13,function=pay][0].timestampUtc` | `text` |
| `{{ts_s13_revert_replay}}` | 13, S13: UTC time of the headline transaction `tx_s13_revert_replay` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S13,function=pay][1].timestampUtc` | `text` |
| `{{ts_s14_refund_nova}}` | 14, S14: UTC time of the headline transaction `tx_s14_refund_nova` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S14,function=refund][0].timestampUtc` | `text` |
| `{{ts_s15_violation}}` | 15, S15: UTC time of the headline transaction `tx_s15_violation` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S15,function=recordViolation][0].timestampUtc` | `text` |
| `{{ts_s15_report_injection}}` | 16, S15: UTC time of the headline transaction `tx_s15_report_injection` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S15,function=giveFeedback][0].timestampUtc` | `text` |
| `{{ts_s16_violation_freeze}}` | 17, S16: UTC time of the headline transaction `tx_s16_violation_freeze` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S16,function=recordViolation][0].timestampUtc` | `text` |
| `{{ts_s16_grade_orion}}` | 18, S16: UTC time of the headline transaction `tx_s16_grade_orion` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S16,function=postGrade][0].timestampUtc` | `text` |
| `{{ts_s17_revert_frozen}}` | 19, S17: UTC time of the headline transaction `tx_s17_revert_frozen` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S17,function=pay][0].timestampUtc` | `text` |
| `{{ts_s18_unfreeze}}` | 20, S18: UTC time of the headline transaction `tx_s18_unfreeze` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S18,function=unfreeze][0].timestampUtc` | `text` |
| `{{ts_s18_owner_approve}}` | 21, S18: UTC time of the headline transaction `tx_s18_owner_approve` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S18,function=ownerApprove][0].timestampUtc` | `text` |
| `{{ts_s18_execute_approved}}` | 22, S18: UTC time of the headline transaction `tx_s18_execute_approved` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S18,function=executeApproved][0].timestampUtc` | `text` |
| `{{ts_s19_report_spam}}` | 23, S19: UTC time of the headline transaction `tx_s19_report_spam` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S19,function=giveFeedback][0].timestampUtc` | `text` |
| `{{ts_s20_file_appeal}}` | 24, S20: UTC time of the headline transaction `tx_s20_file_appeal` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S20,function=fileAppeal][0].timestampUtc` | `text` |
| `{{ts_s20_resolve_appeal}}` | 25, S20: UTC time of the headline transaction `tx_s20_resolve_appeal` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S20,function=resolveAppeal][0].timestampUtc` | `text` |
| `{{tier_s5_register_mandate}}` | 1, S5: wallet vault tier after the headline transaction `tx_s5_register_mandate` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S5,function=registerMandate][0].tierAfter` | `tier` |
| `{{tier_s6_pay_atlas}}` | 2, S6: wallet vault tier after the headline transaction `tx_s6_pay_atlas` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S6,function=pay][0].tierAfter` | `tier` |
| `{{tier_s7_pay_capped}}` | 3, S7: wallet vault tier after the headline transaction `tx_s7_pay_capped` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S7,function=pay][0].tierAfter` | `tier` |
| `{{tier_s8_escrow_nova}}` | 4, S8: wallet vault tier after the headline transaction `tx_s8_escrow_nova` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S8,function=escrow][0].tierAfter` | `tier` |
| `{{tier_s8_claim_nova}}` | 5, S8: wallet vault tier after the headline transaction `tx_s8_claim_nova` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S8,function=claimDelivered][0].tierAfter` | `tier` |
| `{{tier_s8_release_nova}}` | 6, S8: wallet vault tier after the headline transaction `tx_s8_release_nova` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S8,function=release][0].tierAfter` | `tier` |
| `{{tier_s10_grade_wallet}}` | 7, S10: wallet vault tier after the headline transaction `tx_s10_grade_wallet` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S10,function=postGrade][0].tierAfter` | `tier` |
| `{{tier_s10_promotion}}` | 8, S10: wallet vault tier after the headline transaction `tx_s10_promotion` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S10,function=setTier][0].tierAfter` | `tier` |
| `{{tier_s11_refund_ghost}}` | 9, S11: wallet vault tier after the headline transaction `tx_s11_refund_ghost` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S11,function=refund][0].tierAfter` | `tier` |
| `{{tier_s11_grade_ghost}}` | 10, S11: wallet vault tier after the headline transaction `tx_s11_grade_ghost` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S11,function=postGrade][0].tierAfter` | `tier` |
| `{{tier_s12_refuse_ghost}}` | 11, S12: wallet vault tier after the headline transaction `tx_s12_refuse_ghost` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S12,function=recordDecision][0].tierAfter` | `tier` |
| `{{tier_s13_revert_grade}}` | 12, S13: wallet vault tier after the headline transaction `tx_s13_revert_grade` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S13,function=pay][0].tierAfter` | `tier` |
| `{{tier_s13_revert_replay}}` | 13, S13: wallet vault tier after the headline transaction `tx_s13_revert_replay` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S13,function=pay][1].tierAfter` | `tier` |
| `{{tier_s14_refund_nova}}` | 14, S14: wallet vault tier after the headline transaction `tx_s14_refund_nova` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S14,function=refund][0].tierAfter` | `tier` |
| `{{tier_s15_violation}}` | 15, S15: wallet vault tier after the headline transaction `tx_s15_violation` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S15,function=recordViolation][0].tierAfter` | `tier` |
| `{{tier_s15_report_injection}}` | 16, S15: wallet vault tier after the headline transaction `tx_s15_report_injection` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S15,function=giveFeedback][0].tierAfter` | `tier` |
| `{{tier_s16_violation_freeze}}` | 17, S16: wallet vault tier after the headline transaction `tx_s16_violation_freeze` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S16,function=recordViolation][0].tierAfter` | `tier` |
| `{{tier_s16_grade_orion}}` | 18, S16: wallet vault tier after the headline transaction `tx_s16_grade_orion` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S16,function=postGrade][0].tierAfter` | `tier` |
| `{{tier_s17_revert_frozen}}` | 19, S17: wallet vault tier after the headline transaction `tx_s17_revert_frozen` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S17,function=pay][0].tierAfter` | `tier` |
| `{{tier_s18_unfreeze}}` | 20, S18: wallet vault tier after the headline transaction `tx_s18_unfreeze` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S18,function=unfreeze][0].tierAfter` | `tier` |
| `{{tier_s18_owner_approve}}` | 21, S18: wallet vault tier after the headline transaction `tx_s18_owner_approve` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S18,function=ownerApprove][0].tierAfter` | `tier` |
| `{{tier_s18_execute_approved}}` | 22, S18: wallet vault tier after the headline transaction `tx_s18_execute_approved` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S18,function=executeApproved][0].tierAfter` | `tier` |
| `{{tier_s19_report_spam}}` | 23, S19: wallet vault tier after the headline transaction `tx_s19_report_spam` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S19,function=giveFeedback][0].tierAfter` | `tier` |
| `{{tier_s20_file_appeal}}` | 24, S20: wallet vault tier after the headline transaction `tx_s20_file_appeal` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S20,function=fileAppeal][0].tierAfter` | `tier` |
| `{{tier_s20_resolve_appeal}}` | 25, S20: wallet vault tier after the headline transaction `tx_s20_resolve_appeal` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S20,function=resolveAppeal][0].tierAfter` | `tier` |
| `{{rc_s6_pay_atlas}}` | 2, S6: reason codes recorded on the headline transaction `tx_s6_pay_atlas` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S6,function=pay][0].reasonCodes` | `list(int)` |
| `{{rc_s7_pay_capped}}` | 3, S7: reason codes recorded on the headline transaction `tx_s7_pay_capped` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S7,function=pay][0].reasonCodes` | `list(int)` |
| `{{rc_s8_escrow_nova}}` | 4, S8: reason codes recorded on the headline transaction `tx_s8_escrow_nova` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S8,function=escrow][0].reasonCodes` | `list(int)` |
| `{{rc_s8_release_nova}}` | 6, S8: reason codes recorded on the headline transaction `tx_s8_release_nova` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S8,function=release][0].reasonCodes` | `list(int)` |
| `{{rc_s10_promotion}}` | 8, S10: reason codes recorded on the headline transaction `tx_s10_promotion` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S10,function=setTier][0].reasonCodes` | `list(int)` |
| `{{rc_s11_refund_ghost}}` | 9, S11: reason codes recorded on the headline transaction `tx_s11_refund_ghost` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S11,function=refund][0].reasonCodes` | `list(int)` |
| `{{rc_s12_refuse_ghost}}` | 11, S12: reason codes recorded on the headline transaction `tx_s12_refuse_ghost` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S12,function=recordDecision][0].reasonCodes` | `list(int)` |
| `{{rc_s14_refund_nova}}` | 14, S14: reason codes recorded on the headline transaction `tx_s14_refund_nova` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S14,function=refund][0].reasonCodes` | `list(int)` |
| `{{rc_s15_violation}}` | 15, S15: reason codes recorded on the headline transaction `tx_s15_violation` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S15,function=recordViolation][0].reasonCodes` | `list(int)` |
| `{{rc_s16_violation_freeze}}` | 17, S16: reason codes recorded on the headline transaction `tx_s16_violation_freeze` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S16,function=recordViolation][0].reasonCodes` | `list(int)` |
| `{{rc_s18_unfreeze}}` | 20, S18: reason codes recorded on the headline transaction `tx_s18_unfreeze` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S18,function=unfreeze][0].reasonCodes` | `list(int)` |
| `{{rc_s18_execute_approved}}` | 22, S18: reason codes recorded on the headline transaction `tx_s18_execute_approved` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S18,function=executeApproved][0].reasonCodes` | `list(int)` |
| `{{rc_s20_resolve_appeal}}` | 25, S20: reason codes recorded on the headline transaction `tx_s20_resolve_appeal` | `evidence/<network>/evidence.json` | `json:transactions[headline=true,step=S20,function=resolveAppeal][0].reasonCodes` | `list(int)` |

## Other step transactions (supporting)

| Placeholder | Meaning | Source | Extract | Format |
|---|---|---|---|---|
| `{{tx_s6_delivery_atlas}}` | S6: `recordDelivery(ON_TIME)` for Atlas | `evidence/<network>/evidence.json` | `json:transactions[step=S6,function=recordDelivery][0].txHash` | `hash` |
| `{{tx_s7_delivery_late}}` | S7: `recordDelivery(LATE)` for Slowpoke | `evidence/<network>/evidence.json` | `json:transactions[step=S7,function=recordDelivery][0].txHash` | `hash` |
| `{{tx_s9_escrow_nova}}` | S9: second escrow with Nova (never claimed) | `evidence/<network>/evidence.json` | `json:transactions[step=S9,function=escrow][0].txHash` | `hash` |
| `{{tx_s10_pay_orion}}` | S10: clean payment to Orion before the promotion | `evidence/<network>/evidence.json` | `json:transactions[step=S10,function=pay][0].txHash` | `hash` |
| `{{tx_s11_report_ghost}}` | S11: Beacon's NON_DELIVERY report on Ghost | `evidence/<network>/evidence.json` | `json:transactions[step=S11,function=giveFeedback,actor=beacon][0].txHash` | `hash` |
| `{{tx_s11_sybil_positive}}` | S11: first POSITIVE feedback on Ghost from an Echo sybil identity (weight 0) | `evidence/<network>/evidence.json` | `json:transactions[step=S11,function=giveFeedback,decision=REPORT_POSITIVE][0].txHash` | `hash` |
| `{{tx_s11_unbacked_report}}` | S11: first unbacked NON_DELIVERY report on Atlas from an Echo sybil identity (weight 0) | `evidence/<network>/evidence.json` | `json:transactions[step=S11,function=giveFeedback,decision=REPORT_NON_DELIVERY,actor!=beacon][0].txHash` | `hash` |
| `{{s11_sybil_reports}}` | S11: feedback transactions sent by the Echo sybil identities | `evidence/<network>/evidence.json` | `count:transactions[step=S11,function=giveFeedback,actor~=echo]` | `int` |
| `{{tx_s14_report_nova}}` | S14: NON_DELIVERY report on Nova filed by code | `evidence/<network>/evidence.json` | `json:transactions[step=S14,function=giveFeedback][0].txHash` | `hash` |
| `{{tx_s15_refuse}}` | S15: on-chain refusal of the injected invoice | `evidence/<network>/evidence.json` | `json:transactions[step=S15,function=recordDecision][0].txHash` | `hash` |
| `{{tx_s16_refuse}}` | S16: on-chain refusal of the drain proposal | `evidence/<network>/evidence.json` | `json:transactions[step=S16,function=recordDecision][0].txHash` | `hash` |
| `{{tx_s16_report_orion}}` | S16: verified INJECTION_ATTEMPT report on Orion (agent key) | `evidence/<network>/evidence.json` | `json:transactions[step=S16,function=giveFeedback,signedByAgentKey=true][0].txHash` | `hash` |
| `{{tx_s16_grade_nova}}` | S16: grade change of Nova Labs | `evidence/<network>/evidence.json` | `json:gradeChanges[step=S16,agent=nova][-1].txHash` | `hash` |
| `{{tx_s17_refuse_frozen}}` | S17: refusal recorded while frozen [2101] | `evidence/<network>/evidence.json` | `json:transactions[step=S17,function=recordDecision][0].txHash` | `hash` |
| `{{tx_s18_needs_approval}}` | S18: NEEDS_OWNER_APPROVAL recorded on-chain [2200] | `evidence/<network>/evidence.json` | `json:transactions[step=S18,function=recordDecision][0].txHash` | `hash` |
| `{{tx_s18_delivery_atlas}}` | S18: `recordDelivery` after the approved payment | `evidence/<network>/evidence.json` | `json:transactions[step=S18,function=recordDelivery][0].txHash` | `hash` |

## Transaction counts

| Placeholder | Meaning | Source | Extract | Format |
|---|---|---|---|---|
| `{{tx_total}}` | All transactions of the run in `evidence.json` | `evidence/<network>/evidence.json` | `count:transactions` | `int` |
| `{{tx_total_csv}}` | Rows of `evidence.csv` (must equal `tx_total`) | `evidence/<network>/evidence.csv` | `fn:csv_rows` | `int` |
| `{{tx_headline}}` | Headline transactions (one per claim) | `evidence/<network>/evidence.json` | `count:transactions[headline=true]` | `int` |
| `{{tx_supporting}}` | Supporting transactions (bootstrap, simulators, mirrors) | `evidence/<network>/evidence.json` | `count:transactions[headline=false]` | `int` |
| `{{tx_agent_key}}` | Transactions signed by the wallet agent's key (signer process or stolen-key demonstrations) | `evidence/<network>/evidence.json` | `count:transactions[signedByAgentKey=true]` | `int` |
| `{{tx_agent_key_headline}}` | Headline transactions signed by the wallet agent's key | `evidence/<network>/evidence.json` | `count:transactions[signedByAgentKey=true,headline=true]` | `int` |
| `{{tx_success}}` | Transactions with status 1 | `evidence/<network>/evidence.json` | `count:transactions[status=1]` | `int` |
| `{{tx_expected_reverts}}` | Transactions that reverted as the step expected (stolen-key demonstrations) | `evidence/<network>/evidence.json` | `count:transactions[status=0,expectedStatus=0]` | `int` |
| `{{tx_unexpected_status}}` | Transactions whose status differs from the expected status (target 0) | `evidence/<network>/evidence.json` | `fn:tx_unexpected_status` | `int` |
| `{{tx_actor_addresses}}` | Distinct sender addresses in the run | `evidence/<network>/evidence.json` | `fn:distinct_actor_addresses` | `int` |
| `{{tx_actor_bureau}}` | Transactions sent by the bureau key | `evidence/<network>/evidence.json` | `count:transactions[actor=bureau]` | `int` |
| `{{tx_actor_owner}}` | Transactions sent by the owner key | `evidence/<network>/evidence.json` | `count:transactions[actor=owner]` | `int` |
| `{{tx_fn_pay}}` | `pay` transactions (including reverted stolen-key ones) | `evidence/<network>/evidence.json` | `count:transactions[function=pay]` | `int` |
| `{{tx_fn_escrow}}` | `escrow` transactions | `evidence/<network>/evidence.json` | `count:transactions[function=escrow]` | `int` |
| `{{tx_fn_claim_delivered}}` | `claimDelivered` transactions by providers | `evidence/<network>/evidence.json` | `count:transactions[function=claimDelivered]` | `int` |
| `{{tx_fn_release}}` | `release` transactions | `evidence/<network>/evidence.json` | `count:transactions[function=release]` | `int` |
| `{{tx_fn_refund}}` | `refund` transactions | `evidence/<network>/evidence.json` | `count:transactions[function=refund]` | `int` |
| `{{tx_fn_record_decision}}` | `recordDecision` transactions (on-chain refusals and approval requests) | `evidence/<network>/evidence.json` | `count:transactions[function=recordDecision]` | `int` |
| `{{tx_fn_record_violation}}` | `recordViolation` transactions | `evidence/<network>/evidence.json` | `count:transactions[function=recordViolation]` | `int` |
| `{{tx_fn_record_delivery}}` | `recordDelivery` transactions | `evidence/<network>/evidence.json` | `count:transactions[function=recordDelivery]` | `int` |
| `{{tx_fn_post_grade}}` | `postGrade` transactions | `evidence/<network>/evidence.json` | `count:transactions[function=postGrade]` | `int` |
| `{{tx_fn_give_feedback}}` | ERC-8004 `giveFeedback` transactions (grade mirrors and reports) | `evidence/<network>/evidence.json` | `count:transactions[function=giveFeedback]` | `int` |
| `{{tx_fn_revoke_feedback}}` | ERC-8004 `revokeFeedback` transactions (mirror revoke-then-post) | `evidence/<network>/evidence.json` | `count:transactions[function=revokeFeedback]` | `int` |
| `{{tx_fn_register}}` | ERC-8004 identity `register` transactions | `evidence/<network>/evidence.json` | `count:transactions[function=register]` | `int` |
| `{{tx_fn_register_vault}}` | `registerVault` transactions | `evidence/<network>/evidence.json` | `count:transactions[function=registerVault]` | `int` |
| `{{tx_fn_register_mandate}}` | `registerMandate` transactions (Beacon's MB and the owner's M1) | `evidence/<network>/evidence.json` | `count:transactions[function=registerMandate]` | `int` |
| `{{tx_fn_deploy}}` | Contract deployments | `evidence/<network>/evidence.json` | `count:transactions[function=deploy]` | `int` |
| `{{tx_fn_set_tier}}` | `setTier` transactions by the autonomy operator | `evidence/<network>/evidence.json` | `count:transactions[function=setTier]` | `int` |
| `{{tx_fn_unfreeze}}` | `unfreeze` transactions | `evidence/<network>/evidence.json` | `count:transactions[function=unfreeze]` | `int` |
| `{{tx_fn_owner_approve}}` | `ownerApprove` transactions | `evidence/<network>/evidence.json` | `count:transactions[function=ownerApprove]` | `int` |
| `{{tx_fn_execute_approved}}` | `executeApproved` transactions | `evidence/<network>/evidence.json` | `count:transactions[function=executeApproved]` | `int` |
| `{{tx_fn_file_appeal}}` | `fileAppeal` transactions | `evidence/<network>/evidence.json` | `count:transactions[function=fileAppeal]` | `int` |
| `{{tx_fn_resolve_appeal}}` | `resolveAppeal` transactions | `evidence/<network>/evidence.json` | `count:transactions[function=resolveAppeal]` | `int` |
| `{{offchain_decisions}}` | Decisions made off-chain with no transaction (dropped invoices, superseded quotes) | `evidence/<network>/evidence.json` | `count:offchainDecisions` | `int` |
| `{{s19_dropped}}` | S19: unsolicited invoices dropped off-chain | `evidence/<network>/evidence.json` | `count:offchainDecisions[step=S19,decision=DROPPED]` | `int` |
| `{{s19_duplicates}}` | S19: of those, dropped as duplicates [2107] | `evidence/<network>/evidence.json` | `count:offchainDecisions[step=S19,reasonCodes~=2107]` | `int` |

## Grades reached in the run

| Placeholder | Meaning | Source | Extract | Format |
|---|---|---|---|---|
| `{{grade_path_wallet}}` | Grade trajectory of our wallet agent, with steps | `evidence/<network>/evidence.json` | `fn:grade_path(wallet)` | `text` |
| `{{grade_path_atlas}}` | Grade trajectory of Atlas Data | `evidence/<network>/evidence.json` | `fn:grade_path(atlas)` | `text` |
| `{{grade_path_slowpoke}}` | Grade trajectory of Slowpoke Analytics | `evidence/<network>/evidence.json` | `fn:grade_path(slowpoke)` | `text` |
| `{{grade_path_ghost}}` | Grade trajectory of Ghost Feeds | `evidence/<network>/evidence.json` | `fn:grade_path(ghost)` | `text` |
| `{{grade_path_orion}}` | Grade trajectory of Orion API | `evidence/<network>/evidence.json` | `fn:grade_path(orion)` | `text` |
| `{{grade_path_nova}}` | Grade trajectory of Nova Labs | `evidence/<network>/evidence.json` | `fn:grade_path(nova)` | `text` |
| `{{grade_path_beacon}}` | Grade trajectory of Beacon | `evidence/<network>/evidence.json` | `fn:grade_path(beacon)` | `text` |
| `{{grade_path_blaster}}` | Grade trajectory of Blaster Ads | `evidence/<network>/evidence.json` | `fn:grade_path(blaster)` | `text` |
| `{{grade_path_attacker}}` | Grade trajectory of the attacker identity | `evidence/<network>/evidence.json` | `fn:grade_path(attacker)` | `text` |
| `{{grade_final_wallet}}` | Last grade posted for our wallet agent | `evidence/<network>/evidence.json` | `fn:grade_final(wallet)` | `text` |
| `{{grade_final_atlas}}` | Last grade posted for Atlas Data | `evidence/<network>/evidence.json` | `fn:grade_final(atlas)` | `text` |
| `{{grade_final_slowpoke}}` | Last grade posted for Slowpoke Analytics | `evidence/<network>/evidence.json` | `fn:grade_final(slowpoke)` | `text` |
| `{{grade_final_ghost}}` | Last grade posted for Ghost Feeds | `evidence/<network>/evidence.json` | `fn:grade_final(ghost)` | `text` |
| `{{grade_final_orion}}` | Last grade posted for Orion API | `evidence/<network>/evidence.json` | `fn:grade_final(orion)` | `text` |
| `{{grade_final_nova}}` | Last grade posted for Nova Labs | `evidence/<network>/evidence.json` | `fn:grade_final(nova)` | `text` |
| `{{grade_wallet_s10}}` | Grade posted for the wallet agent in S10 | `evidence/<network>/evidence.json` | `json:gradeChanges[step=S10,agent=wallet][-1].to` | `text` |
| `{{grade_ghost_s11}}` | Grade posted for Ghost Feeds in S11 | `evidence/<network>/evidence.json` | `json:gradeChanges[step=S11,agent=ghost][-1].to` | `text` |
| `{{grade_orion_s16}}` | Grade posted for Orion API in S16 | `evidence/<network>/evidence.json` | `json:gradeChanges[step=S16,agent=orion][-1].to` | `text` |
| `{{grade_nova_s16}}` | Grade posted for Nova Labs in S16 | `evidence/<network>/evidence.json` | `json:gradeChanges[step=S16,agent=nova][-1].to` | `text` |
| `{{score_atlas_s4}}` | Atlas score at the end of the S4 bootstrap | `evidence/<network>/evidence.json` | `json:gradeChanges[step=S4,agent=atlas][-1].score` | `int` |
| `{{score_slowpoke_s4}}` | Slowpoke score at the end of S4 | `evidence/<network>/evidence.json` | `json:gradeChanges[step=S4,agent=slowpoke][-1].score` | `int` |
| `{{score_ghost_s4}}` | Ghost score at the end of S4 | `evidence/<network>/evidence.json` | `json:gradeChanges[step=S4,agent=ghost][-1].score` | `int` |
| `{{score_wallet_s10}}` | Wallet score when graded in S10 | `evidence/<network>/evidence.json` | `json:gradeChanges[step=S10,agent=wallet][-1].score` | `int` |
| `{{reasons_wallet_s10}}` | Reason codes of the wallet's S10 grade | `evidence/<network>/evidence.json` | `json:gradeChanges[step=S10,agent=wallet][-1].reasons` | `list(int)` |
| `{{score_ghost_s11}}` | Ghost score after its defaults (S11) | `evidence/<network>/evidence.json` | `json:gradeChanges[step=S11,agent=ghost][-1].score` | `int` |
| `{{reasons_ghost_s11}}` | Reason codes of Ghost's S11 grade | `evidence/<network>/evidence.json` | `json:gradeChanges[step=S11,agent=ghost][-1].reasons` | `list(int)` |
| `{{score_orion_s16}}` | Orion score after the verified injection report (S16) | `evidence/<network>/evidence.json` | `json:gradeChanges[step=S16,agent=orion][-1].score` | `int` |
| `{{reasons_orion_s16}}` | Reason codes of Orion's S16 grade | `evidence/<network>/evidence.json` | `json:gradeChanges[step=S16,agent=orion][-1].reasons` | `list(int)` |
| `{{score_nova_s16}}` | Nova score after the S14 non-delivery (graded in S16) | `evidence/<network>/evidence.json` | `json:gradeChanges[step=S16,agent=nova][-1].score` | `int` |
| `{{reasons_nova_s16}}` | Reason codes of Nova's S16 grade | `evidence/<network>/evidence.json` | `json:gradeChanges[step=S16,agent=nova][-1].reasons` | `list(int)` |
| `{{grade_changes_total}}` | Grade changes posted in the run | `evidence/<network>/evidence.json` | `count:gradeChanges` | `int` |
| `{{grade_changes_recompute_match}}` | Grade changes whose recompute gave MATCH | `evidence/<network>/evidence.json` | `count:gradeChanges[recompute=MATCH]` | `int` |
| `{{recompute_ghost_s11}}` | S21 recompute result for Ghost's S11 posting | `evidence/<network>/evidence.json` | `json:recompute[agent=ghost][0].result` | `text` |
| `{{recompute_orion_s16}}` | S21 recompute result for Orion's S16 posting | `evidence/<network>/evidence.json` | `json:recompute[agent=orion][0].result` | `text` |

## Wallet tier timeline

| Placeholder | Meaning | Source | Extract | Format |
|---|---|---|---|---|
| `{{wallet_tier_timeline}}` | Tier of the wallet vault through the run, each change with its step and reason code | `evidence/<network>/evidence.json` | `fn:tier_timeline` | `text` |
| `{{wallet_tier_initial}}` | Tier at the start of the run | `evidence/<network>/evidence.json` | `json:transactions[0].tierBefore` | `tier` |
| `{{wallet_tier_after_s10}}` | Tier after the S10 promotion | `evidence/<network>/evidence.json` | `json:transactions[step=S10][-1].tierAfter` | `tier` |
| `{{wallet_tier_after_s15}}` | Tier after the S15 demotion | `evidence/<network>/evidence.json` | `json:transactions[step=S15][-1].tierAfter` | `tier` |
| `{{wallet_tier_after_s16}}` | Tier after the S16 freeze | `evidence/<network>/evidence.json` | `json:transactions[step=S16][-1].tierAfter` | `tier` |
| `{{wallet_tier_after_s18}}` | Tier after the S18 unfreeze | `evidence/<network>/evidence.json` | `json:transactions[step=S18][-1].tierAfter` | `tier` |
| `{{wallet_tier_final}}` | Tier at the end of the run | `evidence/<network>/evidence.json` | `json:transactions[-1].tierAfter` | `tier` |

## Gas, cost and verification

| Placeholder | Meaning | Source | Extract | Format |
|---|---|---|---|---|
| `{{gas_total}}` | Gas used by all run transactions | `evidence/<network>/evidence.json` | `sum:transactions.gasUsed` | `grouped` |
| `{{gas_agent_key}}` | Gas used by transactions signed with the agent key | `evidence/<network>/evidence.json` | `sum:transactions[signedByAgentKey=true].gasUsed` | `grouped` |
| `{{gas_median_pay}}` | Median gas of a successful `pay` | `evidence/<network>/evidence.json` | `median:transactions[function=pay,status=1].gasUsed` | `grouped` |
| `{{gas_median_record_decision}}` | Median gas of a `recordDecision` | `evidence/<network>/evidence.json` | `median:transactions[function=recordDecision,status=1].gasUsed` | `grouped` |
| `{{gas_median_post_grade}}` | Median gas of a `postGrade` | `evidence/<network>/evidence.json` | `median:transactions[function=postGrade,status=1].gasUsed` | `grouped` |
| `{{gas_deploy_acb}}` | Gas of the AgentCreditBureau deployment | `evidence/<network>/evidence.json` | `json:transactions[function=deploy,contract=AgentCreditBureau][0].gasUsed` | `grouped` |
| `{{gas_deploy_vault_wallet}}` | Gas of the wallet TierVault deployment | `evidence/<network>/evidence.json` | `json:transactions[function=deploy,contract=TierVault(wallet)][0].gasUsed` | `grouped` |
| `{{eth_spent_total}}` | ETH paid for all run transactions (L2 execution plus L1 data fee) | `evidence/<network>/evidence.json`, `evidence/<network>/receipts/` | `fn:eth_spent(all)` | `eth` |
| `{{eth_spent_agent_key}}` | ETH paid by the agent key | `evidence/<network>/evidence.json`, `evidence/<network>/receipts/` | `fn:eth_spent(agent_key)` | `eth` |
| `{{eth_l1_fees}}` | Part of the total that was the OP-stack L1 data fee | `evidence/<network>/evidence.json`, `evidence/<network>/receipts/` | `fn:eth_spent(l1_fee)` | `eth` |
| `{{gas_price_median_gwei}}` | Median effective gas price in gwei | `evidence/<network>/evidence.json`, `evidence/<network>/receipts/` | `fn:gas_price_median` | `gwei` |
| `{{verify_offline_summary}}` | `verify_evidence.py --receipts-only` result as "verified / total" | `evidence/<network>/evidence.json`, `evidence/<network>/receipts/`, `evidence/<network>/bundles/` | `fn:verify_offline(summary)` | `text` |
| `{{verify_offline_verified}}` | Items verified offline (transactions and grade bundles) | `evidence/<network>/evidence.json`, `evidence/<network>/receipts/` | `fn:verify_offline(verified)` | `int` |
| `{{verify_offline_total}}` | Items checked offline | `evidence/<network>/evidence.json`, `evidence/<network>/receipts/` | `fn:verify_offline(total)` | `int` |

## LLM mode and the S5 owner dialogue

| Placeholder | Meaning | Source | Extract | Format |
|---|---|---|---|---|
| `{{llm_mode}}` | LLM mode of the run (`openai` or `mock`) | `evidence/<network>/evidence.json` | `json:llm.mode` | `text` |
| `{{llm_model}}` | Provider model id used for the careful turns | `evidence/<network>/evidence.json` | `json:llm.model` | `text` |
| `{{llm_prompt_hash}}` | Hash of the system prompt version used | `evidence/<network>/evidence.json` | `json:llm.promptHash` | `hash` |
| `{{llm_s5_mode}}` | Model that handled the S5 owner dialogue | `evidence/<network>/evidence.json` | `json:llm.perStep.S5` | `text` |
| `{{llm_s15_mode}}` | Model label in S15 (the scripted naive MockLLM, masking off) | `evidence/<network>/evidence.json` | `json:llm.perStep.S15` | `text` |
| `{{llm_s16_mode}}` | Model label in S16 (the scripted naive MockLLM, masking off) | `evidence/<network>/evidence.json` | `json:llm.perStep.S16` | `text` |
| `{{llm_latency_ms_median}}` | Median LLM latency in the run, milliseconds | `evidence/<network>/evidence.json` | `json:llm.latencyMsMedian` | `num` |
| `{{s5_owner_turns}}` | Owner turns from first intent to the registered mandate in S5 | `evidence/<network>/dialogue/S5.json` | `count:turns[owner?]` | `int` |

## Attack suite totals (spec 12.3)

The suite runs on Anvil (`acb attacks run --network anvil --export`). The expected totals of spec 12.3
(54 Blocked, 4 Contained, 2 Bounded, 3 Escalated, 0 Failed) are a design prediction; the deliverables
cite the measured totals below.

| Placeholder | Meaning | Source | Extract | Format |
|---|---|---|---|---|
| `{{atk_run_id}}` | Run id of the attack-suite run | `evidence/attack_suite.json` | `json:runId` | `text` |
| `{{atk_run_at}}` | Time of the attack-suite run | `evidence/attack_suite.json` | `json:runAt` | `text` |
| `{{atk_network}}` | Network of the attack-suite run | `evidence/attack_suite.json` | `json:network` | `text` |
| `{{atk_git_commit}}` | Git commit of the attack-suite run | `evidence/attack_suite.json` | `json:gitCommit` | `text` |
| `{{atk_total}}` | Attacks run | `evidence/attack_suite.json` | `count:results` | `int` |
| `{{atk_blocked}}` | Attacks classified Blocked | `evidence/attack_suite.json` | `json:totals.blocked` | `int` |
| `{{atk_contained}}` | Attacks classified Contained (circuit breaker, owner reset needed) | `evidence/attack_suite.json` | `json:totals.contained` | `int` |
| `{{atk_bounded}}` | Attacks classified Bounded (partial effect within a documented limit) | `evidence/attack_suite.json` | `json:totals.bounded` | `int` |
| `{{atk_escalated}}` | Attacks classified Escalated (turned into an owner approval) | `evidence/attack_suite.json` | `json:totals.escalated` | `int` |
| `{{atk_failed}}` | Attacks classified Failed (goal achieved) | `evidence/attack_suite.json` | `json:totals.failed` | `int` |
| `{{atk_errors}}` | Attacks the harness could not run | `evidence/attack_suite.json` | `json:totals.errors` | `int` |
| `{{atk_failed_ids}}` | Ids of Failed attacks (`none` when empty) | `evidence/attack_suite.json` | `fn:atk_ids(failed)` | `list` |
| `{{atk_funds_lost_usdc}}` | USDC moved outside owner intent across the suite | `evidence/attack_suite.json` | `json:totals.fundsLostOutsideIntent` | `usdc` |
| `{{atk_matched_expected}}` | Attacks whose measured category equals the 12.3 prediction | `evidence/attack_suite.json` | `fn:atk_matched_expected` | `int` |
| `{{atk_mismatch_ids}}` | Ids whose measured category differs from the prediction (`none` when empty) | `evidence/attack_suite.json` | `fn:atk_mismatch_ids` | `list` |
| `{{atk_frozen_count}}` | Attacks after which the vault was frozen | `evidence/attack_suite.json` | `count:results[frozen=true]` | `int` |
| `{{atk_tx_count}}` | Transactions sent by the attack suite | `evidence/attack_suite.json` | `fn:atk_tx_count` | `int` |
| `{{atk_table}}` | Table of all attacks: id, class group, attack, first layer, expected, actual, reason codes | `evidence/attack_suite.json` | `fn:atk_table` | `table` |
| `{{atk_group_table}}` | Table of totals per class group and category | `evidence/attack_suite.json` | `fn:atk_group_table` | `table` |

## Attack suite by class group

Groups are defined in "Attack class groups" below. Each group has its total and one count per
category.

| Placeholder | Meaning | Source | Extract | Format |
|---|---|---|---|---|
| `{{atk_induced_transfer_total}}` | Induced-transfer attacks run | `evidence/attack_suite.json` | `fn:atk_count(induced_transfer, total)` | `int` |
| `{{atk_induced_transfer_blocked}}` | Induced-transfer attacks Blocked | `evidence/attack_suite.json` | `fn:atk_count(induced_transfer, blocked)` | `int` |
| `{{atk_induced_transfer_contained}}` | Induced-transfer attacks Contained | `evidence/attack_suite.json` | `fn:atk_count(induced_transfer, contained)` | `int` |
| `{{atk_induced_transfer_bounded}}` | Induced-transfer attacks Bounded | `evidence/attack_suite.json` | `fn:atk_count(induced_transfer, bounded)` | `int` |
| `{{atk_induced_transfer_escalated}}` | Induced-transfer attacks Escalated | `evidence/attack_suite.json` | `fn:atk_count(induced_transfer, escalated)` | `int` |
| `{{atk_induced_transfer_failed}}` | Induced-transfer attacks Failed | `evidence/attack_suite.json` | `fn:atk_count(induced_transfer, failed)` | `int` |
| `{{atk_data_extraction_total}}` | Data-extraction attacks run | `evidence/attack_suite.json` | `fn:atk_count(data_extraction, total)` | `int` |
| `{{atk_data_extraction_blocked}}` | Data-extraction attacks Blocked | `evidence/attack_suite.json` | `fn:atk_count(data_extraction, blocked)` | `int` |
| `{{atk_data_extraction_contained}}` | Data-extraction attacks Contained | `evidence/attack_suite.json` | `fn:atk_count(data_extraction, contained)` | `int` |
| `{{atk_data_extraction_bounded}}` | Data-extraction attacks Bounded | `evidence/attack_suite.json` | `fn:atk_count(data_extraction, bounded)` | `int` |
| `{{atk_data_extraction_escalated}}` | Data-extraction attacks Escalated | `evidence/attack_suite.json` | `fn:atk_count(data_extraction, escalated)` | `int` |
| `{{atk_data_extraction_failed}}` | Data-extraction attacks Failed | `evidence/attack_suite.json` | `fn:atk_count(data_extraction, failed)` | `int` |
| `{{atk_privilege_escalation_total}}` | Privilege-escalation attacks run | `evidence/attack_suite.json` | `fn:atk_count(privilege_escalation, total)` | `int` |
| `{{atk_privilege_escalation_blocked}}` | Privilege-escalation attacks Blocked | `evidence/attack_suite.json` | `fn:atk_count(privilege_escalation, blocked)` | `int` |
| `{{atk_privilege_escalation_contained}}` | Privilege-escalation attacks Contained | `evidence/attack_suite.json` | `fn:atk_count(privilege_escalation, contained)` | `int` |
| `{{atk_privilege_escalation_bounded}}` | Privilege-escalation attacks Bounded | `evidence/attack_suite.json` | `fn:atk_count(privilege_escalation, bounded)` | `int` |
| `{{atk_privilege_escalation_escalated}}` | Privilege-escalation attacks Escalated | `evidence/attack_suite.json` | `fn:atk_count(privilege_escalation, escalated)` | `int` |
| `{{atk_privilege_escalation_failed}}` | Privilege-escalation attacks Failed | `evidence/attack_suite.json` | `fn:atk_count(privilege_escalation, failed)` | `int` |
| `{{atk_prompt_injection_total}}` | Prompt-injection attacks run | `evidence/attack_suite.json` | `fn:atk_count(prompt_injection, total)` | `int` |
| `{{atk_prompt_injection_blocked}}` | Prompt-injection attacks Blocked | `evidence/attack_suite.json` | `fn:atk_count(prompt_injection, blocked)` | `int` |
| `{{atk_prompt_injection_contained}}` | Prompt-injection attacks Contained | `evidence/attack_suite.json` | `fn:atk_count(prompt_injection, contained)` | `int` |
| `{{atk_prompt_injection_bounded}}` | Prompt-injection attacks Bounded | `evidence/attack_suite.json` | `fn:atk_count(prompt_injection, bounded)` | `int` |
| `{{atk_prompt_injection_escalated}}` | Prompt-injection attacks Escalated | `evidence/attack_suite.json` | `fn:atk_count(prompt_injection, escalated)` | `int` |
| `{{atk_prompt_injection_failed}}` | Prompt-injection attacks Failed | `evidence/attack_suite.json` | `fn:atk_count(prompt_injection, failed)` | `int` |
| `{{atk_bureau_gaming_total}}` | Bureau-gaming attacks run | `evidence/attack_suite.json` | `fn:atk_count(bureau_gaming, total)` | `int` |
| `{{atk_bureau_gaming_blocked}}` | Bureau-gaming attacks Blocked | `evidence/attack_suite.json` | `fn:atk_count(bureau_gaming, blocked)` | `int` |
| `{{atk_bureau_gaming_contained}}` | Bureau-gaming attacks Contained | `evidence/attack_suite.json` | `fn:atk_count(bureau_gaming, contained)` | `int` |
| `{{atk_bureau_gaming_bounded}}` | Bureau-gaming attacks Bounded | `evidence/attack_suite.json` | `fn:atk_count(bureau_gaming, bounded)` | `int` |
| `{{atk_bureau_gaming_escalated}}` | Bureau-gaming attacks Escalated | `evidence/attack_suite.json` | `fn:atk_count(bureau_gaming, escalated)` | `int` |
| `{{atk_bureau_gaming_failed}}` | Bureau-gaming attacks Failed | `evidence/attack_suite.json` | `fn:atk_count(bureau_gaming, failed)` | `int` |
| `{{atk_other_total}}` | Other attacks run (limits, failure recovery, gas drain, registry upgrade) | `evidence/attack_suite.json` | `fn:atk_count(other, total)` | `int` |
| `{{atk_other_blocked}}` | Other attacks Blocked | `evidence/attack_suite.json` | `fn:atk_count(other, blocked)` | `int` |
| `{{atk_other_contained}}` | Other attacks Contained | `evidence/attack_suite.json` | `fn:atk_count(other, contained)` | `int` |
| `{{atk_other_bounded}}` | Other attacks Bounded | `evidence/attack_suite.json` | `fn:atk_count(other, bounded)` | `int` |
| `{{atk_other_escalated}}` | Other attacks Escalated | `evidence/attack_suite.json` | `fn:atk_count(other, escalated)` | `int` |
| `{{atk_other_failed}}` | Other attacks Failed | `evidence/attack_suite.json` | `fn:atk_count(other, failed)` | `int` |

## Benign suite (false positives, spec 12.4)

| Placeholder | Meaning | Source | Extract | Format |
|---|---|---|---|---|
| `{{benign_tasks}}` | Benign tasks run | `evidence/benign_suite.json` | `json:totals.tasks` | `int` |
| `{{benign_false_positives}}` | Benign tasks refused, escalated or failed against their expectation | `evidence/benign_suite.json` | `json:totals.falsePositives` | `int` |
| `{{benign_false_positive_ids}}` | Ids of the false positives (`none` when empty) | `evidence/benign_suite.json` | `json:results[falsePositive=true].id` | `list` |
| `{{benign_run_at}}` | Time of the benign-suite run | `evidence/benign_suite.json` | `json:runAt` | `text` |
| `{{benign_table}}` | Table of the benign tasks: id, task, expected, actual, false positive | `evidence/benign_suite.json` | `fn:benign_table` | `table` |

## Backtest (spec 4.12)

| Placeholder | Meaning | Source | Extract | Format |
|---|---|---|---|---|
| `{{bt_agents}}` | Synthetic agents in the backtest | `evidence/backtest.json` | `json:params.agents` | `int` |
| `{{bt_rounds}}` | Scoring rounds | `evidence/backtest.json` | `json:params.rounds` | `int` |
| `{{bt_seed}}` | Random seed | `evidence/backtest.json` | `json:params.seed` | `int` |
| `{{bt_config_hash}}` | `configHash` of the scorecard configuration backtested | `evidence/backtest.json` | `json:model.configHash` | `hash` |
| `{{bt_good_count}}` | Agents of "good" types in the AUC and KS sample | `evidence/backtest.json` | `json:metrics.goodCount` | `int` |
| `{{bt_bad_count}}` | Agents of "bad" types in the AUC and KS sample | `evidence/backtest.json` | `json:metrics.badCount` | `int` |
| `{{bt_auc}}` | AUC of the score, bad vs good types | `evidence/backtest.json` | `json:metrics.aucBps` | `bps_dec` |
| `{{bt_ks}}` | KS statistic of the score, bad vs good types | `evidence/backtest.json` | `json:metrics.ksBps` | `bps_dec` |
| `{{bt_median_rounds_to_d}}` | Median rounds until a defaulter reaches D | `evidence/backtest.json` | `json:metrics.medianRoundsToD` | `num` |
| `{{bt_defaulters_reached_d}}` | Defaulters that reached D | `evidence/backtest.json` | `json:metrics.defaultersReachedD` | `int` |
| `{{bt_defaulters_never_d}}` | Defaulters that never reached D within the rounds | `evidence/backtest.json` | `json:metrics.defaultersNeverD` | `int` |
| `{{bt_defaulters_total}}` | Defaulters in the population | `evidence/backtest.json` | `fn:bt_defaulters_total` | `int` |
| `{{bt_false_adverse_pct}}` | False adverse-action rate in the last round (honest agents graded C or D) | `evidence/backtest.json` | `json:metrics.falseAdverseBps` | `bps_pct` |
| `{{bt_false_adverse_round1_pct}}` | False adverse-action rate in round 1 (cold start) | `evidence/backtest.json` | `json:rounds[0].falseAdverseBps` | `bps_pct` |
| `{{bt_psi_last}}` | PSI between the last two rounds | `evidence/backtest.json` | `json:metrics.psiX10000[-1]` | `bps_dec` |
| `{{bt_psi_series}}` | PSI between consecutive rounds | `evidence/backtest.json` | `json:metrics.psiX10000` | `list(bps_dec)` |
| `{{bt_smear_cases}}` | Honest agents targeted by smears and NEW-sybil failure records | `evidence/backtest.json` | `json:metrics.smearCases` | `int` |
| `{{bt_smear_max_abs_delta}}` | Largest score change of a smeared honest agent (expected 0) | `evidence/backtest.json` | `json:metrics.smearMaxAbsDelta` | `int` |
| `{{bt_smear_grade_changes}}` | Grade changes caused by smears (expected 0) | `evidence/backtest.json` | `json:metrics.smearGradeChanges` | `int` |
| `{{bt_smear_delta_zero}}` | Smears changed no honest score | `evidence/backtest.json` | `json:expectations.smearScoreDeltaZero` | `yesno` |
| `{{bt_dust_ring_never_ab}}` | Dust sybil ring never graded A or B | `evidence/backtest.json` | `json:expectations.dustRingNeverAorB` | `yesno` |
| `{{bt_wash_ring_never_ab}}` | Wash-trading ring never graded A or B | `evidence/backtest.json` | `json:expectations.washRingNeverAorB` | `yesno` |
| `{{bt_pop_bust_out}}` | Bust-out agents in the population | `evidence/backtest.json` | `json:population.bust_out` | `int` |
| `{{bt_bust_out_final_d}}` | Bust-out agents graded D in the last round | `evidence/backtest.json` | `json:metrics.finalGradesByType.bust_out.D` | `int` |
| `{{bt_pop_hijacked}}` | Hijacked agents in the population | `evidence/backtest.json` | `json:population.hijacked` | `int` |
| `{{bt_hijacked_final_d}}` | Hijacked agents graded D in the last round | `evidence/backtest.json` | `json:metrics.finalGradesByType.hijacked.D` | `int` |

## Dialogue evaluation with a real LLM (spec 8.9)

| Placeholder | Meaning | Source | Extract | Format |
|---|---|---|---|---|
| `{{de_provider}}` | Provider host of the evaluated LLM | `evidence/dialogue_eval.json` | `json:provider` | `text` |
| `{{de_model}}` | Model id evaluated | `evidence/dialogue_eval.json` | `json:model` | `text` |
| `{{de_prompt_hash}}` | Prompt version hash evaluated | `evidence/dialogue_eval.json` | `json:promptHash` | `hash` |
| `{{de_run_at}}` | Time of the evaluation | `evidence/dialogue_eval.json` | `json:runAt` | `text` |
| `{{de_cases}}` | Owner requests evaluated | `evidence/dialogue_eval.json` | `json:metrics.cases` | `int` |
| `{{de_chinese_cases}}` | Requests written in Chinese | `evidence/dialogue_eval.json` | `count:results[kind=chinese]` | `int` |
| `{{de_chinese_correct}}` | Chinese requests that produced the correct mandate | `evidence/dialogue_eval.json` | `count:results[kind=chinese,correct=true]` | `int` |
| `{{de_ambiguous_cases}}` | Ambiguous requests that must trigger a clarification | `evidence/dialogue_eval.json` | `count:results[kind=ambiguous]` | `int` |
| `{{de_correction_cases}}` | Requests with a correction | `evidence/dialogue_eval.json` | `count:results[kind=correction]` | `int` |
| `{{de_hostile_cases}}` | Hostile requests (A10, A19, A21 and similar) | `evidence/dialogue_eval.json` | `count:results[kind=hostile]` | `int` |
| `{{de_correct_mandate_rate}}` | Correct mandate rate (payees, per-tx, total and validity as expected) | `evidence/dialogue_eval.json` | `json:metrics.correctMandateRate` | `rate_pct` |
| `{{de_clarification_precision}}` | Forced-clarification precision | `evidence/dialogue_eval.json` | `json:metrics.clarificationPrecision` | `rate_pct` |
| `{{de_clarification_recall}}` | Forced-clarification recall | `evidence/dialogue_eval.json` | `json:metrics.clarificationRecall` | `rate_pct` |
| `{{de_correction_success_rate}}` | Correction success rate | `evidence/dialogue_eval.json` | `json:metrics.correctionSuccessRate` | `rate_pct` |
| `{{de_turns_median}}` | Median owner turns to a registered mandate | `evidence/dialogue_eval.json` | `json:metrics.turnsToMandateMedian` | `num` |
| `{{de_turns_p90}}` | 90th percentile of owner turns to a registered mandate | `evidence/dialogue_eval.json` | `json:metrics.turnsToMandateP90` | `num` |
| `{{de_tool_call_validity_rate}}` | Share of LLM calls with valid tool arguments | `evidence/dialogue_eval.json` | `json:metrics.toolCallValidityRate` | `rate_pct` |
| `{{de_fallback_rate}}` | Share of LLM calls answered by the rule-based fallback | `evidence/dialogue_eval.json` | `json:metrics.fallbackRate` | `rate_pct` |
| `{{de_hostile_safe_rate}}` | Share of hostile requests handled safely | `evidence/dialogue_eval.json` | `json:metrics.hostileSafeRate` | `rate_pct` |
| `{{de_latency_ms_median}}` | Median LLM latency, milliseconds | `evidence/dialogue_eval.json` | `json:metrics.latencyMsMedian` | `num` |
| `{{de_llm_calls}}` | LLM calls made by the evaluation | `evidence/dialogue_eval.json` | `json:metrics.llmCalls` | `int` |

## Attack class groups

The judging names four attack families (induced transfers, data extraction, privilege escalation,
prompt injection). The suite's `class` field (from `config/scenario.yaml`, `attacks`) is finer. The
renderer maps each class to one group with the block below and stops if `attack_suite.json` contains a
class that is not listed. Stolen-key cases go with the goal of the attacker (a stolen-key `pay` is an
induced transfer; a stolen-key `setTier` is privilege escalation). Attacks on the bureau's grades go to
bureau gaming; robustness cases without an attacker goal of their own go to other.

```yaml
groups:
  induced_transfer:      # A01-A07, A09, A27-A29, A46, A52, A54, A56, A61
    - induced_transfer
    - induced_transfer_stolen_key
    - social_engineering
    - drain
    - replay
    - replay_stolen_key
    - unrated_counterparty
    - duplicate_billing
    - revoked_mandate_stolen_key
    - payout_redirection
    - precision_forcing
    - stolen_key_unmandated
  data_extraction:       # A10-A13, A51
    - data_extraction
    - data_exfiltration
  privilege_escalation:  # A08, A14-A20, A44, A45
    - privilege_escalation
    - privilege_escalation_stolen_key
    - safety_regression
    - self_promotion_stolen_key
  prompt_injection:      # A21-A25, A47-A50, A53
    - direct_injection
    - indirect_injection
    - injection_outside_memo
    - inbox_injection
    - owner_console
  bureau_gaming:         # A34-A43, A55, A59, A62, A63
    - bureau_sybil_boost
    - bureau_smear
    - bureau_wash_trading
    - bureau_fake_vault
    - bureau_report_replay
    - bureau_reporter_collusion
    - bureau_dust_smear
    - bureau_fake_accusations
    - bureau_dust_self_trade
    - bureau_config_forged_vault
    - smear_after_freeze
    - bust_out
    - buyer_clawback
    - bureau_key_compromise
  other:                 # A26, A30-A33, A57, A58, A60
    - exceed_limits
    - grade_drop
    - frozen_wallet
    - failure_recovery
    - policy_vault_mismatch
    - gas_drain
    - registry_upgrade
```

With the 63 attacks of spec 12.3 the groups hold 16, 5, 10, 10, 14 and 8 attacks. A01 is labelled
`induced_transfer` by the suite although its vector is an injected memo; the prompt-injection group
counts only the attacks whose class names injection.

## Design constants are not placeholders

These come from the design and configuration, not from the run. Write them as text and cite the file;
they must match it exactly.

| Value | File |
|---|---|
| Tier caps, window, max transactions, grade masks, new-payee cap, promotion counts, reserve floor, `maxAutoTier` | `config/tiers.yaml` (spec 5.1) |
| Score points, bands, grade thresholds, reporter weights, `a_min_age_s`, fail caps | `config/scorecard.yaml` (spec 4.4-4.5) |
| Reason codes and their text | `src/acb/reason_codes.py` (spec Appendix A) |
| Mandate M1 terms (payees, 1.00 per tx, 4.00 total, 24 h), purchases per step, expected decisions | `config/scenario.yaml` (spec 10.2) |
| Expected attack categories and first layers per attack | `config/scenario.yaml` `attacks` (spec 12.3) |
| Upgrade delay, ERC-8004 pinned addresses and expected version, funding plan | `config/networks.yaml` (spec 13.2) |
| Prompt version id `acb-prompt-1.0.0`, scorecard model id `acb-sc-1.0.0`, detector id `acb-inj-1.0.0` | `src/acb/agent/config.py`, `config/scorecard.yaml`, `src/acb/injection.py` |
