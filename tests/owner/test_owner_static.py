"""Static checks of the owner console page and the bureau lookup page (spec 11.1, 11.4).

- ``app.js`` never uses the HTML-parsing DOM APIs, ``document.write``, ``eval`` or handler properties;
- ``index.html`` has no inline script, no inline style and no inline event handler;
- every static file is ASCII-clean in its own code (LF line endings, no BOM).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
STATIC = REPO / "src" / "acb" / "owner" / "static"
SITE = REPO / "site"

FORBIDDEN_JS = [
    r"innerHTML",
    r"outerHTML",
    r"insertAdjacentHTML",
    r"document\.write",
    r"\beval\s*\(",
    r"new\s+Function\b",
    r"\.on[a-z]+\s*=",
    r"setAttribute\(\s*[\"']on",
    r"setAttribute\(\s*[\"']style",
    r"javascript:",
    r"createContextualFragment",
    r"DOMParser",
    r"\bsrcdoc\b",
]


def _code_only(js: str) -> str:
    """The script without comments (comments may name the forbidden APIs)."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.DOTALL)
    return re.sub(r"(^|[^:\\])//[^\n]*", r"\1", js)


@pytest.mark.parametrize("path", [STATIC / "app.js", SITE / "bureau.js"], ids=["app.js", "bureau.js"])
def test_js_never_uses_forbidden_dom_apis(path: Path):
    code = _code_only(path.read_text(encoding="utf-8"))
    found = [p for p in FORBIDDEN_JS if re.search(p, code)]
    assert found == [], f"{path.name} uses forbidden APIs: {found}"


def test_forbidden_api_grep_catches_violations():
    bad = 'el.innerHTML = x; e.outerHTML = 1; n.insertAdjacentHTML("beforeend", y); document.write(z); b.onclick = f;'
    assert all(re.search(p, bad) for p in FORBIDDEN_JS[:4])
    assert re.search(FORBIDDEN_JS[6], bad)
    assert not re.search(FORBIDDEN_JS[6], 'a.addEventListener("click", f); root.confirm("x")')


@pytest.mark.parametrize("path", [STATIC / "index.html", SITE / "bureau.html"], ids=["index.html", "bureau.html"])
def test_html_has_no_inline_script_style_or_handler(path: Path):
    html = path.read_text(encoding="utf-8")
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, flags=re.DOTALL | re.IGNORECASE)
    assert scripts, "the page loads its script from a file"
    for attrs, body in scripts:
        assert "src=" in attrs and body.strip() == "", "no inline script"
        assert not re.search(r"src=\"(https?:)?//", attrs), "no external CDN"
    assert not re.search(r"<style\b", html, flags=re.IGNORECASE), "no inline style element"
    assert not re.search(r"\sstyle\s*=", html, flags=re.IGNORECASE), "no style attribute"
    assert not re.search(r"\son[a-z]+\s*=", html, flags=re.IGNORECASE), "no inline event handler"
    assert "javascript:" not in html.lower()
    for href in re.findall(r"<link\b[^>]*href=\"([^\"]+)\"", html):
        assert not href.startswith(("http:", "https:", "//")), "no external stylesheet"


def test_index_has_csrf_placeholder_and_five_panels():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '<meta name="acb-csrf" content="">' in html
    for panel in ("panel-wallet", "panel-grades", "panel-decisions", "panel-autonomy", "panel-messages"):
        assert f'id="{panel}"' in html
    assert 'id="banner-fallback"' in html and "AI offline: rule-based mode" in html


@pytest.mark.parametrize(
    "path",
    [*sorted(STATIC.iterdir()), *sorted(SITE.iterdir())],
    ids=lambda p: p.name,
)
def test_static_files_are_lf_ascii_without_bom(path: Path):
    data = path.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in data
    assert all(b < 0x80 for b in data), "plain ASCII (no smart quotes or dashes)"


def test_css_has_grade_palette_and_base_font():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "font-size: 18px" in css
    for grade in ("A", "B", "C", "D", "NEW"):
        assert f".grade-{grade}" in css
    assert "@import" not in css and "url(" not in css
