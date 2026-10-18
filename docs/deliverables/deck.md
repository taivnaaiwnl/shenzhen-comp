# Presentation deck: Agent Credit Bureau (content only)

Content for the final presentation (at most 10 minutes) of Team Mongolia, FinTechathon 2026,
International AI Track, Topic F. The slides are built later on the official
`presentation-template.pptx`; every Chinese placeholder there is replaced with the English text below.

**Structure a script can parse.** Every H2 heading is one slide, in order, written
`## Slide N: <title>`. Nothing else in this file uses H2. Inside a slide, these labelled blocks appear
in this order:

- `On slide:` 3-6 short bullets (about 12 words each at most), the only text printed on the slide body.
- `Table:` (optional) a Markdown table printed on the slide, or a small evidence strip of hash links.
- `Visual:` what to draw or show and where each text goes; not printed as text.
- `Notes (mm:ss):` speaker notes; mm:ss is the speaking time of that slide at about 140 words per
  minute, counting each rendered placeholder at its likely length.
- `Sources:` files behind the slide's claims and design constants; printed as a small footer or left
  in the notes.

Measured values are placeholders from `docs/deliverables/placeholders.md`, filled by
`scripts/render_deliverables.py` from the recorded Base Sepolia run. Build the slides from the rendered
copy in `docs/deliverables/out/deck.md`, never from this file. Design constants (tier caps, score
bands, weights, reason codes, mandate terms) are typed as text and cite their file.

## Slide 1: Agent Credit Bureau

On slide:
- Agent Credit Bureau
- Team Mongolia, National University of Mongolia
- A credit bureau for AI agents, and a wallet that earns its autonomy
- Taivanbat Bayarsaikhan (team leader, Computer Science) and Tuguldur Munkhbat (Software Engineering)
- FinTechathon 2026, International AI Track, Topic F: Agent Wallet - Autonomous On-chain Wallet

Visual: Template slide 1 (layout "Title Slide"). Bullet 1 goes in the project-name field, bullet 2 in
the team-name field. Add one text box under the team name, same font, smaller size, holding bullets
3-5 on three lines (tagline, members, Topic F line). Small footer: "Recorded run on Base Sepolia
(chain id {{chain_id}}). Code and evidence: github.com/taivnaaiwnl/shenzhen-comp".

Notes (00:20): Good morning. We are Team Mongolia from the National University of Mongolia. Our Topic
F entry is the Agent Credit Bureau. Before an AI wallet pays another agent, it checks that agent's
credit grade, and the wallet earns and loses its own spending power the same way, on-chain.

Sources: docs/design/spec.md header and 16.1.

## Slide 2: Contents

On slide:
- 01 Problem
- 02 Idea
- 03 How it works
- 04 On-chain demo
- 05 Security and compliance
- 06 Value and roadmap

Visual: Template slide 2 (layout "Two Content"). The heading box reads "CONTENTS" (replace the Chinese
word above it with "Contents" or delete it). Items 01-06 fill the six numbered entries of the template
in this order.

Notes (00:10): Six parts: the problem, our idea, how it works, the on-chain demo, security and
compliance, and the value and road ahead.

Sources: the organiser's official presentation template (template slide 2, items 01-06; not redistributed).

## Slide 3: AI agents pay each other, and nobody knows who is good for the money

On slide:
- x402 lets software pay per request; AP2 and ACP give agents payment mandates
- Freysa (Nov 2024, about 13.19 ETH): the LLM held the transfer decision
- Grok / Bankrbot (May 2026, about $150-200k): Morse-code injection, NFT privilege escalation
- Lobstar Wilde (Feb 2026, about $450k): no attacker, and no hard caps
- Know Your Agent asks who acts, not how the agent behaved when paid

Visual: Template slide 3 (layout "Title and Content"), slide title in the main-title box at top left.
Left half, three incident cards (name, date, reported amount, one-word cause: "persuaded", "injected",
"no caps"). Right half, a KYA box with three ticked questions (who is the agent, who authorised it,
which permissions) and one empty box: "how has it behaved?". No logos.

Notes (00:40): Software already pays per request: x402, whose foundation launched under the Linux
Foundation in April, makes that possible, and AP2 and ACP give agents payment mandates. The failures
are real too. Freysa: the model held the transfer decision and was persuaded to pay. Grok and
Bankrbot: a Morse-code injection plus privileges raised by a gifted NFT. Lobstar Wilde: no attacker;
the agent lost its state and nothing capped the loss. Know Your Agent tells you who an agent is. A bank
would also ask: what is this counterparty's payment record?

Sources: docs/research/research-brief.md 3.5 (incident labels and confidence: Freysa, Grok / Bankrbot,
Lobstar Wilde [H]), 3.2 (x402 Foundation under the Linux Foundation, 2 Apr 2026) and 3.6 (KYA
definition; AP2 [M-H], agent-to-agent delegation out of scope; ACP as a scoped payment token).

## Slide 4: Our idea: grade the counterparty, and make the wallet earn its limits

On slide:
- Every agent gets a grade (A, B, C, D or NEW) from on-chain outcomes
- The wallet pays A and B, escrows NEW and C, refuses D
- The wallet's own limits rise with a clean record, drop or freeze on violations
- The LLM proposes; deterministic code, a separate signer and the vault decide

Visual: Template slide 3 (layout "Title and Content"), slide title in the main-title box. The four
bullets as two rows of two short cards ("Credit bureau", "Grade gate", "Earned Autonomy", "LLM is not
the boundary"), with three small labels for Topic F's words under them: asset management, transaction
execution, risk control. At the bottom, one row of five result tiles: "{{tx_total}} on-chain
transactions, {{tx_agent_key}} signed by the agent key"; "{{atk_failed}} of {{atk_total}} attacks
failed"; "{{atk_funds_lost_usdc}} USDC lost to attacks"; "{{benign_false_positives}} / {{benign_tasks}}
false positives"; "{{grade_changes_recompute_match}} / {{grade_changes_total}} grades recompute to
MATCH".

Notes (00:35): We borrow two things banks already do. First, a credit bureau: every agent with an
ERC-8004 identity gets a grade computed only from settlement outcomes on the chain. Second, earned
limits: our wallet starts on small payments, a clean record promotes it, a violation demotes it, and a
severe one trips a circuit breaker. The model talks; it is never the security boundary. The results
are on the bottom row: {{atk_failed}} failed attacks and {{atk_funds_lost_usdc}} USDC lost.

Sources: docs/design/spec.md 1.2 (Topic F mapping), 1.3 (pitch sentence), 6.5 (decision table);
result tiles from evidence/base_sepolia/evidence.json, evidence/attack_suite.json and
evidence/benign_suite.json.

## Slide 5: How it works

On slide:
- The owner states intent in chat; the LLM holds no key
- The owner registers the mandate on-chain: payees, pinned wallets, budgets, expiry
- A separate signer re-checks each payment; only it holds the agent key
- The TierVault enforces mandate, tier caps, grade gate, reserve floor, freeze
- Separate keys: the bureau grades, the autonomy operator promotes, the owner unfreezes

Visual: Template slide 3 (layout "Title and Content"), slide title in the main-title box. Simplified
component diagram of spec 2.3, left to right. Off-chain boxes: Owner console (owner key), Wallet agent
+ LLM (no keys), Signer (agent key), Bureau (bureau key), Autonomy operator (autonomy key), Simulated
counterparties (their own keys). On-chain boxes (Base Sepolia): ERC-8004 Identity and Reputation
(official registries, Draft standard), AgentCreditBureau, TierVault (wallet), USDC. Arrows: invoice
-> agent; agent -> signer -> TierVault; TierVault reads grade from AgentCreditBureau and the pinned
wallet from Identity; bureau -> AgentCreditBureau and Reputation; autonomy operator -> TierVault. Draw
a dashed red line around "Wallet agent + LLM" labelled "assume hijacked". Footer with addresses: ACB
{{addr_acb|short}}, wallet vault {{addr_vault_wallet|short}}, Identity
{{addr_identity_registry|short}}, Reputation {{addr_reputation_registry|short}}.

Notes (00:45): The owner chats with the wallet agent. The model picks one tool from a closed list, and
code re-derives every payee and amount, so a hijacked model can propose but not decide. The owner's
intent goes on-chain as a mandate with payees, pinned payout wallets and budgets. A separate signer
re-checks every payment and holds the only agent key. Then the vault checks mandate, tier cap, grade,
reserve floor and freeze. So even if the model and the agent are compromised, money goes only to an
approved payee's pinned wallet, within budget, never to a D.

Sources: docs/design/spec.md 2.1-2.5 (processes, trust boundaries, diagram, security claim);
deployment addresses from the run.

## Slide 6: The credit bureau: a scorecard anyone can recompute

On slide:
- Counts only registered-vault orders with a payee-signed invoice
- Score = 600 + points, 300-850; A >= 700, B >= 640, C >= 560, else D
- Under 3 settled orders: NEW, or C or D after failures or flags
- Praise and a new identity's accusations weigh 0
- Each grade posts model id, config hash, evidence hash, up to 4 reason codes

Visual: Template slide 3 (layout "Title and Content"), slide title in the main-title box. Left, the
scorecard as a points table (features F1 fulfilment, F2 failures, F3 flagged behaviour, F4 experience
and age, F5 diversity, counterparty quality and circular volume, F6 volume spike) with the grade rules
beside it (hard flag, cold start, bands, circular cap, standing gate, age gate). Right, Ghost Feeds'
evidence card drawn from its S11 bundle in `evidence/base_sepolia/bundles/`: score bar from the base
of 600 with each feature's points, the reason codes with their texts (1002 "Orders were paid but not
delivered", 1001 "Share of orders delivered on time is low"), and the command line
`acb bureau recompute --tx <hash>` printing the recompute result. Evidence strip under the card: "Ghost
Feeds: {{grade_path_ghost}}; score {{score_ghost_s11}}; reasons {{reasons_ghost_s11}}; recompute
{{recompute_ghost_s11}}; grade tx {{tx_s11_grade_ghost|link}}".

Notes (00:55): An order counts only if it happened in a registered vault, the payee signed the
invoice, and, for escrow, the provider claimed delivery on-chain. Dust and same-owner orders do not
count. The scorecard is additive and integer-only, so the same inputs give the same bytes anywhere.
Evidence is weighted by its source: praise weighs zero, and a new identity's unverified accusations
and failure records weigh zero, which stops cheap smears. Each posting carries the model id, config
hash, evidence hash and reason codes, like a credit report. Ghost Feeds stopped delivering; its path
was {{grade_path_ghost}}. Anyone can recompute a posting from public logs and the published evidence
files: {{grade_changes_recompute_match}} of {{grade_changes_total}} grade changes MATCH. This is an
expert-judgement scorecard, not a fitted model.

Sources: config/scorecard.yaml (base 600, clamp 300-850, bands A 700, B 640, C 560, standing weights,
cold start 3); docs/design/spec.md 4.1-4.9 (4.2 standing and reporter weights: verified
INJECTION_ATTEMPT and SPAM reports count regardless of the reporter's grade; 4.5 cold start rule;
4.9 step 3 recompute inputs); src/acb/reason_codes.py (texts of 1001, 1002);
docs/deliverables/model-card.md.

## Slide 7: Earned Autonomy: the wallet's limits are on-chain and move by formula

On slide:
- Starts at tier 1; promoted one tier after clean settled orders
- Serious violation: demote one tier; severe violation: freeze in the same transaction
- Only the owner unfreezes, to tier 0 or 1, with key rotation
- Automatic promotion stops at tier 2; tier 3 needs the owner

Table:

| Tier | Name | Per payment | 24 h window | Tx per window | Pays directly | Clean orders to promote |
|---|---|---|---|---|---|---|
| 0 | Maker-checker | 0 | 0 | 0 | none (owner approves each) | 3 |
| 1 | Small payments | 1.00 USDC | 5.00 USDC | 10 | A; B up to half | 3 |
| 2 | Standard | 2.00 USDC | 10.00 USDC | 20 | A; B up to half | 6 |
| 3 | Extended | 4.00 USDC | 20.00 USDC | 32 | A; B up to half; NEW | - |
| FROZEN | Circuit breaker | nothing leaves except owner actions | | | | |

Footnote under the table: "B payees are paid directly up to half the per-payment cap (b_cap_bps 5000);
C and NEW go to escrow; D is refused."

Visual: Template slide 3 (layout "Title and Content"), slide title in the main-title box. The table on
the left; on the right a step chart of the wallet's tier over the run (x axis: steps S6 to S18, y
axis: tier 0-3 and FROZEN), each change labelled with its step, reason code and short transaction
hash: promotion {{tx_s10_promotion|link}}, demotion {{tx_s15_violation|link}}, freeze
{{tx_s16_violation_freeze|link}}, owner reset {{tx_s18_unfreeze|link}}. Caption under the chart:
"In the run: {{wallet_tier_timeline}}".

Notes (00:50): The vault, not the agent, holds the wallet's tier. At tier 1 the wallet pays up to 1
USDC to an A payee and half that to a B; C and NEW go to escrow, D is refused, and anything larger
needs the owner. The autonomy operator promotes only when the contract agrees the record is clean, one
tier at a time, never above tier 2 without the owner. A violation needs no permission: the signer
records it and the vault demotes or freezes in the same transaction. In the run the wallet went
{{wallet_tier_timeline}}.

Sources: config/tiers.yaml (wallet profile: caps, windows, masks, min_clean_to_promote 3, 3, 6,
max_auto_tier 2, initial_tier 1); config/policy.yaml (b_cap_bps 5000); docs/design/spec.md 5.1-5.5
(violation severities, promotion rules, reason codes 3000, 3100, 3200, 3300) and 6.5 (decision table,
REFUSE 2100 for D).

## Slide 8: Demo: one recorded run on Base Sepolia, every claim has a hash

On slide:
- Owner mandate on-chain; pay grade A; capped pay to grade B
- Unrated provider paid through escrow, released after its on-chain claim
- Ghost defaults, drops to D, and is refused on-chain
- Stolen key reverts; scripted hijack freezes the vault; owner resets
- {{tx_total}} transactions, {{tx_agent_key}} agent-signed; offline check {{verify_offline_summary}}

Table:

| Step | Claim | Transaction |
|---|---|---|
| S5 | Owner registers mandate M1 | {{tx_s5_register_mandate|link}} |
| S6 / S7 | Pay grade A; capped pay to grade B | {{tx_s6_pay_atlas|link}}, {{tx_s7_pay_capped|link}} |
| S8 | Escrow, delivery claim, release | {{tx_s8_escrow_nova|link}}, {{tx_s8_claim_nova|link}}, {{tx_s8_release_nova|link}} |
| S12 / S13 | Refuse grade D; stolen key reverts | {{tx_s12_refuse_ghost|link}}, {{tx_s13_revert_grade|link}} |
| S16 / S18 | Freeze; owner reset; approved payment | {{tx_s16_violation_freeze|link}}, {{tx_s18_unfreeze|link}}, {{tx_s18_execute_approved|link}} |

Visual: Template slide 3 (layout "Title and Content"), slide title in the main-title box. Top half, a
horizontal timeline S5 to S20 with five screenshots from the demo video: (1) the owner chat with the
clarification card, (2) Basescan page of the S6 payment, (3) the decision log showing the S12 refusal
with reason 2100, (4) the red FROZEN tier badge after S16 with the banner "scripted worst-case LLM",
(5) the owner approval card of S18. Optional: a 30-second clip from the demo video in place of the
screenshots. Bottom, the table above as a small evidence strip. Footer: "All {{tx_headline}} headline
claims: evidence/base_sepolia/HEADLINE.md; verify with scripts/verify_evidence.py". Backup slide (not
counted, only for questions): {{headline_table}}.

Notes (01:05): One recorded run on Base Sepolia, {{steps_passed}} of {{steps_total}} steps passing.
The owner registers mandate M1: five payees, 1 USDC per payment, 4 in total, 24 hours. The agent pays
Atlas, graded A. Slowpoke is B, so the invoice is re-quoted down to the B cap. Nova is unrated, so the
money goes to escrow and is released after Nova claims delivery on-chain. Ghost stops delivering,
drops to D, and the wallet refuses its next invoice on-chain. A stolen agent key cannot pay Ghost or
replay an order: both revert. Steps 15 and 16 use a scripted naive model on purpose. Its first
injected proposal costs a tier; its drain proposal freezes the vault. The owner rotates the key and
approves the next payment by hand. {{tx_agent_key}} of {{tx_total}} transactions were signed by the
agent's own key.

Sources: config/scenario.yaml (mandate M1 terms, purchases, headline claims); docs/design/spec.md
10.2 (steps), 10.4-10.5 (HEADLINE.md, verify_evidence.py); evidence/base_sepolia/.

## Slide 9: Security and compliance: counted attacks, by the official threat classes

On slide:
- {{atk_total}} scripted attacks, classified from observed effects
- Failed: {{atk_failed}} ({{atk_failed_ids}}); USDC lost: {{atk_funds_lost_usdc}}
- Contained: breaker froze wallet. Bounded: within a stated limit. Escalated: owner approval
- Compliance: no personal data on-chain, only agent ids, amounts, reason codes, hashes
- Denylist and USDC blacklist check (2120); simulated agents labelled on the registry

Table:

| Class | Attacks | Blocked | Contained | Bounded | Escalated | Failed |
|---|---|---|---|---|---|---|
| Induced transfer | {{atk_induced_transfer_total}} | {{atk_induced_transfer_blocked}} | {{atk_induced_transfer_contained}} | {{atk_induced_transfer_bounded}} | {{atk_induced_transfer_escalated}} | {{atk_induced_transfer_failed}} |
| Data extraction | {{atk_data_extraction_total}} | {{atk_data_extraction_blocked}} | {{atk_data_extraction_contained}} | {{atk_data_extraction_bounded}} | {{atk_data_extraction_escalated}} | {{atk_data_extraction_failed}} |
| Privilege escalation | {{atk_privilege_escalation_total}} | {{atk_privilege_escalation_blocked}} | {{atk_privilege_escalation_contained}} | {{atk_privilege_escalation_bounded}} | {{atk_privilege_escalation_escalated}} | {{atk_privilege_escalation_failed}} |
| Prompt injection | {{atk_prompt_injection_total}} | {{atk_prompt_injection_blocked}} | {{atk_prompt_injection_contained}} | {{atk_prompt_injection_bounded}} | {{atk_prompt_injection_escalated}} | {{atk_prompt_injection_failed}} |
| Attacks on the bureau | {{atk_bureau_gaming_total}} | {{atk_bureau_gaming_blocked}} | {{atk_bureau_gaming_contained}} | {{atk_bureau_gaming_bounded}} | {{atk_bureau_gaming_escalated}} | {{atk_bureau_gaming_failed}} |
| Other (limits, failure recovery, gas, registry) | {{atk_other_total}} | {{atk_other_blocked}} | {{atk_other_contained}} | {{atk_other_bounded}} | {{atk_other_escalated}} | {{atk_other_failed}} |
| Total | {{atk_total}} | {{atk_blocked}} | {{atk_contained}} | {{atk_bounded}} | {{atk_escalated}} | {{atk_failed}} |

Visual: Template slide 3 (layout "Title and Content"), slide title in the main-title box. The table as
the main element, the first four rows (the judging's four classes) highlighted. To its right, a narrow
column of the nine defence layers (L1 invoice validation and detector, L2 quarantine and turn-type
tool sets, L3 action selector with echo-and-verify, L4 policy, L5 signer, L6 vault on-chain, L7 bureau
rules, L8 owner console, L9 autonomy operator). The two compliance bullets sit in a separate box
headed "Compliance" under the table. Footer: "Suite run {{atk_run_id}} on {{atk_network}}, commit
{{atk_git_commit}}. Stolen-key reverts on Base Sepolia: grade gate {{tx_s13_revert_grade|link}},
replay {{tx_s13_revert_replay|link}}, frozen {{tx_s17_revert_frozen|link}}. Full table in the
security self-assessment".

Notes (00:55): Security is 30 percent of the score, so we counted it: {{atk_total}} scripted attacks
in the judging's four classes, plus attacks on the bureau itself, such as sybil praise, smears and fake
vaults. The harness classifies each one from what happened: did money leave the owner's intent, did a
secret leak, did a tier or grade move. Contained means the circuit breaker froze the wallet:
availability lost on purpose, never money.
Say this line only if Failed is 0: No attack reached its goal, and {{atk_funds_lost_usdc}} USDC left
the owner's intent.
Say this line only if Failed is above 0: Attack {{atk_failed_ids}} reached its goal; here is the fix
we made, from the security self-assessment.
On compliance: in China, credit reporting is a licensed activity. We score software agents only and
put no personal data on-chain; a production service would need a licensing review.

Sources: evidence/attack_suite.json (measured totals); config/scenario.yaml `attacks` and
docs/deliverables/placeholders.md "Attack class groups" (class-to-group mapping); docs/design/spec.md
12.3 (categories and layers), 16.2 (compliance and privacy statement; denylist and token blacklist,
check C11, reason 2120; "ACB demo, simulated agent" label); config/policy.yaml (denylist).

## Slide 10: Interaction: clarify, correct, fall back, explain

On slide:
- Owner: "I need FX market data and API calls for my research today, budget 5 USDC"
- Agent (forced clarification): each provider shown with grade and payout wallet
- Owner: "Actually make the total 4 USDC, not 5" -> the card shows the diff
- Fallbacks: LLM down -> rule-based; unrated -> escrow; over cap -> owner
- S5: {{s5_owner_turns}} owner turns ({{llm_s5_mode}}); eval median {{de_turns_median}} turns

Visual: Template slide 3 (layout "Title and Content"), slide title in the main-title box. Screenshot of
the owner console chat column from S5: the clarification card with option buttons, the mandate card
showing the diff "total 5.00 -> 4.00", and the decision card for the Ghost refusal answering "Why did
you refuse Ghost Feeds?" with reason 2100 and its transaction link. Small banner example at the top:
"AI offline: rule-based mode". Label the chat column "Owner lines scripted, model live". Side box with
the dialogue evaluation of {{de_cases}} requests: correct mandates {{de_correct_mandate_rate}},
clarification precision {{de_clarification_precision}} and recall {{de_clarification_recall}},
correction success {{de_correction_success_rate}}, Chinese requests {{de_chinese_correct}} of
{{de_chinese_cases}} correct, hostile requests handled safely {{de_hostile_safe_rate}}.

Notes (00:45): The judging asks for clarification, correction and fallback. When a name matches more
than one provider, code forces a question, even if the model did not ask, and shows each candidate's
grade and payout wallet. The owner corrects the budget and the card shows the diff. In the run the
owner's lines are scripted and the model's side is live. S5 took {{s5_owner_turns}} owner turns; the
script has three, and any extra turn is a scripted fallback line that spells the mandate out. If the
model is down, a rule-based parser takes over. A separate evaluation of {{de_cases}} requests gave a
median of {{de_turns_median}} turns.

Sources: config/scenario.yaml `dialogue` (the three scripted owner lines) and `dialogue_fallback`
(sent only with a real LLM when the draft is not yet right); src/acb/scenario/steps.py s5;
docs/design/spec.md 8.3, 8.6 and 8.9; evidence/base_sepolia/dialogue/S5.json;
evidence/dialogue_eval.json.

## Slide 11: Measurable value

On slide:
- Stolen agent key, worst case: 4.00 USDC, only to approved payees
- Bust-out provider: bounded by the single-name exposure limit (A: 1.00 USDC)
- False positives: {{benign_false_positives}} of {{benign_tasks}} normal tasks
- Backtest on {{bt_agents}} synthetic agents: AUC {{bt_auc}}, KS {{bt_ks}}
- Attempted outflow stopped (scripted hijack): refused, vault frozen, nothing moved

Visual: Template slide 3 (layout "Title and Content"), slide title in the main-title box. Three number
tiles in a row: "stolen-key bound" (4.00 USDC), "single-name exposure limit, grade A" (1.00 USDC),
"false positives" ({{benign_false_positives}} / {{benign_tasks}}). Under them, a small bar chart of the
backtest's false adverse-action rate in round 1 ({{bt_false_adverse_round1_pct}}) and in the last round
({{bt_false_adverse_pct}}), labelled "honest agents graded C or D", with AUC {{bt_auc}} and the largest
honest score change from smears ({{bt_smear_max_abs_delta}} points) beside it. Small grey line at the
bottom: "Cost of the run: {{eth_spent_total}} ETH of testnet fees; a payment uses about
{{gas_median_pay}} gas".

Notes (00:45): What does this buy a user? A stolen agent key moves at most 4 USDC in our demo profile,
only to payees the owner approved, and the autonomy operator freezes the vault after the first such
payment. A provider that builds an A grade and then defaults is bounded by the single-name exposure
limit, 1 USDC for an A. The hijacked drain in the run was a blocked attempt by our scripted model, not
a real loss. Safety must not stop normal work: {{benign_false_positives}} of {{benign_tasks}} normal
tasks were refused or escalated by mistake. The backtest on {{bt_agents}} synthetic agents gave AUC
{{bt_auc}}.

Sources: docs/design/spec.md 2.5-2.6 (security claim; stolen-key bound min(remaining mandate budget,
window cap, free balance) = 4.00 USDC with mandate M1 of config/scenario.yaml and the caps of
config/tiers.yaml; bust-out bound), 5.4 rule 2 (unmatched outflow -> freeze 3205), 6.5 (C14 single-name
exposure limit), 4.12 (backtest), 12.4 (benign suite), 8.7 and 10.1 (scripted naive MockLLM);
config/policy.yaml (exposure_limit A 1000000); evidence/backtest.json; evidence/benign_suite.json.

## Slide 12: Related work, and what is new

On slide:
- Not new: a trust score that gates an agent's spending
- Not new: deterministic policy, tiers or earned autonomy in 2026 entries
- New: counterparty grades only from verifiable settlement outcomes, recomputable by anyone
- New: the wallet's autonomy earned and lost on-chain from its own violations

Table:

| Work | What it has | What we add |
|---|---|---|
| Leash (Base Sepolia) | LLM is not the security boundary; tiers; 30 counted attacks | Counterparty grades that change the decision; tiers that move by formula on-chain |
| agentic-wallet (Base Sepolia) | Scoped expiring session key; tiers with emergency pause | Autonomy earned and lost from the agent's own violations, enforced by the vault |
| ReceivableOS (FISCO BCOS) | "AI proposes; the policy guard decides" | Counterparty grades and an on-chain tier on top of that split |
| DuitDuit (Topic E) | "Earned autonomy" from an approval record | Autonomy driven by violations, including "the LLM proposed what the policy blocked" |
| Trust-score gating (Triage-OWS, AsterPay KYA, DJD Agent Score, Ant International) | A score sets spending limits | Public, versioned, evidence-hashed grades of counterparties, enforced by a contract |

Visual: Template slide 3 (layout "Title and Content"), slide title in the main-title box. The table.
Below it, one line in bold: "Our narrow claim: shared on-chain behaviour grades for agents, from
outcomes anyone can verify, that change both whom a wallet pays and how much freedom the wallet itself
has."

Notes (00:30): A trust score that limits an agent's spending already exists, so we do not claim it.
Strong 2026 entries have deterministic policy, tiers or earned autonomy; we have those too. What we
add is narrower: counterparty grades from settled outcomes only, recomputable by anyone to the same
hash, that change whom the wallet pays, and a wallet tier that moves on-chain from its own violations.

Sources: docs/design/spec.md 1.3 (related work and narrow claim); docs/research/research-brief.md 2
item 9 (Leash, agentic-wallet, ReceivableOS, DuitDuit) and 3.6 (trust-score gating, related work).

## Slide 13: On-chain exploration (bonus)

On slide:
- Official ERC-8004 registries (Draft standard); implementations pinned and checked in S0
- Contract sources verified on Blockscout
- Permissionless recompute: `acb bureau recompute --tx <hash>`
- Vault registry bound to code and config (BadVaultCode, BadVaultConfig)
- Delayed grade upgrades; on-chain mandates with pinned payout wallets

Visual: Template slide 3 (layout "Title and Content"), slide title in the main-title box. The five
bullets as five small cards in one column, each with a tiny chain icon. Evidence strip at the bottom:
"ERC-8004 version {{erc8004_version}} checked at block {{erc8004_checked_block}}; AgentCreditBureau
{{addr_acb|blockscout}}; wallet vault {{addr_vault_wallet|blockscout}}; upgrade delay
{{acb_upgrade_delay_s}} s".

Notes (00:25): For on-chain exploration: we use the official ERC-8004 registries, a Draft standard,
and check their pinned implementations before the run. Contract sources are verified on Blockscout.
Anyone can recompute a grade. The bureau accepts only vaults with our exact code and configuration.
Grade upgrades wait a delay, and mandates pin payout wallets on-chain.

Sources: docs/design/spec.md 1.2 (bonus row), 3 (registerVault: BadVaultCode, BadVaultConfig), 3.7
(Blockscout verification command), 4.9 (recompute), 16.1; docs/research/research-brief.md 3.1
(ERC-8004 status).

## Slide 14: Known limits, and what comes next

On slide:
- Limits: testnet only, with simulated counterparties labelled as such
- Limits: one bureau poster key and one anchor, both chosen by us
- Limits: the hijack steps use a scripted "naive" LLM
- Next: poster quorum, sybil clustering by funding source, keys in a KMS or HSM

Visual: Template slide 3 (layout "Title and Content"), slide title in the main-title box. Two columns,
"Known limits" (bullets 1-3) and "Next" (bullet 4, one item per line). Small grey line at the bottom:
"Demo timings: age gate 600 s (production 30 days), grade upgrade delay 60 s (production 1 day)".

Notes (00:30): Our limits. It is testnet only, with simulated counterparties labelled as such. The
bureau has one poster key and one anchor, both chosen by us: grades are recomputable and upgrades are
delayed, but it is not yet decentralised. The hijack steps use a scripted naive model. Next: several
posters with a quorum, sybil clustering by funding source, and signer keys in a KMS or HSM.

Sources: docs/design/spec.md 17.1 (KR-1, KR-2, KR-4, KR-5, KR-6, KR-8, KR-15), 2.6 and 4.10 (grade
upgrade delay, production 1 day), 4.5 and 5.1 (demo vs production timings); config/scorecard.yaml
(a_min_age_s demo 600, production 2592000); config/networks.yaml (upgrade_delay_s 60 on both
networks).

## Slide 15: Thank you

On slide:
- Thank you
- github.com/taivnaaiwnl/shenzhen-comp
- Team Mongolia: Taivanbat Bayarsaikhan and Tuguldur Munkhbat, National University of Mongolia

Visual: Template slide 4 (layout "Section Header"); its background artwork already reads "THANKS", so
do not add a title or any other text. Bottom right, over the plain part of the background: the
repository URL, a QR code of it, and the team line in small type.

Notes (00:10): Thank you. The code, the evidence and the verifier are at this link. We welcome your
questions.

Sources: docs/design/spec.md 16.1.

# Build notes (not slides)

### Timing

| Slide | Time | Ends at |
|---|---|---|
| 1 Title | 00:20 | 00:20 |
| 2 Contents | 00:10 | 00:30 |
| 3 Problem | 00:40 | 01:10 |
| 4 Idea and results | 00:35 | 01:45 |
| 5 How it works | 00:45 | 02:30 |
| 6 Credit bureau | 00:55 | 03:25 |
| 7 Earned Autonomy | 00:50 | 04:15 |
| 8 Demo | 01:05 | 05:20 |
| 9 Security and compliance | 00:55 | 06:15 |
| 10 Interaction | 00:45 | 07:00 |
| 11 Measurable value | 00:45 | 07:45 |
| 12 Related work | 00:30 | 08:15 |
| 13 On-chain exploration | 00:25 | 08:40 |
| 14 Limits and next | 00:30 | 09:10 |
| 15 Thank you | 00:10 | 09:20 |

Total 09:20, which leaves 40 seconds of the 10-minute limit for transitions and slow passages. The
notes hold about 1,250 words before rendering, about 140 words per minute. After rendering, count the
words of `docs/deliverables/out/deck.md` again: `{{wallet_tier_timeline}}` and `{{grade_path_ghost}}`
expand to several words each. If the rendered notes pass 1,300 words, shorten slide 7's last sentence
to "The chart shows the run." Rehearse with a timer; slide 9 is read with only one of its two
Failed-count lines.

### Template mapping

`presentation-template.pptx` has four slides:

| Template slide | Layout | Fields | Used for |
|---|---|---|---|
| 1 | Title Slide | project name, team name | Slide 1 (add one text box under the team name for the tagline, members and Topic F line) |
| 2 | Two Content | CONTENTS heading, items 01-06 | Slide 2 |
| 3 | Title and Content | main-title box at top left, white page with the FinTechathon banner | Slides 3-14 (duplicate it; the slide title goes in the main-title box) |
| 4 | Section Header | background artwork reading "THANKS" | Slide 15 only (repository URL, QR code, team line; no other text) |

Every Chinese placeholder of the template (project name, team name, contents heading, contents items,
main title) must be replaced with English text or deleted.

### Checks before presenting

- Render first: `uv run python scripts/render_deliverables.py`; build the slides only from
  `docs/deliverables/out/deck.md`.
- Slide 9: read the Failed-count line that matches the rendered `{{atk_failed}}`; delete the other
  from the speaker notes in the PPTX. If an attack failed, add one short sentence naming the fix from
  the security self-assessment.
- Slide 13: the three contracts are source-verified on Blockscout (README 6.1); build with`n  `--blockscout-verified` so the slide keeps that bullet.
- Slide 10: if the rendered `{{s5_owner_turns}}` is above 3, the runner sent fallback lines; keep the
  sentence that says so.
