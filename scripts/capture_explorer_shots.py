"""Blockscout screenshots of four transactions of the recorded Base Sepolia run, for the execution evidence.

Reads ``evidence/base_sepolia/evidence.json``, opens each transaction's Blockscout page in headless Chromium
(Playwright) and saves it to ``evidence/base_sepolia/screenshots/``. The pages are the public explorer's
view of the recorded transactions, captured after the run; nothing is sent. A page that does not show the
transaction (rate limit, explorer down) stops the script without writing a stale file. Basescan answers a
headless browser with a bot check, which this script never tries to pass.

    uv run --with playwright python scripts/capture_explorer_shots.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence" / "base_sepolia" / "evidence.json"
OUT = ROOT / "evidence" / "base_sepolia" / "screenshots"

# (file name, step, function, text the transaction's log line must contain). The Details tab: the contracts are not
# source-verified on Blockscout, so its Logs tab shows raw topics; the decoded events are in the saved receipts
SHOTS = (
    ("s05-register-mandate-blockscout.png", "S5", "registerMandate", ""),
    ("s12-refusal-blockscout.png", "S12", "recordDecision", ""),
    ("s13-stolen-key-revert-blockscout.png", "S13", "pay", "GradeNotAllowed"),
    ("s16-freeze-blockscout.png", "S16", "recordViolation", ""),
)


# Blockscout shows third-party ads. Only the explorer's own hosts, its fonts and its config files load (every
# ad and tracker request is aborted), and a watcher hides any "Sponsored" row that still renders (the same
# filter as video/pipeline/build.py), so no ad is committed with the evidence
EXPLORER_HOSTS = ("blockscout.com", "fonts.googleapis.com", "fonts.gstatic.com", "raw.githubusercontent.com")
HIDE_SPONSORED = """(() => {
  const hide = () => {
    for (const e of document.querySelectorAll('div, span, p')) {
      const p = e.parentElement;
      if (e.children.length === 0 && /^\\s*Sponsored:?\\s*$/.test(e.textContent) && p && p.style.display !== 'none') {
        p.style.display = 'none';
      }
    }
  };
  new MutationObserver(hide).observe(document, {subtree: true, childList: true, characterData: true});
  document.addEventListener('DOMContentLoaded', hide);
})();"""


def no_ads(route) -> None:
    host = urlparse(route.request.url).netloc
    if any(host == h or host.endswith("." + h) for h in EXPLORER_HOSTS):
        route.continue_()
    else:
        route.abort()


def pick(txs: list[dict], step: str, function: str, text: str) -> dict:
    for t in txs:
        if t["step"] == step and t["function"] == function and text in (t.get("logLine") or ""):
            return t
    raise SystemExit(f"no {function} in {step} with {text!r} in evidence.json")


def main() -> int:
    from playwright.sync_api import sync_playwright

    txs = json.loads(EVIDENCE.read_text(encoding="utf-8"))["transactions"]
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1100}, device_scale_factor=1.5)
        page.route("**/*", no_ads)
        page.add_init_script(HIDE_SPONSORED)
        for name, step, function, text in SHOTS:
            tx = pick(txs, step, function, text)
            h = tx["txHash"]
            url = (tx.get("explorer") or {}).get("blockscout") or f"https://base-sepolia.blockscout.com/tx/{h}"
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            body = ""
            for _ in range(30):  # the page renders client-side: wait for the hash and the details
                page.wait_for_timeout(750)
                body = page.inner_text("body")[:8000]
                if h.lower() in body.lower() and "Transaction details" in body:
                    break
            else:
                print(f"{url}: the page did not show the transaction; nothing written", file=sys.stderr)
                return 1
            page.wait_for_timeout(1500)
            page.screenshot(path=str(OUT / name))
            print(f"wrote {(OUT / name).relative_to(ROOT)} ({step} {function} {h[:10]}...)")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
