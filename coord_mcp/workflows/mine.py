"""`python -m coord_mcp.workflows mine`: tag every past task and intent to a
catalog workflow and report on it. Read-only: the board is opened with
SQLite's mode=ro, so nothing here can write.

Per workflow: cases (root tasks), tasks, intents, median cycle time of the
finished cases, and the loose ends handed to Ben (all, and still open).
Leftovers are grouped by pattern (kind plus the title's first two words), so
the catalog can be revised from them.
"""

from __future__ import annotations

import html
import json
import sqlite3
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from . import CATALOG, GROUPS, name
from .tag import pattern, tag_intent, tag_root


def connect_ro(path: Path | str | None = None) -> sqlite3.Connection:
    from ..db import default_db_path
    p = Path(path) if path else default_db_path()
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def roots(conn: sqlite3.Connection) -> dict[str, str]:
    """task id -> its root task id (itself when it has no parent)."""
    parent = {r["id"]: r["parent_id"] for r in conn.execute("SELECT id, parent_id FROM tasks")}
    out: dict[str, str] = {}
    for tid in parent:
        seen, cur = [], tid
        while parent.get(cur) and parent[cur] in parent and cur not in seen:
            seen.append(cur)
            if parent[cur] in out:
                cur = out[parent[cur]]
                break
            cur = parent[cur]
        out[tid] = cur
        for s in seen:
            out[s] = cur
    return out


def tag_board(conn: sqlite3.Connection) -> dict[str, Any]:
    """Every task and intent with its workflow. Returns
    {"tasks": {id: {...}}, "intents": {id: {...}}, "cases": {root id: {...}}}."""
    root_of = roots(conn)
    rows = {r["id"]: dict(r) for r in conn.execute(
        """SELECT t.id, t.parent_id, t.kind, t.title, t.spec, t.state, t.assigned_to, t.from_intent,
                  t.created_at, t.completed_at, i.source AS source, i.created_at AS intent_at
           FROM tasks t LEFT JOIN intents i ON i.id = t.from_intent""")}
    cases: dict[str, dict[str, Any]] = {}
    for rid in sorted({root_of[t] for t in rows}):
        r = rows[rid]
        code, rule = tag_root(r["kind"], r["title"], r["spec"], r["assigned_to"], r["source"])
        cases[rid] = {"root": rid, "workflow": code, "rule": rule, "client": r["assigned_to"],
                      "title": r["title"], "kind": r["kind"], "source": r["source"],
                      "intent": r["from_intent"], "tasks": [],
                      "start": min(x for x in (r["created_at"], r["intent_at"]) if x)}
    tasks: dict[str, dict[str, Any]] = {}
    for tid, r in rows.items():
        c = cases[root_of[tid]]
        c["tasks"].append(tid)
        tasks[tid] = {"workflow": c["workflow"], "root": c["root"], "kind": r["kind"], "state": r["state"],
                      "created_at": r["created_at"], "completed_at": r["completed_at"]}
    for c in cases.values():
        ts = [tasks[t] for t in c["tasks"]]
        c["open"] = any(t["state"] == "open" for t in ts)
        ends = [t["completed_at"] for t in ts if t["completed_at"]]
        c["end"] = None if c["open"] or not ends else max(ends)
    by_intent: dict[str, list[str | None]] = defaultdict(list)
    for tid, r in rows.items():
        if r["from_intent"]:
            by_intent[r["from_intent"]].append(tasks[tid]["workflow"])
    intents: dict[str, dict[str, Any]] = {}
    for r in conn.execute("SELECT id, source, state, summary, task_ids FROM intents"):
        codes = list(by_intent.get(r["id"], []))
        for tid in json.loads(r["task_ids"] or "[]") if (r["task_ids"] or "").startswith("[") else []:
            if tid in tasks:
                codes.append(tasks[tid]["workflow"])
        code, rule = tag_intent(r["source"], codes)
        intents[r["id"]] = {"workflow": code, "rule": rule, "source": r["source"], "state": r["state"],
                            "summary": r["summary"]}
    return {"tasks": tasks, "intents": intents, "cases": cases}


def _ben_loose_ends(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT task_id, state FROM follow_ups "
                        "WHERE lower(trim(who))='ben' AND task_id IS NOT NULL").fetchall()


def mine(conn: sqlite3.Connection) -> dict[str, Any]:
    tagged = tag_board(conn)
    tasks, cases, intents = tagged["tasks"], tagged["cases"], tagged["intents"]
    per: dict[str | None, dict[str, Any]] = defaultdict(lambda: {
        "cases": 0, "open_cases": 0, "tasks": 0, "intents": 0, "cycle": [], "ben": 0, "ben_open": 0,
        "kinds": Counter(), "clients": Counter(), "rules": Counter()})
    for c in cases.values():
        p = per[c["workflow"]]
        p["cases"] += 1
        p["open_cases"] += c["open"]
        p["rules"][c["rule"]] += 1
        p["clients"][c["client"] or "-"] += 1
        if c["end"]:
            p["cycle"].append((c["end"] - c["start"]) / 3600)
    for t in tasks.values():
        per[t["workflow"]]["tasks"] += 1
        per[t["workflow"]]["kinds"][t["kind"]] += 1
    for i in intents.values():
        per[i["workflow"]]["intents"] += 1
    for f in _ben_loose_ends(conn):
        if f["task_id"] in tasks:
            p = per[tasks[f["task_id"]]["workflow"]]
            p["ben"] += 1
            p["ben_open"] += f["state"] == "open"
    rows = []
    for code in list(CATALOG) + [None]:
        p = per.get(code)
        if p is None:
            p = per[code]
        rows.append({"workflow": code, "name": name(code), "cases": p["cases"], "open_cases": p["open_cases"],
                     "tasks": p["tasks"], "intents": p["intents"],
                     "median_cycle_h": round(statistics.median(p["cycle"]), 1) if p["cycle"] else None,
                     "finished_cases": len(p["cycle"]), "ben_loose_ends": p["ben"],
                     "ben_open": p["ben_open"], "kinds": dict(p["kinds"].most_common()),
                     "clients": dict(p["clients"].most_common(5)), "rules": dict(p["rules"].most_common())})
    left = [c for c in cases.values() if c["workflow"] is None]
    groups: dict[str, list[str]] = defaultdict(list)
    for c in left:
        groups[pattern(c["title"], c["kind"])].append(c["title"])
    leftovers = sorted(({"pattern": k, "count": len(v), "examples": v[:3]} for k, v in groups.items()),
                       key=lambda g: (-g["count"], g["pattern"]))
    untagged_intents = Counter(i["source"] for i in intents.values() if i["workflow"] is None)
    return {"generated_at": int(time.time()), "totals": {"tasks": len(tasks), "cases": len(cases),
            "intents": len(intents), "tagged_tasks": sum(1 for t in tasks.values() if t["workflow"]),
            "tagged_intents": sum(1 for i in intents.values() if i["workflow"])},
            "workflows": rows, "leftovers": leftovers, "untagged_intents": dict(untagged_intents)}


# ------------------------------------------------------------------ report

CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--mute:#6b6a64;--line:#e4e1d8;--card:#fff;--acc:#2f5d8a;--warn:#a5531b}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#17181a;--fg:#e9e7e1;--mute:#a09d94;
--line:#2c2d30;--card:#1f2023;--acc:#8db7e0;--warn:#e0a070}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;margin:0}
main{max-width:1080px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 8px}
.sub{color:var(--mute);margin:0 0 20px}
table{border-collapse:collapse;width:100%;background:var(--card);font-size:13.5px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mute);font-weight:600}td.n{text-align:right;font-variant-numeric:tabular-nums}
.wrap{overflow-x:auto}.mute{color:var(--mute)}.warn{color:var(--warn)}
ul{padding-left:20px}li{margin:4px 0}code{font-size:12.5px}
"""


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def render(m: dict[str, Any], findings: list[str], revisions: list[str]) -> str:
    t = m["totals"]
    day = time.strftime("%Y-%m-%d", time.localtime(m["generated_at"]))
    out = [f"<!doctype html><html lang=en><head><meta charset=utf-8>"
           f"<meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>Workflow mining</title><style>{CSS}</style></head><body><main>",
           f"<h1>Workflow mining, {day}</h1>",
           f"<p class=sub>Internal. Every task and intent on the board tagged to a catalog workflow by "
           f"<code>python -m coord_mcp.workflows mine</code> (read-only). "
           f"{t['tagged_tasks']:,} of {t['tasks']:,} tasks and {t['tagged_intents']:,} of {t['intents']:,} "
           f"intents tagged; {t['cases']:,} cases (a case is a root task and everything under it).</p>"]
    if findings:
        out.append("<h2>What it says</h2><ul>" + "".join(f"<li>{f}</li>" for f in findings) + "</ul>")
    out.append("<h2>Per workflow</h2><div class=wrap><table><tr><th>#</th><th>Workflow</th><th>Cases</th>"
               "<th>Open</th><th>Tasks</th><th>Intents</th><th>Median cycle</th><th>Ben loose ends</th>"
               "<th>Still open</th><th>Kinds</th></tr>")
    for r in m["workflows"]:
        if not (r["cases"] or r["intents"]):
            cyc = "<span class=mute>no cases</span>"
        else:
            cyc = (f"{r['median_cycle_h'] / 24:.1f} d" if r["median_cycle_h"] is not None
                   else "<span class=mute>none finished</span>")
        kinds = ", ".join(f"{k} {v}" for k, v in r["kinds"].items())
        out.append(f"<tr><td>{_e(r['workflow'] or '—')}</td><td>{_e(r['name'])}</td><td class=n>{r['cases']}</td>"
                   f"<td class=n>{r['open_cases']}</td><td class=n>{r['tasks']}</td><td class=n>{r['intents']}</td>"
                   f"<td class=n>{cyc}</td><td class=n>{r['ben_loose_ends']}</td><td class=n>{r['ben_open']}</td>"
                   f"<td class=mute>{_e(kinds)}</td></tr>")
    out.append("</table></div><p class=mute>Median cycle: first of intent or root task created, to the last "
               "task in the case finished; finished cases only. Ben loose ends: follow-ups whose "
               "<code>who</code> is Ben, on any task in the case.</p>")
    out.append("<h2>Untagged leftovers, by pattern</h2>")
    if m["leftovers"]:
        out.append("<div class=wrap><table><tr><th>Pattern</th><th>Cases</th><th>Examples</th></tr>")
        for g in m["leftovers"][:40]:
            out.append(f"<tr><td>{_e(g['pattern'])}</td><td class=n>{g['count']}</td>"
                       f"<td>{'<br>'.join(_e(x) for x in g['examples'])}</td></tr>")
        out.append("</table></div>")
    else:
        out.append("<p>None.</p>")
    if m["untagged_intents"]:
        out.append("<p class=mute>Untagged intents by source: " + ", ".join(
            f"{_e(k)} {v}" for k, v in sorted(m["untagged_intents"].items(), key=lambda kv: -kv[1])) + "</p>")
    if revisions:
        out.append("<h2>Catalog revisions from this run</h2><ul>"
                   + "".join(f"<li>{r}</li>" for r in revisions) + "</ul>")
    out.append("<h2>Groups</h2><p class=mute>" + "; ".join(f"{k}: {v}" for k, v in GROUPS.items()) + "</p>")
    out.append("</main></body></html>")
    return "\n".join(out)


def findings_for(m: dict[str, Any]) -> list[str]:
    """Plain statements drawn from the numbers, so the report reads on its own."""
    rows = [r for r in m["workflows"] if r["workflow"]]
    t = m["totals"]
    out = [f"{t['tagged_tasks'] / max(1, t['tasks']):.0%} of tasks fall in a catalog workflow "
           f"({t['tasks'] - t['tagged_tasks']} left over)."]
    top = sorted(rows, key=lambda r: -r["tasks"])[:4]
    out.append("Most board work: " + ", ".join(f"{r['workflow']} {r['name']} ({r['tasks']} tasks)" for r in top) + ".")
    ben = sorted(rows, key=lambda r: -r["ben_open"])[:3]
    out.append("Most open Ben loose ends: " + ", ".join(
        f"{r['workflow']} {r['name']} ({r['ben_open']} of {r['ben_loose_ends']})" for r in ben) + ".")
    slow = sorted((r for r in rows if r["median_cycle_h"] is not None and r["finished_cases"] >= 3),
                  key=lambda r: -r["median_cycle_h"])[:3]
    if slow:
        out.append("Slowest median cycle (3+ finished cases): " + ", ".join(
            f"{r['workflow']} {r['median_cycle_h'] / 24:.1f} d" for r in slow) + ".")
    empty = [r["workflow"] for r in rows if not r["cases"]]
    if empty:
        out.append("No cases at all: " + ", ".join(empty) + " (done outside the board, or not yet a task shape).")
    return out


def write_report(conn: sqlite3.Connection, out: Path, revisions: list[str] | None = None) -> dict[str, Any]:
    m = mine(conn)
    out.write_text(render(m, findings_for(m), revisions or []), encoding="utf-8", newline="\n")
    return m
