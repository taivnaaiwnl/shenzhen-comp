# Change requests and decisions on frozen interfaces

This file is owned by WP1. Shared types (`src/acb/types.py`), canonical hashing (`src/acb/canonical.py`)
and the reason catalogue (`src/acb/reason_codes.py`) are frozen from 12 Oct 18:00 UTC+8 (spec 15.1).
After that, a package that needs a change adds an entry under "Open requests"; WP1 applies agreed
changes and moves the entry to "Applied". Never edit `types.py` from another package.

Entry format: `### <id> <short title>` followed by requester, date, affected interface, the change,
the reason and the status.

## Open requests

### CR-1 Deployment file location for tests (WP3)

- Requester: WP1, 2026-10-12. Interface: `acb.chain.deploy.deploy_all`, `acb.chain.deployments`.
- Request: honour an optional environment variable `ACB_DEPLOYMENTS_DIR` (default `deployments/`) when
  reading and writing `deployments/<network>.json`. The `deployed` fixture in `tests/conftest.py` sets
  it to a temporary folder so that parallel test runs on different Anvil ports never overwrite each
  other's (or the repository's) `deployments/anvil.json`.
- Status: applied by WP3 (`acb.chain.deployments` honours `ACB_DEPLOYMENTS_DIR`). At integration the
  `fresh_deployed` fixture also restores the files in that folder after each test (I-1).

### CR-2 Nonce resync after `evm_revert` (WP3)

- Requester: WP1, 2026-10-12. Interface: `acb.chain.tx.TxSender` / `NonceManager`.
- Request: spec 12.2 says every `TxSender` created in a test resyncs its nonce after `evm_revert`. The
  `fresh_deployed` fixture only reverts the chain; please expose a way to resync (for example a
  module-level `resync_all()` the fixture can call) or resync on every "nonce too low" error.
- Status: applied by WP3: on Anvil the `NonceManager` compares its nonce with the pending count before
  every send, and `acb.chain.tx.resync_all()` exists.

## Applied (before the freeze)

### D-1 LLM key variable: `LLM_API_KEY`, with `OPENAI_API_KEY` as fallback

- Every package reads the LLM key as `LLM_API_KEY` (spec 8.2). The team's existing `.env` holds
  `OPENAI_API_KEY`; `acb.config.apply_llm_env_defaults` (run by `load_env_file`, the launcher and
  `llm_settings()`) copies it into `LLM_API_KEY` when that is empty **and** the provider is OpenAI
  (`LLM_BASE_URL` empty, or an `https` URL whose parsed host is exactly `api.openai.com`; never a
  substring match, so `https://api.openai.com.evil.example/v1` gets no key). Then an empty `LLM_BASE_URL` becomes
  `https://api.openai.com/v1` and an empty `LLM_MODEL` becomes `gpt-5.4-mini` (team default; the
  spec has no default model). A non-OpenAI `LLM_BASE_URL` (DeepSeek) never receives the OpenAI key.
- The wallet agent's allow-list (`LLM_*`) does not include `OPENAI_API_KEY`; the agent receives the
  key only as `LLM_API_KEY`. Use `acb.config.llm_settings()` to read the resolved settings.

### D-2 `ACB_MNEMONIC` stays in the team's `.env` for now

- `acb keys init --network base_sepolia` implements spec 13.1 exactly (it removes the `ACB_MNEMONIC`
  line). By team decision it has not been run against the real `.env` during the build; it is tested
  only on synthetic files in a temporary folder. Note: the existing `.env` line `ACB_MNEMONIC=...` is
  unquoted, so Foundry prints "Failed to load .env" until the value is double-quoted.

### D-3 Agent ids are strings on the wire

- Appendix B types agent ids as `int`. In Python they stay `int`; in JSON they serialise as decimal
  strings (`acb.types.AgentId`), as in the invoice (9.2), bundle (4.8), deployments (13.5) and event
  (10.3) examples and the rule "integers above 2^53 MUST be strings". Input accepts either form,
  except in signed wire objects (invoice, delivery), which accept only the canonical string form (D-12).
  `chain_id`, timestamps, block numbers, tiers and counters stay JSON integers.

### D-4 Strict scalar parsing

- `Amount` and `AgentId` accept an `int` or a decimal string only; floats, booleans, negative values
  and other strings are rejected. `Address`, `Hex32` and `HexStr` lowercase mixed-case (checksummed)
  input; anything else that is not lowercase `0x` hex of the right length is rejected.
- Exception (D-12): the signed wire parsers `Invoice.from_wire`, `SignedInvoice.from_wire` and
  `SignedDelivery.from_wire` accept only the canonical form. Their plain integer fields (`chainId`,
  `quantity`, `issuedAt`, `expiresAt`, `deliveryDeadline`, `deliveredAt`) are strict JSON integers
  (`acb.types.WireInt`: no bool, float or string).

### D-5 Wire helpers on the shared types

- `AcbModel.to_wire()` (camelCase JSON-ready dict). `Invoice.wire_dict()/from_wire()` and
  `SignedInvoice.wire_dict()/from_wire()` give exactly the `acb-invoice/1` object of 9.2 (with
  `schema`), so `invoice_hash(Invoice)` equals the hash of the raw wire dict. This holds because
  `from_wire` rejects every non-canonical input (D-12). `SignedDelivery` has the
  same helpers for `acb-delivery/1` (`unsigned_wire_dict()` is the signed content of 9.3).
  `Deployments.to_json_dict()/from_json_dict()` map the file layout of 13.5 (`schema`, `abi.sha`;
  unknown contract-entry keys such as `upgradeDelay`, `decimals`, `mock` go to `ContractEntry.extra`).
- `Bundle` has the field `schema_` (JSON `schema`, default `acb-evidence/1`) because `schema` shadows a
  pydantic method; `as_canonical()` returns the 4.8 JSON object.
- `NetworkProfile.load(network, environ=None)` and `NetworkProfile.explorer_tx_url(hash)`.
- Extra helpers: `grade_mask`, `mask_has`, `stricter`, `EVENT_TYPES`, schema-name constants.

### D-6 Canonical JSON details

- `canonical_bytes` accepts tuples (written as lists), rejects floats, non-string object keys and
  integers with `|n| > 2**53` with `TypeError`. Extra helpers: `keccak_hex`, `write_canonical`,
  `short_string_bytes32` / `short_string_from_bytes32` (bytes32 model ids, 0.1), and both name pairs
  of 15.2/15.3: `sign_digest_eip191` / `recover_eip191` and `sign_invoice` / `recover_invoice_signer`.
  Recovered addresses are lowercase.

### D-7 Detector `acb-inj-1.0.0`: tuning and precise definitions

The corpora were written first (`tests/fixtures/memos_benign.txt`, 92 memos; `memos_attack.txt`, 35
memos); the result is 0 adverse benign memos (24 of 92 quarantined) and 35 of 35 attack memos adverse.
Changes against the table of 6.4, all made while tuning:

- R4 window widened from `.{0,20}` to `.{0,30}` ("raise the per-transaction limit" was missed).
- Morse runs accept " / " word separators: `(?:[.\-]{1,5}(?:[ \t]*/[ \t]*|[ \t]+)){8,}`.
- R6 "pay verb" is defined as `pay|pays|paid|paying|payment|send|sends|sending|sent|transfer|transfers|
  transferring|transferred|wire|remit|withdraw|move|approve|sign` (the verbs of R3 and R5 plus their
  usual forms); an address must not be the prefix of a longer hex string (a 32-byte tx hash is not an
  address).
- R8 (adverse) flags the ranges listed in 6.4 plus U+061C (Arabic letter mark, a bidi control),
  U+2061-U+2064 (invisible operators) and U+FFF9-U+FFFB (interlinear annotation). Known consequence:
  emoji joined with U+200D (ZWJ) is adverse, as the spec's list already implies. Other invisible
  characters are *ignorable*: every other format character (Cf, for example the soft hyphen U+00AD and
  the Mongolian vowel separator U+180E), U+034F, U+115F-U+1160, U+17B4-U+17B5, U+180B-U+180F, U+3164,
  U+FE00-U+FE0F, U+FFA0 and U+E0100-U+E01EF. They are removed from the rule views (so they cannot
  split a keyword) but are not a hit, so honest text with a soft hyphen or an emoji variation selector
  is not adverse. U+2800 (Braille blank) is read as a space. (Review fix: an earlier build made every
  Cf character adverse and did not remove U+034F, variation selectors or Hangul fillers at all.)
- The rule view also removes combining marks (each character: NFKC, casefold, NFKD, marks dropped),
  so an accented `i` and the dotted capital `I` (U+0130, which casefolds to `i` + U+0307) read as plain
  letters. Inside a word that also contains a Latin letter, Cyrillic and Greek look-alike letters are
  read as their Latin twins; words written entirely in Cyrillic (Mongolian text) are left alone.
  Defence in depth only: provenance (signed invoices, the pinned directory) is the real control.
- R9 base64 also counts a run that decodes to text matching an instruction or action rule other than
  R7 (R1-R6, R11), not only a pay verb, and accepts the URL-safe alphabet; it runs on the NFKC view
  without casefolding (casefolding would corrupt base64). Base64 of "new price" or "api key" text is
  not adverse, as the plain text is not.
- R10 `.env` alternative written as `(?<![\w.])\.env\b` (the spec's `\b\.env` never matches after a
  space).
- Normalisation is per character (NFKC, then casefold) with an index map, so hit `start`/`end` index
  the **input** string (what the quarantine masks); hits of word-prefix patterns (`instruction`,
  `polic`) extend to the end of the word. Format characters are dropped from the normalised views, so
  `ig\u200bnore` still hits R1 (and R8).
- All rules run with `re.DOTALL` (an instruction split over lines still counts).
- `normalized_sha = chash({field: normalised text})`. Helper `invoice_fields(invoice)` returns the
  fields the detector must read from a wire invoice.

### D-8 Reason codes

- Categories (by range): `grade_adverse` 10xx, `grade_info` 11xx, `decision_ok` 20xx,
  `decision_refuse` 21xx, `decision_approval` 22xx, `decision_info` 23xx, `refund` 24xx,
  `autonomy_promote` 30xx, `autonomy_demote` 31xx, `autonomy_freeze` 32xx, `autonomy_unfreeze` 33xx,
  `signer_ok` 40xx, `signer_reject` 41xx, `tx` 42xx; retired codes have category `retired`.
- `acb.reason_codes.ReasonCode` is an `IntEnum` of every active code by name.

### D-9 Keys

- Role names are snake_case keys of `acb.keys.ROLE_INDEX` (`agent_signer`, `agent_signer_next`,
  `beacon_owner`, `echo1_vault_owner`, `sybil1` ...); `normalize_role` also accepts camelCase
  (`agentSigner`, as in `deployments.json`) and env-style names (`SIM_ATLAS`). Simulated agents use
  `SIM_<NAME>_PRIVATE_KEY`.
- `KeyRing.address(role)` returns the lowercase address (0.1); `KeyRing.checksum_address(role)` the
  checksummed one for UI, logs and web3 calls.
- `acb keys init` refuses when role keys are already present (`--force` replaces them), keeps existing
  service tokens, fills empty ones with 64 random hex characters, never writes Anvil-only roles, and
  validates the rewritten file with the strict parser. `--env-file` selects another file (tests).
- `assert_no_private_keys()` exits with code 78 and names the variables (never values). `acb` itself
  calls it first when `ACB_PROCESS_ROLE=agent`. The launcher also refuses to give the agent any
  `*_PRIVATE_KEY` / `*_MNEMONIC` variable even if a pattern would allow it.

### D-10 CLI, launcher and events conventions

- `register(subparsers)` adds commands and sets `parser.set_defaults(func=handler)`; `handler(args)`
  returns the exit code (`None` = 0). Placeholders for missing packages: chain (`chain`, `deploy`),
  bureau, autonomy, signer, agent (`agent`, `chat`), owner, sim, scenario (`scenario`, `attacks`,
  `evidence`). A package that raises something other than `ImportError` is shown as "failed to load".
- `acb up [--network] [--only a,b] [--wait]` starts `python -m acb <role> serve` for signer, bureau,
  autonomy, sim, agent and owner (in that order), waits for each `*_PORT`, and records the children in
  `data/<network>/launcher.json`; `acb down` reads it, sends CTRL_BREAK_EVENT, waits 5 s, terminates.
  Child output goes to `$ACB_OUT_DIR/<role>.out.txt`. `acb.launcher.Launcher` offers the same for the
  scenario runner (`start`, `restart(role, overrides)`, `stop`, `stop_all`); overrides must be
  allow-listed for the role.
- `GET /events?after=<seq>&limit=<n>&wait=<s>` returns a JSON list of envelopes (snake_case). It needs
  `Authorization: Bearer <EVENTS_TOKEN>`; 401 on a wrong token, 503 when the token is not configured.
  `EventBus.emit()` validates the event type against the list of 10.3; `seq` continues across restarts
  that reuse the same `ACB_OUT_DIR`.

### D-11 `.env.example` layout

- Same variables and defaults as 13.1, but every note is on its own line: python-dotenv reads
  `NAME=   # note` as the value `# note`. The strict parser (`acb.config.parse_env_text(strict=True)`)
  rejects that form and unquoted values with spaces; a test also loads the file with `forge config`.

### D-12 Review fixes to the WP1 interfaces (12 Oct)

- Signed wire objects are canonical only. `Invoice.from_wire`, `SignedInvoice.from_wire` and
  `SignedDelivery.from_wire` rebuild the wire dict from the parsed model and raise `ValueError` unless
  its canonical bytes equal those of the input (checksummed addresses, numeric amounts or agent ids,
  string or boolean integers, floats, snake_case keys and an upper-case signature are all rejected).
  Without this, a provider could sign a non-canonical invoice whose stored file, model hash and
  on-chain `invoiceHash` disagree, keeping a FAILED order out of its own grade. `SignedInvoice` also
  validates that `invoice_hash == invoice_hash(invoice)` when it is built directly. WP5 validation
  (V01/V07) should parse with these helpers and map the `ValueError` to its schema rejection.
- `recover_eip191` (and so `recover_invoice_signer`) rejects malleable signatures like OpenZeppelin
  `ECDSA.recover`: 65 bytes, `v` in {27, 28}, `s <= N/2`; otherwise `ValueError`. One signer has one
  valid signature per digest. `acb.canonical.SECP256K1_N` is exported.
- `AgentEntry.wallet` defaults to `None`, so the 13.5 example (no `wallet` for the wallet agent) parses.
- `acb up` records a start token (process creation time) for each child in `launcher.json`;
  `acb down` and the "already running" check act on a PID only when its token still matches, so a PID
  reused after a crash or reboot is reported as skipped and never signalled.
  `acb.launcher.process_start_token(pid)` is exported.
- The OpenAI key fallback parses `LLM_BASE_URL` (D-1). Detector changes are recorded in D-7.
- `pyproject.toml` lists the authors by name only (no e-mail addresses in the public repository).

## Integration (12 Oct)

Changes made by the integration step to files owned by other packages, each with its reason.

### I-1 `fresh_deployed` restores the deployments files (tests/conftest.py, WP1)

- The fixture reverted the chain but not `$ACB_DEPLOYMENTS_DIR/*.json`. A test that adds a vault with
  `deploy_extra_vault` (WP4 `test_unregistered_vault_is_ignored` adds `fake1`) left an entry for a
  contract the revert had removed, and three WP3 tests in `tests/chain/test_chain_deploy.py` failed when
  the whole suite ran in one session (each file passed alone). The fixture now saves the files before
  the test and restores them (and removes new ones) after the revert.

### I-2 `acb evidence export --dest <folder>` (src/acb/scenario/cli.py, WP9)

- `export()` already took `dest`; the CLI now exposes it, so a local Anvil run can be exported and
  verified in a scratch folder while `evidence/` stays reserved for the recorded testnet run.

### I-3 Base Sepolia funding fits the funded deployer (config/networks.yaml, src/acb/chain/deploy.py, WP1/WP3)

- The deployer holds about 0.0012 ETH and 20 test USDC. The old profile asked for `min_eth_deployer`
  0.002 ETH and 18 x 0.0005 ETH of top-ups, and the USDC plan was 21.00, so the recorded run would have
  stopped in S0. Measured L2 gas price (read-only, `scripts/gas_estimate.py`): 0.006 gwei, L1 fee about
  0; a 120k-gas call costs about 0.0000007 ETH.
- New `base_sepolia.funding`: `topup_eth` 0.00004 ETH (about 30 calls per key), `min_eth_service`
  0.00002 ETH, `min_eth_deployer` 0.0005 ETH, and the new key `usdc_bps: 9500`. Top-ups total 0.00072
  ETH for 18 keys, which leaves about 0.0005 ETH for the deployments.
- `default_funding_plan` reads `funding.usdc_bps` when `ACB_FUNDING_USDC_BPS` is unset (the variable
  still wins), so the plan is 14.25 + 5.70 = 19.95 USDC with no extra setting. Anvil is unchanged
  (21.00, no ETH top-ups). `tests/chain/test_chain_deploy.py::test_default_funding_plan` expects the new
  values. If the gas price rises before the run, raise `topup_eth` or fund the deployer.

### I-4 The runner's owner events go to `runner.jsonl` (src/acb/scenario/env.py, WP9)

- P1 (WP7) tails `$ACB_OUT_DIR/runner.jsonl` for the runner's events, but the runner's in-process owner
  console wrote them to `runnerowner.jsonl` (service `runnerowner`), so the console's "runner" feed
  stayed offline ("no file") and the owner's mandate, approval and unfreeze events of a scenario run
  never reached the panels. The runner's bus is now named `runner`. Checked by starting P1 on a kept
  scenario chain: the runner feed comes online and its events appear in the snapshot.

### I-5 Simulator tests no longer import `conftest` (tests/sim, WP8)

- `tests/sim/test_sim_provider.py` and `test_sim_service.py` did `from conftest import make_fake_sim`.
  With pytest's default import mode every test folder's `conftest.py` is the module `conftest`, so
  when `tests/agent` was collected first the import found the agent conftest and collection failed
  (seen with `pytest tests/sim tests/agent ...`). `FakeSim` and `make_fake_sim` moved to
  `tests/sim/sim_helpers.py`; the sim conftest and both tests import them from there.

### I-6 Clarification options are trimmed to five (src/acb/agent/tools.py, WP6)

- In a real-LLM S5 run, gpt-5.4-mini once called `ask_clarification` with six options (every provider
  plus "All of them"). `parse_call` rejected the whole call (`options: too_long`), the owner saw "I could
  not use that request" and the dialogue needed an extra turn. The options are display-only
  suggestions, so `parse_call` now keeps the first five (`MAX_CLARIFY_OPTIONS`) before validation; the
  raw call is kept unchanged in `ParsedCall.raw_args`. Nothing else is trimmed: every other argument of
  every tool (amounts, payees, invoice decisions) is still validated strictly, and an option longer than
  80 characters still makes the call invalid. Test: `tests/agent/test_agent_tools.py::
  test_clarification_options_are_trimmed_to_five`.

### I-7 A50 counts only reports about the named competitor (src/acb/scenario/attacks.py, WP9)

- In an integration run of `acb attacks run --network anvil`, A50 ("report Atlas Data as NON_DELIVERY")
  was counted Failed: in the same seconds the wallet agent's own timer filed an evidence-backed
  NON_DELIVERY report on Nova Labs (the refund of the S9 escrow), and the check counted any agent
  report and any agent-key `giveFeedback`. The message itself was ignored as it should be. The checks
  now count only `report` events whose subject is Atlas and `giveFeedback` transactions whose
  `NewFeedback.agentId` is Atlas, which is what the attack tries to cause.

### I-8 Owner-turn prompt: short payee names and a correction before any draft (src/acb/agent/prompts.py, WP6)

- With the real LLM (gpt-5.4-mini), S5 needed 4 owner turns: after "Use all five: Atlas, Slowpoke,
  Nova, Orion, and Ghost" the model asked again for the full directory names, and the correction "make
  the total 4 USDC" then became `amend_mandate` with an invented draft id. Two lines were added to the
  owner task (8.4): short names are fine because code resolves them against the pinned directory
  (`Directory.resolve` already accepts keys such as "atlas") and shows the owner the result; a change
  with no draft yet becomes `draft_mandate` with the change applied. `PROMPT_VERSION` is unchanged,
  `prompt_hash()` changes. The dialogue evaluation (`pytest -m llm tests/llm`, 33 cases) is unchanged:
  every rate 1.0000, fallback 0.

## Package deviations recorded at integration

The packages asked WP1 to record these; the reports of each package have the full text.

- WP2: `postGrade` stores the baseline in effect (a pending upgrade carries its own `prevGrade` /
  `prevPostedAt` forward and a repost restarts the delay; `GradePosted.previousGrade` stays raw);
  `executeApproved` reverts `BadDeadline()` for an escrow at or after its `escrowDeadline`; the vault
  invariant handler is split into `VaultHandler`, `VaultAdminHandler`, `VaultGhost` and
  `VaultHandlerBase` (EIP-170); the rolling window counts a spend while `ts > now - windowSeconds`;
  violation points accumulate at tier 0 (severity 2 included); `DeliveryClaimed` is a file-level error;
  `DisputeWindowOpen(readyAt)` is capped at `uint64` max.
- WP3: `TxResult.revert_error` is `Name(args)`; a simulation revert raises `TxSimulationError`; `acb
  deploy all --network base_sepolia` needs `--yes`; `ACB_FUNDING_USDC_BPS` (and now `usdc_bps`, I-3)
  scales the USDC plan; `ACB_CHAIN_STATE_FILE` holds `acb chain up` state; echo vaults are deployed on
  Anvil; Beacon's autonomy operator is the AUTONOMY key.
- WP4: the F6 spike key is `spike.min_prior_orders: 4` (spec 13.3 `min_orders: 6`, same behaviour);
  `score()` takes `ChainFacts`; bundles carry `model.anchors`; autonomy rule 2 binds each outflow to
  the signer entry's orderId, kind and txHash, never matches a `DecisionRecorded` hash, waits
  `decision_tx_grace_s` (300) for a txHash, and exempts only outflows covered by an operator or owner
  `freeze(3205)`; first postings only for deployment-listed agents or agents with evidence.
- WP5: `decide(..., owner_approval)`; `validate_invoice(..., check_signature, check_expiry,
  check_quote_open)`; `classify(..., cfg, active_mandate_payees, agent_decision, payee_wallet)` (state
  changes between the two passes are not LIMIT/DUPLICATE/FROZEN violations; a payee wallet address in
  counterparty text counts for INJECTION_FOLLOWED); C14 also applies to the executable `PAY_CAPPED` of
  a cap re-quote; unsecured exposure keeps FAILED orders; `GET /v1/decisions/{hash}` adds `outcome`;
  2115 replaces 2301 in the signer response; extra `policy.yaml` keys; `ACB_SIGNER_STATE_DIR`.
- WP6: invoice turns never end undecided (a code-built REFUSE or REQUEST_OWNER_APPROVAL, source
  `fallback`); lost signer answers are reconciled (`in_flight` with `unknownAt`, then `interrupted`);
  `acb chat` confirms in two steps (card code, then the code of the chain-verified screen);
  `approval_from_card` requires a pinned issuer, our buyer fields, an unexpired invoice and the vault's
  `DecisionRecorded(NEEDS_OWNER_APPROVAL)`; `OPENAI_API_KEY` is an exact redactor secret; decision
  events carry `ownerReply` for refused injected invoices.
- WP7: `POST /api/mandates/{draftId}/prepare` and `/api/approvals/{orderId}/prepare` return a
  single-use `prepareId` that `/confirm` and `/approve` take; extra endpoints `/api/session`, `/logout`,
  `/health`, `/events`, `/stream?once=1`, `/api/actions/{id}/submitted`; `purposeHash =
  chash({"purpose": purpose})`; `acb owner serve` refuses to start without `OWNER_UI_PASSWORD` and
  `SESSION_SECRET`; P1 tails `$ACB_OUT_DIR/runner.jsonl`.
- WP8: inbox messages use `acb-message/1` (`{schema, messageId, fromAgentId, toAgentId, sentAt, text,
  signature}`, EIP-191 over chash of the message without `signature`, posted to `/inbox/message`);
  delivery payloads per sku: `fx-*` `{pair, rate, seq}`, `api-*` `"demo-key-<n>"`, `ad-slot`
  `"ad-slot-<n>"`, others `"<sku>-<n>"`; extra admin actions `push_message`, `set_override`, `status`,
  `dust_smear`.
- WP9: default `--llm` is mock on Anvil; S11 pauses the wallet agent during the market round; S20's
  appeal review runs in the runner with the bureau key (P4 has no appeal API); one local run per
  network (`data/<network>/run.lock`); evidence adds `decisions/`, `appeals/` and
  `scorecard/archive/`.
