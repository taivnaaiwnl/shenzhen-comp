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


def crop_parts(parts: list[tuple[Path, float, tuple[float, float, float, float], list[tuple[Any, ...]]]], max_scale=2.2):
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
        return "<table>" + "".join(
            f'<tr><td class="k">{esc(k)}</td><td{" class=big" if k == "Amount" else ""}>{esc(v)}</td></tr>'
            for k, v in rows
        ) + "</table>"

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
