"""Passive Ben steps close themselves (Ben, 2026-11-15).

A step marked `"passive": true` in its definition is one where Ben only
reads or uses what an agent produced ("Ben reads it", "Ben uses the
result"). Nothing downstream waits on it, yet each such case held a loose
end in his queue: C3, C4 and D6 alone held 65 of his 194 open cases.

Once a day the dashboard closes every open Ben loose end on a case waiting
at a passive step, once it has been open PASSIVE_DAYS business days, behind
one decision (`Auto-closed N passive Ben item(s) ...`). It closes them
('dropped' behind that decision), it does not defer them: reading is done
by then or not needed.

Kept, as the queue expiry keeps them (queue.stale):
  - anything dated: its own due date, its task's commitment
  - a draft reply waiting on him
  - the question currently presented
  - an item whose text asks for a ruling or an outward act (ASKS), since a
    passive step's loose end may still carry one
  - one this rule closed before and someone reopened (--undo)

`python -m coord_mcp.workflows passive` is the dry run; `--apply` closes;
`--undo <decision id>` reopens a batch. Switch `passive_close`, OFF: the
first dry run (2026-11-15) would have closed 30 items, most of them real
actions ("Raise with MAG ...", "Ben reviews and merges ...", "Name owners
...") that landed at a read/use step because it is the case's last one.
Turn it on only once landing sorts those into a step of their own.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from ..db import now
from . import definitions as defs

PASSIVE_DAYS = 5
EVERY = 86400
META = "passive_close_last"
DDL = """
CREATE TABLE IF NOT EXISTS follow_up_passive (
  follow_up_id TEXT PRIMARY KEY REFERENCES follow_ups(id),
  decision_id  TEXT NOT NULL,
  closed_at    INTEGER NOT NULL,
  reopened     INTEGER NOT NULL DEFAULT 0
);
"""
# Words that make a loose end more than reading: a ruling, an approval or
# something that leaves the building. Those stay in his queue.
ASKS = re.compile(r"\?|\b(approv\w*|rul(e|es|ing)|decid\w*|decision|confirm\w*|choose|pick|sign\w*|send|sends|"
                  r"paste|post|promot\w*|submit\w*|reply|respond|call|tell|estimate|price|scope)\b", re.I)


def ensure(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='follow_up_passive'").fetchone() is None:
        conn.executescript(DDL)


def steps() -> dict[str, set[str]]:
    """{workflow: {passive step keys}} from the shipped definitions."""
    out: dict[str, set[str]] = {}
    for code, d in defs.load().items():
        ks = {s["key"] for s in d["steps"] if s.get("passive")}
        if ks:
            out[code] = ks
    return out


def closable(conn: sqlite3.Connection, now_ts: int | None = None, days: int = PASSIVE_DAYS) -> list[dict[str, Any]]:
    """Open Ben loose ends on cases waiting at a passive step, old enough and not kept."""
    from .. import commitments, store
    from . import sla
    t = now() if now_ts is None else now_ts
    if not sla._has_cases(conn):
        return []
    ensure(conn)
    pas = steps()
    if not pas:
        return []
    q = store.current_question(conn)
    dues = commitments.due_map(conn)
    held = {r["follow_up_id"] for r in conn.execute(
        "SELECT follow_up_id FROM commitments WHERE state IN ('open','slipped') AND follow_up_id IS NOT NULL")} \
        if dues else set()
    reopened = {r["follow_up_id"] for r in conn.execute("SELECT follow_up_id FROM follow_up_passive")}
    need = days * sla.day_seconds()
    out = []
    for c in conn.execute("SELECT id, workflow, step, client FROM workflow_cases "
                          "WHERE state='open' AND waits_on='ben'"):
        if c["step"] not in pas.get(c["workflow"], set()):
            continue
        for f in conn.execute(
                """SELECT f.id, f.what, f.created_at, f.due_at, f.task_id, t.kind FROM follow_ups f
                   JOIN tasks t ON t.id = f.task_id
                   WHERE t.case_id=? AND f.state='open' AND lower(trim(f.who))='ben'""", (c["id"],)):
            if f["id"] in reopened or f["id"] in held or f["due_at"] is not None:
                continue
            if f["kind"] == "draft_reply" or f["task_id"] in dues:
                continue
            if q and q.get("follow_up_id") == f["id"]:
                continue
            if ASKS.search(f["what"] or ""):
                continue
            if sla.business_seconds(f["created_at"], t) < need:
                continue
            out.append({"id": f["id"], "case": c["id"], "workflow": c["workflow"], "step": c["step"],
                        "client": c["client"] or "unassigned", "what": f["what"],
                        "age_days": (t - f["created_at"]) // 86400})
    return out


def close(conn: sqlite3.Connection, apply: bool = False, now_ts: int | None = None,
          days: int = PASSIVE_DAYS) -> dict[str, Any]:
    """Close every closable item behind one decision. Dry run unless `apply`."""
    from .. import store
    t = now() if now_ts is None else now_ts
    rows = closable(conn, t, days)
    by: dict[str, int] = {}
    for r in rows:
        k = f"{r['workflow']} {r['step']}"
        by[k] = by.get(k, 0) + 1
    out: dict[str, Any] = {"ok": True, "applied": False, "count": len(rows), "by_step": by, "items": rows}
    if not apply or not rows:
        return out
    split = ", ".join(f"{k} {n}" for k, n in sorted(by.items(), key=lambda kv: -kv[1]))
    dec = store.record_decision(
        conn, f"Auto-closed {len(rows)} passive Ben item(s) open {days}+ business days: {split}"[:200],
        "passive-step rule, Ben 2026-11-15: read/use steps close themselves; "
        "python -m coord_mcp.workflows passive --undo <this id>")
    for r in rows:
        store.resolve_follow_up(conn, r["id"], decision_id=dec["decision_id"])
        conn.execute("INSERT OR REPLACE INTO follow_up_passive(follow_up_id, decision_id, closed_at) VALUES (?,?,?)",
                     (r["id"], dec["decision_id"], t))
    conn.commit()
    try:
        from . import cases as cs
        cs.sync(conn)
    except Exception:  # the dashboard's next sync catches up
        pass
    return {**out, "applied": True, "decision_id": dec["decision_id"]}


def undo(conn: sqlite3.Connection, decision_id: str) -> dict[str, Any]:
    """Reopen what one auto-close decision closed. They stay marked, so the
    rule leaves them alone after."""
    ensure(conn)
    ids = [r["id"] for r in conn.execute(
        """SELECT f.id FROM follow_up_passive p JOIN follow_ups f ON f.id = p.follow_up_id
           WHERE p.decision_id=? AND f.state='dropped' AND f.resolved_by=?""", (decision_id, decision_id))]
    for i in ids:
        conn.execute("UPDATE follow_ups SET state='open', resolved_by=NULL, resolved_at=NULL WHERE id=?", (i,))
        conn.execute("UPDATE follow_up_passive SET reopened=1 WHERE follow_up_id=?", (i,))
    conn.commit()
    return {"ok": True, "reopened": len(ids), "decision_id": decision_id}


def due(conn: sqlite3.Connection, now_ts: int | None = None) -> dict[str, Any] | None:
    """The dashboard's daily pass, behind switch `passive_close`. The first
    pass on a board only stamps the clock, as queue.expire_due does."""
    from .. import queue
    if not queue.switch(conn, "passive_close"):
        return None
    t = now() if now_ts is None else now_ts
    r = conn.execute("SELECT value FROM meta WHERE key=?", (META,)).fetchone()
    if r is not None and t - int(r["value"]) < EVERY:
        return None
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (META, str(t)))
    conn.commit()
    return close(conn, apply=True, now_ts=t) if r is not None else None
