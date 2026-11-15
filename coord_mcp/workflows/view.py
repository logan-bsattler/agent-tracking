"""/workflows: every open case at its step, and per workflow how cases flow.

Read-only. Built from workflow_cases and workflow_case_events (cases.py),
which the dashboard loop keeps current; the page never writes.

Per workflow:
  cycle       median hours from a case's start to its close, finished cases
  Ben touches mean Ben loose ends raised per case (open, done, deferred alike)
  stalls      the steps where cases sit longest: median hours between
              entering a step and the next event, over every case's history
  over limit  open cases past their step's sla (sla.py), the whole backlog
"""

from __future__ import annotations

import sqlite3
import statistics
import time
from collections import defaultdict
from typing import Any

from . import CATALOG, GROUPS, name
from . import definitions as defs
from . import sla
from .cases import have_workflows

WAITS = ("ben", "master", "agent")


def _median_h(xs: list[int]) -> float | None:
    return round(statistics.median(xs) / 3600, 1) if xs else None


def overview(conn: sqlite3.Connection, t: int | None = None) -> dict[str, Any]:
    """{as_of, open: [case...], workflows: [row...], totals} for the page."""
    t = t or int(time.time())
    have = have_workflows(conn)  # read-only: no tables yet means no cases, not a migration
    cases = [dict(r) for r in conn.execute("SELECT * FROM workflow_cases ORDER BY created_at")] if have else []
    by_wf: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    for c in cases:
        by_wf[c["workflow"]].append(c)

    ben = defaultdict(int)
    for r in conn.execute("""SELECT t.case_id, COUNT(*) n FROM follow_ups f JOIN tasks t ON t.id=f.task_id
                             WHERE lower(trim(f.who))='ben' AND t.case_id IS NOT NULL GROUP BY t.case_id"""
                          if have else "SELECT NULL case_id, 0 n WHERE 0"):
        ben[r["case_id"]] = r["n"]

    # Dwell: each event opens a stretch at its step that the case's next event ends.
    dwell: dict[tuple[str, str], list[int]] = defaultdict(list)
    wf_of = {c["id"]: c["workflow"] for c in cases}
    state_of = {c["id"]: c["state"] for c in cases}
    prev: dict[str, sqlite3.Row] = {}
    for e in (conn.execute("SELECT case_id, step, event, ts FROM workflow_case_events ORDER BY case_id, ts, rowid")
              if have else []):
        p = prev.get(e["case_id"])
        if p is not None and p["step"] and p["event"] != "closed":
            dwell[(wf_of.get(e["case_id"]) or "", p["step"])].append(max(e["ts"] - p["ts"], 0))
        prev[e["case_id"]] = e
    for cid, p in prev.items():  # still sitting there
        if state_of.get(cid) == "open" and p["step"] and p["event"] != "closed":
            dwell[(wf_of.get(cid) or "", p["step"])].append(max(t - p["ts"], 0))

    rows = []
    for code in list(CATALOG) + ([None] if None in by_wf else []):
        cs = by_wf.get(code, [])
        d = defs.get(code)
        names = {s["key"]: s["name"] for s in (d or {}).get("steps") or []}
        who = {s["key"]: s["who"] for s in (d or {}).get("steps") or []}
        stalls = sorted(((k[1], _median_h(v), len(v)) for k, v in dwell.items() if k[0] == (code or "")),
                        key=lambda x: -(x[1] or 0))[:3]
        open_c = [c for c in cs if c["state"] == "open"]
        rows.append({
            "workflow": code, "name": name(code), "group": CATALOG.get(code or "", ("", ""))[1],
            "engine": bool((d or {}).get("engine")),
            "cases": len(cs), "open": len(open_c),
            "waits": {w: sum(1 for c in open_c if c["waits_on"] == w) for w in WAITS},
            "median_cycle_h": _median_h([c["closed_at"] - c["created_at"] for c in cs
                                         if c["state"] == "done" and c["closed_at"]]),
            "ben_touches": round(sum(ben[c["id"]] for c in cs) / len(cs), 1) if cs else None,
            "stalls": [{"step": s, "name": names.get(s, s), "who": who.get(s), "median_h": h, "n": n}
                       for s, h, n in stalls],
        })

    open_cases = []
    for c in cases:
        if c["state"] != "open":
            continue
        d = defs.get(c["workflow"])
        st = next((s for s in (d or {}).get("steps") or [] if s["key"] == c["step"]), None)
        lim = sla.status(c, t)
        open_cases.append({**c, "step_name": st["name"] if st else (c["step"] or "—"),
                           "age_h": round((t - (c["step_at"] or c["created_at"])) / 3600, 1),
                           "ben_touches": ben[c["id"]],
                           "limit_h": round(lim["limit"] / 3600, 1) if lim else None,
                           "over_h": round(lim["over"] / 3600, 1) if lim and lim["over"] > 0 else None})
    for r in rows:
        r["over"] = sum(1 for c in open_cases if c["workflow"] == r["workflow"] and c["over_h"])
    open_cases.sort(key=lambda c: -c["age_h"])
    return {"as_of": t, "open": open_cases, "workflows": rows,
            "totals": {"cases": len(cases), "open": len(open_cases),
                       "waits": {w: sum(1 for c in open_cases if c["waits_on"] == w) for w in WAITS},
                       "over": sum(1 for c in open_cases if c["over_h"])}}


def _engine_on() -> bool:
    from .engine import enabled
    return enabled()


def _hours(h: float | None) -> str:
    if h is None:
        return "—"
    return f"{h:.0f} h" if h < 48 else f"{h / 24:.0f} d"


def render(v: dict[str, Any], wf: str | None = None, waits: str | None = None) -> str:
    from ..report import _task_link, esc, page  # report imports this module lazily
    tot = v["totals"]
    sub = (f'{tot["open"]} open of {tot["cases"]} cases: {tot["waits"]["ben"]} wait on Ben, '
           f'{tot["waits"]["master"]} on master, {tot["waits"]["agent"]} on an agent; '
           f'<b>{tot["over"]}</b> past their step limit. Read-only; '
           'the dashboard loop re-derives each case from the board. Engine '
           + ('<b>on</b> (COORD_WORKFLOW_ENGINE=1)' if _engine_on() else 'off')
           + '.')
    filt = ('<form method="get" action="/workflows" class="btns"><select name="wf"><option value="">all workflows</option>'
            + "".join(f'<option value="{c}"{" selected" if c == wf else ""}>{c} {esc(n)}</option>'
                      for c, (n, _) in CATALOG.items())
            + '</select><select name="waits"><option value="">anyone</option>'
            + "".join(f'<option{" selected" if w == waits else ""}>{w}</option>' for w in WAITS + ("over limit",))
            + '</select><button>Filter</button></form>'
            + ' <a class="btn" href="/workflows/proposed">Proposals</a>')

    head = ('<tr><th>Workflow</th><th class="n">Cases</th><th class="n">Open</th><th class="n">Ben</th>'
            '<th class="n">Master</th><th class="n">Agent</th><th class="n">Over limit</th><th class="n">Median cycle</th>'
            '<th class="n">Ben touches / case</th><th>Where cases sit longest</th></tr>')
    body = []
    for r in v["workflows"]:
        if not r["cases"]:
            continue
        stalls = "; ".join(f'{esc(s["name"])} ({esc(s["who"] or "?")}) {_hours(s["median_h"])}'
                           for s in r["stalls"]) or '<span class="muted">—</span>'
        eng = ' <span class="muted">engine</span>' if r["engine"] else ""
        label = (f'<a href="/workflows?wf={esc(r["workflow"])}">{esc(r["workflow"])} {esc(r["name"])}</a>'
                 if r["workflow"] else "untagged")
        body.append(f'<tr><td>{label}{eng}</td><td class="n">{r["cases"]}</td><td class="n">{r["open"]}</td>'
                    f'<td class="n">{r["waits"]["ben"]}</td><td class="n">{r["waits"]["master"]}</td>'
                    f'<td class="n">{r["waits"]["agent"]}</td><td class="n">{r.get("over") or 0}</td>'
                    f'<td class="n">{_hours(r["median_cycle_h"])}</td>'
                    f'<td class="n">{r["ben_touches"] if r["ben_touches"] is not None else "—"}</td>'
                    f'<td>{stalls}</td></tr>')
    table = (f'<div class="card"><h2>By workflow</h2><div class="wrap"><table>{head}{"".join(body)}'
             '</table></div></div>')

    def _wanted(c: dict[str, Any]) -> bool:
        if waits == "over limit":
            return bool(c.get("over_h"))
        return not waits or c["waits_on"] == waits

    shown = [c for c in v["open"] if (not wf or c["workflow"] == wf) and _wanted(c)]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in shown:
        groups[c["workflow"] or ""].append(c)
    cards = []
    for code in [c for c in CATALOG if c in groups] + ([""] if "" in groups else []):
        items = "".join(
            f'<li data-since="{int(c["step_at"] or c["created_at"])}"><b>{esc(c["client"] or "—")}</b> · '
            f'{_task_link(c["root_task_id"], c["title"])} · at <b>{esc(c["step_name"])}</b> '
            f'{_hours(c["age_h"])}, waits on <b>{esc(c["waits_on"] or "—")}</b>'
            + (f' <b class="over" style="color:#c0392b">over limit by {_hours(c["over_h"])}</b>'
               f' <span class="muted">(limit {_hours(c["limit_h"])} business)</span>' if c.get("over_h") else "")
            + (f' <span class="muted">{c["ben_touches"]} Ben loose end(s)</span>' if c["ben_touches"] else "")
            + f' <span class="muted"><code>{esc(c["id"])}</code></span></li>'
            for c in groups[code])
        title = f"{code} {name(code)}" if code else "untagged"
        cards.append(f'<details class="card grp"{" open" if wf else ""}><summary><h2>{esc(title)}'
                     f'<span class="count">{len(groups[code])}</span></h2></summary>'
                     f'<ul class="queue">{items}</ul></details>')
    open_html = "".join(cards) or '<p class="clear">No open cases match.</p>'
    groups_note = " · ".join(f"{k} {esc(n)}" for k, n in GROUPS.items())
    return page("Workflows", f'({tot["open"]}) Workflows', "Workflows", sub,
                table + f'<h2 style="margin-top:18px">Open cases, longest at their step first</h2>'
                f'<p class="sub">{groups_note}</p>' + open_html,
                actions=filt, refresh=120)
