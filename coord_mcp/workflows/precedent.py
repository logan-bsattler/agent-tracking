"""Fewer rulings: match Ben's open loose ends to his past rulings of the same shape.

`python -m coord_mcp.workflows precedent` is read-only: it writes an HTML
report of how many open Ben loose ends a precedent would settle. Applying is
`precedent --apply`, refused unless COORD_WORKFLOW_PRECEDENT=1, which is off
until Ben has read the report.

Shape: the loose end's text with its entities stripped (ids, numbers, file
names, quoted text, proper nouns), as a set of words. Two loose ends match
when they are for the same client and their shapes overlap by MATCH or more
(Jaccard).

A precedent is an earlier Ben loose end of the same client that was ruled:
  dropped  by a decision that closed at most BULK loose ends (a cleanup
           sweep or the 14-day expiry is housekeeping, not a ruling)
  tasked   it became a task (Ben said do it)
An open loose end is settled when every precedent it matches has the same
outcome; mixed outcomes are "conflicting" and stay Ben's.

Apply touches only "dropped" settlements: it records a decision ("precedent;
Ben may override", citing the precedent's decision) and resolves the loose
end with it. A "tasked" precedent would mean creating work in Ben's name, so
it is reported and never applied.

Workbook rows: an open Ben loose end that names a workbook ("Rule on the
rows in work/X.xlsx") is a batch of rulings. The report reads it with
openpyxl (workbook_rows.py) and counts its rows waiting on a ruling, and how
many a past ruling covers: a Ben decision naming the row's key, or a settled
ruling column for the same key in another workbook of the client's work/.
Counts only: apply never reads or edits a workbook.
"""

from __future__ import annotations

import html
import os
import re
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

MATCH = 0.75
BULK = 3
HOUSEKEEPING = re.compile(r"^(auto-deferred|board cleanup|desk cleanup|cleanup)", re.I)
STOP = set("a an the and or of to in on for is are be it this that with as at by from not no if then "
           "still also per its it's into than so do does".split())
_ENT = [
    (re.compile(r"\b[0-9a-f]{8,12}\b"), " "),                      # board ids
    (re.compile(r"\S+\.(?:p|i|w|html?|xlsx|csv|md|docx|pdf|json)\b", re.I), " "),  # file names
    (re.compile(r'"[^"]*"|\'[^\']*\''), " "),                        # quoted text
    (re.compile(r"https?://\S+|[A-Za-z]:\\\S+|\S+/\S+"), " "),       # urls and paths
    (re.compile(r"[#]?\d[\w./:-]*"), " "),                           # numbers, dates, tickets
]


def enabled() -> bool:
    return os.environ.get("COORD_WORKFLOW_PRECEDENT", "").strip() == "1"


def shape(text: str, client: str | None = None) -> frozenset[str]:
    s = text or ""
    for rx, sub in _ENT:
        s = rx.sub(sub, s)
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", s)
    out = set()
    for i, w in enumerate(words):
        if i > 0 and w[0].isupper():  # a proper noun mid-sentence (a person, a program, the client)
            continue
        lw = w.lower()
        if lw in STOP or len(lw) < 3 or (client and lw == client.lower()):
            continue
        out.add(lw)
    return frozenset(out)


def _sim(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _client_expr() -> str:
    return "COALESCE(t.assigned_to, f.client)"


def precedents(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    bulk = {r["resolved_by"]: r["n"] for r in conn.execute(
        "SELECT resolved_by, COUNT(*) n FROM follow_ups WHERE state='dropped' GROUP BY resolved_by")}
    out = []
    for r in conn.execute(f"""SELECT f.id, f.what, f.state, f.resolved_by, f.resolved_at, {_client_expr()} AS client,
                                     d.statement FROM follow_ups f LEFT JOIN tasks t ON t.id=f.task_id
                              LEFT JOIN decisions d ON d.id=f.resolved_by
                              WHERE lower(trim(f.who))='ben' AND f.state IN ('dropped','tasked')"""):
        if r["state"] == "dropped":
            if not r["statement"] or bulk.get(r["resolved_by"], 0) > BULK or HOUSEKEEPING.match(r["statement"]):
                continue
        out.append({**dict(r), "shape": shape(r["what"], r["client"])})
    return out


def report(conn: sqlite3.Connection, rows_root: Path | None = None) -> dict[str, Any]:
    """Read-only: every open Ben loose end and what precedent says about it,
    plus the workbook rows those loose ends point at (under rows_root, default
    the clients' Documents folder)."""
    pool = defaultdict(list)
    for p in precedents(conn):
        pool[p["client"]].append(p)
    rows, tally, by_client, open_les = [], Counter(), defaultdict(Counter), []
    for r in conn.execute(f"""SELECT f.id, f.what, f.created_at, f.task_id, {_client_expr()} AS client
                              FROM follow_ups f LEFT JOIN tasks t ON t.id=f.task_id
                              WHERE lower(trim(f.who))='ben' AND f.state='open' ORDER BY f.created_at"""):
        open_les.append({"id": r["id"], "client": r["client"], "what": r["what"]})
        sh = shape(r["what"], r["client"])
        hits = sorted(((s, p) for p in pool.get(r["client"], [])
                       for s in [_sim(sh, p["shape"])] if s >= MATCH), key=lambda x: -x[0])
        outcomes = {p["state"] for _, p in hits}
        verdict = ("none" if not hits else "conflicting" if len(outcomes) > 1
                   else "drop" if outcomes == {"dropped"} else "task")
        tally[verdict] += 1
        by_client[r["client"] or "—"][verdict] += 1
        if hits:
            s, p = hits[0]
            rows.append({"id": r["id"], "client": r["client"], "what": r["what"], "verdict": verdict,
                         "score": round(s, 2), "matches": len(hits), "precedent": p["id"],
                         "precedent_what": p["what"], "outcome": p["state"], "cite": p["resolved_by"],
                         "statement": p.get("statement")})
    from . import workbook_rows as wr
    if rows_root is None:
        from ..store import DOCS_ROOT as rows_root
    return {"as_of": int(time.time()), "open": sum(tally.values()), "tally": dict(tally),
            "by_client": {k: dict(v) for k, v in sorted(by_client.items())}, "rows": rows,
            "pool": sum(len(v) for v in pool.values()), "match": MATCH, "bulk": BULK,
            "workbooks": wr.scan(conn, open_les, Path(rows_root))}


def apply(conn: sqlite3.Connection, rep: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Resolve the 'drop' settlements, each behind its own decision. Refused
    unless COORD_WORKFLOW_PRECEDENT=1."""
    if not enabled():
        raise PermissionError("precedent apply is off; set COORD_WORKFLOW_PRECEDENT=1 once Ben has read the report")
    from .. import store
    rep = rep or report(conn)
    done = []
    for r in rep["rows"]:
        if r["verdict"] != "drop":
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("SELECT state FROM follow_ups WHERE id=?", (r["id"],)).fetchone()["state"] != "open":
                conn.execute("ROLLBACK")
                continue
            dec = store.record_decision(
                conn, f"Precedent: Ben loose end {r['id']} settled as {r['precedent']} was, by decision {r['cite']}",
                because=f"same shape (similarity {r['score']}); precedent; Ben may override")
            store.resolve_follow_up(conn, r["id"], decision_id=dec["decision_id"])
            conn.execute("COMMIT")
            done.append({"follow_up": r["id"], "decision": dec["decision_id"], "cites": r["cite"]})
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return done


def write_report(rep: dict[str, Any], out: Path) -> None:
    e = html.escape
    t = rep["tally"]
    day = time.strftime("%Y-%m-%d", time.localtime(rep["as_of"]))
    clients = "".join(
        f"<tr><td>{e(c)}</td>" + "".join(f'<td class="n">{v.get(k, 0)}</td>'
                                         for k in ("drop", "task", "conflicting", "none")) + "</tr>"
        for c, v in rep["by_client"].items())
    rows = "".join(
        f'<tr><td>{e(r["client"] or "—")}</td><td>{e(r["verdict"])}</td><td class="n">{r["score"]}</td>'
        f'<td>{e(r["what"][:220])}</td><td>{e(r["precedent_what"][:220])}</td>'
        f'<td><code>{e(r["cite"] or "")}</code> {e((r["statement"] or "")[:160])}</td></tr>'
        for r in sorted(rep["rows"], key=lambda r: (r["verdict"], -r["score"]))[:300])
    wb = rep.get("workbooks") or {"totals": {}, "files": []}
    wt = {k: 0 for k in ("loose_ends", "files_read", "open_rows", "by_decision", "by_workbook", "unruled")}
    wt.update(wb["totals"])
    wbrows = "".join(
        f'<tr><td><code>{e(f["follow_up"])}</code></td><td>{e(f["client"] or "—")}</td><td>{e(f["file"])}</td>'
        f'<td>{e(f["status"])}</td><td>{e("; ".join(f["sheets"]))}</td><td class="n">{f["open_rows"]}</td>'
        f'<td class="n">{f["by_decision"]}</td><td class="n">{f["by_workbook"]}</td><td class="n">{f["unruled"]}</td>'
        f'<td>{e(", ".join(x["key"] + " (" + x["cite"] + ")" for x in f["examples"]))}</td></tr>'
        for f in wb["files"])
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Precedent report</title>
<style>
:root {{ --bg:#fcfcfb; --ink:#111; --muted:#6b6a66; --grid:#e1e0d9; --accent:#2a78d6; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141413; --ink:#ecebe6; --muted:#9a9890; --grid:#2c2b28; --accent:#6aa5ea; }} }}
body {{ background:var(--bg); color:var(--ink); font:14px/1.5 system-ui, sans-serif; margin:0 auto; max-width:1180px; padding:24px 16px; }}
h1 {{ font-size:22px; margin:0 0 4px }} h2 {{ font-size:16px; margin:28px 0 8px }}
.muted {{ color:var(--muted) }} .kpis {{ display:flex; flex-wrap:wrap; gap:12px; margin:16px 0 }}
.kpi {{ border:1px solid var(--grid); border-radius:8px; padding:10px 14px; min-width:120px }}
.kpi b {{ display:block; font-size:22px }}
.wrap {{ overflow-x:auto }} table {{ border-collapse:collapse; width:100%; font-size:13px }}
th,td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--grid); vertical-align:top }}
td.n {{ text-align:right; font-variant-numeric:tabular-nums }} code {{ font-size:12px }}
</style></head><body>
<h1>Precedent report, {day}</h1>
<p class="muted">Read-only. How many of Ben's open loose ends an earlier ruling of the same shape would settle.
Nothing has been applied: apply is off (COORD_WORKFLOW_PRECEDENT) until Ben has read this.</p>
<div class="kpis">
<div class="kpi"><b>{rep["open"]}</b>open Ben loose ends</div>
<div class="kpi"><b>{t.get("drop", 0)}</b>settled: decided, no work</div>
<div class="kpi"><b>{t.get("task", 0)}</b>precedent says task it</div>
<div class="kpi"><b>{t.get("conflicting", 0)}</b>conflicting precedent</div>
<div class="kpi"><b>{t.get("none", 0)}</b>no precedent</div>
</div>
<h2>How it matches</h2>
<p>Each loose end's text is reduced to its shape: ids, numbers, file names, quoted text and proper nouns removed.
A precedent is an earlier Ben loose end for the same client that was ruled: dropped by a decision closing at most
{rep["bulk"]} loose ends (cleanup sweeps and the 14-day expiry are not rulings), or turned into a task. Shapes match
at {rep["match"]} word overlap or more. {rep["pool"]} past rulings qualify as precedent.</p>
<p>Apply would act only on the <b>decided, no work</b> column: one decision per loose end, "precedent; Ben may
override", citing the earlier decision. "Task it" rows would create work in Ben's name, so they are reported only.</p>
<h2>Workbook rows</h2>
<p>{wt["loose_ends"]} open Ben loose ends name a workbook. {wt["files_read"]} were read; their sheets asking for a
ruling ("Ben to rule", "Awaiting ruling", "QAD to confirm", rows with Status=Ruling, or a workbook's only data sheet)
hold <b>{wt["open_rows"]}</b> rows. A past ruling covers <b>{wt["by_decision"] + wt["by_workbook"]}</b> of them:
{wt["by_decision"]} by a Ben decision naming the row (FDD or tracker number, ADP/IF item, program), {wt["by_workbook"]}
by a settled ruling column for the same row in another workbook of the client's <code>work/</code>. {wt["unruled"]}
have none. Counts only: apply never reads or edits a workbook, and a matched row is a pointer for Ben, not a
settlement (a row's ruling can turn on context the key does not carry).</p>
<div class="wrap"><table><tr><th>Loose end</th><th>Client</th><th>Workbook</th><th>Read</th><th>Sheets</th>
<th class="n">Rows</th><th class="n">By decision</th><th class="n">By workbook</th><th class="n">None</th>
<th>Examples</th></tr>{wbrows}</table></div>
<h2>By client</h2>
<div class="wrap"><table><tr><th>Client</th><th class="n">Decided, no work</th><th class="n">Task it</th>
<th class="n">Conflicting</th><th class="n">None</th></tr>{clients}</table></div>
<h2>Matches (up to 300)</h2>
<div class="wrap"><table><tr><th>Client</th><th>Verdict</th><th class="n">Overlap</th><th>Open loose end</th>
<th>Precedent</th><th>Ruling cited</th></tr>{rows}</table></div>
</body></html>
"""
    out.write_text(doc, encoding="utf-8", newline="\n")
