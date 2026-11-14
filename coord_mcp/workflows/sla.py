"""Step time limits: which open cases have sat at their step past its sla.

Each step of a definition may carry an sla (definitions.py):
{"business_hours": n} or {"business_days": n}. Business time skips Saturdays
and Sundays in Ben's zone (COORD_TZ, else this machine's), the same rule
commitments.working_days_after uses; a business day is 24 such hours.

A case is over when the business time since it reached its step
(workflow_cases.step_at, kept by cases.sync) passes that step's limit.
Who it waits on decides where it goes:
  - agent or master: master owes it (chase the agent, re-dispatch, task the
    next step, or record why it waits)
  - ben: an item for Ben's queue (present it, or record why it waits)

The minder lists only cases whose limit passed after it first ran
(meta workflow_sla_since, set on that first pass): the backlog from before
is on /workflows, and listing it every pass would bury the rest of the
minder. A decision on any of the case's tasks after the limit passed, or for a Ben
case a question presented on it after then, quiets the case: master has
acted, or said why not. It returns when the case moves to another step and
passes that step's limit.

Read-only. COORD_WORKFLOW_SLA=0 turns the minder condition off.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, time as dtime, timedelta
from typing import Any

from . import definitions as defs

HOUR = 3600
DAY = 24 * HOUR


SINCE_KEY = "workflow_sla_since"


def since(conn: sqlite3.Connection, now: int) -> int:
    """When the minder condition first ran; set on the first pass that can write."""
    r = conn.execute("SELECT value FROM meta WHERE key=?", (SINCE_KEY,)).fetchone()
    if r:
        return int(r[0])
    try:
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?,?)", (SINCE_KEY, str(now)))
    except sqlite3.Error:  # a read-only connection: start from now, write next time
        pass
    return now


def enabled() -> bool:
    return os.environ.get("COORD_WORKFLOW_SLA", "1").strip().lower() not in ("0", "off", "false", "no", "")


def limit_seconds(sla: dict[str, Any] | None) -> int | None:
    """Business seconds a step may take; None when it has no sla."""
    if not sla:
        return None
    if "business_hours" in sla:
        return int(float(sla["business_hours"]) * HOUR)
    if "business_days" in sla:
        return int(float(sla["business_days"]) * DAY)
    return None


def _tz(tz: Any = None):
    from ..commitments import _tz as ctz
    return ctz(tz)


def business_seconds(start: int, end: int, tz: Any = None) -> int:
    """Seconds between start and end that fall on a weekday, local time."""
    if end <= start:
        return 0
    z = _tz(tz)
    cur = datetime.fromtimestamp(start, z)
    stop = datetime.fromtimestamp(end, z)
    total = 0.0
    while cur < stop:
        nxt = datetime.combine(cur.date() + timedelta(days=1), dtime(0), tzinfo=z)
        seg_end = min(nxt, stop)
        if cur.weekday() < 5:
            total += seg_end.timestamp() - cur.timestamp()
        cur = seg_end
    return int(total)


def deadline(start: int, limit: int, tz: Any = None) -> int:
    """The moment `limit` business seconds after `start` have passed."""
    z = _tz(tz)
    cur = datetime.fromtimestamp(start, z)
    left = float(limit)
    while True:
        nxt = datetime.combine(cur.date() + timedelta(days=1), dtime(0), tzinfo=z)
        if cur.weekday() < 5:
            span = nxt.timestamp() - cur.timestamp()
            if left <= span:
                return int(cur.timestamp() + left)
            left -= span
        cur = nxt


def _has_cases(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_cases'").fetchone() is not None


def _fmt(sec: int) -> str:
    h = sec / HOUR
    return f"{h:.0f}h" if h < 48 else f"{h / 24:.0f}bd"


def status(case: dict[str, Any], now: int, tz: Any = None) -> dict[str, Any] | None:
    """{limit, due_at, over, step_name, who} for an open case whose step has
    an sla; None otherwise. `over` is business seconds past the limit (<= 0
    while still inside it)."""
    if case.get("state") != "open" or not case.get("step"):
        return None
    d = defs.get(case.get("workflow"))
    st = next((s for s in (d or {}).get("steps") or [] if s["key"] == case["step"]), None)
    lim = limit_seconds((st or {}).get("sla"))
    if lim is None:
        return None
    since = case.get("step_at") or case.get("created_at")
    used = business_seconds(since, now, tz)
    return {"limit": lim, "due_at": deadline(since, lim, tz), "over": used - lim,
            "step_name": st["name"], "who": st["who"], "sla": st["sla"]}


def _quiet(conn: sqlite3.Connection, case_id: str, after: int, ben: bool) -> bool:
    if conn.execute("SELECT 1 FROM decisions d JOIN tasks t ON t.id=d.task_id "
                    "WHERE t.case_id=? AND d.created_at > ? LIMIT 1", (case_id, after)).fetchone():
        return True
    if ben and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='questions'").fetchone():
        return conn.execute(
            """SELECT 1 FROM questions q
               WHERE q.created_at > ?
                 AND (q.task_id IN (SELECT id FROM tasks WHERE case_id=?)
                      OR q.follow_up_id IN (SELECT f.id FROM follow_ups f JOIN tasks t ON t.id=f.task_id
                                            WHERE t.case_id=?))
               LIMIT 1""", (after, case_id, case_id)).fetchone() is not None
    return False


def overdue(conn: sqlite3.Connection, now: int, tz: Any = None, quiet: bool = True) -> list[dict[str, Any]]:
    """Open cases past their step's limit, most over first. With `quiet`,
    cases master has acted on since the limit passed are left out."""
    if not _has_cases(conn):
        return []
    out = []
    for r in conn.execute("SELECT * FROM workflow_cases WHERE state='open' AND step IS NOT NULL"):
        c = dict(r)
        s = status(c, now, tz)
        if not s or s["over"] <= 0:
            continue
        if quiet and _quiet(conn, c["id"], s["due_at"], c["waits_on"] == "ben"):
            continue
        out.append({"case": c["id"], "workflow": c["workflow"], "client": c["client"], "title": c["title"],
                    "root_task_id": c["root_task_id"], "step": c["step"], "step_name": s["step_name"],
                    "waits_on": c["waits_on"], "over": s["over"], "limit": s["limit"], "due_at": s["due_at"]})
    out.sort(key=lambda x: -x["over"])
    return out


def label(o: dict[str, Any]) -> str:
    """One minder entry: case id, workflow/step, time over, who it waits on.
    No commas: the minder joins entries with them."""
    return f'{o["case"]} {o["workflow"] or "untagged"}/{o["step"]} +{_fmt(o["over"])} {o["waits_on"] or "?"}'


def minder_owed(conn: sqlite3.Connection, now: int, tz: Any = None) -> dict[str, list[str]]:
    """{"master": [...], "ben": [...]} minder entries; empty when off."""
    if not enabled():
        return {"master": [], "ben": []}
    start = since(conn, now)
    rows = [o for o in overdue(conn, now, tz) if o["due_at"] >= start]
    return {"master": [label(o) for o in rows if o["waits_on"] in ("agent", "master")],
            "ben": [label(o) for o in rows if o["waits_on"] == "ben"]}
