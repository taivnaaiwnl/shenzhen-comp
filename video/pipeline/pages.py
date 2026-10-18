"""HTML for every frame of the video: the 1920x1080 frame layout and the content pages (cards, receipts,
terminal pages, tables). Rendered to PNG by ``build.py`` with Playwright; nothing here edits the console.

Every string that comes from evidence or a run is escaped (``esc``).
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

esc = html.escape

CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; width: 1920px; height: 1080px; overflow: hidden; }
body { background: #0e1621; color: #e8edf3; font-family: "Segoe UI", Arial, sans-serif;
  display: flex; flex-direction: column; }
.top { flex: none; min-height: 92px; padding: 14px 40px 6px; display: flex; flex-direction: column;
  justify-content: center; gap: 4px; }
.lab { font-size: 23px; line-height: 1.25; }
.lab.local { color: #ffcf5c; font-weight: 600; }
.lab.red { color: #ff8a80; font-weight: 700; }
.lab.info { color: #c9d6ea; }
.content { flex: 1; min-height: 0; margin: 6px 40px 18px; display: flex; align-items: center; justify-content: center;
  position: relative; }
.caption { flex: none; margin: 0 40px 6px; }
.caption div { background: #1b2838; border: 1px solid #3a5070; border-radius: 8px; padding: 9px 18px;
  font-size: 23px; line-height: 1.3; color: #f1f5f9; }
.subspace { flex: none; height: 150px; }
.redframe { position: absolute; left: 0; top: 0; width: 1920px; height: 1080px; border: 10px solid #d32f2f; }
.crop { position: relative; overflow: hidden; border-radius: 8px; box-shadow: 0 0 0 2px #2b3a4d; background: #fff; }
.stack { display: flex; flex-direction: column; align-items: center; gap: 10px; }
.crop img { position: absolute; left: 0; top: 0; max-width: none; }
.hl { position: absolute; border: 5px solid #ff9d00; border-radius: 8px; }
.hl.mark { border: 3px solid #ff9d00; border-radius: 4px; background: rgba(255, 196, 0, 0.28); }
.fit { transform-origin: center center; }
.mono { font-family: Consolas, "Courier New", monospace; }

/* cards */
.card { background: #f6f7f9; color: #1d232b; border-radius: 14px; padding: 40px 56px; width: 1700px; }
.title-card h1 { font-size: 76px; margin: 0 0 12px; color: #11335f; }
.title-card .sub { font-size: 38px; color: #1f4e9c; margin-bottom: 34px; }
.title-card p { font-size: 29px; margin: 10px 0; line-height: 1.35; }
.title-card .note { font-size: 25px; color: #7a4b00; background: #fff4d6; padding: 10px 16px; border-radius: 8px; }
.problem-card p { font-size: 36px; line-height: 1.4; margin: 0 0 28px; }
.problem-card p.dim { color: #9aa3ae; }
.problem-card .src { font-size: 22px; color: #5b6573; }

/* receipt card */
.tx { width: 1760px; padding: 26px 34px; font-size: 22px; }
.tx .head { display: flex; gap: 18px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }
.badge { padding: 4px 14px; border-radius: 6px; font-weight: 700; color: #fff; }
.badge.ok { background: #1b5e20; } .badge.bad { background: #c62828; }
.tx .hash { font-family: Consolas, monospace; font-size: 21px; }
.tx table { border-collapse: collapse; width: 100%; }
.tx td { padding: 4px 10px; border-bottom: 1px solid #d8dde4; vertical-align: top; }
.tx td.k { color: #5b6573; width: 230px; }
.tx h3 { margin: 16px 0 6px; font-size: 22px; color: #1f4e9c; }
.ev { border: 1px solid #d8dde4; border-radius: 8px; padding: 6px 12px; margin: 6px 0; background: #fff; }
.ev.em { border: 3px solid #ff9d00; }
.ev .n { font-weight: 700; }
.ev .a { font-family: Consolas, monospace; font-size: 19px; color: #333; overflow-wrap: anywhere; }
.pending { margin-top: 14px; font-size: 21px; color: #7a4b00; background: #fff4d6; padding: 8px 14px; border-radius: 8px; }

/* terminal */
.term { width: 1760px; background: #0b0f14; border: 1px solid #2b3a4d; border-radius: 10px; overflow: hidden; }
.term .bar { background: #1c2733; color: #c9d6ea; padding: 10px 18px; font-size: 21px; }
.term pre { margin: 0; padding: 18px 22px; font-family: Consolas, "Courier New", monospace; font-size: 23px;
  line-height: 1.32; color: #e6edf3; white-space: pre-wrap; overflow-wrap: anywhere; }
.term .cmd { color: #8bd5ff; }
.term .em { background: #4a3a00; color: #ffe08a; }

/* invoice */
.inv { width: 1700px; font-size: 25px; }
.inv table { border-collapse: collapse; width: 100%; margin-bottom: 18px; }
.inv td { padding: 6px 10px; border-bottom: 1px solid #d8dde4; }
.inv td.k { color: #5b6573; width: 300px; }
.untrusted { border-left: 6px solid #b26a00; background: #fffaf0; padding: 12px 16px; white-space: pre-wrap;
  overflow-wrap: anywhere; font-size: 26px; }
.untrusted .l { display: block; font-size: 18px; color: #7a4b00; text-transform: uppercase; margin-bottom: 6px; }

/* tables */
.tbl { width: 1760px; font-size: 25px; }
.tbl table { border-collapse: collapse; width: 100%; }
.tbl th, .tbl td { padding: 7px 12px; border-bottom: 1px solid #d8dde4; text-align: left; }
.tbl th { color: #5b6573; }
.tbl .total { margin-top: 18px; font-size: 27px; line-height: 1.4; background: #fff4d6; border: 3px solid #ff9d00;
  border-radius: 8px; padding: 12px 18px; }
.tbl h2 { margin: 0 0 12px; color: #11335f; font-size: 32px; }
.checks li { margin: 4px 0; }
.flag { background: #ffd54f; outline: 2px solid #b26a00; border-radius: 3px; }
.term .cut { color: #8b98a9; font-style: italic; }

/* proposal versus decision */
.pd { width: 1760px; font-size: 25px; }
.pd h2 { margin: 0 0 16px; color: #11335f; font-size: 31px; }
.pd .cols { display: flex; gap: 24px; }
.pd .box { flex: 1; border: 2px solid #d8dde4; border-radius: 10px; padding: 14px 18px; background: #fff; }
.pd .box.model { border-color: #c62828; background: #fff5f5; }
.pd .box h3 { margin: 0 0 8px; font-size: 23px; color: #5b6573; }
.pd .box.model h3 { color: #b71c1c; }
.pd table { border-collapse: collapse; width: 100%; }
.pd td { padding: 4px 8px; vertical-align: top; }
.pd td.k { color: #5b6573; width: 190px; }
.pd .big { font-size: 30px; font-weight: 700; }
.pd .verdict { margin-top: 18px; border: 3px solid #1b5e20; background: #f1f8f1; border-radius: 10px; padding: 12px 18px; }
.pd .verdict .big { color: #1b5e20; }
.pd .src { margin-top: 12px; font-size: 19px; color: #5b6573; font-family: Consolas, monospace; overflow-wrap: anywhere; }
.pd ul { margin: 6px 0 0; padding-left: 26px; }
"""

FIT_JS = """
(function () {
  document.querySelectorAll('.stack').forEach(function (st) {
    var box = st.parentElement.getBoundingClientRect();
    var crops = st.querySelectorAll('.crop');
    var maxw = 0, sumh = 0, maxs = 9;
    crops.forEach(function (c) { maxw = Math.max(maxw, +c.dataset.w); sumh += +c.dataset.h; maxs = Math.min(maxs, +c.dataset.maxs); });
    var gaps = 10 * (crops.length - 1);
    var s = Math.min(box.width / maxw, (box.height - gaps) / sumh, maxs);
    crops.forEach(function (c) {
      var w = +c.dataset.w, h = +c.dataset.h;
      c.style.width = (w * s) + 'px'; c.style.height = (h * s) + 'px';
      var img = c.querySelector('img');
      img.style.width = (+c.dataset.pw * s) + 'px';
      img.style.left = (-c.dataset.x * s) + 'px'; img.style.top = (-c.dataset.y * s) + 'px';
      c.querySelectorAll('.hl').forEach(function (b) {
        b.style.left = ((+b.dataset.x - c.dataset.x) * s - 6) + 'px';
        b.style.top = ((+b.dataset.y - c.dataset.y) * s - 6) + 'px';
        b.style.width = (+b.dataset.w * s + 12) + 'px';
        b.style.height = (+b.dataset.h * s + 12) + 'px';
      });
    });
    document.body.dataset.scale = s.toFixed(3);
  });
  document.querySelectorAll('.fit').forEach(function (f) {
    var box = f.parentElement.getBoundingClientRect();
    var r = f.getBoundingClientRect();
    var s = Math.min(1, box.width / r.width, box.height / r.height);
    f.style.transform = 'scale(' + s + ')';
  });
  document.body.dataset.ready = '1';
})();
"""


def frame(content: str, labels: list[tuple[str, str]], caption: str | None = None, red: bool = False) -> str:
    tops = "".join(f'<div class="lab {esc(kind)}">{esc(text)}</div>' for kind, text in labels)
    cap = f'<div class="caption"><div>{esc(caption)}</div></div>' if caption else ""
    rf = '<div class="redframe"></div>' if red else ""
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>'
        f'<div class="top">{tops}</div><div class="content">{content}</div>{cap}<div class="subspace"></div>{rf}'
        f"<script>{FIT_JS}</script></body></html>"
    )


def crop(
    image: Path,
    page_w: float,
    rects: list[tuple[float, float, float, float]],
    highlights: list[tuple[Any, ...]],
    max_scale: float = 2.2,
) -> str:
    """One or more regions of a capture, stacked and scaled together; highlights go to the region holding them."""
    return crop_parts([(image, page_w, r, highlights) for r in rects], max_scale)


def crop_parts(
    parts: list[tuple[Path, float, tuple[float, float, float, float], list[tuple[Any, ...]]]], max_scale=2.2
):
    """Regions of one or more captures (image, page width, region, highlights), stacked and scaled together.

    A highlight is ``(x, y, w, h)`` or ``(x, y, w, h, "mark")`` (a lighter box for words in a text)."""
    out = []
    for image, page_w, (x, y, w, h), highlights in parts:
        hls = "".join(
            f'<div class="hl {esc(str(hl[4])) if len(hl) > 4 else ""}" data-x="{hl[0]}" data-y="{hl[1]}" '
            f'data-w="{hl[2]}" data-h="{hl[3]}"></div>'
            for hl in highlights
            if y - 1 <= hl[1] + hl[3] / 2 <= y + h + 1 and x - 1 <= hl[0] + hl[2] / 2 <= x + w + 1
        )
        out.append(
            f'<div class="crop" data-x="{x}" data-y="{y}" data-w="{w}" data-h="{h}" data-pw="{page_w}" '
            f'data-maxs="{max_scale}"><img src="{image.resolve().as_uri()}">{hls}</div>'
        )
    return '<div class="stack">' + "".join(out) + "</div>"


def title_card(lines: dict[str, str]) -> str:
    return (
        f'<div class="card title-card fit"><h1>{esc(lines["title"])}</h1><div class="sub">{esc(lines["sub"])}</div>'
        f'<p>{esc(lines["team"])}</p><p>{esc(lines["entry"])}</p><p class="note">{esc(lines["note"])}</p></div>'
    )


def problem_card(lines: list[str], shown: int, source: str | None) -> str:
    ps = "".join(
        f'<p class="{"" if i < shown else "dim"}">{esc(t) if i < shown else "&nbsp;"}</p>' for i, t in enumerate(lines)
    )
    src = f'<div class="src">{esc(source)}</div>' if source else ""
    return f'<div class="card problem-card fit">{ps}{src}</div>'


# ---------------------------------------------------------------------------------------------------
# animated scenes: CSS animations that build.py scrubs frame by frame (window.__seek), so every frame is
# deterministic and the motion lands on the narration's timing. A scene runs from 0 to its total seconds.
# ---------------------------------------------------------------------------------------------------

SEEK_JS = """
window.__seek = function (ms) {
  document.getAnimations().forEach(function (a) { a.pause(); a.currentTime = ms; });
};
"""

EASE = "cubic-bezier(.2,.7,.2,1)"


class Timeline:
    """Keyframes for one scene: every animation lasts the whole scene and holds its state at each stop."""

    def __init__(self, total: float):
        self.total = total
        self.rules: list[str] = []

    def anim(self, stops: list[tuple[float, dict[str, str]]], ease: str = EASE) -> str:
        """``stops``: (seconds, css properties) in time order; returns the element's animation style."""
        name = f"k{len(self.rules) + 1}"
        frames = []
        for t, props in [(0.0, stops[0][1]), *stops, (self.total, stops[-1][1])]:
            pct = max(0.0, min(100.0, t / self.total * 100))
            decl = ";".join(f"{k}:{v}" for k, v in props.items())
            frames.append(f"{pct:.4f}% {{{decl};animation-timing-function:{ease}}}")
        self.rules.append(f"@keyframes {name} {{{' '.join(frames)}}}")
        return f"animation:{name} {self.total}s linear both;"

    def show(self, t_in: float, t_out: float | None = None, d: float = 0.5, dy: int = 24, scale: float = 1.0) -> str:
        """Fade (and rise) in at ``t_in``; fade out at ``t_out`` if given."""
        hidden = {"opacity": "0", "transform": f"translateY({dy}px) scale({scale})"}
        shown = {"opacity": "1", "transform": "translateY(0) scale(1)"}
        stops = [(t_in, hidden), (t_in + d, shown)]
        if t_out is not None:
            stops += [(t_out, shown), (t_out + d, {"opacity": "0", "transform": "translateY(0) scale(1)"})]
        return self.anim(stops)

    def pop(self, t_in: float, t_out: float | None = None) -> str:
        """Scale up with a small overshoot."""
        stops = [
            (t_in, {"opacity": "0", "transform": "scale(0.4)"}),
            (t_in + 0.25, {"opacity": "1", "transform": "scale(1.12)"}),
            (t_in + 0.45, {"opacity": "1", "transform": "scale(1)"}),
        ]
        if t_out is not None:
            stops += [
                (t_out, {"opacity": "1", "transform": "scale(1)"}),
                (t_out + 0.4, {"opacity": "0", "transform": "scale(1)"}),
            ]
        return self.anim(stops)

    def draw(self, length: float, t0: float, t1: float, t_out: float | None = None) -> str:
        """An SVG stroke drawn from start to end (stroke-dasharray = length)."""
        stops = [
            (t0 - 0.01, {"stroke-dashoffset": f"{length:.1f}", "opacity": "0"}),
            (t0, {"stroke-dashoffset": f"{length:.1f}", "opacity": "1"}),
            (t1, {"stroke-dashoffset": "0", "opacity": "1"}),
        ]
        if t_out is not None:
            stops += [
                (t_out, {"stroke-dashoffset": "0", "opacity": "1"}),
                (t_out + 0.4, {"stroke-dashoffset": "0", "opacity": "0"}),
            ]
        return f"stroke-dasharray:{length:.1f};" + self.anim(stops, "ease-in-out")

    def arrow(
        self, x1: float, x2: float, y: float, color: str, t0: float, t1: float, t_out: float | None = None
    ) -> str:
        """A horizontal SVG arrow drawn left to right; its head appears when the line arrives."""
        head = [(t1 - 0.05, {"opacity": "0"}), (t1 + 0.1, {"opacity": "1"})]
        if t_out is not None:
            head += [(t_out, {"opacity": "1"}), (t_out + 0.4, {"opacity": "0"})]
        return (
            f'<path d="M {x1} {y} L {x2 - 14} {y}" stroke="{color}" stroke-width="5" '
            f'style="{self.draw(x2 - 14 - x1, t0, t1, t_out)}"/>'
            f'<polygon points="{x2 - 18},{y - 12} {x2},{y} {x2 - 18},{y + 12}" fill="{color}" style="{self.anim(head)}"/>'
        )

    def travel(self, a: tuple[float, float], b: tuple[float, float], t0: float, t1: float) -> str:
        """A dot that moves from ``a`` to ``b`` (pixel centres) between t0 and t1, visible only while moving."""
        p0, p1 = f"translate({a[0]:.0f}px,{a[1]:.0f}px)", f"translate({b[0]:.0f}px,{b[1]:.0f}px)"
        return self.anim(
            [
                (t0 - 0.01, {"opacity": "0", "transform": p0}),
                (t0, {"opacity": "1", "transform": p0}),
                (t1, {"opacity": "1", "transform": p1}),
                (t1 + 0.12, {"opacity": "0", "transform": p1}),
            ],
            "ease-in-out",
        )

    def css(self) -> str:
        return "\n".join(self.rules)


SCENE_CSS = """
.scene { position: relative; width: 1840px; height: 790px; overflow: hidden; }
.scene .abs { position: absolute; }
.scene .kick { font-size: 26px; color: #8fb3e8; letter-spacing: 1px; }
.scene h1 { margin: 0; font-size: 96px; color: #ffffff; font-weight: 700; line-height: 1.05; }
.scene .tsub { font-size: 38px; color: #9cc4ff; line-height: 1.3; }
.scene .team { font-size: 29px; color: #dfe7f2; line-height: 1.4; }
.scene .tnote { font-size: 21px; color: #ffd98a; background: rgba(255, 196, 0, 0.10); border: 1px solid rgba(255, 196, 0, 0.35);
  border-radius: 8px; padding: 9px 14px; line-height: 1.35; }
.scene .node { width: 74px; height: 74px; border-radius: 50%; background: #17304f; border: 3px solid #4f8cff;
  display: flex; align-items: center; justify-content: center; color: #cfe0ff; font-weight: 700; font-size: 22px;
  margin: -37px 0 0 -37px; }
.scene .node.wallet { background: #10406a; border-color: #8fd3ff; width: 96px; height: 96px; margin: -48px 0 0 -48px; font-size: 24px; }
.scene .coin { width: 22px; height: 22px; margin: -11px 0 0 -11px; border-radius: 50%; background: #ffcf40;
  box-shadow: 0 0 12px #ffcf40; left: 0; top: 0; }
.scene .grade { width: 46px; height: 46px; margin: -23px 0 0 -23px; border-radius: 10px; color: #fff; font-weight: 800;
  font-size: 26px; display: flex; align-items: center; justify-content: center; }
.g-a { background: #2e7d32; } .g-b { background: #00897b; } .g-c { background: #ef8f00; } .g-d { background: #c62828; }
.g-new { background: #5c6b7e; font-size: 17px !important; }
.scene h2 { margin: 0; font-size: 46px; color: #ffffff; font-weight: 600; line-height: 1.25; width: 1840px; text-align: center; }
.scene .axis-lab { font-size: 22px; color: #8fb3e8; text-align: center; width: 160px; margin-left: -80px; }
.scene .dot { width: 30px; height: 30px; margin: -15px 0 0 -15px; border-radius: 50%; background: #ef5350;
  box-shadow: 0 0 0 7px rgba(239, 83, 80, 0.25); }
.scene .inc { width: 410px; background: #f6f7f9; color: #1d232b; border-radius: 12px; padding: 14px 18px;
  border-top: 6px solid #ef5350; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35); }
.scene .inc .when { font-size: 20px; color: #5b6573; }
.scene .inc .who { font-size: 30px; font-weight: 700; color: #11335f; margin: 2px 0; }
.scene .inc .amt { font-size: 24px; font-weight: 700; color: #b71c1c; }
.scene .inc .what { font-size: 21px; line-height: 1.3; margin-top: 4px; }
.scene .inc .src { font-size: 16px; color: #6b7686; margin-top: 6px; }
.scene .box { background: #f6f7f9; color: #1d232b; border-radius: 12px; padding: 16px 20px; text-align: center;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35); }
.scene .box .t { font-size: 27px; font-weight: 700; color: #11335f; }
.scene .box .s { font-size: 20px; color: #5b6573; margin-top: 4px; line-height: 1.3; }
.scene .box.bad { border: 4px solid #ef5350; }
.scene .box.good { border: 4px solid #4caf50; }
.scene .stamp { font-size: 40px; font-weight: 800; color: #ef5350; border: 5px solid #ef5350; border-radius: 10px;
  padding: 4px 18px; transform: rotate(-6deg); background: rgba(14, 22, 33, 0.85); }
.scene .chip { font-size: 22px; padding: 8px 14px; border-radius: 22px; background: #1b2838; border: 2px solid #3a5070;
  color: #e8edf3; white-space: nowrap; }
.scene .chip.ok { border-color: #4caf50; } .scene .chip.late { border-color: #ffb300; } .scene .chip.fail { border-color: #ef5350; }
.scene .out { font-size: 25px; font-weight: 700; padding: 10px 18px; border-radius: 10px; color: #fff; white-space: nowrap; }
.scene .foot { font-size: 24px; color: #c9d6ea; width: 1840px; text-align: center; }
.scene svg { position: absolute; left: 0; top: 0; overflow: visible; }
"""


def scene_frame(tl: Timeline, content: str, labels: list[tuple[str, str]]) -> str:
    """A full frame holding one animated scene (the seek hook and the scene's keyframes included)."""
    page = frame(f'<div class="scene">{content}</div>', labels)
    return page.replace("</style>", SCENE_CSS + tl.css() + "</style>", 1).replace(
        "<script>", f"<script>{SEEK_JS}</script><script>", 1
    )


def title_scene(lines: dict[str, str]) -> tuple[Timeline, str]:
    """0-5 s: title and team over a small network of agents paying each other and receiving grades."""
    tl = Timeline(5.0)
    nodes = [(1240, 150), (1500, 110), (1720, 260), (1330, 380), (1610, 470), (1220, 600), (1480, 680), (1760, 640)]
    wallet = (1440, 320)
    grades = [
        ("A", "g-a"),
        ("B", "g-b"),
        ("NEW", "g-new"),
        ("A", "g-a"),
        ("C", "g-c"),
        ("B", "g-b"),
        ("D", "g-d"),
        ("A", "g-a"),
    ]
    svg = []
    for i, (x, y) in enumerate(nodes):
        length = ((x - wallet[0]) ** 2 + (y - wallet[1]) ** 2) ** 0.5
        svg.append(
            f'<line x1="{wallet[0]}" y1="{wallet[1]}" x2="{x}" y2="{y}" stroke="#3b6db3" stroke-width="3" '
            f'style="{tl.draw(length, 0.2 + i * 0.12, 0.9 + i * 0.12)}"/>'
        )
    out = [f'<svg width="1840" height="790">{"".join(svg)}</svg>']
    for i, (x, y) in enumerate(nodes):
        out.append(f'<div class="abs node" style="left:{x}px;top:{y}px;{tl.pop(0.5 + i * 0.1)}">AI</div>')
    out.append(f'<div class="abs node wallet" style="left:{wallet[0]}px;top:{wallet[1]}px;{tl.pop(0.3)}">wallet</div>')
    for i, (g, cls) in enumerate(grades):
        x, y = nodes[i]
        out.append(
            f'<div class="abs grade {cls}" style="left:{x + 46}px;top:{y - 40}px;{tl.pop(1.8 + i * 0.16)}">{g}</div>'
        )
    for k, i in enumerate([0, 3, 1, 5, 7, 2]):  # payments flow from the wallet to graded agents
        t0 = 2.0 + k * 0.42
        out.append(f'<div class="abs coin" style="{tl.travel(wallet, nodes[i], t0, t0 + 0.8)}"></div>')
    out.append(f'<div class="abs kick" style="left:40px;top:70px;{tl.show(0.1)}">{esc(lines["entry"])}</div>')
    out.append(
        f'<div class="abs" style="left:40px;top:130px;width:1100px;{tl.show(0.3, dy=40)}"><h1>{esc(lines["title"])}</h1></div>'
    )
    out.append(
        f'<div class="abs tsub" style="left:40px;top:250px;width:1080px;{tl.show(0.9)}">{esc(lines["sub"])}</div>'
    )
    out.append(
        f'<div class="abs team" style="left:40px;top:400px;width:1080px;{tl.show(1.4)}">{esc(lines["team"])}</div>'
    )
    out.append(
        f'<div class="abs tnote" style="left:40px;top:620px;width:1080px;{tl.show(2.2)}">{esc(lines["note"])}</div>'
    )
    return tl, "".join(out)


def why_scene(incidents: list[dict[str, str]], heads: list[str], foot: str) -> tuple[Timeline, str]:
    """16 s in three beats matched to the narration (scene time 0 = 0:05 of the video):
    0-6 s the reported incidents on a timeline; 6-11 s Freysa's case step by step; 11-16 s our answer."""
    tl = Timeline(16.0)
    out = []
    # headlines, one per beat
    for (t_in, t_out), text in zip([(0.2, 5.6), (6.1, 10.6), (11.1, None)], heads, strict=True):
        out.append(f'<div class="abs" style="left:0;top:4px;{tl.show(t_in, t_out, dy=16)}"><h2>{esc(text)}</h2></div>')
    # beat 1: the timeline (an ordinal axis: the dates are written on it; a break marks the 15-month gap)
    y_axis, xs = 430, [240, 760, 1190, 1610]
    svg = [
        (
            f'<line x1="60" y1="{y_axis}" x2="1780" y2="{y_axis}" stroke="#8fb3e8" stroke-width="4" '
            f'style="{tl.draw(1720, 0.5, 1.7, 5.7)}"/>'
        ),
        (
            f'<path d="M 455 {y_axis - 22} l 18 44 M 475 {y_axis - 22} l 18 44" stroke="#0e1621" stroke-width="12" '
            f'style="{tl.draw(110, 1.2, 1.4, 5.7)}"/>'
        ),
        (
            f'<path d="M 455 {y_axis - 22} l 18 44 M 475 {y_axis - 22} l 18 44" stroke="#8fb3e8" stroke-width="3" '
            f'style="{tl.draw(110, 1.2, 1.4, 5.7)}"/>'
        ),
    ]
    out.append(f'<svg width="1840" height="790">{"".join(svg)}</svg>')
    out.append(
        f'<div class="abs axis-lab" style="left:470px;top:{y_axis + 40}px;width:140px;margin-left:-70px;'
        f'font-size:20px;{tl.show(1.4, 5.7)}">15 months</div>'
    )
    for i, (x, inc) in enumerate(zip(xs, incidents, strict=True)):
        t = 1.7 + i * 0.85
        above = i % 2 == 0
        top = y_axis - 290 if above else y_axis + 40
        out.append(f'<div class="abs dot" style="left:{x}px;top:{y_axis}px;{tl.pop(t, 5.7)}"></div>')
        card = (
            f'<div class="when">{esc(inc["when"])}</div><div class="who">{esc(inc["who"])}</div>'
            f'<div class="amt">{esc(inc["amount"])}</div><div class="what">{esc(inc["what"])}</div>'
            f'<div class="src">{esc(inc["source"])}</div>'
        )
        if i == 0:  # Freysa stays: it moves to the centre for beat 2, then leaves at 10.6 s
            fx, fy = 920 - x, 130 - top
            style = tl.anim(
                [
                    (t, {"opacity": "0", "transform": "translate(0,24px) scale(1)"}),
                    (t + 0.5, {"opacity": "1", "transform": "translate(0,0) scale(1)"}),
                    (5.8, {"opacity": "1", "transform": "translate(0,0) scale(1)"}),
                    (6.6, {"opacity": "1", "transform": f"translate({fx}px,{fy}px) scale(1.18)"}),
                    (10.6, {"opacity": "1", "transform": f"translate({fx}px,{fy}px) scale(1.18)"}),
                    (11.0, {"opacity": "0", "transform": f"translate({fx}px,{fy}px) scale(1.18)"}),
                ]
            )
            out.append(
                f'<div class="abs inc" style="left:{x - 205}px;top:{top}px;transform-origin:center;{style}">{card}</div>'
            )
        else:
            out.append(f'<div class="abs inc" style="left:{x - 205}px;top:{top}px;{tl.show(t, 5.7)}">{card}</div>')
    # beat 2: Freysa step by step
    flow = [
        ("A persuasive message", "arrives in the chat"),
        ("The LLM decides", "it holds the transfer decision"),
        ("The money leaves", "about 13.19 ETH (around $47k)"),
    ]
    bx = [260, 800, 1340]
    for i, ((t1, s1), x) in enumerate(zip(flow, bx, strict=True)):
        cls = "box bad" if i == 2 else "box"
        out.append(
            f'<div class="abs {cls}" style="left:{x - 10}px;top:440px;width:300px;{tl.show(7.0 + i * 0.7, 10.6)}">'
            f'<div class="t">{esc(t1)}</div><div class="s">{esc(s1)}</div></div>'
        )
    arrows = [
        tl.arrow(x + 300, x + 520, 495, "#ef5350", 7.4 + i * 0.7, 7.8 + i * 0.7, 10.6) for i, x in enumerate(bx[:2])
    ]
    out.append('<svg width="1840" height="790">' + "".join(arrows) + "</svg>")
    out.append(f'<div class="abs stamp" style="left:1330px;top:620px;{tl.pop(9.2, 10.6)}">talked into it</div>')
    # beat 3: our answer, built from settlements
    chips = [("on time", "ok"), ("on time", "ok"), ("late", "late"), ("not delivered", "fail")]
    for i, (text, cls) in enumerate(chips):
        out.append(
            f'<div class="abs chip {cls}" style="left:140px;top:{170 + i * 74}px;{tl.show(11.7 + i * 0.25)}">'
            f"settlement: {esc(text)}</div>"
        )
    out.append(
        f'<div class="abs foot" style="left:0;top:500px;width:520px;text-align:left;padding-left:140px;'
        f'{tl.show(12.6)}">recorded on-chain by the vaults</div>'
    )
    a3 = [tl.arrow(470, 690, 300, "#8fb3e8", 12.8, 13.2), tl.arrow(1150, 1370, 300, "#8fb3e8", 13.8, 14.2)]
    out.append('<svg width="1840" height="790">' + "".join(a3) + "</svg>")
    out.append(
        f'<div class="abs box good" style="left:710px;top:200px;width:420px;{tl.show(13.1)}">'
        '<div class="t">Credit grade</div><div class="s">recomputable by anyone from public data</div></div>'
    )
    for i, (g, cls) in enumerate([("A", "g-a"), ("B", "g-b"), ("C", "g-c"), ("D", "g-d")]):
        out.append(
            f'<div class="abs grade {cls}" style="left:{821 + i * 66}px;top:370px;{tl.pop(13.4 + i * 0.12)}">{g}</div>'
        )
    outs = [("A, B: pay", "#2e7d32"), ("C, NEW: escrow", "#ef8f00"), ("D: refused", "#c62828")]
    for i, (text, color) in enumerate(outs):
        out.append(
            f'<div class="abs out" style="left:1390px;top:{190 + i * 80}px;background:{color};{tl.show(14.3 + i * 0.3)}">'
            f"{esc(text)}</div>"
        )
    out.append(f'<div class="abs foot" style="left:0;top:640px;{tl.show(15.1)}">{esc(foot)}</div>')
    return tl, "".join(out)


def closing_card(lines: list[str]) -> str:
    first = f'<p style="font-size:40px;font-weight:600;color:#11335f">{esc(lines[0])}</p>'
    rest = "".join(f"<p>{esc(t)}</p>" for t in lines[1:])
    return f'<div class="card title-card fit">{first}{rest}</div>'


def receipt_card(
    rec: dict[str, Any], events: list[dict[str, Any]], emphasis: list[str], pending: str, reasons: list[str]
) -> str:
    status = rec.get("status")
    if status == 1:
        badge = '<span class="badge ok">Success</span>'
    else:
        exp = " (expected)" if rec.get("expectedStatus") == status else ""
        badge = f'<span class="badge bad">Failed{exp}</span>'
    rows = [
        ("Step", rec.get("step")),
        ("Action", rec.get("action")),
        ("Function", rec.get("function")),
        ("From", f"{rec.get('actor')} {rec.get('actorAddress')}"),
        ("To", f"{rec.get('contract')} {rec.get('to')}"),
        ("Block", rec.get("block")),
        ("Block time (UTC)", rec.get("timestampUtc")),
        ("Signed by the agent key", "yes" if rec.get("signedByAgentKey") else "no"),
    ]
    if reasons:
        rows.append(("Reason codes", "; ".join(reasons)))
    trs = "".join(
        f'<tr><td class="k">{esc(str(k))}</td><td>{esc(str(v))}</td></tr>' for k, v in rows if v not in (None, "")
    )
    evs = ""
    for ev in events:
        em = " em" if ev["name"] in emphasis else ""
        args = ", ".join(f"{k}={v}" for k, v in ev["args"].items())
        evs += f'<div class="ev{em}"><span class="n">{esc(ev["name"])}</span> <span class="a">{esc(args)}</span></div>'
    if not events:
        evs = '<div class="ev"><span class="a">no logs (the transaction reverted)</span></div>'
    return (
        f'<div class="card tx fit"><div class="head">{badge}<span class="hash">{esc(rec.get("txHash", ""))}</span></div>'
        f"<table>{trs}</table><h3>Logs, decoded from the saved receipt with the contract ABIs</h3>{evs}"
        f'<div class="pending">{esc(pending)}</div></div>'
    )


def explorer_image(path: Path) -> str:
    return f'<img src="{path.resolve().as_uri()}" style="max-width:1840px;max-height:100%;border-radius:8px">'


def terminal(
    title: str, command: str, output: str, emphasis: list[str] | None = None, marks: list[str] | None = None
) -> str:
    """A terminal page. ``emphasis``: lines holding one of these strings are highlighted; ``marks``: only these
    substrings are highlighted. A line starting with ``[`` and ending with ``not shown]`` is shown as a cut."""
    lines = []
    for line in output.splitlines():
        e = esc(line)
        if line.startswith("[") and line.endswith("not shown]"):
            e = f'<span class="cut">{e}</span>'
        elif emphasis and any(s in line for s in emphasis):
            e = f'<span class="em">{e}</span>'
        for m in marks or []:
            e = e.replace(esc(m), f'<span class="em">{esc(m)}</span>')
        lines.append(e)
    return (
        f'<div class="term fit"><div class="bar">{esc(title)}</div><pre><span class="cmd">$ {esc(command)}</span>\n'
        + "\n".join(lines)
        + "</pre></div>"
    )


def flagged(text: str, spans: list[tuple[int, int]]) -> str:
    """``text`` escaped, with the character ranges ``spans`` (start, end) marked."""
    marks = sorted((max(0, a), min(len(text), b)) for a, b in spans if b > a)
    merged: list[list[int]] = []
    for a, b in marks:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out, pos = [], 0
    for a, b in merged:
        out.append(esc(text[pos:a]))
        out.append(f'<span class="flag">{esc(text[a:b])}</span>')
        pos = b
    out.append(esc(text[pos:]))
    return "".join(out)


def invoice(
    inv: dict[str, Any],
    issuer: str,
    memo_label: str = "counterparty text (untrusted)",
    spans: list[tuple[int, int]] | None = None,
    note: str = "",
) -> str:
    items = "; ".join(
        f"{i.get('sku')} x {i.get('quantity')} at {int(i.get('unitPrice', 0)) / 1e6:.2f} USDC"
        for i in inv.get("items") or []
    )
    rows = [
        ("Invoice", inv.get("invoiceId")),
        ("Issuer", issuer),
        ("Items", items),
        ("Amount", f"{int(inv.get('amount', 0)) / 1e6:.2f} USDC"),
        ("Signed", "yes, EIP-712 signature of the issuer's registered wallet"),
    ]
    trs = "".join(f'<tr><td class="k">{esc(str(k))}</td><td>{esc(str(v))}</td></tr>' for k, v in rows)
    memo = str(inv.get("memo", ""))
    body = flagged(memo, spans) if spans else esc(memo)
    foot = f'<div style="margin-top:12px;font-size:21px;color:#5b6573">{esc(note)}</div>' if note else ""
    return (
        f'<div class="card inv fit"><table>{trs}</table><div class="untrusted"><span class="l">{esc(memo_label)}</span>'
        f"{body}</div>{foot}</div>"
    )


def proposal_card(
    title: str,
    model_rows: list[tuple[str, str]],
    model_head: str,
    invoice_rows: list[tuple[str, str]],
    verdict: str,
    reasons: list[str],
    source: str,
) -> str:
    """What the model proposed next to the signed invoice, and the signer's decision."""

    def table(rows: list[tuple[str, str]]) -> str:
        return (
            "<table>"
            + "".join(
                f'<tr><td class="k">{esc(k)}</td><td{" class=big" if k == "Amount" else ""}>{esc(v)}</td></tr>'
                for k, v in rows
            )
            + "</table>"
        )

    lis = "".join(f"<li>{esc(r)}</li>" for r in reasons)
    return (
        f'<div class="card pd fit"><h2>{esc(title)}</h2><div class="cols">'
        f'<div class="box model"><h3>{esc(model_head)}</h3>{table(model_rows)}</div>'
        f'<div class="box"><h3>The signed invoice (structured fields)</h3>{table(invoice_rows)}</div></div>'
        f'<div class="verdict"><span class="big">{esc(verdict)}</span><ul>{lis}</ul></div>'
        f'<div class="src">{esc(source)}</div></div>'
    )


def checks_card(title: str, rows: list[tuple[str, str, str]], verdict: str, notes: list[str], source: str) -> str:
    """The policy's own checks: (check, result, reason) rows, a verdict and notes."""
    trs = "".join(
        f'<tr><td>{esc(c)}</td><td style="font-weight:700;color:{"#c62828" if res != "passed" else "#1b5e20"}">'
        f"{esc(res)}</td><td>{esc(why)}</td></tr>"
        for c, res, why in rows
    )
    lis = "".join(f"<li>{esc(n)}</li>" for n in notes)
    return (
        f'<div class="card pd fit"><h2>{esc(title)}</h2><table class="tblc" style="font-size:25px">'
        f'<tr><td class="k">Check</td><td class="k">Result</td><td class="k">Reason code</td></tr>{trs}</table>'
        f'<div class="verdict"><span class="big">{esc(verdict)}</span><ul>{lis}</ul></div>'
        f'<div class="src">{esc(source)}</div></div>'
    )


def md_table(md: str) -> str:
    rows = [r.strip().strip("|").split("|") for r in md.strip().splitlines() if r.strip().startswith("|")]
    rows = [r for r in rows if not all(set(c.strip()) <= set("-: ") for c in r)]
    if not rows:
        return ""
    head = "".join(f"<th>{esc(c.strip().strip('*'))}</th>" for c in rows[0])
    body = "".join("<tr>" + "".join(f"<td>{esc(c.strip().strip('*'))}</td>" for c in r) + "</tr>" for r in rows[1:])
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def attack_page(title: str, table_md: str, totals: str, notes: list[str] | None = None) -> str:
    extra = "".join(f'<p style="margin:12px 0 0;font-size:24px;line-height:1.35">{esc(n)}</p>' for n in notes or [])
    return (
        f'<div class="card tbl fit"><h2>{esc(title)}</h2>{md_table(table_md)}<div class="total">{esc(totals)}</div>'
        f"{extra}</div>"
    )


def attack_result(r: dict[str, Any], heading: str, extra: str = "") -> str:
    checks = "".join(
        f"<li>{'PASS' if c.get('ok') else 'FAIL'}: {esc(str(c.get('check')))}"
        + (
            f'<div class="untrusted" style="font-size:22px">{esc(str(c["detail"]))}</div>'
            if isinstance(c.get("detail"), str)
            else ""
        )
        + "</li>"
        for c in r.get("checks") or []
    )
    rows = [
        ("Attack", f"{r.get('id')}: {r.get('title')}"),
        ("Class", r.get("class")),
        ("First layer", r.get("layer")),
        ("Expected / actual", f"{r.get('expected')} / {r.get('actual')}"),
        ("Reason codes", ", ".join(str(c) for c in r.get("reasonCodes") or []) or "-"),
        ("Funds lost", f"{int(r.get('fundsLost') or 0) / 1e6:.2f} USDC"),
        ("Notes", r.get("notes") or "-"),
    ]
    trs = "".join(
        f'<tr><td class="k" style="color:#5b6573;width:280px">{esc(str(k))}</td><td>{esc(str(v))}</td></tr>'
        for k, v in rows
    )
    return (
        f'<div class="card tbl fit"><h2>{esc(heading)}</h2><table>{trs}</table>'
        f'<h3 style="color:#1f4e9c">Checks</h3><ul class="checks">{checks}</ul>{extra}</div>'
    )
