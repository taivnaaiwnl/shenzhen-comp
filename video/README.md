# Demo video pipeline

Builds `InternationalAI-TeamMongolia-DemoVideo.mp4` (16:9, 1920x1080, 30 fps, H.264 + AAC, English
narration, English subtitles burned in, with the same subtitles as an `.srt` file next to the video, 4:57) from
`docs/deliverables/video-script.md` and `docs/deliverables/video-narration.srt`. Everything is scripted:
a new run gives a new video without hand edits.

## Re-render from the Base Sepolia run (one command)

After the recorded run has been exported to `evidence/base_sepolia/` (`acb evidence export --run-id <id>
--network base_sepolia`) and the suites to `evidence/attack_suite.json` and `evidence/benign_suite.json`:

```powershell
uv run --with playwright --with imageio-ffmpeg python video/make_video.py --network base_sepolia
```

This takes every hash, value and explorer page from the Base Sepolia evidence. The console shots come
from a console capture of that same run if one exists (see below); otherwise from the newest local Anvil
capture, and the video then says so on every console shot (fallback F1 of the script: "Console recorded
on a local Anvil run of the same scenario. Hashes and explorer pages: Base Sepolia run ..."). Explorer
pages are screenshots of `https://base-sepolia.blockscout.com/tx/<hash>` (Basescan, tried second, answers a
headless browser with a Cloudflare bot check, which the pipeline never tries to pass); a page that does not
show the transaction is replaced by the saved receipt with its explorer URLs (fallback F3, listed under
`notes` in the report; it does not fail `--strict`). Add `--strict` for the
submission build: it fails on any unresolved value or on a narration clip that does not fit its cue.

To capture the console during the recorded run itself, run the scenario through the recorder instead of
`acb scenario run` (same runner, same steps, plus a Chromium that watches the owner console):

```powershell
uv run --with playwright python video/pipeline/record.py --network base_sepolia --yes
uv run acb evidence export --run-id <run_id> --network base_sepolia
uv run --with playwright --with imageio-ffmpeg python video/make_video.py --network base_sepolia
```

## Local test render

```powershell
uv run --with playwright python -m playwright install chromium     # once
uv run --with playwright --with imageio-ffmpeg python video/make_video.py --network anvil --record
```

`--record` runs the whole scenario on a fresh local Anvil chain (about 8 minutes, one local run at a
time) while capturing the console; without it the newest passed capture is reused. The local render is
labelled on screen as a local test run: the title card, every console shot ("LOCAL TEST RUN on Anvil
(chain 31337)"), every page made from the run's files (invoices, terminal pages, proposal and check
cards) and every transaction card ("the transaction's saved receipt, not an explorer page"). Cue 5 says
"in a local test run of the same scenario" instead of "recorded during our Base Sepolia run". Local renders
take the newest complete `attack_suite.json` and `benign_suite.json` under `runs/*/out/anvil/` (or
`--attack-suite`, `--benign-suite`; these also work with `--network base_sepolia`, through a private copy of
the evidence). A suite that ran before the last change to `src/acb`, `contracts/src` or `config/*.yaml`
(except `networks.yaml`) is reported as a problem and fails `--strict`: re-run it with `acb attacks run` /
`acb attacks benign`.

## What the pipeline does

| Step | File | What |
|---|---|---|
| 1. Capture | `pipeline/record.py` | Runs the scenario in-process (`Env`, `Runner` of `acb scenario run`), signs in to the owner console with the run's own password (never on screen, never on disk), and captures it in Chromium with Playwright: page A at 1920x1080 (device scale 2) at the end of every step and about twice a second during S5, S15, S17 and S18; page B (2400x2000, scale 1.5) panel by panel at the end of every step, a panel taller than the viewport in overlapping slices (`#panel-grades@1`, ...) so every table row is whole in one of them. Before each end-of-step capture it waits (at most 8 s) until the banner lists no feed as offline. It records the on-screen rectangles of the ids, grades, registration ages and payout wallets in the agent's chat messages, so a still can mark them. It logs the bounding boxes and text of the console selectors and table rows for every capture, never clicks or types in the console, and after the run exports the evidence, runs `acb bureau recompute` for Ghost's S11 grade while the chain is up and runs `scripts/verify_evidence.py --receipts-only`. Output: `work/captures/<run_id>/` |
| 2. Values | `pipeline/values.py` | Every measured value comes from `docs/deliverables/placeholders.md` through `scripts/render_deliverables.py` (same extraction, formats and checks). For a local run the renderer reads a private copy of the run's evidence under `work/`; nothing outside `video/` is written |
| 3. Cues | `pipeline/build.py` | Renders the SRT. Fallback wordings of the script apply when a precondition does not hold: F1 (cue 5), P2 (cue 13 when S5 ran on the MockLLM), F2 (cues 30-32: request R1, the real-LLM rehearsal, does not exist; cue 31 says what the screen shows: "Instructions are masked; only the signed amount is payable."). Cue 52 names the two counts the screen shows (transactions and grade files, both from the registry) when every item verified. The shorter wordings of section 6 are used only when a clip is too long. Subtitles keep the script's own line breaks; a changed wording is broken at a phrase boundary |
| 4. Frames | `pipeline/pages.py`, `pipeline/build.py` | The shot list of section 5 as an edit list: each segment is composed as HTML (crop and highlight of a capture, or a card, receipt, terminal or table page) with the honesty labels of section 2 in the top band and the overlay text as a caption, then rendered to PNG |
| 5. Narration | `pipeline/tts.py` | One OpenAI text-to-speech clip per cue (`tts-1-hd`, voice `sage`, speed 1.0), cached in `work/tts/` so a re-render only pays for changed cues, trimmed of leading and trailing silence and placed at the cue start. The subtitle keeps the written form; the voice gets a few spoken forms ("0.80" as "zero point eight zero", "2108" as "twenty-one oh eight"). A clip that is still too long after the shorter wording is sped up by at most x1.15 (the report lists it; `--strict` fails instead). `gpt-4o-mini-tts` was tried and read too slowly for the cues. `OPENAI_API_KEY` is read from `.env`, never printed. The title card says the narration is an AI-generated voice |
| 6. Encode and check | `pipeline/build.py` | ffmpeg from the `imageio-ffmpeg` wheel: subtitles burned in (Arial 46 px, white on a dark box, bottom centre, in a band the frames keep free); `libx264 -crf 18`, loudness-normalised AAC LC mono 48 kHz at a 160k target (the native encoder averages about 120 kb/s on mono speech; the report gives the measured rate), `+faststart`, cut at 4:57. No soft track inside the MP4 (the muxer would mark it default and some players would show the subtitles twice); the rendered `InternationalAI-TeamMongolia-DemoVideo.srt` is written next to the video. Checks duration, size, fps, audio, no `{{` left. Writes `work/build-<run>-<network>/editlist.json` and `report.json` |

## Shots that differ from the script, and why

| Shot | Script | This pipeline |
|---|---|---|
| 03-13 console | C1 recorded during the Base Sepolia run, sped up where marked | End-of-step stills of the console (and stills about twice a second in S5, shown time-scaled with the factor on screen) |
| 04-16 explorer | C3 Blockscout or Basescan page | Blockscout screenshot on Base Sepolia (Basescan second); the saved receipt, decoded with the contract ABIs, on Anvil or when no page shows the transaction (F3). Receipt times are chain time; on Anvil a note says the chain clock is fast-forwarded past escrow deadlines, so it runs ahead of the console's wall clock |
| 04 cue 9, 11 | Console | Cue 9: the same still with the ids, grades, ages and wallets marked. Cue 11: the agent's mandate draft card with the console's own Review, then the registered mandate |
| 09 cue 24 | Console | Ghost's grade row before (last capture showing its S4 score) and after S11, stacked |
| 10 | C2 real-LLM rehearsal (request R1) | F2: the invoice with the spans the injection detector flagged, the agent's own `wrap()` output with what masking removed highlighted, and the policy's own checks on that invoice (2105, 2108, REFUSE) from the S15 decision record, labelled "No model shown" |
| 11, 12 | Console | The scripted model's actual proposal (3.00 USDC to Orion; all funds to #11) from the step's violation record, beside the signed invoice and the signer's REFUSE; the decision log's reason text is labelled as the wallet's fixed refusal reply |
| 13 cue 44 | Console | The approval card in the chat (the console's own Review), then the approval and the payment transactions |
| 14 | C6 console during attacks A32 and A10 | F4: the A32 and A10 results from the attack-suite file, labelled; the console replay is not recorded |
| 15 | Newest `evidence/attack_suite.json` | Same on Base Sepolia (or `--attack-suite`, `--benign-suite`). A local render uses the newest complete local suite files. Both suites' run ids and times are on screen, each Failed attack gets one line (what it is, the check it did not meet, funds lost), and the verifier output is trimmed with counted cuts and names each grade file |

## Files

- `make_video.py`: the one command (optionally records first, then builds).
- `pipeline/record.py`, `pipeline/build.py`, `pipeline/pages.py`, `pipeline/values.py`, `pipeline/tts.py`.
- `InternationalAI-TeamMongolia-DemoVideo.mp4` and `.srt`: the latest render (the local test render until
  the Base Sepolia run exists).
- `work/`: captures, frames, narration clips, edit list and report (git-ignored, regenerated).
