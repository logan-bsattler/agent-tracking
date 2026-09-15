"""Static HTML usage report. No server, no external assets, inline SVG.

Built to answer one question: where did the week's budget go? So it reads in
that order: limits now, limits over time, spend per day by model, when in the
day it burns, then which projects and sessions did the spending.
"""

from __future__ import annotations

import html
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from . import usage

# Fixed slot per model family. Color follows the entity, never its rank.
MODEL_SLOTS = [("sonnet", 1), ("opus", 2), ("fable", 3), ("haiku", 4)]
SLOT_NAMES = {1: "Sonnet", 2: "Opus", 3: "Fable", 4: "Haiku", 5: "Other"}


def family(model: str) -> int:
    for key, slot in MODEL_SLOTS:
        if key in model:
            return slot
    return 5


def money(v: float) -> str:
    return f"${v:,.2f}"


def tokens(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return f"{int(n)}"


def when(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M") if ts else ""


def esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


CSS = """
:root { color-scheme: light;
  --surface:#fcfcfb; --page:#f9f9f7; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10);
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100; --s5:#e87ba4;
  --good:#0ca30c; --warn:#fab219; --serious:#ec835a; --critical:#d03b3b; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { color-scheme: dark;
  --surface:#1a1a19; --page:#0d0d0d; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181; } }
:root[data-theme="dark"] { color-scheme: dark;
  --surface:#1a1a19; --page:#0d0d0d; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181; }
* { box-sizing: border-box; }
body { margin:0; background:var(--page); color:var(--ink);
  font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; padding-block:24px; padding-inline:20px; }
main { max-width:1100px; margin:0 auto; }
h1 { font-size:22px; font-weight:600; margin:0 0 4px; }
h2 { font-size:15px; font-weight:600; margin:0 0 12px; }
.sub { color:var(--ink2); margin:0 0 20px; }
.row { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:20px; }
.tile,.card { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:14px 16px; }
.card { margin-bottom:20px; }
.tile .label { color:var(--ink2); font-size:13px; }
.tile .value { font-size:28px; font-weight:600; line-height:1.2; margin-top:4px; }
.tile .delta { font-size:12px; color:var(--ink2); margin-top:2px; }
.tile .bar { height:6px; border-radius:3px; background:var(--grid); margin-top:8px; overflow:hidden; }
.tile .bar i { display:block; height:100%; }
.band-ok i{background:var(--s1)} .band-warn i{background:var(--warn)} .band-drain i{background:var(--critical)}
.legend { display:flex; flex-wrap:wrap; gap:14px; font-size:12px; color:var(--ink2); margin-bottom:8px; }
.legend span::before { content:""; display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:6px; vertical-align:-1px; background:var(--c); }
svg { display:block; width:100%; height:auto; font:11px system-ui,sans-serif; }
svg text { fill:var(--muted); }
.axis { stroke:var(--axis); stroke-width:1; } .grid { stroke:var(--grid); stroke-width:1; }
.empty { color:var(--ink2); padding:20px 0; }
.empty code { background:var(--grid); padding:2px 6px; border-radius:4px; font-size:12px; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th,td { text-align:left; padding:7px 8px; border-bottom:1px solid var(--grid); vertical-align:top; }
th { color:var(--ink2); font-weight:500; font-size:12px; }
td.n,th.n { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
.wrap { overflow-x:auto; }
.dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:5px; vertical-align:0; }
.muted { color:var(--muted); }
#tip { position:fixed; pointer-events:none; background:var(--ink); color:var(--page); padding:6px 9px;
  border-radius:6px; font-size:12px; display:none; z-index:9; white-space:pre; }
.hit { opacity:0; }
"""

JS = """
const tip=document.getElementById('tip');
document.querySelectorAll('[data-tip]').forEach(el=>{
  el.addEventListener('mousemove',e=>{tip.textContent=el.dataset.tip;tip.style.display='block';
    tip.style.left=(e.clientX+12)+'px';tip.style.top=(e.clientY+12)+'px';});
  el.addEventListener('mouseleave',()=>tip.style.display='none');});
"""


def band(p: float | None, warn: float, drain: float) -> str:
    if p is None:
        return "band-ok"
    return "band-drain" if p >= drain else "band-warn" if p >= warn else "band-ok"


def _tile(label: str, value: str, delta: str = "", bar: tuple[float, str] | None = None) -> str:
    b = f'<div class="bar {bar[1]}"><i style="width:{min(bar[0], 100):.0f}%"></i></div>' if bar else ""
    return (f'<div class="tile"><div class="label">{esc(label)}</div><div class="value">{esc(value)}</div>'
            f'<div class="delta">{esc(delta)}</div>{b}</div>')


def kpis(s: dict[str, Any]) -> str:
    qv = s["quota"]
    t = s["totals"]
    out = []
    if qv and qv["five_hour_pct"] is not None:
        age = (s["generated"] - qv["ts"]) // 60
        out.append(_tile("5-hour limit used", f"{qv['five_hour_pct']:.0f}%",
                         f"resets {when(qv['five_hour_reset'])}" if qv["five_hour_reset"] else f"{age}m ago",
                         (qv["five_hour_pct"], band(qv["five_hour_pct"], 75, 90))))
        out.append(_tile("Weekly limit used", f"{qv['seven_day_pct']:.0f}%",
                         f"resets {when(qv['seven_day_reset'])}" if qv["seven_day_reset"] else f"{age}m ago",
                         (qv["seven_day_pct"], band(qv["seven_day_pct"], 85, 95))))
    else:
        out.append(_tile("Limits", "n/a", "statusLine hook not wired yet"))
    prev = s["prev_cost"]
    delta = f"vs {money(prev)} previous {s['days']}d" if prev else "no previous period"
    out.append(_tile(f"Spend, last {s['days']} days", money(t["cost"]), delta))
    out.append(_tile("Requests", f"{t['requests']:,}", f"{t['sessions']} sessions"))
    total_in = t["cache_read"] + t["input_tokens"] + t["cache_write"]
    hit = (t["cache_read"] / total_in * 100) if total_in else 0
    out.append(_tile("Cache hit rate", f"{hit:.0f}%", f"{tokens(t['cache_write'])} tokens written to cache"))
    sub = (t["subagent_cost"] / t["cost"] * 100) if t["cost"] else 0
    out.append(_tile("Output tokens", tokens(t["output_tokens"]), f"{sub:.0f}% of spend in subagents"))
    return '<div class="row">' + "".join(out) + "</div>"


# ------------------------------------------------------------------ charts

W, H, PL, PR, PT, PB = 1000, 260, 44, 12, 12, 28


def _yaxis(vmax: float, fmt, steps: int = 4) -> tuple[str, float]:
    if vmax <= 0:
        vmax = 1
    parts = []
    for i in range(steps + 1):
        v = vmax * i / steps
        y = PT + (H - PT - PB) * (1 - i / steps)
        cls = "axis" if i == 0 else "grid"
        parts.append(f'<line class="{cls}" x1="{PL}" x2="{W - PR}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text x="{PL - 6}" y="{y + 4:.1f}" text-anchor="end">{esc(fmt(v))}</text>')
    return "".join(parts), vmax


def quota_chart(s: dict[str, Any]) -> str:
    snaps = s["snapshots"]
    if len(snaps) < 2:
        return ('<div class="empty">No limit history yet. Run '
                '<code>python -m coord_mcp.usage setup</code> once; every prompt then records the 5-hour and '
                'weekly percentages here.</div>')
    t0, t1 = s["start"], s["end"]
    grid, _ = _yaxis(100, lambda v: f"{v:.0f}%")
    x = lambda ts: PL + (W - PL - PR) * (ts - t0) / max(t1 - t0, 1)  # noqa: E731
    y = lambda p: PT + (H - PT - PB) * (1 - p / 100)  # noqa: E731
    lines = []
    for key, slot in (("five_hour_pct", 1), ("seven_day_pct", 2)):
        pts = [(x(r["ts"]), y(r[key])) for r in snaps if r[key] is not None]
        if not pts:
            continue
        d = "M" + " L".join(f"{px:.1f},{py:.1f}" for px, py in pts)
        lines.append(f'<path d="{d}" fill="none" stroke="var(--s{slot})" stroke-width="2" '
                     f'stroke-linejoin="round" stroke-linecap="round"/>')
        px, py = pts[-1]
        lines.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4" fill="var(--s{slot})" stroke="var(--surface)" stroke-width="2"/>')
    hits = "".join(
        f'<rect class="hit" x="{x(r["ts"]) - 6:.1f}" y="{PT}" width="12" height="{H - PT - PB}" '
        f'data-tip="{esc(when(r["ts"]))}\n5h {r["five_hour_pct"] or 0:.0f}%  7d {r["seven_day_pct"] or 0:.0f}%"/>'
        for r in snaps)
    labels = "".join(
        f'<text x="{x(int(datetime.strptime(d, "%Y-%m-%d").timestamp())):.0f}" y="{H - 8}" text-anchor="middle">'
        f'{datetime.strptime(d, "%Y-%m-%d").strftime("%a %d")}</text>' for d in s["days_list"])
    legend = '<div class="legend"><span style="--c:var(--s1)">5-hour window</span><span style="--c:var(--s2)">Weekly window</span></div>'
    return legend + f'<svg viewBox="0 0 {W} {H}">{grid}{"".join(lines)}{hits}{labels}</svg>'


def _top_rounded(x: float, y: float, w: float, h: float, r: float = 4) -> str:
    r = min(r, h, w / 2)
    return (f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
            f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z")


def daily_chart(s: dict[str, Any]) -> str:
    days = s["days_list"]
    by_day = s["by_day"]
    if not any(by_day.values()):
        return '<div class="empty">No requests in this period.</div>'
    slots_used = sorted({family(m) for d in by_day.values() for m in d})
    day_tot = {d: sum(by_day.get(d, {}).values()) for d in days}
    grid, vmax = _yaxis(max(day_tot.values()) * 1.1, money)
    n = len(days)
    band_w = (W - PL - PR) / n
    bw = min(24, band_w * 0.6)
    ph = H - PT - PB
    bars, labels = [], []
    for i, d in enumerate(days):
        cx = PL + band_w * (i + 0.5)
        x0 = cx - bw / 2
        per_slot: dict[int, float] = {}
        for m, c in by_day.get(d, {}).items():
            per_slot[family(m)] = per_slot.get(family(m), 0) + c
        y_cursor = PT + ph
        segs = sorted(per_slot.items())
        for j, (slot, c) in enumerate(segs):
            h = ph * c / vmax
            top = j == len(segs) - 1
            y0 = y_cursor - h
            gap = 2 if j < len(segs) - 1 else 0
            if top:
                shape = f'<path d="{_top_rounded(x0, y0, bw, max(h - gap, 0))}" fill="var(--s{slot})"/>'
            else:
                shape = f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{bw:.1f}" height="{max(h - gap, 0):.1f}" fill="var(--s{slot})"/>'
            bars.append(shape)
            y_cursor = y0
        tip = f"{datetime.strptime(d, '%Y-%m-%d').strftime('%a %d %b')}  {money(day_tot[d])}\n" + "\n".join(
            f"{SLOT_NAMES[k]} {money(v)}" for k, v in segs)
        bars.append(f'<rect class="hit" x="{PL + band_w * i:.1f}" y="{PT}" width="{band_w:.1f}" height="{ph}" data-tip="{esc(tip)}"/>')
        if day_tot[d] >= 0.6 * vmax / 1.1:  # direct labels only on the days that matter
            labels.append(f'<text x="{cx:.1f}" y="{y_cursor - 5:.1f}" text-anchor="middle" style="fill:var(--ink2)">{money(day_tot[d])}</text>')
        labels.append(f'<text x="{cx:.1f}" y="{H - 8}" text-anchor="middle">{datetime.strptime(d, "%Y-%m-%d").strftime("%a %d")}</text>')
    legend = '<div class="legend">' + "".join(
        f'<span style="--c:var(--s{k})">{SLOT_NAMES[k]}</span>' for k in slots_used) + "</div>"
    return legend + f'<svg viewBox="0 0 {W} {H}">{grid}{"".join(bars)}{"".join(labels)}</svg>'


def hour_chart(s: dict[str, Any]) -> str:
    by_hour = s["by_hour"]
    if not any(by_hour):
        return '<div class="empty">No requests in this period.</div>'
    h_ = 180
    vmax = max(by_hour) * 1.1
    ph = h_ - PT - PB
    band_w = (W - PL - PR) / 24
    bw = min(24, band_w * 0.6)
    parts = []
    for i in range(5):
        v = vmax * i / 4
        y = PT + ph * (1 - i / 4)
        parts.append(f'<line class="{"axis" if i == 0 else "grid"}" x1="{PL}" x2="{W - PR}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text x="{PL - 6}" y="{y + 4:.1f}" text-anchor="end">{money(v)}</text>')
    for hr, c in enumerate(by_hour):
        x0 = PL + band_w * (hr + 0.5) - bw / 2
        hgt = ph * c / vmax
        parts.append(f'<path d="{_top_rounded(x0, PT + ph - hgt, bw, hgt)}" fill="var(--s1)"/>')
        parts.append(f'<rect class="hit" x="{PL + band_w * hr:.1f}" y="{PT}" width="{band_w:.1f}" height="{ph}" '
                     f'data-tip="{hr:02d}:00 to {hr + 1:02d}:00  {money(c)}"/>')
        if hr % 3 == 0:
            parts.append(f'<text x="{PL + band_w * (hr + 0.5):.1f}" y="{h_ - 8}" text-anchor="middle">{hr:02d}</text>')
    return f'<svg viewBox="0 0 {W} {h_}">{"".join(parts)}</svg>'


# ------------------------------------------------------------------ tables


def model_table(s: dict[str, Any]) -> str:
    rows = sorted(s["by_model"].items(), key=lambda kv: -kv[1]["cost"])
    total = sum(v["cost"] for _, v in rows) or 1
    body = "".join(
        f'<tr><td><i class="dot" style="background:var(--s{family(m)})"></i>{esc(m)}</td>'
        f'<td class="n">{money(v["cost"])}</td><td class="n">{v["cost"] / total * 100:.0f}%</td>'
        f'<td class="n">{v["requests"]:,}</td><td class="n">{tokens(v["ctx"])}</td><td class="n">{tokens(v["output"])}</td></tr>'
        for m, v in rows)
    return ('<div class="wrap"><table><tr><th>Model</th><th class="n">Spend</th><th class="n">Share</th>'
            '<th class="n">Requests</th><th class="n">Context tokens</th><th class="n">Output tokens</th></tr>'
            f"{body}</table></div>")


def project_table(s: dict[str, Any]) -> str:
    total = sum(p["cost"] for p in s["projects"]) or 1
    body = "".join(
        f'<tr><td>{esc(p["project"])}</td><td class="n">{money(p["cost"])}</td><td class="n">{p["cost"] / total * 100:.0f}%</td>'
        f'<td class="n">{p["sessions"]}</td><td class="n">{p["requests"]:,}</td><td class="n">{tokens(p["ctx_tokens"])}</td>'
        f'<td class="n">{tokens(p["output_tokens"])}</td></tr>' for p in s["projects"])
    return ('<div class="wrap"><table><tr><th>Project</th><th class="n">Spend</th><th class="n">Share</th>'
            '<th class="n">Sessions</th><th class="n">Requests</th><th class="n">Context tokens</th>'
            f'<th class="n">Output tokens</th></tr>{body}</table></div>')


def session_table(s: dict[str, Any]) -> str:
    body = []
    for r in s["sessions"]:
        dots = "".join(f'<i class="dot" style="background:var(--s{family(m)})"></i>' for m in sorted(set((r["models"] or "").split(","))))
        hit = f"{r['cache_hit'] * 100:.0f}%" if r["cache_hit"] is not None else ""
        sub = f"{r['subagent_cost'] / r['cost'] * 100:.0f}%" if r["cost"] and r["subagent_cost"] else ""
        dur = (r["last_ts"] - r["first_ts"]) // 60
        title = r["title"] or r["session_id"][:8]
        body.append(
            f'<tr><td>{esc(title)[:70]}<br><span class="muted">{esc(r["project"])} · {esc(r["session_id"][:8])}</span></td>'
            f'<td>{dots}</td><td class="n">{money(r["cost"])}</td><td class="n">{sub}</td><td class="n">{r["requests"]:,}</td>'
            f'<td class="n">{tokens(r["output_tokens"])}</td><td class="n">{hit}</td>'
            f'<td class="n">{when(r["first_ts"])}<br><span class="muted">{dur // 60}h{dur % 60:02d}m</span></td></tr>')
    return ('<div class="wrap"><table><tr><th>Session</th><th>Models</th><th class="n">Spend</th>'
            '<th class="n">In subagents</th><th class="n">Requests</th><th class="n">Output</th>'
            '<th class="n">Cache hit</th><th class="n">Started</th></tr>' + "".join(body) + "</table></div>")


# ------------------------------------------------------------------ page


def render(s: dict[str, Any]) -> str:
    period = f"{datetime.fromtimestamp(s['start']).strftime('%d %b')} to {datetime.fromtimestamp(s['end']).strftime('%d %b %Y')}"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Claude usage</title>
<style>{CSS}</style></head><body><main>
<h1>Claude usage</h1>
<p class="sub">{esc(period)} · generated {when(s['generated'])} · spend is API-equivalent, the same figure as <code>/cost</code></p>
{kpis(s)}
<div class="card"><h2>Limits over time</h2>{quota_chart(s)}</div>
<div class="card"><h2>Spend per day, by model</h2>{daily_chart(s)}</div>
<div class="card"><h2>Spend by hour of day</h2>{hour_chart(s)}</div>
<div class="card"><h2>By model</h2>{model_table(s)}</div>
<div class="card"><h2>By project</h2>{project_table(s)}</div>
<div class="card"><h2>Sessions, most expensive first</h2>{session_table(s)}</div>
<p class="sub">Source: Claude Code transcripts under ~/.claude/projects. Cache reads are priced at the cached rate; 1-hour cache writes at 2x input.</p>
</main><div id="tip"></div><script>{JS}</script></body></html>"""


def write(conn: sqlite3.Connection, out: Path, days: int = 7) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(usage.summary(conn, days)), encoding="utf-8")
    return out
