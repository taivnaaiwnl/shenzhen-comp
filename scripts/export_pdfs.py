"""PDF copies of the rendered deliverables (docs/deliverables/out/*.md) for the submission.

Each rendered Markdown file becomes HTML (python-markdown with tables and pymdown superfences) with a
plain print stylesheet, and headless Chromium (Playwright) prints it to A4 with page numbers. Links stay
clickable. Nothing is fetched from the network: the stylesheet is inline, figures linked from GitHub are read
from the local checkout, and a missing local image stops the export.

    uv run --with markdown --with pymdown-extensions --with playwright python scripts/export_pdfs.py

Writes ``docs/deliverables/pdf/InternationalAI-TeamMongolia-<Name>.pdf``. Run ``scripts/render_deliverables.py``
first; a file that still holds a ``{{placeholder}}`` is refused.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "deliverables" / "out"
OUT = ROOT / "docs" / "deliverables" / "pdf"
DOCS = {
    "technical-document.md": "TechnicalDocument",
    "security-self-assessment.md": "SecuritySelfAssessment",
    "execution-evidence.md": "ExecutionEvidence",
    "model-card.md": "ModelCard",
}

CSS = """
@page { size: A4; margin: 16mm 14mm 18mm 14mm; }
html { font-family: "Segoe UI", Arial, sans-serif; font-size: 9.5pt; color: #1e2235; line-height: 1.4; }
body { margin: 0; }
h1 { font-size: 20pt; color: #2b3a8c; margin: 0 0 6pt; }
h2 { font-size: 14.5pt; color: #2b3a8c; margin: 16pt 0 6pt; border-bottom: 1.5px solid #c9d0ea;
     padding-bottom: 2pt; break-after: avoid; }
h3 { font-size: 12pt; margin: 12pt 0 4pt; break-after: avoid; }
h4 { font-size: 10.5pt; margin: 10pt 0 3pt; break-after: avoid; }
p, li { orphans: 3; widows: 3; }
a { color: #0b5cad; text-decoration: none; overflow-wrap: anywhere; }
code { font-family: Consolas, "Courier New", monospace; font-size: 8.6pt; background: #f2f4fa;
       padding: 0 2px; border-radius: 2px; overflow-wrap: anywhere; }
pre { background: #f2f4fa; border: 1px solid #dde2f0; border-radius: 4px; padding: 6pt 8pt;
      white-space: pre-wrap; word-break: break-all; font-size: 8.4pt; break-inside: avoid; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0 10pt; font-size: 8.6pt; }
th, td { border: 1px solid #c9d0ea; padding: 3pt 5pt; vertical-align: top; text-align: left; }
th { background: #e9ecf7; }
tr { break-inside: avoid; }
blockquote { margin: 8pt 0; padding: 6pt 10pt; background: #f4f6fc; border: 1px solid #c9d0ea;
             border-radius: 4px; }
blockquote p { margin: 4pt 0; }
hr { border: none; border-top: 1px solid #c9d0ea; margin: 12pt 0; }
img { max-width: 100%; max-height: 240mm; height: auto; display: block; margin: 6pt auto; break-inside: avoid; }
"""
RAW = "https://raw.githubusercontent.com/taivnaaiwnl/shenzhen-comp/main/"
"""Figures are linked from GitHub so the Markdown shows them there; the PDF takes the same files locally."""

FOOTER = (
    '<div style="font-size:7.5pt;color:#6b7186;width:100%;padding:0 14mm;display:flex;'
    'justify-content:space-between;font-family:Segoe UI,Arial,sans-serif">'
    "<span>Team Mongolia - Agent Credit Bureau - {title}</span>"
    '<span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>'
)


def to_html(text: str, title: str) -> str:
    import markdown

    text = re.sub(r"^<!--.*?-->\s*", "", text, flags=re.DOTALL)
    text = text.replace(RAW, ROOT.as_uri() + "/")
    # superfences: a code fence inside a blockquote keeps its lines (fenced_code joins them)
    body = markdown.markdown(text, extensions=["tables", "pymdownx.superfences", "sane_lists", "attr_list"])
    # a key-value table written with an empty header row ("| | |") prints without that row
    body = re.sub(r"<thead>\s*<tr>(?:\s*<th>\s*</th>)+\s*</tr>\s*</thead>", "", body)
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{title}</title>'
        f"<style>{CSS}</style></head><body>{body}</body></html>"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", type=Path, default=SRC)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright

    args.out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        for name, label in DOCS.items():
            path = args.src / name
            text = path.read_text(encoding="utf-8")
            if "{{" in text:
                print(f"{path}: unrendered placeholder; run scripts/render_deliverables.py first", file=sys.stderr)
                return 1
            title = re.search(r"^# (.+)$", text, flags=re.MULTILINE)
            title = title.group(1).strip() if title else label
            missing = [
                m
                for m in re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", text)
                if not m.startswith("http") and not (path.parent / m).resolve().is_file()
            ]
            if missing:
                print(f"{path}: image files missing: {', '.join(missing)}", file=sys.stderr)
                return 1
            # written beside the Markdown so its relative image paths resolve, removed after printing
            html = path.parent / f".{path.stem}.print.html"
            html.write_text(to_html(text, title), encoding="utf-8")
            try:
                page.goto(html.as_uri(), wait_until="load")
            finally:
                html.unlink(missing_ok=True)
            target = args.out / f"InternationalAI-TeamMongolia-{label}.pdf"
            page.pdf(
                path=str(target),
                format="A4",
                print_background=True,
                display_header_footer=True,
                header_template="<span></span>",
                footer_template=FOOTER.format(title=label),
                margin={"top": "16mm", "bottom": "18mm", "left": "14mm", "right": "14mm"},
            )
            print(f"wrote {target.relative_to(ROOT)}")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
