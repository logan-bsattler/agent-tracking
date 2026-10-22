"""Static HTML usage report. No server, no external assets, inline SVG.

Built to answer one question: where did the week's budget go? So it reads in
that order: limits now, limits over time, spend per day by model, when in the
day it burns, then which projects and sessions did the spending.
"""

from __future__ import annotations

import html
from urllib.parse import quote
import json
import os
import re
import subprocess
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from . import hours, meetings, store, usage

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


when = usage.when


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


BAND_KPI = {"band-ok": "k-ok", "band-warn": "k-warn", "band-drain": "k-fail"}


def _tile(label: str, value: str, delta: str = "", bar: tuple[float, str] | None = None, cls: str = "k-info") -> str:
    """A usage number in the same tile the board's KPIs use: number, label,
    hint, and for a limit a bar coloured by how close it is."""
    b = f'<div class="bar {bar[1]}"><i style="width:{min(bar[0], 100):.0f}%"></i></div>' if bar else ""
    if bar:
        cls = BAND_KPI.get(bar[1], cls)
    return (f'<div class="kpi {cls}"><span class="n">{esc(value)}</span><span class="label">{esc(label)}</span>'
            f'<span class="hint">{esc(delta)}</span>{b}</div>')


def kpis(s: dict[str, Any]) -> str:
    qv = s["quota"]
    t = s["totals"]
    out = []
    if qv and (qv["five_hour_pct"] is not None or qv["seven_day_pct"] is not None):
        age = (s["generated"] - qv["ts"]) // 60
        for label, pk, rk, warn, hard in (("5-hour limit used", "five_hour_pct", "five_hour_reset", 75, 90),
                                          ("Weekly limit used", "seven_day_pct", "seven_day_reset", 85, 95)):
            if qv[pk] is None:
                continue
            out.append(_tile(label, f"{qv[pk]:.0f}%",
                             f"resets {when(qv[rk])}" if qv.get(rk) else f"read {age}m ago",
                             (qv[pk], band(qv[pk], warn, hard))))
    elif usage.hook_wired():
        out.append(_tile("Limits", "pending", "hook is wired; a session started since then will report on its next prompt",
                         cls="k-plain"))
    else:
        out.append(_tile("Limits", "n/a", "run: python -m coord_mcp.usage setup", cls="k-plain"))
    prev = s["prev_cost"]
    delta = f"vs {money(prev)} previous {s['days']}d" if prev else "no previous period"
    out.append(_tile(f"Spend, last {s['days']} days", money(t["cost"]), delta))
    if s["days"] != 30 and "cost_30d" in s:
        p30 = s["prev_cost_30d"]
        out.append(_tile("Spend, last 30 days", money(s["cost_30d"]),
                         f"vs {money(p30)} previous 30d" if p30 else "no previous period"))
    out.append(_tile("Requests", f"{t['requests']:,}", f"{t['sessions']} sessions", cls="k-alt"))
    total_in = t["cache_read"] + t["input_tokens"] + t["cache_write"]
    hit = (t["cache_read"] / total_in * 100) if total_in else 0
    out.append(_tile("Cache hit rate", f"{hit:.0f}%", f"{tokens(t['cache_write'])} tokens written to cache", cls="k-alt"))
    sub = (t["subagent_cost"] / t["cost"] * 100) if t["cost"] else 0
    out.append(_tile("Output tokens", tokens(t["output_tokens"]), f"{sub:.0f}% of spend in subagents", cls="k-alt"))
    a = s["attribution"]
    if a["claude_code_share"] is not None:
        share = a["claude_code_share"] * 100
        out.append(_tile("Limit used by Claude Code", f"{share:.0f}%",
                         f"{100 - share:.0f}% went to chat, Desktop or another machine",
                         (share, "band-ok")))
    return '<div class="kpis">' + "".join(out) + "</div>"


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
        if usage.hook_wired():
            return ('<div class="empty">The statusLine hook is wired but has not reported yet. Sessions started '
                    'before it was wired do not run it; the chart fills in from the next new session\'s first '
                    'prompt onward.</div>')
        return ('<div class="empty">No limit history yet. Run '
                '<code>python -m coord_mcp.usage setup</code> once; every prompt then records the 5-hour and '
                'weekly percentages here.</div>')
    t0, t1 = s["start"], s["end"]
    grid, _ = _yaxis(100, lambda v: f"{v:.0f}%")
    x = lambda ts: PL + (W - PL - PR) * (ts - t0) / max(t1 - t0, 1)  # noqa: E731
    y = lambda p: PT + (H - PT - PB) * (1 - p / 100)  # noqa: E731
    lines = []
    gap_s = 90 * 60  # don't draw a line across a period with no readings
    for key, slot in (("five_hour_pct", 1), ("seven_day_pct", 2)):
        segs: list[list[tuple[float, float]]] = []
        prev_ts = None
        for r in snaps:
            if r[key] is None:
                continue
            if prev_ts is None or r["ts"] - prev_ts > gap_s:
                segs.append([])
            segs[-1].append((x(r["ts"]), y(r[key])))
            prev_ts = r["ts"]
        if not segs:
            continue
        for seg in segs:
            if len(seg) == 1:
                lines.append(f'<circle cx="{seg[0][0]:.1f}" cy="{seg[0][1]:.1f}" r="2" fill="var(--s{slot})"/>')
                continue
            d = "M" + " L".join(f"{px:.1f},{py:.1f}" for px, py in seg)
            lines.append(f'<path d="{d}" fill="none" stroke="var(--s{slot})" stroke-width="2" '
                         f'stroke-linejoin="round" stroke-linecap="round"/>')
        px, py = segs[-1][-1]
        lines.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4" fill="var(--s{slot})" stroke="var(--surface)" stroke-width="2"/>')
    hits = "".join(
        f'<rect class="hit" x="{x(r["ts"]) - 6:.1f}" y="{PT}" width="12" height="{H - PT - PB}" '
        f'data-tip="{esc(when(r["ts"]))}\n5h {r["five_hour_pct"] or 0:.0f}%  7d {r["seven_day_pct"] or 0:.0f}%"/>'
        for r in snaps)
    labels = "".join(
        f'<text x="{x(int(datetime.strptime(d, "%Y-%m-%d").timestamp())):.0f}" y="{H - 8}" text-anchor="middle">'
        f'{datetime.strptime(d, "%Y-%m-%d").strftime("%a %d")}</text>' for d in s["days_list"])
    src = s.get("quota_sources") or []
    note = ""
    if "desktop" in src:
        note = ('<p class="sub" style="margin:6px 0 0">Includes Claude Desktop\'s own plan-usage history, which '
                'covers the whole shared pool. Lines break where there were no readings.</p>')
    legend = '<div class="legend"><span style="--c:var(--s1)">5-hour window</span><span style="--c:var(--s2)">Weekly window</span></div>'
    return legend + f'<svg viewBox="0 0 {W} {H}">{grid}{"".join(lines)}{hits}{labels}</svg>' + note


def attribution_chart(s: dict[str, Any]) -> str:
    """Limit consumed per day, split into what Claude Code explains and what it doesn't."""
    a = s["attribution"]
    if not a["points"]:
        note = usage.desktop_quota_note()
        return ('<div class="empty">Not enough limit readings in this period to attribute consumption.'
                + (f'<br><br>{esc(note)}' if note else "") + "</div>")
    days = s["days_list"]
    per_day = a["per_day"]
    tot = {d: sum(per_day.get(d, {}).values()) for d in days}
    grid, vmax = _yaxis(max(max(tot.values()), 1) * 1.1, lambda v: f"{v:.0f}")
    n = len(days)
    band_w = (W - PL - PR) / n
    bw = min(24, band_w * 0.6)
    ph = H - PT - PB
    bars, labels = [], []
    for i, d in enumerate(days):
        cx = PL + band_w * (i + 0.5)
        x0 = cx - bw / 2
        segs = [(k, v) for k, v in (("claude_code", per_day.get(d, {}).get("claude_code", 0)),
                                    ("elsewhere", per_day.get(d, {}).get("elsewhere", 0))) if v > 0]
        y_cursor = PT + ph
        for j, (key, v) in enumerate(segs):
            slot = 1 if key == "claude_code" else 2
            h = ph * v / vmax
            y0 = y_cursor - h
            gap = 2 if j < len(segs) - 1 else 0
            top = j == len(segs) - 1
            shape = (f'<path d="{_top_rounded(x0, y0, bw, max(h - gap, 0))}" fill="var(--s{slot})"/>' if top
                     else f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{bw:.1f}" height="{max(h - gap, 0):.1f}" fill="var(--s{slot})"/>')
            bars.append(shape)
            y_cursor = y0
        tip = (f"{datetime.strptime(d, '%Y-%m-%d').strftime('%a %d %b')}  {tot[d]:.0f} pts\n"
               + "\n".join(f"{'Claude Code' if k == 'claude_code' else 'Elsewhere'} {v:.0f}" for k, v in segs))
        bars.append(f'<rect class="hit" x="{PL + band_w * i:.1f}" y="{PT}" width="{band_w:.1f}" height="{ph}" data-tip="{esc(tip)}"/>')
        if tot[d] >= 0.6 * vmax / 1.1:
            labels.append(f'<text x="{cx:.1f}" y="{y_cursor - 5:.1f}" text-anchor="middle" style="fill:var(--ink2)">{tot[d]:.0f}</text>')
        labels.append(f'<text x="{cx:.1f}" y="{H - 8}" text-anchor="middle">{datetime.strptime(d, "%Y-%m-%d").strftime("%a %d")}</text>')
    legend = ('<div class="legend"><span style="--c:var(--s1)">Claude Code</span>'
              '<span style="--c:var(--s2)">Elsewhere (chat, Desktop, mobile, other machines)</span></div>')
    extra = ""
    if a["unattributed"]:
        extra = (f'<p class="sub" style="margin:8px 0 0">{a["unattributed"]:.0f} further points rose across gaps longer '
                 f'than six hours between readings, too coarse to attribute either way.</p>')
    return (legend + f'<svg viewBox="0 0 {W} {H}">{grid}{"".join(bars)}{"".join(labels)}</svg>'
            + '<p class="sub" style="margin:10px 0 0">Percentage points of the 5-hour window consumed per day. A rise '
            'counts as Claude Code when a local request falls in the same interval, and as elsewhere when none does.</p>' + extra)


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


def runway_chart(v: dict[str, Any], key: str = "burn", hours: float = 5) -> str:
    """A limit window as a runway: where the limit is now, where the pace
    lands it, and whether that happens before the window resets. key picks
    the pace in v: "burn" for the 5-hour window, "weekly" for the week.

    The whole question is whether the projected line crosses the ceiling
    before the right-hand edge, so the chart is drawn to make exactly that
    crossing visible.
    """
    b = v.get(key) or {}
    w = b.get("window") or {}
    rows, start, end = w.get("rows") or [], w.get("start"), w.get("end")
    if not rows or not start or not end:
        return ""
    t = v["as_of"]
    b_slope, hit = b.get("pct_per_hour"), b.get("hit_at")
    certain = bool(w.get("reset_known"))
    # Without a reset time the window end is only an estimate, so don't let it
    # set the right-hand edge: the crossing and "now" both have to stay visible.
    domain_end = end if certain else max([end, t + 900] + ([hit + 600] if hit else []))
    RW, RH, rl, rr, rt, rb = 1000, 170, 46, 78, 22, 28
    plot_w, plot_h = RW - rl - rr, RH - rt - rb
    span = max(domain_end - start, 1)
    x = lambda ts: rl + plot_w * min(max(ts - start, 0), span) / span  # noqa: E731
    y = lambda p: rt + plot_h * (1 - min(max(p, 0), 100) / 100)  # noqa: E731

    parts = [f'<rect x="{rl}" y="{rt}" width="{plot_w}" height="{plot_h}" fill="var(--grid)" opacity="0.35" rx="3"/>']
    for p, label in ((100, "limit"), (0, "")):
        yy = y(p)
        parts.append(f'<line class="{"axis" if p == 0 else "grid"}" x1="{rl}" x2="{rl + plot_w}" y1="{yy:.1f}" y2="{yy:.1f}"/>')
        parts.append(f'<text x="{rl - 6}" y="{yy + 4:.1f}" text-anchor="end">{p}%</text>')
        if label:
            parts.append(f'<text x="{rl + plot_w + 6}" y="{yy + 4:.1f}">{label}</text>')

    cur = rows[-1]["pct"]
    pts = [(x(r["ts"]), y(r["pct"])) for r in rows]
    area = (f'M{pts[0][0]:.1f},{y(0):.1f} L' + " L".join(f"{px:.1f},{py:.1f}" for px, py in pts)
            + f' L{pts[-1][0]:.1f},{y(0):.1f} Z')
    parts.append(f'<path d="{area}" fill="var(--s1)" opacity="0.14"/>')
    if len(pts) > 1:
        parts.append('<path d="M' + " L".join(f"{px:.1f},{py:.1f}" for px, py in pts)
                     + '" fill="none" stroke="var(--s1)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')

    short = hours <= 5
    hhmm = lambda ts: datetime.fromtimestamp(ts).strftime("%H:%M" if short else "%a %d %b %H:%M")  # noqa: E731
    # The 5-hour window counts in minutes; a week in hours and days.
    mins = (lambda sec: f"{sec // 60:.0f} minutes") if short else _span  # noqa: E731
    in_h = (lambda sec: f"{sec / 3600:.1f} hours") if short else _span  # noqa: E731
    length = "Five hours" if short else "A week"
    bad = bool(b.get("hits_limit_before_reset"))
    # Three states, not two: a green verdict needs a known reset time, because
    # without one the comparison it rests on cannot be made.
    state = "unknown" if not b_slope else "over" if bad else "ok" if certain else "unclear"
    colour = {"over": "var(--critical)", "ok": "var(--good)",
              "unclear": "var(--warn)", "unknown": "var(--muted)"}[state]

    end_pct = None
    if b_slope:
        if hit and (bad or not certain) and hit <= domain_end:
            ex, ey, lbl = x(hit), y(100), f"100% at {hhmm(hit)}"
        else:
            end_pct = min(cur + b_slope * (domain_end - t) / 3600, 100)
            ex, ey = x(domain_end), y(end_pct)
            lbl = f"{end_pct:.0f}% by reset" if certain else f"{end_pct:.0f}% in {in_h(domain_end - t)}"
        parts.append(f'<path d="M{pts[-1][0]:.1f},{pts[-1][1]:.1f} L{ex:.1f},{ey:.1f}" fill="none" '
                     f'stroke="{colour}" stroke-width="2" stroke-dasharray="5 4" stroke-linecap="round"/>')
        parts.append(f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="4.5" fill="{colour}" stroke="var(--surface)" stroke-width="2"/>')
        anchor = "end" if ex > rl + plot_w * 0.72 else "start"
        parts.append(f'<text x="{ex + (-8 if anchor == "end" else 8):.1f}" y="{ey - 9:.1f}" text-anchor="{anchor}" '
                     f'style="fill:{colour};font-weight:600">{esc(lbl)}</text>')

    stale = bool(w.get("stale_estimate"))
    if not certain and not stale:  # the estimated reset is a marker inside the plot, not the edge
        exr = x(end)
        parts.append(f'<line x1="{exr:.1f}" x2="{exr:.1f}" y1="{rt}" y2="{y(0):.1f}" stroke="var(--axis)" stroke-width="1"/>')
        parts.append(f'<text x="{exr:.1f}" y="{RH - 8}" text-anchor="middle">~{hhmm(end)} reset?</text>')

    nx = x(t)
    parts.append(f'<line x1="{nx:.1f}" x2="{nx:.1f}" y1="{rt - 4}" y2="{y(0):.1f}" stroke="var(--muted)" stroke-width="1"/>')
    parts.append(f'<circle cx="{nx:.1f}" cy="{y(cur):.1f}" r="4" fill="var(--s1)" stroke="var(--surface)" stroke-width="2"/>')
    parts.append(f'<text x="{nx:.1f}" y="{rt - 8:.1f}" text-anchor="middle" style="fill:var(--ink2)">now · {cur:.0f}%</text>')
    parts.append(f'<text x="{rl}" y="{RH - 8}" text-anchor="start">{hhmm(start)} window opened</text>')
    if certain:
        parts.append(f'<text x="{rl + plot_w}" y="{RH - 8}" text-anchor="end">{hhmm(end)} resets</text>')

    dot = f'<span class="dot" style="background:{colour}"></span>'
    if state == "over":
        verdict = (f'{dot}<b>Will hit the limit</b> at {hhmm(hit)}, {mins(hit - t)} from now and '
                   f'{mins(end - hit)} before the window resets, at the current {b_slope:.{0 if short else 1}f}% per hour.')
    elif state == "ok":
        verdict = (f'{dot}<b>On track.</b> At {b_slope:.{0 if short else 1}f}% per hour the window reaches about '
                   f'{end_pct:.0f}% by the time it resets, in {in_h(end - t)}.')
    elif state == "unclear":
        guess = (f'{length} after the first reading has already passed with no reset, so the window started later '
                 'than that reading and its end cannot even be guessed.' if stale else
                 f'The marked reset is a guess at {length.lower()} after the first reading.')
        verdict = (f'{dot}<b>Pace is {b_slope:.{0 if short else 1}f}% per hour</b>, reaching 100% at {hhmm(hit)}. No reset time is known '
                   f'yet, so whether that lands before the window resets cannot be said. {guess} Open a new Claude '
                   f'Code session and its status line reports the real reset time from then on.')
    else:
        verdict = f'{dot}Not enough readings in this window yet to project a pace.'
    return (f'<p class="sub" style="margin:0 0 8px">{verdict}</p>'
            f'<svg viewBox="0 0 {RW} {RH}">{"".join(parts)}</svg>')


def _span(sec: float) -> str:
    return f"{sec / 60:.0f} minutes" if sec < 7200 else f"{sec / 3600:.1f} hours" if sec < 172800 else f"{sec / 86400:.1f} days"


def now_section(v: dict[str, Any]) -> str:
    """The current 5-hour window: burn per 15 minutes and who is spending it."""
    q, b = v["quota"], v["burn"]
    head = []
    runway = runway_chart(v)
    if runway:
        head.append(runway)
    elif q and q.get("five_hour_pct") is not None:
        head.append('<p class="sub" style="margin:0 0 10px">Not enough recent snapshots to project a burn rate.</p>')
    extra = ""
    if b.get("headroom_usd") is not None:
        extra = f' · headroom about {money(b["headroom_usd"])} at the recent rate'
    head.append(f'<p class="sub" style="margin:14px 0 10px">Spend last 15 min {money(b["spend_last_15m"])} · '
                f'last hour {money(b["spend_last_hour"])} · this window {money(v["total"])}{extra}</p>')

    buckets = v["buckets"]
    h_ = 140
    ph = h_ - PT - PB
    vmax = max(max(buckets), 0.01) * 1.1
    band_w = (W - PL - PR) / len(buckets)
    bw = min(24, band_w * 0.6)
    parts = []
    for i in range(3):
        y = PT + ph * (1 - i / 2)
        parts.append(f'<line class="{"axis" if i == 0 else "grid"}" x1="{PL}" x2="{W - PR}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text x="{PL - 6}" y="{y + 4:.1f}" text-anchor="end">{money(vmax * i / 2)}</text>')
    for i, c in enumerate(buckets):
        x0 = PL + band_w * (i + 0.5) - bw / 2
        hgt = ph * c / vmax
        t0 = v["window_start"] + i * 900
        parts.append(f'<path d="{_top_rounded(x0, PT + ph - hgt, bw, hgt)}" fill="var(--s1)"/>')
        parts.append(f'<rect class="hit" x="{PL + band_w * i:.1f}" y="{PT}" width="{band_w:.1f}" height="{ph}" '
                     f'data-tip="{datetime.fromtimestamp(t0).strftime("%H:%M")} to {datetime.fromtimestamp(t0 + 900).strftime("%H:%M")}  {money(c)}"/>')
        if i % 4 == 0:
            parts.append(f'<text x="{PL + band_w * (i + 0.5):.1f}" y="{h_ - 8}" text-anchor="middle">{datetime.fromtimestamp(t0).strftime("%H:%M")}</text>')
    chart = f'<svg viewBox="0 0 {W} {h_}">{"".join(parts)}</svg>'

    rows = []
    for s in v["sessions"][:12]:
        title = s["title"] or s["session_id"][:8]
        live = '<span class="dot" style="background:var(--good)"></span>' if s["active"] else ""
        ctx = s["last_ctx"]
        ctx_style = ' style="color:var(--critical)"' if ctx >= 300000 else ' style="color:var(--serious)"' if ctx >= 150000 else ""
        rows.append(f'<tr><td>{live}{esc(title[:60])}<br><span class="muted">{esc(s["project"])} · {esc(s["session_id"][:8])}</span></td>'
                    f'<td class="n">{money(s["cost"])}</td><td class="n">{money(s["cost_15m"])}</td>'
                    f'<td class="n">{s["requests"]:,}</td><td class="n"{ctx_style}>{tokens(ctx)}</td>'
                    f'<td class="n">{tokens(s["peak_ctx"])}</td><td class="n">{when(s["last_ts"])}</td></tr>')
    table = ('<div class="wrap"><table><tr><th>Session</th><th class="n">This window</th><th class="n">Last 15m</th>'
             '<th class="n">Requests</th><th class="n">Context now</th><th class="n">Peak</th><th class="n">Last request</th></tr>'
             + "".join(rows) + "</table></div>") if rows else '<div class="empty">No requests in this window.</div>'
    return "".join(head) + chart + '<p class="sub" style="margin:12px 0 6px">Spend per 15 minutes across the window. Context in red is past the guard\'s hard limit; orange is past its warning.</p>' + table


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
            f'<tr><td>{esc(title[:70])}<br><span class="muted">{esc(r["project"])} · {esc(r["session_id"][:8])}</span></td>'
            f'<td>{dots}</td><td class="n">{money(r["cost"])}</td><td class="n">{sub}</td><td class="n">{r["requests"]:,}</td>'
            f'<td class="n">{tokens(r["output_tokens"])}</td><td class="n">{hit}</td>'
            f'<td class="n">{when(r["first_ts"])}<br><span class="muted">{dur // 60}h{dur % 60:02d}m</span></td></tr>')
    return ('<div class="wrap"><table><tr><th>Session</th><th>Models</th><th class="n">Spend</th>'
            '<th class="n">In subagents</th><th class="n">Requests</th><th class="n">Output</th>'
            '<th class="n">Cache hit</th><th class="n">Started</th></tr>' + "".join(body) + "</table></div>")


# ------------------------------------------------------------------ page


def render(s: dict[str, Any], v: dict[str, Any] | None = None, refresh: int | None = None) -> str:
    period = f"{datetime.fromtimestamp(s['start']).strftime('%d %b')} to {datetime.fromtimestamp(s['end']).strftime('%d %b %Y')}"
    now_card = f'<div class="card"><h2>Right now: the current 5-hour window</h2>{now_section(v)}</div>' if v else ""
    week = runway_chart(v, "weekly", 7 * 24) if v else ""
    week_card = f'<div class="card"><h2>This week: the weekly window</h2>{week}</div>' if week else ""
    sub = (f"{esc(period)} · generated {when(s['generated'])}{' · refreshes every ' + str(refresh) + 's' if refresh else ''}"
           " · spend is API-equivalent, the same figure as <code>/cost</code>")
    body = f"""{kpis(s)}
{now_card}
{week_card}
<div class="card"><h2>Limits over time</h2>{quota_chart(s)}</div>
<div class="card"><h2>Where the limit went</h2>{attribution_chart(s)}</div>
<div class="card"><h2>Spend per day, by model</h2>{daily_chart(s)}</div>
<div class="card"><h2>Spend by hour of day</h2>{hour_chart(s)}</div>
<div class="card"><h2>By model</h2>{model_table(s)}</div>
<div class="card"><h2>By project</h2>{project_table(s)}</div>
<div class="card"><h2>Sessions, most expensive first</h2>{session_table(s)}</div>
<p class="sub">Spend and sessions come from Claude Code transcripts under ~/.claude/projects, so they cover Claude Code only. Limit percentages are the shared subscription pool, read from the statusLine hook and from Claude Desktop's plan-usage history, so they also move for claude.ai chat, the Desktop app and mobile. Cache reads are priced at the cached rate; 1-hour cache writes at 2x input.</p>"""
    return page("Usage", "Claude usage", "Claude usage", sub, body, refresh=refresh,
                tail=f'<div id="tip"></div><script>{JS}</script>')


DRAFT_CSS = """.card.draft .btns{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:10px}
.card.draft h2 .count{margin-left:8px}
.card.draft{scroll-margin-top:12px}
.draft-body{white-space:pre-wrap;font:13px/1.45 ui-sans-serif,system-ui,sans-serif;background:var(--bg2,#f6f7f9);border:1px solid var(--line,#dde1e6);border-radius:8px;padding:12px 14px;margin:8px 0;max-height:60vh;overflow:auto}"""

BOARD_CSS = """
details.client { border-top:1px solid var(--grid); }
details.client summary { cursor:pointer; padding:8px 0; font-weight:600; list-style-position:inside; }
details.client summary .count { margin-left:8px; }
.grp { margin-bottom:20px; }
.grp h2 .count { color:var(--muted); font-weight:400; margin-left:6px; }
.grp.alert { border-color:var(--critical); border-width:2px; }
.grp.alert h2 { color:var(--critical); }
.item { display:grid; grid-template-columns:90px 1fr auto; gap:4px 12px; padding:8px 0; border-bottom:1px solid var(--grid); }
.item:last-child { border-bottom:0; }
.client { font-weight:600; }
.item .note { grid-column:2 / 4; color:var(--ink2); font-size:13px; }
.item.bare { grid-template-columns:1fr auto; } .item.bare .note { grid-column:1 / 3; }
.item form.go { grid-column:2 / 4; display:flex; gap:6px; align-items:center; margin-top:2px; }
.item form.go input[name=comment] { flex:1; max-width:52ch; font:inherit; font-size:13px; padding:4px 8px;
  border:1px solid var(--axis); border-radius:6px; background:var(--page); color:var(--ink); }
.item form.go button { font-size:13px; padding:4px 12px; } .item form.go .muted { font-size:12px; }
h3.type { font-size:13px; text-transform:uppercase; letter-spacing:.04em; color:var(--ink2); margin:14px 0 2px; }
h3.type .count { color:var(--muted); font-weight:400; margin-left:6px; }
.next .head { font-size:16px; } .next .head .item { border-bottom:2px solid var(--s1); }
.next .behind { color:var(--ink2); }
.item .age { color:var(--muted); font-size:12px; white-space:nowrap; font-variant-numeric:tabular-nums; }
.item code { color:var(--muted); font-size:12px; }
.clear { color:var(--good); font-weight:600; }
nav { margin:0 0 16px; font-size:13px; } nav a { color:var(--s1); }
a.drill { color:var(--ink); text-decoration:none; border-bottom:1px dotted var(--axis); }
a.drill:hover { color:var(--s1); border-bottom-color:var(--s1); }
dl.kv { display:grid; grid-template-columns:minmax(110px,max-content) 1fr; gap:6px 16px; margin:0; }
dl.kv dt { color:var(--ink2); font-size:13px; } dl.kv dd { margin:0; min-width:0; overflow-wrap:anywhere; }
dl.kv dl.kv { padding:6px 0 6px 10px; border-left:2px solid var(--grid); }
ul.vals { margin:0; padding-left:18px; } ul.vals li { margin:2px 0; overflow-wrap:anywhere; }
.state { display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px; font-weight:600;
  border:1px solid currentColor; margin-left:8px; vertical-align:3px; }
.st-done { color:var(--good); } .st-failed { color:var(--critical); } .st-open { color:var(--s1); }
.actions { margin-bottom:20px; } .actions .btns { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
form.act { display:inline; margin:0; }
form.act button { font:inherit; font-size:13px; padding:5px 12px; border-radius:6px; cursor:pointer;
  border:1px solid var(--axis); background:var(--surface); color:var(--ink); }
form.act button.primary { background:var(--s1); border-color:var(--s1); color:#fff; font-weight:600; }
form.act button:hover { border-color:var(--s1); }
ul.parked { list-style:none; margin:12px 0 0; padding:0; }
ul.parked li { padding:6px 0; border-top:1px solid var(--grid); overflow-wrap:anywhere; }
ul.parked .age { color:var(--muted); font-size:12px; margin-left:6px; }
form.go textarea { width:100%; box-sizing:border-box; font:inherit; font-size:14px; padding:8px; border-radius:6px;
  border:1px solid var(--axis); background:var(--page); color:var(--ink); resize:vertical; }
form.go .btns { display:flex; gap:10px; align-items:center; margin-top:8px; }
form.go button { font:inherit; font-size:14px; padding:6px 18px; border-radius:6px; cursor:pointer;
  background:var(--s1); border:1px solid var(--s1); color:#fff; font-weight:600; }
a.file { color:var(--s1); text-decoration:none; border-bottom:1px solid transparent; cursor:pointer; }
a.file:hover { border-bottom-color:var(--s1); }
a.reveal { display:inline-block; margin-left:4px; color:var(--muted); cursor:pointer; vertical-align:-2px; }
a.reveal:hover { color:var(--s1); } a.reveal svg { width:14px; height:14px; display:inline; }
.file.gone { color:var(--muted); text-decoration:line-through; text-decoration-color:var(--axis); cursor:not-allowed; }
ul.files { list-style:none; margin:0; padding:0; } ul.files li { padding:4px 0; overflow-wrap:anywhere; }
ul.files .where { color:var(--muted); font-size:12px; }
.card > h2 .count { color:var(--muted); font-weight:400; margin-left:6px; }
.card > h2 .count { color:var(--muted); font-weight:400; margin-left:6px; }
#opened { position:fixed; bottom:16px; right:16px; background:var(--ink); color:var(--page); padding:8px 12px;
  border-radius:6px; font-size:13px; display:none; z-index:9; max-width:60ch; overflow-wrap:anywhere; }
"""


def _age(ts: int, t: int) -> str:
    m = max(t - ts, 0) // 60
    return f"{m}m ago" if m < 60 else f"{m // 60}h ago" if m < 48 * 60 else f"{m // 1440}d ago"


def _by_client(rows: list[dict[str, Any]], t: int, scope: str = "") -> str:
    """One collapsible line per client, so a busy Needs-you reads as a short
    list of names with counts and opens only where you drill in. The client is
    the group heading, so the rows inside drop their client column."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(r.get("client") or "—", []).append(r)
    return "".join(
        f'<details class="client" data-client="{esc(scope + c)}"><summary>{esc(c)}<span class="count">{len(g)}</span></summary>'
        f'{_items(g, t, "", client=False)}</details>'
        for c, g in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])))


# Needs-you items sorted by what Ben has to do, so a sitting can take one kind
# at a time. A keyword guess on the item's text; failed tasks are always Fix.
NEED_TYPES = ("Fix failed", "Do in DEVL / on a server", "Review & send", "Decide")
STALE_DAYS = 7


def need_type(d: dict[str, Any]) -> str:
    if d.get("failed"):
        return "Fix failed"
    w = d["title"].lower()
    if re.search(r"\b(devl|compile|run t\d+|test plan|winscp|kiro)\b", w):
        return "Do in DEVL / on a server"
    if re.search(r"\b(review|send|forward|route)\b", w):
        return "Review & send"
    return "Decide"


def _answer_buttons(q: dict[str, Any], back: str) -> str:
    """One button per option of master's open question; a press queues
    "Answer on <ref>: <option>" for master, then returns to `back`."""
    return "".join(
        f'<form method="post" action="/api/prompt" class="act"><input type="hidden" name="action" value="answer">'
        f'<input type="hidden" name="question" value="{q["id"]}"><input type="hidden" name="option" value="{i}">'
        f'<input type="hidden" name="back" value="{esc(back)}">'
        f'<button{" class=primary" if i == q["recommended"] else ""}>{esc(o)}</button></form>'
        for i, o in enumerate(q["options"]))


def _next_up(rows: list[dict[str, Any]], t: int, q: dict[str, Any] | None = None) -> str:
    """The one thing to do first, and the three behind it. Master's open
    question leads when there is one: it is the head of Ben's queue as master
    ordered it. Otherwise failures lead, since they gate dispatch, then oldest."""
    order = sorted(rows, key=lambda d: (not d.get("failed"), d["since"]))
    if q:
        tid = q.get("task_id") or ""
        link = (f' <a class="drill" href="/board/task/{esc(tid)}">{esc(tid[:8])}</a>' if tid else "")
        head = (f'<div class="head"><p class="ask">{esc(q["question"])}{link}</p>'
                f'<div class="btns">{_answer_buttons(q, "/board")}</div></div>')
        behind = [d for d in order if not q.get("follow_up_id") or d.get("fid") != q["follow_up_id"]][:3]
    elif rows:
        head, behind = f'<div class="head">{_items([order[0]], t, "")}</div>', order[1:4]
    else:
        return ""
    return (f'<div class="card grp next"><h2>Next up</h2>{head}'
            + (f'<div class="behind">{_items(behind, t, "")}</div>' if behind else "") + "</div>")


# Every card on the board pages folds on a click of its heading. The board
# auto-refreshes, so the folded set lives in localStorage, keyed by page path
# and heading text (the count badge stripped, so a changing count keeps it).
COLLAPSE_CSS = """
.card > h2 { cursor: pointer; user-select: none; }
.card > h2::before { content: "\\25BE  "; color: var(--muted, #888); }
.card.collapsed > h2::before { content: "\\25B8  "; }
.card.collapsed > :not(h2) { display: none; }
"""
COLLAPSE_JS = """<script>
try { const k = "coord-collapsed", x = "coord-expanded",
    c = new Set(JSON.parse(localStorage.getItem(k) || "[]")), e = new Set(JSON.parse(localStorage.getItem(x) || "[]"));
  document.querySelectorAll(".card > h2").forEach(h => {
    const card = h.parentElement, label = h.cloneNode(true), shut = card.dataset.fold === "closed";
    label.querySelectorAll(".count").forEach(s => s.remove());
    const key = location.pathname + "|" + label.textContent.trim();
    if (shut ? !e.has(key) : c.has(key)) card.classList.add("collapsed");
    h.addEventListener("click", () => {
      const folded = card.classList.toggle("collapsed");
      if (shut) { folded ? e.delete(key) : e.add(key); } else { folded ? c.add(key) : c.delete(key); }
      try { localStorage.setItem(k, JSON.stringify([...c])); localStorage.setItem(x, JSON.stringify([...e])); }
      catch (err) {} }); }); } catch (err) {}
</script>"""


def _button(action: str, label: str, task: str = "", primary: bool = False) -> str:
    hidden = f'<input type="hidden" name="task" value="{esc(task)}">' if task else ""
    return (f'<form method="post" action="/api/prompt" class="act"><input type="hidden" name="action" '
            f'value="{esc(action)}">{hidden}<button{" class=primary" if primary else ""}>{esc(label)}</button></form>')


# ---- command-center board -------------------------------------------------

AGE_BUCKETS = ((86400, "fresh"), (3 * 86400, "day"), (7 * 86400, "week"))
EVENT_WORDS = {"picked_up": "picked up", "parked": "parked", "completed": "done", "failed": "failed"}


def _age_bucket(since: int, t: int) -> str:
    gap = max(t - since, 0)
    for limit, name in AGE_BUCKETS:
        if gap <= limit:
            return name
    return "old"


def _items(rows: list[dict[str, Any]], t: int, empty: str, client: bool = True, verb: str = "") -> str:
    """One row per item. `verb` ("running", "waiting") prefixes the age."""
    if not rows:
        return f'<p class="empty">{esc(empty)}</p>'
    out = []
    for d in rows:
        tid = f" <code>{esc(d['id'][:8])}</code>" if d["id"] else ""
        title = (f'<a class="drill" href="/board/task/{esc(d["id"])}">{esc(d["title"])}</a>' if d["id"]
                 else esc(d["title"]))
        note = f'<div class="note">{esc(d["note"][:220])}</div>' if d.get("note") else ""
        who = f'<div class="client">{esc(d["client"])}</div>' if client else ""
        age = (verb + " " if verb else "") + _age(d["since"], t)
        out.append(f'<div class="item{"" if client else " bare"}" data-since="{int(d["since"])}" '
                   f'data-age="{_age_bucket(d["since"], t)}">{who}'
                   f'<div>{title}{tid}</div><div class="age">{esc(age)}</div>{note}</div>')
    return "".join(out)


def _kpis(v: dict[str, Any], n_needs: int, n_done: int) -> str:
    """Six numbers across the top, each a link to the lane it counts."""
    parked = [d for d in v["master_owes"] if d.get("parked")]
    failed = [d for d in v["needs_you"] if d.get("failed")]
    tiles = (
        ("#flight", "Running", len(v["running"]), "k-run", "agents working now"),
        ("#flight", "Waiting", len(v["waiting"]), "k-wait", "created, not picked up"),
        ("#flight", "Parked", len(parked), "k-park", "owed a re-dispatch"),
        ("#needs", "Failed", len(failed), "k-fail", "needs a fix or a ruling"),
        ("#needs", "Waiting on you", n_needs, "k-you", "last 7 days"),
        ("#done", "Done today", n_done, "k-done", "last 24h"),
    )
    return '<div class="kpis">' + "".join(
        f'<a class="kpi {cls}{" zero" if not n else ""}" href="{href}"><span class="n">{n}</span>'
        f'<span class="label">{esc(label)}</span><span class="hint">{esc(hint)}</span></a>'
        for href, label, n, cls, hint in tiles) + "</div>"


def _fleet(rows: list[dict[str, Any]], t: int, selected: str | None) -> str:
    """One tile per client: a status light, what it is on, and what it owes.
    A click narrows the page to that client; a second click widens it again."""
    if not rows:
        return ""
    tiles = []
    for r in rows:
        bits = []
        if r["running"]:
            bits.append(f'<b class="c-run">{r["running"]} running</b>')
        if r["parked"]:
            bits.append(f'<b class="c-park">{r["parked"]} parked</b>')
        if r["waiting"]:
            bits.append(f'<span class="c-wait">{r["waiting"]} waiting</span>')
        if r["failed"]:
            bits.append(f'<b class="c-fail">{r["failed"]} failed</b>')
        if r["needs"]:
            bits.append(f'<span class="c-you">{r["needs"]} for you</span>')
        if r["done"]:
            bits.append(f'<span class="c-done">{r["done"]} done</span>')
        last = f'last activity {_age(r["last"], t)}' if r["last"] else "no activity yet"
        task = f'<div class="doing">{esc(r["task"][:70])}</div>' if r["task"] else ""
        href = "/board" if selected == r["client"] else f'/board?client={quote(r["client"])}'
        tiles.append(
            f'<a class="fleet-tile st-{r["state"]}{" selected" if selected == r["client"] else ""}" href="{href}">'
            f'<div class="head"><i class="light"></i><span class="name">{esc(r["client"])}</span>'
            f'<span class="state-word">{esc(r["state"])}</span></div>{task}'
            f'<div class="bits">{" · ".join(bits) if bits else "<span class=muted>quiet</span>"}</div>'
            f'<div class="last">{esc(last)}</div></a>')
    return f'<div class="fleet">{"".join(tiles)}</div>'


def _in_flight(v: dict[str, Any], t: int) -> str:
    """Running, held, not picked up, and parked (with its Re-dispatch button)."""
    parked = v.get("parked", [])
    held = [d for d in v["waiting"] if d.get("held")]
    loose = [d for d in v["waiting"] if not d.get("held")]
    parked_rows = "".join(
        f'<div class="item parked-row" data-since="{d["parked_at"]}" data-age="{_age_bucket(d["parked_at"], t)}">'
        f'<div class="client">{esc(d["client"])}</div>'
        f'<div><a class="drill" href="/board/task/{esc(d["id"])}">{esc(d["title"])}</a> '
        f'<code>{esc(d["id"][:8])}</code></div>'
        f'<div class="age">parked {d["parks"]}x · {_age(d["parked_at"], t)}</div>'
        f'<div class="note">{_button("redispatch", "Re-dispatch", d["id"], primary=True)}</div></div>'
        for d in parked)
    return (f'<div class="card lane" id="flight"><h2>In flight<span class="count">'
            f'{len(v["running"]) + len(v["waiting"]) + len(parked)}</span></h2>'
            f'<h3 class="type">Running<span class="count">{len(v["running"])}</span></h3>'
            f'{_items(v["running"], t, "Nothing picked up and in progress.", verb="running")}'
            + (f'<h3 class="type">Held<span class="count">{len(held)}</span></h3>'
               f'{_items(held, t, "", verb="waiting")}' if held else "") +
            f'<h3 class="type">Not picked up<span class="count">{len(loose)}</span></h3>'
            f'{_items(loose, t, "Every open task has been picked up or held.", verb="waiting")}'
            f'<h3 class="type">Parked<span class="count">{len(parked)}</span></h3>'
            f'{parked_rows or "<p class=empty>No parked tasks.</p>"}</div>')


def _day_label(day, today) -> str:
    gap = (today - day).days
    return "Today" if gap == 0 else "Yesterday" if gap == 1 else day.strftime("%a %b %d")


def _done(rows: list[dict[str, Any]], t: int) -> str:
    """Finished tasks grouped by day, newest first, each with its one-line gist."""
    if not rows:
        return '<p class="empty">Nothing finished recently.</p>'
    today = datetime.fromtimestamp(t).date()
    groups: dict[str, list[dict[str, Any]]] = {}
    for d in sorted(rows, key=lambda d: -d["since"]):
        groups.setdefault(_day_label(datetime.fromtimestamp(d["since"]).date(), today), []).append(d)
    return "".join(f'<h3 class="type">{esc(k)}<span class="count">{len(g)}</span></h3>{_items(g, t, "")}'
                   for k, g in groups.items())


def _timeline(events: list[dict[str, Any]], t: int, hours: int, cap: int = 80) -> str:
    """Every board event of the last day as one line: when, what, which task."""
    if not events:
        return f'<p class="empty">No agent activity in the last {hours}h.</p>'
    shown, more = events[:cap], max(len(events) - cap, 0)
    today = datetime.fromtimestamp(t).date()
    out, day_shown = [], None
    for e in shown:
        at = datetime.fromtimestamp(e["ts"])
        if at.date() != day_shown:
            day_shown = at.date()
            out.append(f'<div class="tl-day">{esc(_day_label(day_shown, today))}</div>')
        note = f' <span class="tl-note">{esc(e["note"][:140])}</span>' if e.get("note") else ""
        out.append(f'<div class="tl ev-{esc(e["event"])}" data-since="{e["ts"]}"><span class="tl-time">{at.strftime("%H:%M")}</span>'
                   f'<i class="tl-dot"></i><span class="tl-word">{esc(EVENT_WORDS.get(e["event"], e["event"]))}</span>'
                   f'<b class="tl-client">{esc(e["client"])}</b>'
                   f'<span class="tl-what"><a class="drill" href="/board/task/{esc(e["id"])}">{esc(e["title"])}</a>{note}</span></div>')
    if more:
        out.append(f'<p class="muted">and {more} earlier events</p>')
    return f'<div class="timeline">{"".join(out)}</div>'


# "new" marks what arrived since Ben last pressed Mark seen. The timestamp
# lives in localStorage, so the 30-second refresh does not wipe it.
SEEN_JS = """<script>
try { const key = "coord-seen", seen = Number(localStorage.getItem(key) || 0); let n = 0;
  const hhmm = s => new Date(s * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  document.querySelectorAll("[data-since]").forEach(el => {
    if (seen && Number(el.dataset.since) > seen) { el.classList.add("new"); n++; } });
  const b = document.getElementById("seen"), c = document.getElementById("newcount");
  if (c) c.textContent = !seen ? "press Mark seen to track what is new"
    : n ? n + " new since " + hhmm(seen) : "nothing new since " + hhmm(seen);
  if (b) b.addEventListener("click", () => {
    try { localStorage.setItem(key, String(Math.floor(Date.now() / 1000))); } catch (e) {}
    location.reload(); });
} catch (e) {}
</script>"""


MINDER_TITLE = ("Master-minder: the dashboard queues a wake prompt to master when the board shows work "
                "it owes and is not acting on (parked, never delivered, result unread, client loose end "
                "untasked, open intent). At most one per rate window; off with COORD_MINDER=off.")


def minder_line(m: dict[str, Any] | None, t: int) -> str:
    """'Minder: last 14:05, next 14:35' or the reason there is none to show."""
    if not m:
        return "Minder: unknown"
    if not m.get("enabled"):
        return "Minder: off"
    last = m.get("last_fired")
    nxt = m.get("next_eligible") or t
    if not last:
        return "Minder: never fired, eligible now"
    return (f"Minder: last {when(last)} ({_age(last, t)}), "
            + ("eligible now" if nxt <= t else f"next {when(nxt)}"))


def board_page(v: dict[str, Any], refresh: int | None = 30, client: str | None = None) -> str:
    """Operator command center: the fleet, what is in flight, what finished,
    what waits on Ben, and a timeline of what happened. `client` narrows every
    list to one client; the fleet strip stays whole so the way back is a click."""
    t = v["as_of"]
    fleet_rows = v.get("fleet", [])
    if client:
        v = {**v, **{k: [d for d in v[k] if d.get("client") == client]
                     for k in ("needs_you", "master_owes", "loose_ends", "running", "waiting",
                               "recent", "deferred", "acknowledged") if k in v}}
        v["parked"] = [d for d in v.get("parked", []) if d.get("client") == client]
        v["activity"] = [e for e in v.get("activity", []) if e.get("client") == client]
    fresh = [d for d in v["needs_you"] if t - d["since"] <= STALE_DAYS * 86400]
    stale = [d for d in v["needs_you"] if t - d["since"] > STALE_DAYS * 86400]
    n = len(fresh)
    typed: dict[str, list[dict[str, Any]]] = {}
    for d in sorted(fresh, key=lambda d: (not d.get("failed"), -d["since"])):
        typed.setdefault(need_type(d), []).append(d)
    needs = "".join(
        f'<h3 class="type">{esc(k)}<span class="count">{len(typed[k])}</span></h3>{_by_client(typed[k], t, k + "|")}'
        for k in NEED_TYPES if k in typed)
    if not v["needs_you"]:
        needs = '<p class="clear">Nothing needs you.</p>'
    elif not fresh:
        needs = f'<p class="empty">Nothing from the last {STALE_DAYS} days; the rest is under Stale.</p>'
    done_24h = [d for d in v["recent"] if t - d["since"] <= 86400]
    queued = v.get("queued", 0)
    queued_note = (f'<span class="age">{queued} queued for master</span>' if queued
                   else '<span class="age">sent to master when idle</span>')
    minder_note = f'<span class="age minder" title="{esc(MINDER_TITLE)}">{esc(minder_line(v.get("minder"), t))}</span>'
    scope = (f'<span class="scope">showing <b>{esc(client)}</b> · <a href="/board">all clients</a></span>'
             if client else "")
    def grp(title: str, rows: list, empty: str, cls: str = "", shut: bool = False) -> str:
        fold = ' data-fold="closed"' if shut else ""
        return (f'<div class="card grp {cls}"{fold}><h2>{esc(title)}<span class="count">{len(rows)}</span></h2>'
                f"{_items(rows, t, empty)}</div>")
    def by_client_grp(title: str, rows: list, empty: str) -> str:
        body = _by_client(rows, t, title + "|") if rows else f'<p class="empty">{esc(empty)}</p>'
        return (f'<div class="card grp" data-fold="closed"><h2>{esc(title)}<span class="count">{len(rows)}</span></h2>'
                f"{body}</div>")
    sub = f"as of {when(t)}{' · refreshes every ' + str(refresh) + 's' if refresh else ''} · straight from ~/.coord/coord.db {scope}"
    actions = (f'{_button("next", "Next question", primary=True)}{_button("board", "Board check")}'
               f'{queued_note}{minder_note}{SEEN_BOX}')
    body = f"""{_kpis(v, n, len(done_24h))}
{_fleet(fleet_rows, t, client)}
{_next_up(fresh, t, v.get("question"))}
<div class="lanes">
<div class="col">
<div class="card grp lane {'alert' if n else ''}" id="needs"><h2>Needs you<span class="count">{n}</span></h2>{needs}</div>
</div>
<div class="col">
{_in_flight(v, t)}
<div class="card lane" id="done"><h2>Done, last {v['recent_h']}h<span class="count">{len(v['recent'])}</span></h2>{_done(v["recent"], t)}</div>
</div>
</div>
<div class="card lane" id="activity"><h2>Activity, last 24h<span class="count">{len(v.get("activity", []))}</span></h2>{_timeline(v.get("activity", []), t, 24)}</div>
<script>/* the page auto-refreshes, so keep drilled-in clients open across reloads */
try {{ const k = "coord-open", o = new Set(JSON.parse(localStorage.getItem(k) || "[]"));
  document.querySelectorAll("details.client").forEach(d => {{
    if (o.has(d.dataset.client)) d.open = true;
    d.addEventListener("toggle", () => {{ d.open ? o.add(d.dataset.client) : o.delete(d.dataset.client);
      try {{ localStorage.setItem(k, JSON.stringify([...o])); }} catch (e) {{}} }}); }}); }} catch (e) {{}}
</script>
<div class="lanes">
<div class="col">{grp("Master owes", v["master_owes"], "Nothing: no parked tasks, no open intents, no follow-ups.")}</div>
<div class="col">{grp("Others owe", v["loose_ends"], "No follow-ups waiting on clients or colleagues.")}</div>
</div>
{by_client_grp(f"Stale, over {STALE_DAYS} days", stale, "Nothing of yours is older than a week.")}
{by_client_grp("Deferred", v.get("deferred", []), "Nothing deferred.")}
{grp("Acknowledged failures", v.get("acknowledged", []), "No failures on record.", shut=True)}"""
    return page("Board", f"{'(' + str(n) + ') ' if n else ''}Coord board", "Command center", sub, body,
                actions=actions, refresh=refresh)


CENTER_CSS = """
body.wide main { max-width:1500px; }
.topbar { display:flex; flex-wrap:wrap; justify-content:space-between; align-items:flex-start; gap:12px 24px; margin-bottom:16px; }
.topbar .sub { margin:0; } .topbar .btns { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
.topbar .scope { margin-left:8px; } .topbar .scope a { color:var(--s1); }
.seenbox { display:inline-flex; gap:8px; align-items:center; margin-left:12px; padding-left:12px; border-left:1px solid var(--grid); }
.seenbox button { font:inherit; font-size:13px; padding:5px 12px; border-radius:6px; cursor:pointer;
  border:1px solid var(--axis); background:var(--surface); color:var(--ink); }
.kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin-bottom:14px; }
.kpi { display:block; background:var(--surface); border:1px solid var(--border); border-left:5px solid var(--k); border-radius:10px;
  padding:10px 14px; text-decoration:none; color:var(--ink); }
.kpi .n { display:block; font-size:30px; font-weight:700; line-height:1.1; color:var(--k); font-variant-numeric:tabular-nums; }
.kpi .label { display:block; font-size:13px; font-weight:600; margin-top:2px; }
.kpi .hint { display:block; font-size:11px; color:var(--muted); }
.kpi.zero .n { color:var(--muted); } .kpi.zero { border-left-color:var(--grid); }
.k-run { --k:var(--s1); } .k-wait { --k:var(--s4); } .k-park { --k:var(--serious); } .k-fail { --k:var(--critical); }
.k-you { --k:var(--critical); } .k-done { --k:var(--good); }
.fleet { display:grid; grid-template-columns:repeat(auto-fill,minmax(190px,1fr)); gap:8px; margin-bottom:16px; }
.fleet-tile { display:block; background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:9px 11px;
  text-decoration:none; color:var(--ink); font-size:12px; min-height:92px; --l:var(--muted); }
.fleet-tile:hover { border-color:var(--s1); } .fleet-tile.selected { outline:2px solid var(--s1); }
.fleet-tile .head { display:flex; align-items:center; gap:7px; }
.fleet-tile .name { font-weight:700; font-size:14px; } .fleet-tile .state-word { margin-left:auto; color:var(--l); font-weight:600; }
.fleet-tile .light { width:10px; height:10px; border-radius:50%; background:var(--l); flex:none; }
.fleet-tile.st-running { --l:var(--s1); }
.fleet-tile.st-running .light { box-shadow:0 0 0 3px color-mix(in srgb, var(--s1) 25%, transparent); animation:pulse 1.6s ease-in-out infinite; }
.fleet-tile.st-parked { --l:var(--serious); } .fleet-tile.st-failed { --l:var(--critical); }
.fleet-tile.st-waiting { --l:var(--s4); } .fleet-tile.st-idle { --l:var(--muted); }
.fleet-tile.st-idle .name { color:var(--ink2); }
@keyframes pulse { 50% { box-shadow:0 0 0 6px color-mix(in srgb, var(--s1) 10%, transparent); } }
.fleet-tile .doing { color:var(--ink2); margin-top:4px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.fleet-tile .bits { margin-top:5px; line-height:1.5; } .fleet-tile .last { color:var(--muted); font-size:11px; margin-top:3px; }
.c-run { color:var(--s1); } .c-park { color:var(--serious); } .c-fail { color:var(--critical); } .c-wait { color:var(--s4); }
.c-you { color:var(--critical); font-weight:600; } .c-done { color:var(--good); }
.lanes { display:grid; grid-template-columns:1fr 1fr; gap:16px; align-items:start; }
.lanes .col { min-width:0; }
@media (max-width: 900px) { .lanes { grid-template-columns:1fr; } }
.lane .item { border-left:4px solid var(--grid); padding-left:10px; margin-left:-10px; }
.lane .item[data-age="fresh"] { border-left-color:var(--s1); }
.lane .item[data-age="day"] { border-left-color:var(--s4); }
.lane .item[data-age="week"] { border-left-color:var(--serious); }
.lane .item[data-age="old"] { border-left-color:var(--critical); }
.item.new { background:color-mix(in srgb, var(--s2) 8%, transparent); }
.item.new .age::after, .tl.new .tl-time::after { content:"new"; color:var(--s2); font-weight:700; font-size:11px; margin-left:6px; }
.parked-row .note form.act button { margin-top:2px; }
.timeline { max-height:480px; overflow:auto; }
.tl-day { font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--ink2); margin:10px 0 4px; font-weight:600; }
.tl { display:grid; grid-template-columns:52px 14px 72px 72px 1fr; gap:0 8px; align-items:baseline; padding:3px 0; font-size:13px; }
.tl-time { color:var(--muted); font-variant-numeric:tabular-nums; font-size:12px; white-space:nowrap; }
.tl-dot { width:9px; height:9px; border-radius:50%; background:var(--e); display:inline-block; vertical-align:-1px; }
.tl-word { color:var(--e); font-weight:600; font-size:12px; } .tl-client { font-weight:600; }
.tl-note { color:var(--muted); font-size:12px; } .tl-what { overflow-wrap:anywhere; }
.ev-picked_up { --e:var(--s1); } .ev-completed { --e:var(--good); } .ev-failed { --e:var(--critical); } .ev-parked { --e:var(--serious); }
.next { border-left:5px solid var(--s1); }
.k-ok { --k:var(--good); } .k-warn { --k:var(--warn); } .k-info { --k:var(--s1); } .k-alt { --k:var(--s3); }
.k-plain { --k:var(--ink2); } .k-hours { --k:var(--s1); } .k-all { --k:var(--ink); }
.kpi .bar { height:6px; border-radius:3px; background:var(--grid); margin-top:8px; overflow:hidden; }
.kpi .bar i { display:block; height:100%; }
.kpi.selected { outline:2px solid var(--s1); }
.item[hidden], .card[hidden] { display:none; }
a.kpi:hover, button.kpi:hover { border-color:var(--s1); }
button.kpi { font:inherit; text-align:left; cursor:pointer; width:100%; }
.topbar form.filter { display:flex; flex-wrap:wrap; gap:6px 10px; align-items:center; margin:0; font-size:13px; }
.topbar form.filter label { white-space:nowrap; }
.topbar form.filter input { font:inherit; font-size:13px; padding:4px 7px; border-radius:6px;
  border:1px solid var(--axis); background:var(--surface); color:var(--ink); }
.topbar form.filter input[type=number] { width:4.5em; } .topbar form.filter input[type=search] { width:22ch; }
.topbar form.filter button, .topbar a.btn { font:inherit; font-size:13px; padding:5px 12px; border-radius:6px; cursor:pointer;
  border:1px solid var(--axis); background:var(--surface); color:var(--ink); text-decoration:none; }
.topbar form.filter button:hover, .topbar a.btn:hover { border-color:var(--s1); }
.topbar form.go { display:flex; flex-wrap:wrap; gap:8px; align-items:flex-start; margin:0; }
.topbar form.go textarea { width:min(48ch, 70vw); font-size:13px; padding:6px 8px; }
.topbar form.go .btns { margin-top:0; flex-direction:column; align-items:flex-start; gap:4px; }
.topbar form.go .btns .muted { font-size:12px; }
.topbar form.go .btns .next-task { font-size:13px; }
.topbar form.go .btns .next-task.primary { font-weight:600; }
.item .what { min-width:0; overflow-wrap:anywhere; }
.item .path { color:var(--muted); font-size:12px; overflow-wrap:anywhere; }
.item .size { color:var(--muted); font-size:12px; margin-right:8px; }
.item .act { color:var(--ink2); font-size:13px; }
.lane .item.owes { background:color-mix(in srgb, var(--critical) 5%, transparent); }
.count.owed { color:var(--critical); font-weight:600; }
"""

# Nav links in the order every page shows them; the current page is bold.
NAV = (("/", "Usage"), ("/board", "Board"), ("/hours", "Hours"), ("/drafts", "Drafts"),
       ("/deliverables", "Deliverables"), ("/intents", "Intents"))

SEEN_BOX = ('<span class="seenbox"><span id="newcount" class="age"></span>'
            '<button id="seen" type="button">Mark seen</button></span>')


def page(current: str, title: str, h1: str, sub: str, body: str, actions: str = "",
         refresh: int | None = None, tail: str = "") -> str:
    """Every dashboard page, so none can drift from the others: the one nav
    (`current` bold), the topbar (h1 and sub on the left, the page's actions
    on the right), the wide command-center layout, folding cards, and the
    Mark-seen tagging of anything carrying data-since. `title` is text;
    `h1`, `sub`, `body`, `actions` and `tail` are HTML. `refresh` adds the
    meta refresh; `tail` goes after the body for a page's own scripts."""
    nav = "<nav>" + " · ".join(f"<b>{esc(label)}</b>" if label == current else f'<a href="{href}">{esc(label)}</a>'
                               for href, label in NAV) + "</nav>"
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    btns = f'<div class="btns">{actions}</div>' if actions else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">{meta}'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>'
            f'<style>{CSS}{BOARD_CSS}{COLLAPSE_CSS}{CENTER_CSS}{DRAFT_CSS}</style></head><body class="wide"><main>'
            f'{nav}<div class="topbar"><div><h1>{h1}</h1><p class="sub">{sub}</p></div>{btns}</div>\n'
            f'{body}\n{COLLAPSE_JS}{SEEN_JS}{tail}</main></body></html>')


def _val(v: Any, text=esc) -> str:
    """Any JSON value as readable HTML: dicts as key/value, lists as bullets.
    `text` renders each string; task_page passes one that links file paths."""
    if isinstance(v, dict):
        if not v:
            return '<span class="muted">none</span>'
        return '<dl class="kv">' + "".join(
            f"<dt>{esc(str(k).replace('_', ' '))}</dt><dd>{_val(x, text)}</dd>" for k, x in v.items()) + "</dl>"
    if isinstance(v, list):
        if not v:
            return '<span class="muted">none</span>'
        return '<ul class="vals">' + "".join(f"<li>{_val(x, text)}</li>" for x in v) + "</ul>"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if v is None:
        return '<span class="muted">—</span>'
    return text(v) if isinstance(v, str) else esc(v)


FOLDER_SVG = ('<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.4" '
              'stroke-linejoin="round" d="M1.5 4a1 1 0 0 1 1-1h3.6l1.5 1.6h5.9a1 1 0 0 1 1 1V12a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1z"/></svg>')


def file_link(task_id: str, path: str, exists: bool, text: str | None = None) -> str:
    """A file as Explorer would open it: the name opens it on this machine,
    the folder icon shows it in its folder. The dashboard does the opening
    (POST /api/open), since a browser will not follow file:// from http."""
    text = esc(text if text is not None else path)
    if not exists:
        return f'<span class="file gone" title="Not on disk: {esc(path)}">{text}</span>'
    attrs = f'href="#" data-task="{esc(task_id)}" data-path="{esc(path)}" data-name="{esc(Path(path).name)}"'
    return (f'<a class="file" {attrs} data-how="open" title="Open {esc(path)}">{text}</a>'
            f'<a class="reveal" {attrs} data-how="reveal" title="Show in folder">{FOLDER_SVG}</a>')


def _linker(task_id: str, files: list[dict[str, Any]]):
    """str -> HTML, with every path in the task's file list linked in place."""
    by_raw = {r: f for f in files for r in f["raw"]}
    if not by_raw:
        return esc
    pat = re.compile("|".join(re.escape(r) for r in sorted(by_raw, key=len, reverse=True)))

    def link(s: Any) -> str:
        s, out, at = str(s), [], 0
        for m in pat.finditer(s):
            f = by_raw[m.group()]
            out += [esc(s[at:m.start()]), file_link(task_id, f["path"], f["exists"], m.group())]
            at = m.end()
        return "".join(out) + esc(s[at:])
    return link


# A click on a file link posts to /api/open; the outcome shows in a toast.
FILE_JS = """<div id="opened"></div><script>
(() => { const t = document.getElementById("opened"); let h;
  const say = m => { t.textContent = m; t.style.display = "block"; clearTimeout(h);
    h = setTimeout(() => t.style.display = "none", 3000); };
  document.addEventListener("click", async e => {
    const a = e.target.closest("a[data-how]"); if (!a) return; e.preventDefault();
    const d = a.dataset;
    try { const r = await fetch("/api/open", {method: "POST", headers: {Accept: "application/json"},
        body: new URLSearchParams({task: d.task, path: d.path, how: d.how})});
      const j = await r.json();
      say(j.ok ? (d.how === "reveal" ? "Showing " : "Opening ") + d.name : (j.error || "Could not open " + d.name));
    } catch (err) { say("Could not reach the dashboard"); } }); })();
</script>"""


def open_path(p: Path, how: str = "open") -> None:
    """Open a file as a double-click in Explorer would, or select it there."""
    if how == "reveal":
        subprocess.Popen(f'explorer /select,"{p}"')
    else:
        os.startfile(str(p))  # type: ignore[attr-defined]  # Windows only, as is this dashboard


def task_page(d: dict[str, Any] | None, task_id: str = "", queued: bool = False) -> str:
    """One task in full: what was asked, what came back, and how it got there."""
    if d is None:
        return page("Board", "Coord task", "No such task",
                    f'Nothing on the board has id <code>{esc(task_id)}</code>.', "")
    took = ""
    if d["completed_at"]:
        m = (d["completed_at"] - d["created_at"]) // 60
        took = (" · took under a minute" if m < 1 else f" · took {m}m" if m < 120
                else f" · took {m // 60}h{m % 60:02d}m")
    meta = {"client": d["assigned_to"] or "—", "kind": d["kind"], "id": d["id"],
            "created": when(d["created_at"]), "finished": when(d["completed_at"]) if d["completed_at"] else "—"}
    if d["parent_id"]:
        meta["parent"] = d["parent_id"]
    if d["from_intent"]:
        meta["from intent"] = d["from_intent"]
    cards = ['<div class="card"><dl class="kv">' + "".join(
        f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in meta.items()) + "</dl></div>"]
    files = d.get("files") or []
    link = _linker(d["id"], files)
    if files:
        items = "".join(
            f'<li>{file_link(d["id"], f["path"], f["exists"], Path(f["path"]).name)}'
            f' <span class="where">{esc(str(Path(f["path"]).parent))}'
            f'{"" if f["exists"] else " · not on disk"}</span></li>' for f in files)
        cards.append(f'<div class="card"><h2>Files<span class="count">{len(files)}</span></h2>'
                     f'<ul class="files">{items}</ul></div>')
    if d["result"] is not None:
        shown = {k: x for k, x in d["result"].items() if k != "follow_ups"}
        cards.append(f'<div class="card"><h2>Result</h2>{_val(shown, link)}</div>')
    else:
        cards.append('<div class="card"><h2>Result</h2><p class="empty">Still open; nothing reported yet.</p></div>')
    if d.get("follow_ups"):
        items = []
        for f in d["follow_ups"]:
            if f["state"] == "tasked":
                how = f'tasked → <a class="drill" href="/board/task/{esc(f["resolved_by"])}">{esc(f["resolved_by"])}</a>'
            elif f["state"] == "dropped":
                how = f'dropped (decision <code>{esc(f["resolved_by"])}</code>)'
            else:
                how = '<b class="st-failed">open</b>'
            items.append(f'<li><b>{esc(f["who"])}</b>: {link(f["what"])} <span class="muted">— </span>{how}</li>')
        cards.append(f'<div class="card"><h2>Follow-ups</h2><ul class="vals">{"".join(items)}</ul></div>')
    cards.append(f'<div class="card"><h2>What was asked</h2>{_val(d["spec"], link)}</div>')
    for i, p in enumerate(d["parks"], 1):
        p = {k: v for k, v in p.items() if k not in ("park_id", "at")}
        cards.append(f'<div class="card"><h2>Park {i} of {len(d["parks"])}</h2>{_val(p, link)}</div>')
    if d["decisions"]:
        items = "".join(
            f'<li>{esc(x["statement"])}'
            + (f' <span class="muted">— {esc(x["because"])}</span>' if x["because"] else "")
            + f' <span class="muted">({when(x["created_at"])})</span></li>' for x in d["decisions"])
        cards.append(f'<div class="card"><h2>Decisions</h2><ul class="vals">{items}</ul></div>')
    if d["children"]:
        items = "".join(
            f'<li><a class="drill" href="/board/task/{esc(c["id"])}">{esc(c["title"])}</a> '
            f'<span class="muted">{esc(c["assigned_to"] or "")} · {esc(c["state"])}</span></li>'
            for c in d["children"])
        cards.append(f'<div class="card"><h2>Follow-up tasks</h2><ul class="vals">{items}</ul></div>')
    h1 = f'{esc(d["title"])}<span class="state st-{esc(d["state"])}">{esc(d["state"])}</span>'
    sub = f'{esc(d["assigned_to"] or "")} · {esc(d["kind"])}{took} · <code>{esc(d["id"])}</code>'
    # The Go form lives in the topbar: direction on the left, the button beside it.
    go = (f'<form class="go" method="post" action="/api/prompt">'
          f'<input type="hidden" name="action" value="go"><input type="hidden" name="task" value="{esc(d["id"])}">'
          f'<input type="hidden" name="back" value="/board/task/{esc(d["id"])}">'
          f'<textarea name="comment" rows="2" maxlength="{store.COMMENT_MAX}" '
          f'placeholder="Tell master. Optional direction, e.g. &quot;split it: schema first&quot; or &quot;hold until Monday&quot;"></textarea>'
          f'<div class="btns"><button class="primary">Go</button>'
          + ('<b class="st-done">Sent. Master picks it up when idle.</b>' if queued else
             '<span class="muted">queued for master; runs when master is idle</span>')
          + _next_link(d, queued) + '</div></form>')
    head = _draft_card(d, queued)
    q = d.get("question")
    if q:
        head += (f'<div class="card go next"><h2>Master asks</h2><p>{esc(q["question"])}</p>'
                 f'<div class="btns">{_answer_buttons(q, "/board/task/" + d["id"])}</div>'
                 f'<p class="muted">On <code>{esc(q["ref"])}</code>. A press sends master '
                 f'&quot;Answer on {esc(q["ref"])}: &lt;option&gt;&quot; when it is idle.</p></div>')
    return page("Board", f'{d["assigned_to"] or "Task"}: {d["title"][:60]}', h1, sub, head + "".join(cards),
                actions=go, tail=FILE_JS)


def _draft_card(d: dict[str, Any], queued: bool) -> str:
    """A finished draft_reply at the top of its page: the envelope, the body
    as it will read, and two ways into Outlook. 'Draft in Outlook' queues
    master to type it into Outlook on the web (autosaved to Drafts, never
    sent); the compose link opens the same envelope in Ben's own browser for
    him to paste the body himself. Nothing here sends mail."""
    from . import outlook
    if d["kind"] != "draft_reply" or not d["result"] or not d["result"].get("done"):
        return ""
    try:
        dr = outlook.draft_from_result(d["id"], d["assigned_to"], d["title"], d["result"])
    except (KeyError, OSError):
        return ""
    files = {f["path"]: f for f in d.get("files") or []}
    f = files.get(dr["body_path"])
    body_link = file_link(d["id"], dr["body_path"], bool(f and f["exists"]), Path(dr["body_path"]).name)
    sent = ('<b class="st-done">Sent. Master drafts it when idle.</b>' if queued
            else '<span class="muted">master types it into Outlook on the web; it lands in Drafts, unsent</span>')
    btns = (_button("draft", "Draft in Outlook", task=d["id"], primary=True) if dr["exists"] else "")
    return (f'<div class="card go draft"><h2>Draft reply</h2>{_draft_envelope(dr, body_link)}'
            f'<div class="btns">{btns}{sent}{_compose_link(dr)}</div></div>')


def _draft_envelope(dr: dict[str, Any], body_link: str) -> str:
    """A draft's envelope and the body as it will read. Shared, so the task
    page and the drafts walk can never show the same draft differently."""
    env = {"to": "; ".join(dr["to"]) or "—", "cc": "; ".join(dr["cc"]) or "—", "subject": dr["subject"]}
    if dr["in_reply_to"]:
        env["in reply to"] = dr["in_reply_to"]
    kv = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in env.items())
    body = (f'<pre class="draft-body">{esc(dr["body"])}</pre>' if dr["exists"]
            else '<p class="empty">Body file is not on disk; nothing to draft.</p>')
    return f'<dl class="kv">{kv}<dt>body</dt><dd>{body_link}</dd></dl>{body}'


def _compose_link(dr: dict[str, Any]) -> str:
    """The same envelope in Ben's own browser, for him to paste the body."""
    return (f'<a class="drill" href="{esc(dr["url"])}" target="_blank" rel="noopener">'
            f'Open compose in my browser →</a>')


def _next_link(d: dict[str, Any], queued: bool) -> str:
    """Where Ben goes after Go: the client's next task that still wants one.
    Bold once this one is sent, so the walk reads Go, next, Go, next."""
    n = d.get("next_for_client")
    if not n:
        return ""
    side = "done" if d.get("state") == "done" else "open"
    label = f'Next {side} for {esc(d["assigned_to"])}: {esc(n["title"][:50])}'
    note = f'<span class="muted">{n["pos"]} of {n["of"]}</span>'
    cls = "drill next-task primary" if queued else "drill next-task"
    return f'<a class="{cls}" href="/board/task/{esc(n["id"])}">{label} →</a> {note}'


def _size(n: int | None) -> str:
    if n is None:
        return "—"
    return f"{n} B" if n < 1024 else f"{n / 1024:.0f} KB" if n < 1024 ** 2 else f"{n / 1024 ** 2:.1f} MB"


def deliverables_page(v: dict[str, Any]) -> str:
    """Files finished tasks produced, by client, each beside what Ben owes on it."""
    from urllib.parse import urlencode
    t, files = v["as_of"], v["files"]
    form = (f'<form class="filter" method="get" action="/deliverables">'
            f'<label>Last <input type="number" name="days" min="1" max="365" value="{v["days"]}"> days</label>'
            f'<label>client <input name="client" value="{esc(v["client"] or "")}" placeholder="all" size="10"></label>'
            f'<label>search <input type="search" id="dl-q" name="q" '
            f'value="{esc(v.get("q") or "")}" placeholder="file, path, task, owed…" autocomplete="off"></label>'
            f'<button>Show</button><span class="muted" id="dl-n"></span></form>')
    sub = ('Read from results on the board: data-pull outputs, changed files and '
           'investigation pointers that a person would open. Newest first; a file two tasks touched '
           'shows under the newer one. <i>missing</i> means the result named it but it is not on disk.')
    if not files:
        return page("Deliverables", "Deliverables", "Deliverables", sub,
                    '<p class="empty">No deliverables in this range.</p>', actions=form + SEEN_BOX)
    groups: dict[str, list[dict[str, Any]]] = {}
    for d in files:
        groups.setdefault(d["client"], []).append(d)
    owed_n = sum(1 for d in files if d["ben"])
    missing = sum(1 for d in files if not d["exists"])
    fresh = sum(1 for d in files if t - d["completed_at"] <= 86400)
    kpi = '<div class="kpis">' + "".join(
        f'<div class="kpi {cls}{" zero" if not n else ""}"><span class="n">{n}</span>'
        f'<span class="label">{esc(label)}</span><span class="hint">{esc(hint)}</span></div>'
        for label, n, cls, hint in (
            ("Files", len(files), "k-info", f"last {v['days']} days"),
            ("New today", fresh, "k-done", "finished in the last 24h"),
            ("Owed to you", owed_n, "k-you", "a result named a step for Ben"),
            ("Missing", missing, "k-fail", "named in a result, not on disk"),
            ("Clients", len(groups), "k-alt", "with deliverables in range"))) + "</div>"
    cards = []
    for c, g in sorted(groups.items(), key=lambda kv: -max(d["completed_at"] for d in kv[1])):
        rows = []
        for d in g:
            name = file_link(d["task_id"], d["path"], d["exists"], d["name"])
            if not d["exists"]:
                name += ' <b class="st-failed">missing</b>'
            elif Path(d["path"]).suffix.lower() in INLINE_TYPES:  # also readable right here
                name += (f' <a class="drill muted" href="/deliverables/file?'
                         f'{esc(urlencode({"task": d["task_id"], "path": d["path"]}))}">view</a>')
            owed = ("".join(f'<div class="note">Ben: {esc(w[:220])}</div>' for w in d["ben"]) if d["ben"] else "")
            hay = " ".join([d["name"], d["path"], d["title"], d["task_id"], c, *d["ben"]]).lower()
            task = (f'<div class="muted">from task <a class="drill" href="/board/task/{esc(d["task_id"])}">'
                    f'{esc(d["title"])}</a> <code>{esc(d["task_id"])}</code></div>')
            rows.append(f'<div class="item bare dl{" owes" if d["ben"] else ""}" data-s="{esc(hay)}" '
                        f'data-since="{int(d["completed_at"])}" data-age="{_age_bucket(d["completed_at"], t)}">'
                        f'<div class="what">{name}<div class="path">{esc(d["path"])}</div>{task}</div>'
                        f'<div class="age"><span class="size">{_size(d["size"])}</span>{_age(d["completed_at"], t)}</div>'
                        f'{owed}</div>')
        owe = sum(1 for d in g if d["ben"])
        badge = f' <span class="count owed">{owe} owed</span>' if owe else ""
        cards.append(f'<div class="card lane dlc"><h2>{esc(c)}<span class="count">{len(g)}</span>{badge}</h2>'
                     f'{"".join(rows)}</div>')
    none = '<p class="empty" id="dl-none" hidden>Nothing matches.</p>'
    return page("Deliverables", f"Deliverables ({len(files)})", "Deliverables", sub,
                f'{kpi}{none}{"".join(cards)}', actions=form + SEEN_BOX, tail=DELIVERABLES_JS + FILE_JS)



def drafts_page(v: dict[str, Any]) -> str:
    """Every reply an agent wrote, as one walk down the page.

    The board shows a draft as one more thing needing Ben; here it is the
    email itself, in the order he works them, with the next one a click away.
    Unworked first, oldest first; drafted ones stay below as the record of
    what already went to Outlook. Nothing on this page sends mail.
    """
    t, rows = v["as_of"], v["drafts"]
    form = (f'<form class="filter" method="get" action="/drafts">'
            f'<label>Last <input type="number" name="days" min="1" max="365" value="{v["days"]}"> days</label>'
            f'<label>client <input name="client" value="{esc(v["client"] or "")}" placeholder="all" size="10"></label>'
            f'<button>Show</button></form>')
    sub = ('One card per finished <code>draft_reply</code>. <b>Draft in Outlook</b> queues master to type it '
           'into Outlook on the web, where it autosaves to Drafts; the compose link opens the same envelope '
           'in your own browser. Neither sends.')
    if not rows:
        return page("Drafts", "Drafts", "Email drafts", sub,
                    f'<p class="empty">No drafts in the last {v["days"]} days.</p>',
                    actions=form + SEEN_BOX)
    todo = [d for d in rows if d["state"] != "drafted"]
    counts = {k: sum(1 for d in rows if d["state"] == k) for k in ("waiting", "queued", "missing", "drafted")}
    kpi = '<div class="kpis">' + "".join(
        f'<div class="kpi {cls}{" zero" if not n else ""}"><span class="n">{n}</span>'
        f'<span class="label">{esc(label)}</span><span class="hint">{esc(hint)}</span></div>'
        for label, n, cls, hint in (
            ("To go through", counts["waiting"], "k-you", "written, not yet in Drafts"),
            ("Queued", counts["queued"], "k-run", "master drafts it when idle"),
            ("In Drafts", counts["drafted"], "k-done", "already typed into Outlook"),
            ("Body missing", counts["missing"], "k-fail", "the file the result named is gone"),
            ("Clients", len({d["client"] for d in rows}), "k-alt", "with a draft in range"))) + "</div>"
    badge = {"waiting": '<b class="st-open">to go through</b>',
             "queued": '<b class="st-waiting">queued for master</b>',
             "missing": '<b class="st-failed">body missing</b>',
             "drafted": '<b class="st-done">in Drafts</b>'}
    cards = []
    for d in rows:
        i = todo.index(d) if d["state"] != "drafted" else None
        pos = (f'<span class="count">{i + 1} of {len(todo)}</span>' if i is not None else "")
        nxt = (f'<a class="drill next-task" href="#d-{esc(todo[i + 1]["task_id"])}">Next draft →</a>'
               if i is not None and i + 1 < len(todo)
               else '<span class="muted">last one</span>' if i is not None else "")
        # A draft already in Drafts keeps its button, so a reply Ben edited and
        # lost can be retyped -- but never as the primary one, since pressing it
        # twice leaves two drafts in the folder.
        btns = (_button("draft", "Draft again" if d["drafted"] else "Draft in Outlook",
                        task=d["task_id"], primary=d["state"] == "waiting")
                if d["exists"] and d["state"] != "queued" else "")
        body_link = file_link(d["task_id"], d["body_path"], d["exists"], Path(d["body_path"]).name)
        why = f'<div class="note">{esc(d["summary"][:300])}</div>' if d["summary"] else ""
        owed = "".join(f'<div class="note">Ben: {esc(w[:220])}</div>' for w in d["ben"])
        when_sent = (f'drafted {_age(d["drafted"]["at"], t)} · ' if d["drafted"] else "")
        head = (f'<h2>{esc(d["client"])}: {esc(d["subject"][:90])}{pos}</h2>'
                f'<div class="muted">{badge[d["state"]]} · {when_sent}written '
                f'{_age(d["completed_at"], t)} · <a class="drill" href="/board/task/{esc(d["task_id"])}">'
                f'{esc(d["title"][:60])}</a> <code>{esc(d["task_id"][:8])}</code></div>')
        cards.append(f'<div class="card lane draft" id="d-{esc(d["task_id"])}" '
                     f'data-since="{int(d["completed_at"])}" data-age="{_age_bucket(d["completed_at"], t)}">'
                     f'{head}{why}{owed}{_draft_envelope(d, body_link)}'
                     f'<div class="btns">{btns}{_compose_link(d)}{nxt}</div></div>')
    lead = (f'<p class="sub">Start at the top: <a class="drill" href="#d-{esc(todo[0]["task_id"])}">'
            f'{esc(todo[0]["client"])}: {esc(todo[0]["subject"][:70])} →</a></p>' if todo
            else '<p class="clear">Every draft in range is in Outlook Drafts.</p>')
    return page("Drafts", f'{"(" + str(len(todo)) + ") " if todo else ""}Drafts', "Email drafts", sub,
                f'{kpi}{lead}{"".join(cards)}', actions=form + SEEN_BOX)


def intents_page(v: dict[str, Any]) -> str:
    """Every email the hourly triage reviewed, what it did with it, and what Ben owes."""
    t, rows = v["as_of"], v["emails"]
    form = (f'<form class="filter" method="get" action="/intents">'
            f'<label>Last <input type="number" name="days" min="1" max="90" value="{v["days"]}"> days</label>'
            f'<label>client <input name="client" value="{esc(v["client"] or "")}" placeholder="all" size="10"></label>'
            f'<label>search <input type="search" id="in-q" value="{esc(v.get("q") or "")}" '
            f'placeholder="sender, client, subject…" autocomplete="off"></label>'
            f'<label><input type="checkbox" id="in-owed"{" checked" if v.get("owed") else ""}> only what I owe</label>'
            f'<button>Show</button><span class="muted" id="in-n"></span></form>')
    owed = sum(1 for d in rows if d["owed"])
    tasked = sum(1 for d in rows if d["action"] == "converted")
    dismissed = sum(1 for d in rows if d["action"] == "dismissed")
    noted = sum(1 for d in rows if d["action"] == "noted")
    open_intents = sum(1 for d in rows if d["action"] == "intent")
    sub = (f'{len(rows)} emails reviewed · {tasked} turned into tasks · {dismissed} dismissed · '
           f'{noted} noted only · <b>{owed} with a follow-up for you</b>. Follow-up is an open loose end '
           f'addressed to you on the email\'s tasks, or a reply the triage flagged as yours; the page cannot '
           f'see whether you have since replied.')
    if not rows:
        return page("Intents", "Intents", "Intents", sub, '<p class="empty">No emails in this range.</p>',
                    actions=form + SEEN_BOX)
    # Each tile filters the list to what it counts; Reviewed shows them all again.
    kpi = '<div class="kpis">' + "".join(
        f'<button type="button" class="kpi {cls}{" zero" if not n else ""}" data-k="{k}">'
        f'<span class="n">{n}</span>'
        f'<span class="label">{esc(label)}</span><span class="hint">{esc(hint)}</span></button>'
        for k, label, n, cls, hint in (
            ("", "Reviewed", len(rows), "k-info", f"emails, last {v['days']} days"),
            ("intent", "Open intents", open_intents, "k-wait", "on the board, not yet tasked"),
            ("converted", "Tasked", tasked, "k-done", "turned into board tasks"),
            ("dismissed", "Dismissed", dismissed, "k-plain", "triage saw nothing to do"),
            ("owed", "Owed to you", owed, "k-you", "a reply or a ruling is yours"))) + "</div>"
    items = []
    for d in rows:
        if d["action"] == "converted":
            act = "Tasked → " + ", ".join(
                f'<a class="drill" href="/board/task/{esc(k["id"])}">{esc(k["title"])}</a> '
                f'<span class="state st-{esc(k["state"])}">{esc(k["state"])}</span>' for k in d["tasks"])
        elif d["action"] == "dismissed":
            act = f'Dismissed<span class="muted"> · {esc((d["note"] or "")[:300])}</span>'
        elif d["action"] == "intent":
            act = f'Intent <code>{esc(d["intent_id"])}</code>'
        else:
            act = '<span class="muted">Noted, no task</span>'
        fu = "".join(f'<div class="note">Ben: {esc(w[:240])}</div>' for w in d["owed"])
        fu += _intent_go(d, v)
        hay = " ".join([d["sender"], d["client"], d["summary"], d["note"] or "", *d["owed"],
                        *(k["title"] for k in d["tasks"])]).lower()
        items.append(f'<div class="item em{" owes" if d["owed"] else ""}" data-s="{esc(hay)}" '
                     f'data-a="{esc(d["action"])}" '
                     f'data-since="{int(d["at"])}" data-age="{_age_bucket(d["at"], t)}">'
                     f'<div class="client">{esc(d["client"])}</div>'
                     f'<div class="what"><b>{esc(d["sender"])}</b> <span class="muted">{esc(d["received"] or "")}</span>'
                     f'<div>{esc(d["summary"])}</div><div class="act">{act}</div></div>'
                     f'<div class="age">{_age(d["at"], t)}</div>{fu}</div>')
    badge = f' <span class="count owed">{owed} owed</span>' if owed else ""
    table = (f'<div class="card lane"><h2>Emails<span class="count" id="in-c">{len(rows)}</span>'
             f'{badge}</h2>{"".join(items)}</div>')
    skipped = ""
    if v["skipped"]:
        n = sum(s["n"] for s in v["skipped"])
        li = "".join(f'<li><span class="muted">{esc(s["day"])} {esc(s["run"])}</span> · {s["n"]} · '
                     f'{esc(s["what"][:300])}</li>' for s in v["skipped"])
        skipped = (f'<details class="card"><summary><b>Skipped as automated</b> '
                   f'<span class="count">{n}</span></summary><ul>{li}</ul></details>')
    none = '<p class="empty" id="in-none" hidden>Nothing matches.</p>'
    return page("Intents", f"Intents ({owed} owed)", "Intents", sub, f'{kpi}{none}{table}{skipped}',
                actions=form + SEEN_BOX, tail=INTENTS_JS)


def _intent_go(d: dict[str, Any], v: dict[str, Any]) -> str:
    """Go for one email on /intents: Ben decided it needs following up. An
    open intent goes to master as it is, a dismissed one is reopened, a noted
    email becomes an intent first. A tasked one has no Go here: its tasks do."""
    from urllib.parse import urlencode
    if d["action"] == "converted":
        return ""
    back = "/intents?" + urlencode({"days": v["days"], "client": v["client"] or ""})
    iid = d.get("intent_id")
    if iid and v.get("queued") == iid:
        return '<div class="note"><b class="st-done">Sent. Master picks it up when idle.</b></div>'
    if iid:
        ident = f'<input type="hidden" name="intent" value="{esc(iid)}">'
    else:
        ident = "".join(f'<input type="hidden" name="{k}" value="{esc(d.get(k) or "")}">'
                        for k in ("client", "sender", "summary", "received"))
    hint = (' <span class="muted">reopens it</span>' if d["action"] == "dismissed" else
            ' <span class="muted">makes it an intent</span>' if d["action"] == "noted" else "")
    return (f'<form class="go" method="post" action="/api/prompt"><input type="hidden" name="action" value="intent">'
            f'{ident}<input type="hidden" name="back" value="{esc(back)}">'
            f'<input name="comment" maxlength="{store.COMMENT_MAX}" placeholder="Tell master what it needs, optional">'
            f'<button class="primary">Go</button>{hint}</form>')


# Live filter, as on Deliverables, plus the "only what I owe" toggle.
INTENTS_JS = """<script>
(() => { const q = document.getElementById("in-q"), o = document.getElementById("in-owed"),
    n = document.getElementById("in-n"), c = document.getElementById("in-c"),
    none = document.getElementById("in-none"), rows = [...document.querySelectorAll(".em")],
    tiles = [...document.querySelectorAll(".kpi[data-k]")];
  if (!q) return;
  // k: the action a tile narrowed to ("" = all). Owed to you is the checkbox.
  let k = "";
  try { k = new URL(location.href).searchParams.get("k") || ""; } catch (e) {}
  const run = () => {
    const words = q.value.toLowerCase().split(/\\s+/).filter(Boolean);
    let shown = 0;
    rows.forEach(r => { const hit = (!o.checked || r.classList.contains("owes")) &&
      (!k || r.dataset.a === k) &&
      words.every(w => r.dataset.s.includes(w)); r.hidden = !hit; shown += hit; });
    tiles.forEach(t => t.classList.toggle("selected", t.dataset.k === "owed" ? o.checked
      : t.dataset.k === k && (k || !o.checked)));
    if (c) c.textContent = shown;
    n.textContent = shown === rows.length ? "" : shown + " of " + rows.length;
    none.hidden = shown > 0;
    try { const u = new URL(location.href);
      k ? u.searchParams.set("k", k) : u.searchParams.delete("k");
      history.replaceState(null, "", u); } catch (e) {}
  };
  tiles.forEach(t => t.addEventListener("click", () => {
    const want = t.dataset.k;
    if (want === "owed") { o.checked = !o.checked; }
    else if (!want) { k = ""; o.checked = false; }
    else { k = k === want ? "" : want; }
    run(); }));
  q.addEventListener("input", run); o.addEventListener("change", run); run();
})();
</script>"""

# Live filter: every word typed must appear in the row's file, path, task,
# client or what Ben owes. Kept in ?q= so a filtered view can be bookmarked.
DELIVERABLES_JS = """<script>
(() => { const q = document.getElementById("dl-q"), n = document.getElementById("dl-n"),
    none = document.getElementById("dl-none"), rows = [...document.querySelectorAll(".dl")];
  if (!q) return;
  q.form.addEventListener("submit", () => { if (!q.value.trim()) q.disabled = true; });
  const run = () => {
    const words = q.value.toLowerCase().split(/\\s+/).filter(Boolean);
    let shown = 0;
    rows.forEach(r => { const hit = words.every(w => r.dataset.s.includes(w)); r.hidden = !hit; shown += hit; });
    document.querySelectorAll(".card.dlc").forEach(c => {
      const k = c.querySelectorAll(".dl:not([hidden])").length;
      c.hidden = !k; c.querySelector("h2 .count").textContent = k;
      const o = c.querySelector("h2 .owed"), m = c.querySelectorAll(".owes:not([hidden])").length;
      if (o) { o.textContent = m + " owed"; o.hidden = !m; } });
    n.textContent = words.length ? shown + " of " + rows.length : "";
    none.hidden = !(words.length && !shown);
    try { const u = new URL(location.href);
      words.length ? u.searchParams.set("q", q.value.trim()) : u.searchParams.delete("q");
      history.replaceState(null, "", u); } catch (e) {} };
  q.addEventListener("input", run); run(); })();
</script>"""


# What the browser is told a deliverable is. Anything else downloads.
INLINE_TYPES = {".md": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8",
                ".csv": "text/plain; charset=utf-8", ".pdf": "application/pdf",
                ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8"}


def _h(minutes: float) -> str:
    q = hours.quarter_hours(minutes)
    return f"{q:.2f}" if q else '<span class="muted">·</span>'


def hours_csv(rows: list[dict[str, Any]]) -> str:
    """Unrounded minutes plus rounded hours, one line per day/client/task."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["day", "client", "task_id", "title", "pipeline_task_id", "agent_min", "operator_min",
                "meeting_min", "total_min", "hours", "estimated"])
    for r in rows:
        w.writerow([r["day"], r["client"], r["task_id"], r["title"], r["pipeline_task_id"] or "",
                    f"{r['agent_min']:.1f}", f"{r['operator_min']:.1f}", f"{r['meeting_min']:.1f}",
                    f"{r['total_min']:.1f}",
                    f"{hours.quarter_hours(r['total_min']):.2f}", "yes" if r["estimated"] else ""])
    return buf.getvalue()


def hours_page(rows: list[dict[str, Any]], start: str, end: str, client: str | None,
               push: dict[str, Any], today=None, unclassified: list[dict[str, Any]] | None = None) -> str:
    """Day x client grid, then each client's tasks. A draft for the operator."""
    from datetime import date, timedelta
    from urllib.parse import urlencode
    qs = urlencode({k: v for k, v in (("start", start), ("end", end), ("client", client)) if v})
    range_qs = urlencode({"start": start, "end": end})
    # Every day in range, empty ones too, so a gap reads as a gap. Stops at
    # today: the rest of the billing period has nothing to show yet.
    lo, hi = hours.parse_day(start), min(hours.parse_day(end), today or date.today())
    days = sorted({(lo + timedelta(n)).isoformat() for n in range((hi - lo).days + 1)}
                  | {r["day"] for r in rows})
    clients = sorted({r["client"] for r in rows})
    grid: dict[tuple[str, str], float] = {}
    for r in rows:
        grid[(r["day"], r["client"])] = grid.get((r["day"], r["client"]), 0.0) + r["total_min"]
    form = (f'<form class="filter" method="get" action="/hours">'
            f'<label>From <input type="date" name="start" value="{esc(start)}"></label>'
            f'<label>to <input type="date" name="end" value="{esc(end)}"></label>'
            f'<label>client <input name="client" value="{esc(client or "")}" placeholder="all" size="10"></label>'
            f'<button>Show</button><a class="btn" href="/hours.csv?{esc(qs)}">CSV</a></form>')
    caveat = ('Draft, not a bill. Agent time is measured from transcripts (gaps between a '
              f"client's requests, each capped at {hours.IDLE_GAP // 60} min) and is not Ben's time; "
              'operator time is what <code>coord_log_time</code> recorded; meeting time is your Outlook '
              'calendar (busy and tentative, overlaps shared), which shows invitations, not attendance. '
              '<i>Internal</i> is Logan-only meetings and is never pushed. Sessions outside client folders '
              '(master, Posey) are excluded. <i>est</i> marks tasks from before pickup events were recorded, '
              'timed from creation to close. Hours round to 0.25 on this page only.')
    title = f"Hours, {start} to {end}"
    if not rows:
        return page("Hours", "Hours", title, caveat, '<p class="empty">No client activity in this range.</p>',
                    actions=form)
    # One tile per client with its hours in range, then the total; a click narrows the page.
    tiles = []
    for c in clients:
        mins = sum(grid.get((d, c), 0.0) for d in days)
        n_tasks = len({r["task_id"] for r in rows if r["client"] == c and r["task_id"] not in (hours.UNASSIGNED, meetings.MEETINGS)})
        n_days = sum(1 for d in days if grid.get((d, c), 0.0))
        href = f"/hours?{range_qs}" if client == c else f"/hours?{range_qs}&client={quote(c)}"
        tiles.append(f'<a class="kpi k-hours{" selected" if client == c else ""}" href="{esc(href)}">'
                     f'<span class="n">{_h(mins)}</span><span class="label">{esc(c)}</span>'
                     f'<span class="hint">{n_tasks} task{"" if n_tasks == 1 else "s"} · {n_days} day{"" if n_days == 1 else "s"}</span></a>')
    tiles.append(f'<a class="kpi k-all" href="/hours?{range_qs}"><span class="n">{_h(sum(grid.values()))}</span>'
                 f'<span class="label">All clients</span><span class="hint">{len(clients)} client{"" if len(clients) == 1 else "s"} '
                 f'· {len(days)} day{"" if len(days) == 1 else "s"}</span></a>')
    kpi = f'<div class="kpis">{"".join(tiles)}</div>'
    head = "".join(f'<th class="n">{esc(c)}</th>' for c in clients)
    lines = []
    for d in days:
        cells = "".join(f'<td class="n">{_h(grid.get((d, c), 0.0))}</td>' for c in clients)
        tot = sum(grid.get((d, c), 0.0) for c in clients)
        wd = hours.parse_day(d).strftime("%a")
        lines.append(f'<tr><td><span class="muted">{wd}</span> {esc(d)}</td>{cells}<td class="n"><b>{_h(tot)}</b></td></tr>')
    col = "".join(f'<td class="n"><b>{_h(sum(grid.get((d, c), 0.0) for d in days))}</b></td>' for c in clients)
    lines.append(f'<tr><td><b>Total</b></td>{col}<td class="n"><b>{_h(sum(grid.values()))}</b></td></tr>')
    table = (f'<div class="card"><h2>Hours by day</h2><div class="wrap"><table><tr><th>Day</th>{head}'
             f'<th class="n">All</th></tr>{"".join(lines)}</table></div></div>')
    per = []
    for c in clients:
        acc: dict[str, dict[str, Any]] = {}
        for r in rows:
            if r["client"] != c:
                continue
            a = acc.setdefault(r["task_id"], {**r, "agent_min": 0.0, "operator_min": 0.0, "meeting_min": 0.0,
                                              "total_min": 0.0, "days": 0})
            for f in ("agent_min", "operator_min", "meeting_min", "total_min"):
                a[f] += r[f]
            a["days"] += 1
            a["estimated"] = a["estimated"] or r["estimated"]
        trs = []
        for tid, a in sorted(acc.items(), key=lambda kv: -kv[1]["total_min"]):
            name = ('<span class="muted">meetings (Outlook)</span>' if tid == meetings.MEETINGS else
                    f'<a class="drill" href="/board/task/{esc(tid)}">{esc(a["title"] or tid)}</a>'
                    if tid != hours.UNASSIGNED else '<span class="muted">unassigned (no task open)</span>')
            est = ' <span class="muted">est</span>' if a["estimated"] else ""
            trs.append(f'<tr><td>{name}{est}</td><td>{esc(a["pipeline_task_id"] or "—")}</td>'
                       f'<td class="n">{a["days"]}</td><td class="n">{_h(a["agent_min"])}</td>'
                       f'<td class="n">{_h(a["operator_min"])}</td><td class="n">{_h(a["meeting_min"])}</td>'
                       f'<td class="n"><b>{_h(a["total_min"])}</b></td></tr>')
        tot = _h(sum(a["total_min"] for a in acc.values()))
        per.append(f'<details class="card"><summary><b>{esc(c)}</b> · {tot}h</summary><div class="wrap"><table>'
                   f'<tr><th>Task</th><th>Pipeline</th><th class="n">Days</th><th class="n">Agent</th>'
                   f'<th class="n">Operator</th><th class="n">Meetings</th><th class="n">Total</th></tr>{"".join(trs)}</table></div></details>')
    pushed = (f'<div class="card"><h2>SharePoint push</h2><p class="muted">{esc(push["note"])} · '
              f'{len(push["payload"])} pipeline item-day(s) would be sent; rows with no pipeline item are left '
              f'out until linked with <code>coord_set_pipeline_task</code>.</p></div>')
    loose = ""
    if unclassified:
        trs = "".join(f'<tr><td>{esc(u["subject"])}</td><td class="muted">{esc(u["organizer"] or "")}</td>'
                      f'<td class="n">{u["n"]}</td><td class="n">{_h(u["m"])}</td></tr>' for u in unclassified)
        loose = (f'<div class="card"><h2>Unclassified meetings</h2><p class="muted">Not counted yet. Tell Master '
                 f'which client each belongs to (or skip); <code>coord_assign_meeting</code> remembers it.</p>'
                 f'<div class="wrap"><table><tr><th>Subject</th><th>Organizer</th><th class="n">Times</th>'
                 f'<th class="n">Hours</th></tr>{trs}</table></div></div>')
    return page("Hours", "Hours", title, caveat, f'{kpi}{loose}{table}{"".join(per)}{pushed}', actions=form)


def billing_period(today=None):
    """Billing runs in half-months: the 1st to the 15th, the 16th to month end."""
    import calendar
    from datetime import date
    today = today or date.today()
    if today.day <= 15:
        return today.replace(day=1), today.replace(day=15)
    return today.replace(day=16), today.replace(day=calendar.monthrange(today.year, today.month)[1])


def hours_range(q: dict[str, list[str]]) -> tuple[str, str, str | None]:
    """start/end/client from a query string; default is the current billing period."""
    p_start, p_end = billing_period()
    start = (q.get("start") or [""])[0] or p_start.isoformat()
    end = (q.get("end") or [""])[0] or p_end.isoformat()
    client = (q.get("client") or [""])[0].strip() or None
    return start, end, client


def write(conn: sqlite3.Connection, out: Path, days: int = 7) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(usage.summary(conn, days), usage.live(conn)), encoding="utf-8")
    return out


INGEST_EVERY = 30


def _normal_priority() -> None:
    """Lift this process to normal CPU, I/O and memory priority on Windows.

    Task Scheduler starts a task at priority 7 unless told otherwise: CPU
    BelowNormal, I/O and memory priority low. Best effort; a failure leaves
    the priority as it was.
    """
    import sys
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32, nt = ctypes.windll.kernel32, ctypes.windll.ntdll
        me = wintypes.HANDLE(-1)  # GetCurrentProcess(); its int restype would truncate it
        k32.SetPriorityClass(me, 0x20)  # NORMAL_PRIORITY_CLASS
        mem = ctypes.c_ulong(5)  # MEMORY_PRIORITY_NORMAL, ProcessMemoryPriority = 0
        k32.SetProcessInformation(me, 0, ctypes.byref(mem), ctypes.sizeof(mem))
        io = ctypes.c_ulong(2)  # IoPriorityNormal, ProcessIoPriority = 33
        nt.NtSetInformationProcess(me, 33, ctypes.byref(io), ctypes.sizeof(io))
    except Exception:
        pass


def serve(connect, port: int = 8765, days: int = 7, open_browser: bool = False) -> int:
    """Localhost server that re-renders on every request; a background thread
    re-ingests every INGEST_EVERY seconds.

    Ingest used to run inline on "/" and /hours under a lock. Its writes wait
    on the coord servers' (busy_timeout, per transcript), so a page load took
    anywhere from 1 s to a minute and queued every other "/" behind it. Pages
    now only read; WAL readers never wait on a writer.

    That did not fix it. The stall (1.5 s to 236 s for "/", 2026-10-19) was
    the scheduled task: Task Scheduler's default priority 7 starts the process
    BelowNormal with low I/O and memory priority, so every page read lost the
    disk to whatever else the machine was doing. A copy of this server run by
    hand against the same db answered in 0.4 s at the same moments. The task
    now runs at priority 4, and _normal_priority() undoes a priority-7 launch
    in case it is ever re-registered with the default.

    "/" is also built by the ingest thread after each pass and served from
    that copy, so a page load costs no query or render time; it is at most
    one pass (INGEST_EVERY) behind, which the 60-second refresh hides.

    The page carries a 60-second meta refresh, so a browser tab left open is
    a live dashboard. Bound to 127.0.0.1 only.

    Threaded, because a browser that holds a connection open would otherwise
    stall every other request. `connect` makes one sqlite connection per
    request, since a connection cannot cross threads; ingest has one thread,
    so nothing races on the transcript offsets.
    """
    import webbrowser
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    _normal_priority()
    home: list[tuple[float, bytes]] = []  # (built at, page); one slot, swapped whole

    def build_home(conn) -> bytes:
        body = render(usage.summary(conn, days), usage.live(conn), refresh=60).encode("utf-8")
        home[:] = [(time.monotonic(), body)]
        return body

    def ingest_loop() -> None:
        while True:
            try:
                conn = connect()
                try:
                    try:
                        usage.ingest(conn)
                    finally:
                        build_home(conn)
                    # The minder (store.minder_check) rides this pass: it
                    # reads the board and, when master owes work it is not
                    # acting on, queues one wake prompt. Its own try, so a
                    # failure there never costs an ingest.
                    try:
                        store.minder_check(conn)
                    except Exception:
                        pass
                finally:
                    conn.close()
            except Exception:  # a locked db or a vanished file: the next pass gets it
                pass
            time.sleep(INGEST_EVERY)

    threading.Thread(target=ingest_loop, name="ingest", daemon=True).start()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            from urllib.parse import parse_qs
            path, _, query = self.path.partition("?")
            path = path.rstrip("/")
            self._query = parse_qs(query)
            self._type, self._disp, self._sandbox = "text/html; charset=utf-8", None, False
            conn = connect()
            try:
                try:
                    body = self._body(conn, path)
                except ValueError as e:  # a bad date in the query string
                    self.send_error(400, str(e))
                    return
            finally:
                conn.close()
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", self._type)
            if self._disp:
                self.send_header("Content-Disposition", f'attachment; filename="{self._disp}"')
            if self._sandbox:
                self.send_header("Content-Security-Policy", "sandbox")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _foreign(self) -> bool:
            """A POST from some other site's page in Ben's browser. Each one
            ends in a prompt to the master, so only this origin, or no browser
            at all (the mod's own fetch), may send one."""
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                return True
            return self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site")

        def do_POST(self):  # noqa: N802
            from urllib.parse import parse_qs
            path = self.path.partition("?")[0].rstrip("/")
            if self._foreign():
                self.send_error(403, "cross-origin request refused")
                return
            n = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(n).decode("utf-8")) if n else {}
            conn = connect()
            try:
                if path == "/api/prompt":
                    try:
                        one = lambda k: (form.get(k) or [""])[0]  # noqa: E731
                        r = store.queue_operator_prompt(conn, one("action"), one("task") or None, one("comment"),
                                                        one("question") or None, one("option") or None,
                                                        intent_id=one("intent") or None,
                                                        email={k: one(k) for k in
                                                               ("client", "sender", "summary", "received")})
                    except ValueError as e:
                        self._json(400, {"ok": False, "error": str(e)})
                        return
                    if "json" in (self.headers.get("Accept") or ""):
                        self._json(200, r)
                        return
                    self.send_response(303)
                    back = (form.get("back") or ["/board"])[0]
                    if not re.fullmatch(r"(/board(/task/[0-9a-f]{12})?|/intents(\?[\w=&%+.-]*)?)", back):
                        back = "/board"
                    sent = r.get("intent_id") or "1"
                    self.send_header("Location", back + ("&" if "?" in back else "?") + f"queued={sent}")
                    self.end_headers()
                    return
                if path == "/api/open":  # a file link on a task or deliverables page
                    tid, want = (form.get("task") or [""])[0], (form.get("path") or [""])[0]
                    how = (form.get("how") or ["open"])[0]
                    f = store.task_file(conn, tid, want) if how in ("open", "reveal") else None
                    if f is None:
                        self._json(404, {"ok": False, "error": "not a file on this task"})
                        return
                    try:
                        open_path(f, how)
                    except OSError as e:  # no app for the type, or it vanished since the check
                        self._json(500, {"ok": False, "error": str(e)})
                        return
                    self._json(200, {"ok": True, "path": str(f), "how": how})
                    return
                if path == "/api/prompts/take":
                    self._json(200, {"prompt": store.take_operator_prompt(conn)})
                    return
                if path == "/api/question/close":  # the band answered it in chat
                    self._json(200, {"closed": store.close_question(conn, (form.get("question") or [None])[0])})
                    return
            finally:
                conn.close()
            self.send_error(404)

        def _body(self, conn, path):
            if path == "/api/parked":
                self._type = "application/json"
                return json.dumps({"parked": store.parked_tasks(conn),
                                   "queued": store.queued_operator_prompts(conn),
                                   "question": store.current_question(conn),
                                   "minder": store.minder_state(conn),
                                   "actions": {k: v for k, v in store.OPERATOR_ACTIONS.items()
                                               if k in store.PAGE_ACTIONS and k != "redispatch"}}
                                  ).encode("utf-8")
            if path == "/board":
                v = store.operator_view(conn)
                v["parked"] = store.parked_tasks(conn)
                v["queued"] = store.queued_operator_prompts(conn)
                v["question"] = store.current_question(conn)
                v["minder"] = store.minder_state(conn)
                v["fleet"] = store.fleet(conn, v, hours.CLIENTS)
                v["activity"] = store.activity(conn)
                client = (self._query.get("client") or [""])[0].strip() or None
                return board_page(v, client=client).encode("utf-8")
            if path.startswith("/board/task/"):
                tid = path.rsplit("/", 1)[1]
                return task_page(store.task_detail(conn, tid), tid,
                                 queued="queued" in self._query).encode("utf-8")
            if path == "/deliverables":
                # Not `days`: assigning it here would shadow serve()'s and break "/".
                window = int((self._query.get("days") or ["14"])[0] or 14)
                client = (self._query.get("client") or [""])[0].strip() or None
                v = store.deliverables(conn, days=max(1, min(window, 365)), client=client)
                v["q"] = (self._query.get("q") or [""])[0].strip()
                return deliverables_page(v).encode("utf-8")
            if path == "/drafts":
                window = int((self._query.get("days") or ["30"])[0] or 30)
                client = (self._query.get("client") or [""])[0].strip() or None
                return drafts_page(store.draft_replies(conn, days=max(1, min(window, 365)),
                                                       client=client)).encode("utf-8")
            if path == "/intents":
                window = int((self._query.get("days") or ["7"])[0] or 7)
                client = (self._query.get("client") or [""])[0].strip() or None
                v = store.triage_emails(conn, days=max(1, min(window, 90)), client=client)
                v["owed"] = "owed" in self._query
                v["queued"] = (self._query.get("queued") or [""])[0]
                return intents_page(v).encode("utf-8")
            if path == "/deliverables/file":
                f = store.deliverable_path(conn, (self._query.get("task") or [""])[0],
                                           (self._query.get("path") or [""])[0])
                if f is None:
                    return None
                ext = f.suffix.lower()
                if ext in INLINE_TYPES:
                    # An agent wrote this HTML; it must not script the dashboard's origin.
                    self._type, self._sandbox = INLINE_TYPES[ext], ext in (".html", ".htm")
                else:
                    self._type, self._disp = "application/octet-stream", f.name
                return f.read_bytes()
            if path in ("/hours", "/hours.csv"):
                start, end, client = hours_range(self._query)
                rows = hours.breakdown(conn, start, end, client)
                if path == "/hours.csv":
                    self._type, self._disp = "text/csv; charset=utf-8", f"hours-{start}-{end}.csv"
                    return hours_csv(rows).encode("utf-8")
                un = meetings.unclassified(conn, hours.parse_day(start), hours.parse_day(end))
                return hours_page(rows, start, end, client, hours.push_to_pipeline(conn, rows),
                                  unclassified=un).encode("utf-8")
            if path == "":
                cached = home[0] if home else None
                if cached and time.monotonic() - cached[0] < 2 * INGEST_EVERY:
                    return cached[1]
                return build_home(conn)  # first load, or the ingest thread is stuck
            return None

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"
    print(f"serving {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
