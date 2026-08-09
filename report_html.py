"""
report_html.py — SHARED RENDERING for the interactive report suite.
================================================================================
Presentation only. Data, exclusions, confidence and output-folder conventions
all stay in report_lib (the single seam); this file just turns already-trusted
numbers into a self-contained interactive HTML page. One place for the look, so
all four reports stay a family and restyling happens once.

Output: reuses report_lib.build_run_dir / copy_to_latest, writing report.html
(inline SVG, no PNG side-files) into reports/<slug>/<period>/ + latest/.
"""

import html as _html
from datetime import date

import report_lib as R

# --- design tokens (kept in sync with the suite wireframe) -------------------
CSS = """
:root{--paper:#FAFAF8;--ink:#1B1B19;--muted:#6C6A64;--faint:#9A978F;--line:#DAD7CF;
--box:#EFEDE7;--grid:#E6E3DB;--act:#B45309;--act-bg:#FBF1E3;--good:#3F7A4B;--warn:#B4820A;
--bad:#B0413A;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);
font-family:var(--sans);line-height:1.55;font-size:15px}
.wrap{max-width:960px;margin:0 auto;padding:0 22px 80px}
header{border-bottom:1px solid var(--line);background:var(--paper)}
.bar{max-width:960px;margin:0 auto;padding:14px 22px;display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
.bar h1{font-size:16px;margin:0;letter-spacing:.02em}
.bar .tag{font-family:var(--mono);font-size:11px;color:#fff;background:var(--good);border-radius:3px;padding:2px 7px}
.bar .date{font-family:var(--mono);font-size:11px;color:var(--faint);margin-left:auto}
.verdict{border:1px solid var(--act);background:var(--act-bg);border-radius:6px;padding:14px 16px;margin:20px 0 6px}
.verdict .lbl{font-family:var(--mono);font-size:10.5px;letter-spacing:.09em;color:var(--act);text-transform:uppercase}
.verdict .v{font-size:17px;font-weight:650;margin:5px 0 3px}.verdict .sub{font-size:13.5px;color:var(--muted)}
section.blk{border:1px solid var(--line);border-radius:6px;background:#fff;margin:14px 0;overflow:hidden}
.blk>.hd{display:flex;align-items:center;gap:10px;padding:9px 14px;border-bottom:1px solid var(--line);background:var(--box)}
.blk>.hd .n{font-family:var(--mono);font-size:11px;color:var(--faint)}.blk>.hd .t{font-size:13.5px;font-weight:600}
.blk>.hd .badge{margin-left:auto;font-family:var(--mono);font-size:10px;padding:2px 8px;border-radius:20px;background:var(--warn);color:#fff}
.blk>.bd{padding:14px}
.why{font-family:var(--mono);font-size:11.5px;color:var(--muted);margin:10px 2px 0}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--grid)}
th{font-family:var(--mono);font-size:10px;letter-spacing:.03em;color:var(--faint);text-transform:uppercase}
td.m,th.m{font-family:var(--mono)}tr:last-child td{border-bottom:none}
.depthbar{position:relative;height:16px;background:var(--grid);border-radius:3px;min-width:110px}
.depthbar i{position:absolute;left:0;top:0;bottom:0;background:var(--act);border-radius:3px;opacity:.85}
.depthbar span{position:absolute;right:4px;top:0;font-family:var(--mono);font-size:10px;line-height:16px}
.pill{font-family:var(--mono);font-size:10px;padding:1px 7px;border-radius:20px;color:#fff;display:inline-block}
.pill.hi{background:var(--good)}.pill.md{background:var(--warn)}.pill.lo{background:var(--bad)}
.dist{font-family:var(--mono);font-size:10.5px;font-weight:600}
.dist.urgent{color:var(--bad)}.dist.watch{color:var(--warn)}.dist.normal{color:var(--good)}
.sketch{border:1px solid var(--grid);border-radius:5px;background:#fff;padding:10px}
.caption{font-family:var(--mono);font-size:11px;color:var(--faint);margin-top:6px;text-align:center}
.maturing{border:1.5px dashed #C9B79A;background:repeating-linear-gradient(45deg,#fff,#fff 10px,#FBF7EF 10px,#FBF7EF 20px);
border-radius:5px;padding:14px 16px}
.maturing .mh{font-family:var(--mono);font-size:12px;font-weight:650;color:var(--act)}
.cov{display:flex;gap:20px;flex-wrap:wrap;font-family:var(--mono);font-size:11.5px;color:var(--muted)}
.cov b{color:var(--ink)}.cov .x{color:var(--bad)}
"""


def esc(s):
    return _html.escape(str(s))


# --- block primitives (return HTML strings) ----------------------------------
def verdict(label, headline, sub):
    return (f'<div class="verdict"><div class="lbl">{esc(label)}</div>'
            f'<div class="v">{headline}</div><div class="sub">{sub}</div></div>')


def section(n, title, body, badge=None):
    b = f'<span class="badge">{esc(badge)}</span>' if badge else ""
    return (f'<section class="blk"><div class="hd"><span class="n">{esc(n)}</span>'
            f'<span class="t">{esc(title)}</span>{b}</div><div class="bd">{body}</div></section>')


def table(headers, rows):
    """headers: list of (text, mono_bool). rows: list of list of cell-html."""
    head = "".join(f'<th class="m">{esc(t)}</th>' if m else f'<th>{esc(t)}</th>'
                   for t, m in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def pill(kind, label=None):
    """kind: hi|md|lo. label defaults to high/med/low."""
    label = label or {"hi": "high", "md": "med", "lo": "low"}.get(kind, kind)
    return f'<span class="pill {kind}">{esc(label)}</span>'


def conf_pill(n, hi=8, md=5):
    k = "hi" if n >= hi else "md" if n >= md else "lo"
    return pill(k)


def why(text):
    return f'<p class="why">{text}</p>'


def maturing(headline, body):
    return (f'<div class="maturing"><div class="mh">{esc(headline)}</div>'
            f'<p style="font-size:13px;margin:8px 0 0;color:var(--muted)">{body}</p></div>')


def coverage(items):
    """items: list of (text, kind) where kind in normal|excl|plain."""
    def one(t, k):
        cls = ' class="x"' if k == "excl" else ""
        return f"<span{cls}>{t}</span>"
    return '<div class="cov">' + "".join(one(t, k) for t, k in items) + "</div>"


# --- charts (inline SVG) -----------------------------------------------------
def hbars(items, value_key, label_key, unit="%", color_fn=None, caption=""):
    """Horizontal bars, sorted desc by value. items: list of dicts."""
    rows = sorted(items, key=lambda d: d[value_key], reverse=True)
    W, H, padL, padR = 640, max(90, 24 * len(rows) + 20), 100, 44
    rh = (H - 20) / max(1, len(rows))
    mx = max([r[value_key] for r in rows] + [1])
    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" height="auto">']
    for i, r in enumerate(rows):
        y = 10 + i * rh
        bw = (r[value_key] / mx) * (W - padL - padR)
        col = color_fn(r[value_key]) if color_fn else "#B45309"
        parts.append(
            f'<text x="{padL-6}" y="{y+rh/2+3:.0f}" font-family="monospace" font-size="10" '
            f'fill="#6C6A64" text-anchor="end">{esc(r[label_key])}</text>'
            f'<rect x="{padL}" y="{y+rh*0.2:.0f}" width="{bw:.0f}" height="{rh*0.6:.0f}" '
            f'fill="{col}" rx="2"/>'
            f'<text x="{padL+bw+5:.0f}" y="{y+rh/2+3:.0f}" font-family="monospace" '
            f'font-size="10" fill="#1b1b19">{r[value_key]}{unit}</text>')
    parts.append("</svg>")
    cap = f'<div class="caption">{esc(caption)}</div>' if caption else ""
    return f'<div class="sketch">{"".join(parts)}{cap}</div>'


def lines(series, xlabels, title="", caption=""):
    """Line chart. series: list of (name, values, color)."""
    W, H, padL, padR, padT, padB = 640, 180, 44, 12, 24, 24
    allv = [v for _, vals, _ in series for v in vals] or [0, 1]
    mn, mx = min(allv), max(allv)
    if mn == mx:
        mn -= 1; mx += 1
    p = (mx - mn) * 0.12; mn -= p; mx += p
    n = len(xlabels)
    sx = lambda i: padL + (i / (n - 1) if n > 1 else 0) * (W - padL - padR)
    sy = lambda v: padT + (1 - (v - mn) / (mx - mn)) * (H - padT - padB)
    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" height="auto">']
    if title:
        parts.append(f'<text x="{padL}" y="12" font-family="monospace" font-size="10" '
                     f'fill="#6C6A64">{esc(title)}</text>')
    for i, l in enumerate(xlabels):
        parts.append(f'<text x="{sx(i):.0f}" y="{H-6}" font-family="monospace" font-size="8" '
                     f'fill="#9A978F" text-anchor="middle">{esc(l)}</text>')
    for name, vals, col in series:
        d = " ".join(("M" if i == 0 else "L") + f"{sx(i):.1f} {sy(v):.1f}"
                     for i, v in enumerate(vals))
        parts.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="2"/>')
        parts.append(f'<circle cx="{sx(len(vals)-1):.1f}" cy="{sy(vals[-1]):.1f}" r="3" fill="{col}"/>')
    parts.append("</svg>")
    cap = f'<div class="caption">{esc(caption)}</div>' if caption else ""
    return f'<div class="sketch">{"".join(parts)}{cap}</div>'


def bands(items, caption=""):
    """Horizontal price bands. items: list of dict(label, lo, med, hi)."""
    W, padL, padR = 640, 116, 56
    H = max(90, 30 * len(items) + 20)
    rh = (H - 20) / max(1, len(items))
    lo = min(r["lo"] for r in items); hi = max(r["hi"] for r in items)
    span = (hi - lo) or 1
    sx = lambda v: padL + (v - lo) / span * (W - padL - padR)
    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" height="auto">']
    for i, r in enumerate(items):
        y = 10 + i * rh + rh / 2
        parts.append(
            f'<text x="{padL-6}" y="{y+3:.0f}" font-family="monospace" font-size="10" '
            f'fill="#6C6A64" text-anchor="end">{esc(r["label"])}</text>'
            f'<rect x="{sx(r["lo"]):.0f}" y="{y-5:.0f}" width="{sx(r["hi"])-sx(r["lo"]):.0f}" '
            f'height="10" rx="5" fill="#EFE0C6"/>'
            f'<circle cx="{sx(r["med"]):.0f}" cy="{y:.0f}" r="5" fill="#B45309"/>'
            f'<text x="{sx(r["hi"])+6:.0f}" y="{y+3:.0f}" font-family="monospace" '
            f'font-size="9" fill="#1b1b19">{int(r["med"])}</text>')
    parts.append("</svg>")
    cap = f'<div class="caption">{esc(caption)}</div>' if caption else ""
    return f'<div class="sketch">{"".join(parts)}{cap}</div>'


# --- page shell + write ------------------------------------------------------
def _page(title, subtitle, date_line, body, tag):
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{esc(title)}</title><style>{CSS}</style></head><body>'
            f'<header><div class="bar"><h1>{esc(title)}</h1>'
            f'<span class="tag">{esc(tag)}</span>'
            f'<span class="date">{esc(date_line)}</span></div></header>'
            f'<div class="wrap">{body}</div></body></html>')


def write(slug, title, subtitle, body, cadence="weekly", tag="LIVE DATA",
          date_line=None):
    """Render + write report.html using report_lib's folder convention."""
    run_dir, period = R.build_run_dir(slug, cadence)
    date_line = date_line or f"{cadence} · {date.today().isoformat()}"
    doc = _page(title, subtitle, date_line, body, tag)
    (run_dir / "report.html").write_text(doc)
    R.copy_to_latest(run_dir, slug)
    print(f"[OK] {slug}: {run_dir/'report.html'}")
    return run_dir
