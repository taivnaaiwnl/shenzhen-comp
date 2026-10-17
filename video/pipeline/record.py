"""Capture C1: the owner console while the scenario runs (video script, section 3).

Runs the scenario in this process (the same ``Env`` and ``Runner`` as ``acb scenario run``), signs in to
the owner console P1 with the run's own console password (never typed on screen, never written to disk)
and captures the page in a real Chromium through Playwright:

- page A: 1920x1080 viewport, device scale factor 2 (the look of the console on a 1080p screen);
- page B: 2400x2000 viewport, device scale factor 1.5: each panel is scrolled to the top of the console's own
  scroll box and captured on its own, so tall panels (decision log, grades) can be cropped in post.

At the end of every step the runner waits until both pages have been captured (screenshots plus the
bounding boxes and text of the console's selectors and table rows); during the steps listed in
``TICK_STEPS`` page A is also captured about twice a second, so chat bubbles and the banner change can be
shown as they happen. The console is never clicked or edited: captures are read-only.

After the run it exports the curated evidence into the capture folder, runs the real recompute command
for Ghost's S11 grade while the chain is still up, and runs the offline verifier on the exported evidence.
Output: ``video/work/captures/<run_id>/`` with ``manifest.json``, ``frames/``, ``evidence/<network>/`` and
``term/`` (command outputs for the terminal pages).

    uv run --with playwright python video/pipeline/record.py --network anvil
    uv run --with playwright python video/pipeline/record.py --network base_sepolia --yes   # recorded run

Base Sepolia needs ``--yes`` (it sends real testnet transactions, as ``acb scenario run`` does).
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
WORK = REPO / "video" / "work"

TICK_STEPS = {"S5": 0.45, "S15": 0.6, "S17": 1.0, "S18": 0.8}
SETTLE_S = 3.0  # time the console needs to apply the step's events and reload its snapshot

SELECTORS = [
    "#banner",
    "#banner-network",
    "#banner-llm",
    "#banner-owner",
    "#banner-fallback",
    "#chat",
    "#chat-log",
    "#panels",
    "#panel-wallet",
    "#wallet-body",
    "#wallet-actions",
    "#panel-grades",
    "#panel-decisions",
    "#panel-autonomy",
    "#panel-messages",
]

BOXES_JS = """(sels) => {
  const box = (e) => { const r = e.getBoundingClientRect();
    return [r.x + window.scrollX, r.y + window.scrollY, r.width, r.height]; };
  const out = {sel: {}, text: {}};
  for (const s of sels) {
    const e = document.querySelector(s);
    if (e && !e.hidden && e.getClientRects().length) { out.sel[s] = box(e); out.text[s] = (e.innerText || '').slice(0, 400); }
  }
  const rows = (q) => [...document.querySelectorAll(q)].map(e => ({box: box(e), text: (e.innerText || '').slice(0, 600), cls: e.className || ''}));
  out.decisionHead = rows('#decisions-body thead');
  out.decisionRows = rows('#decisions-body tbody tr');
  out.gradeHead = rows('#grades-body thead');
  out.gradeRows = rows('#grades-body tbody tr');
  out.autonomyItems = rows('#autonomy-body > div, #autonomy-body li');
  out.walletParts = rows('#wallet-body > *, #wallet-actions > *');
  out.chatItems = rows('#chat-log > *');
  const log = document.querySelector('#chat-log');
  out.chatScroll = log ? [log.scrollTop, log.scrollHeight, log.clientHeight] : null;
  // the parts of the agent's chat messages a narration cue names (ids, grades, ages, payout wallets): their
  // on-screen rectangles, so a still can highlight them
  const marks = [];
  const pats = [['id', /#\\d+/g], ['grade', /grade [A-Z]+/g], ['age', /registered [^,;)]*? ago/g],
                ['wallet', /wallet 0x[0-9a-fA-F]+(?:\\.\\.\\.|\\u2026)[0-9a-fA-F]+/g]];
  document.querySelectorAll('#chat-log > .msg-agent').forEach((item, i) => {
    const walker = document.createTreeWalker(item, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode()) && marks.length < 200) {
      for (const [kind, re] of pats) {
        re.lastIndex = 0;
        let m;
        while ((m = re.exec(node.data)) && marks.length < 200) {
          const r = document.createRange();
          r.setStart(node, m.index); r.setEnd(node, m.index + m[0].length);
          const rects = [...r.getClientRects()].filter(q => q.width > 0)
            .map(q => [q.x + window.scrollX, q.y + window.scrollY, q.width, q.height]);
          marks.push({item: i, kind, text: m[0], rects});
        }
      }
    }
  });
  out.chatMarks = marks;
  return out;
}"""

SCROLL_JS = """([s, offset]) => {
  const e = document.querySelector(s); if (!e) return false;
  e.scrollIntoView({block: 'start', inline: 'nearest'});
  if (offset > 0) {
    let p = e.parentElement;
    while (p && p !== document.body) {
      const st = getComputedStyle(p);
      if (/(auto|scroll)/.test(st.overflowY) && p.scrollHeight > p.clientHeight) break;
      p = p.parentElement;
    }
    (p && p !== document.body ? p : document.scrollingElement).scrollTop += offset;
  }
  return true;
}"""


def log(text: str) -> None:
    sys.stdout.write(f"[record {time.strftime('%H:%M:%S')}] {text}\n")
    sys.stdout.flush()


class Capturer(threading.Thread):
    """Owns Playwright (sync API, one thread). The runner thread talks to it through a queue."""

    def __init__(self, env: Any, out: Path):
        super().__init__(daemon=True)
        self.env = env
        self.out = out
        self.frames = out / "frames"
        self.frames.mkdir(parents=True, exist_ok=True)
        self.q: queue.Queue = queue.Queue()
        self.manifest: list[dict[str, Any]] = []
        self.current: str | None = None
        self.ready = False
        self.error: str | None = None
        self.seq = 0

    # ---- called from the runner thread
    def step_begin(self, sid: str) -> None:
        self.current = sid
        self.manifest.append({"kind": "begin", "step": sid, "t": time.time()})

    def step_end(self, sid: str, ok: bool) -> None:
        self.current = None
        done = threading.Event()
        self.q.put(("end", sid, ok, done))
        done.wait(timeout=60)

    def close(self) -> None:
        done = threading.Event()
        self.q.put(("stop", None, None, done))
        done.wait(timeout=30)

    # ---- capture thread
    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001 - reported in the manifest, the run itself goes on
            self.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-2000:]}"
            log(f"capture thread failed: {self.error}")
            # drain so the runner never blocks
            while True:
                try:
                    item = self.q.get(timeout=1)
                except queue.Empty:
                    continue
                item[3].set()
                if item[0] == "stop":
                    return

    def _wait_console(self) -> str:
        import httpx

        base = f"http://127.0.0.1:{self.env.ports['owner']}"
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                item = None
            if item is not None:
                if item[0] == "stop":
                    item[3].set()
                    raise SystemExit
                # a step ended before the console was up: nothing to capture yet
                self.manifest.append(
                    {"kind": "end", "step": item[1], "ok": item[2], "t": time.time(), "captured": False}
                )
                item[3].set()
            try:
                if httpx.get(base + "/health", timeout=1.5).status_code == 200:
                    return base
            except Exception:  # noqa: BLE001, S110 - not up yet
                pass
            time.sleep(0.5)

    def _run(self) -> None:
        from playwright.sync_api import sync_playwright

        try:
            base = self._wait_console()
        except SystemExit:
            return
        log(f"owner console is up at {base}")
        password = self.env.tokens["OWNER_UI_PASSWORD"]
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            pages = {}
            for name, (w, h, dsf) in {"A": (1920, 1080, 2), "B": VIEW_B}.items():
                ctx = browser.new_context(
                    viewport={"width": w, "height": h}, device_scale_factor=dsf, color_scheme="light", locale="en-US"
                )
                r = ctx.request.post(
                    base + "/login",
                    data=json.dumps({"password": password}),
                    headers={"Content-Type": "application/json"},
                )
                if not r.ok:
                    raise RuntimeError(f"console sign-in failed: HTTP {r.status}")
                page = ctx.new_page()
                page.goto(base + "/", wait_until="domcontentloaded")
                page.wait_for_selector("#main:not([hidden])", timeout=30000)
                pages[name] = page
            del password
            self.ready = True
            log("signed in on pages A and B")
            last_tick = 0.0
            while True:
                try:
                    item = self.q.get(timeout=0.1)
                except queue.Empty:
                    item = None
                if item is not None:
                    kind, sid, ok, done = item
                    if kind == "stop":
                        done.set()
                        break
                    time.sleep(SETTLE_S)
                    self._settle(pages["A"])
                    entry ={"kind": "end", "step": sid, "ok": ok, "t": time.time(), "captured": True}
                    entry.update(self._shot(pages["A"], f"{sid}_end_A", "A", quality=88))
                    entry.update(self._shot(pages["B"], f"{sid}_end_B", "B", quality=88))
                    self.manifest.append(entry)
                    done.set()
                    continue
                sid = self.current
                period = TICK_STEPS.get(sid or "")
                if period and time.time() - last_tick >= period:
                    last_tick = time.time()
                    self.seq += 1
                    entry = {"kind": "tick", "step": sid, "t": time.time()}
                    entry.update(self._shot(pages["A"], f"{sid}_tick{self.seq:04d}_A", "A", quality=85))
                    if sid in ("S17", "S18"):
                        entry.update(self._shot(pages["B"], f"{sid}_tick{self.seq:04d}_B", "B", quality=85))
                    self.manifest.append(entry)
            browser.close()

    def _shot(self, page: Any, name: str, which: str, quality: int) -> dict[str, Any]:
        t0 = time.time()
        if which == "A":
            boxes = page.evaluate(BOXES_JS, SELECTORS)
            path = self.frames / f"{name}.jpg"
            page.screenshot(path=str(path), type="jpeg", quality=quality, full_page=False)
            return {"fileA": path.relative_to(self.out).as_posix(), "boxesA": boxes, "tA": t0}
        # page B: each panel scrolled to the top of its scroll box and captured on its own (the panels
        # column scrolls inside the page, so a single screenshot cannot hold them all)
        panels = {}
        vw, vh = VIEW_B[0], VIEW_B[1]
        for sel in PANELS_B:
            # a panel taller than the viewport is captured in overlapping slices ("#panel-grades@1", ...), so
            # every table row is whole in at least one of them
            for k in range(MAX_SLICES):
                ok = page.evaluate(SCROLL_JS, [sel, k * SLICE_STEP])
                if not ok:
                    break
                boxes = page.evaluate(BOXES_JS, SELECTORS)
                b = boxes["sel"].get(sel)
                if not b:
                    break
                scroll = window_scroll(page)
                top = b[1] - scroll
                x, y = max(0.0, b[0]), max(0.0, top)
                clip = {"x": x, "y": y, "width": min(b[2], vw - x), "height": min(top + b[3], vh) - y}
                if clip["width"] < 10 or clip["height"] < 10:
                    break
                key = sel if k == 0 else f"{sel}@{k}"
                path = self.frames / f"{name}{key.replace('#', '_').replace('@', '_s')}.jpg"
                page.screenshot(path=str(path), type="jpeg", quality=quality, clip=clip)
                panels[key] = {
                    "file": path.relative_to(self.out).as_posix(),
                    "clip": [clip["x"], clip["y"] + scroll, clip["width"], clip["height"]],
                    "boxes": boxes,
                }
                if top + b[3] <= vh:  # the rest of the panel is on screen
                    break
            page.evaluate(SCROLL_JS, [sel, 0])
        return {"panelsB": panels, "tB": t0, "fileB": True}

    def _settle(self, page: Any) -> None:
        """Wait (at most ``SETTLE_MAX_S``) until the banner lists no feed as offline."""
        stop = time.time() + SETTLE_MAX_S
        while time.time() < stop:
            text = page.evaluate("() => (document.querySelector('#banner') || {}).innerText || ''")
            if "(offline)" not in text:
                return
            time.sleep(0.5)


VIEW_B = (2400, 2000, 1.5)
MAX_SLICES = 4
SLICE_STEP = 1400  # CSS px per slice (600 px overlap at a 2000 px viewport: rows are at most ~400 px tall)
SETTLE_MAX_S = 8.0
PANELS_B = ["#panel-wallet", "#panel-grades", "#panel-decisions", "#panel-autonomy", "#chat-log"]


def window_scroll(page: Any) -> float:
    return float(page.evaluate("() => window.scrollY"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--network", choices=("anvil", "base_sepolia"), default="anvil")
    ap.add_argument("--llm", choices=("mock", "openai", "auto"), default=None)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--to", default="S21")
    ap.add_argument("--yes", action="store_true", help="Base Sepolia: really send the run's transactions")
    args = ap.parse_args()

    from acb import config
    from acb.scenario.env import Env, EnvError, new_run_id
    from acb.scenario.runner import Runner

    network = args.network
    llm = args.llm or ("mock" if network == "anvil" else "auto")
    if network != "anvil" or llm != "mock":
        env_path = config.REPO_ROOT / ".env"
        if env_path.is_file():
            config.load_env_file(env_path)
    if network != "anvil" and not args.yes:
        log("Base Sepolia sends real testnet transactions: add --yes")
        return 2
    run_id = args.run_id or new_run_id()
    out = WORK / "captures" / run_id
    out.mkdir(parents=True, exist_ok=True)
    env = Env.create(network, run_id=run_id, llm_mode=llm, fixed_ports=False, with_owner_ui=True)
    cap = Capturer(env, out)

    class RecordingRunner(Runner):
        def begin(self, sid: str):  # type: ignore[override]
            ctx = super().begin(sid)
            cap.step_begin(sid)
            return ctx

        def finish(self, ctx):  # type: ignore[override]
            ok = super().finish(ctx)
            cap.step_end(ctx.id, ok)
            return ok

    state_path = config.REPO_ROOT / "runs" / run_id / "state.json"
    runner = RecordingRunner(env, state_path=state_path)
    cap.start()
    started = time.time()
    ok = False
    extra: dict[str, Any] = {}
    try:
        log(f"run {run_id} on {network} (llm {llm}); captures in {out}")
        ok = runner.run("S0", args.to)
        extra = after_run(runner, out, network)
    except KeyboardInterrupt:
        log("interrupted")
    except EnvError as exc:
        log(f"environment error: {exc}")
    finally:
        try:
            cap.close()
        finally:
            runner.save_state()
            env.stop(keep_chain=False)
    manifest = {
        "schema": "acb-video-capture/1",
        "runId": run_id,
        "network": network,
        "chainId": 31337 if network == "anvil" else 84532,
        "llm": llm,
        "result": "PASSED" if ok else "FAILED",
        "startedAt": started,
        "finishedAt": time.time(),
        "steps": {
            sid: {k: e.get(k) for k in ("status", "startedAt", "finishedAt", "seconds")}
            for sid, e in (runner.state.get("steps") or {}).items()
        },
        "captureError": cap.error,
        "viewports": {"A": [1920, 1080, 2], "B": list(VIEW_B)},
        "events": cap.manifest,
        **extra,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    log(
        f"result {manifest['result']}; {sum(1 for e in cap.manifest if e.get('fileA'))} frames; manifest {out / 'manifest.json'}"
    )
    return 0 if ok and not cap.error else 1


def after_run(runner: Any, out: Path, network: str) -> dict[str, Any]:
    """Export the evidence, run the recompute CLI while the chain is up, run the offline verifier."""
    from acb.scenario.evidence import EvidenceError, export

    env = runner.env
    result: dict[str, Any] = {}
    term = out / "term"
    term.mkdir(exist_ok=True)
    ev_dir = out / "evidence" / network
    try:
        res = export(env.run_id, network, dest=ev_dir)
        result["export"] = {"files": res["files"], "transactions": res["transactions"], "dest": str(ev_dir)}
        log(f"exported {res['files']} evidence files to {ev_dir}")
    except EvidenceError as exc:
        result["export"] = {"error": str(exc)}
        log(f"evidence export failed: {exc}")
        return result
    dep_file = REPO / "deployments" / f"{network}.json"
    if dep_file.is_file():  # the registry reads role addresses and agent ids from it
        (out / "deployments").mkdir(exist_ok=True)
        (out / "deployments" / dep_file.name).write_bytes(dep_file.read_bytes())
    evidence = json.loads((ev_dir / "evidence.json").read_text(encoding="utf-8"))
    ghost = next(
        (r for r in evidence.get("recompute") or [] if r.get("agent") == "ghost" and r.get("step") == "S11"), None
    )
    child_env = {k: v for k, v in os.environ.items()}
    if env.rpc_url:
        child_env["ANVIL_RPC_URL" if network == "anvil" else "BASE_SEPOLIA_RPC_URL"] = env.rpc_url
    child_env["NETWORK"] = network
    child_env["PYTHONIOENCODING"] = "utf-8"
    if ghost:
        cmd = ["acb", "bureau", "recompute", "--network", network, "--tx", ghost["tx"]]
        extra = ["--evidence-base", str(env.out_dir) + os.sep] if network == "anvil" else []
        proc = subprocess.run(
            [sys.executable, "-m", *cmd, *extra],
            cwd=REPO,
            env=child_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=240,
            check=False,
        )
        shown = "uv run " + " ".join(cmd) + (" --evidence-base runs/" + env.run_id + "/out/anvil/" if extra else "")
        (term / "recompute.json").write_text(
            json.dumps(
                {
                    "command": shown,
                    "exit": proc.returncode,
                    "stdout": proc.stdout,
                    "stderr": proc.stderr[-3000:],
                },
                indent=1,
            ),
            encoding="utf-8",
        )
        log(f"recompute exit {proc.returncode}")
    verifier = REPO / "scripts" / "verify_evidence.py"
    proc = subprocess.run(
        [sys.executable, str(verifier), str(ev_dir / "evidence.json"), "--receipts-only"],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=240,
    )
    (term / "verify.json").write_text(
        json.dumps(
            {
                "command": f"python scripts/verify_evidence.py evidence/{network}/evidence.json --receipts-only",
                "exit": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr[-3000:],
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    log(f"verify exit {proc.returncode}")
    return result


if __name__ == "__main__":
    sys.exit(main())
