"""Cases: one instance of a workflow per trigger.

A case is a root task and everything under it, tagged to a workflow
(tag.py) and walked against its definition (definitions.py). It links the
intent the root came from, every task in the chain (tasks.case_id and
tasks.case_step), the commitment a task of it carries, and through its tasks
the decisions and the draft reply.

Where a case stands is derived from the board, never set by hand:
  - an open task in it: the case is at that task's step, waiting on the agent
  - no open task, a Ben loose end open on it: waiting on Ben
  - no open task, a client loose end open or a failed last task: waiting on
    master (to task or re-dispatch it), or on the engine for a step it spawns
  - none of that: done, closed at its last task's completion

sync() keeps this current. It writes only the two tables below and the two
task columns, so it never changes what an agent or Ben sees elsewhere.
Created on first use, like projects and dispatch, and for the same reason:
no SCHEMA_VERSION bump while older coord servers are still running. Those
servers read tasks with SELECT * and insert by column name, so the two new
columns are invisible to them.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from typing import Any

from ..db import now
from . import definitions as defs
from .tag import tag_root

WORKFLOWS_DDL = """
CREATE TABLE IF NOT EXISTS workflow_cases (
  id            TEXT PRIMARY KEY,
  workflow      TEXT,
  client        TEXT,
  title         TEXT NOT NULL,
  state         TEXT NOT NULL CHECK (state IN ('open','done','dropped')),
  step          TEXT,
  step_at       INTEGER,
  waits_on      TEXT,
  intent_id     TEXT,
  root_task_id  TEXT NOT NULL UNIQUE REFERENCES tasks(id),
  commitment_id TEXT,
  origin        TEXT NOT NULL,
  rule          TEXT,
  created_at    INTEGER NOT NULL,
  updated_at    INTEGER NOT NULL,
  closed_at     INTEGER
);
CREATE INDEX IF NOT EXISTS workflow_cases_open ON workflow_cases(state, workflow);
CREATE TABLE IF NOT EXISTS workflow_case_events (
  id       TEXT PRIMARY KEY,
  case_id  TEXT NOT NULL REFERENCES workflow_cases(id),
  step     TEXT,
  event    TEXT NOT NULL CHECK (event IN ('opened','step','spawned','stopped','closed','reopened')),
  task_id  TEXT,
  waits_on TEXT,
  detail   TEXT,
  ts       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS workflow_case_events_case ON workflow_case_events(case_id, ts);
"""
_TASK_COLS = ("case_id TEXT", "case_step TEXT")


def ensure_workflows(conn: sqlite3.Connection) -> None:
    """Create the case tables and tasks.case_id / case_step if this file has
    none yet. Two cheap reads when both exist, so every reader can call it.
    Never inside a SAVEPOINT the caller needs: executescript commits."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_case_events'").fetchone() is None:
        conn.executescript(WORKFLOWS_DDL)
    have = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    for col in _TASK_COLS:
        if col.split()[0] not in have:
            try:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {col}")
            except sqlite3.OperationalError as e:  # another process added it first
                if "duplicate column" not in str(e):
                    raise
    conn.execute("CREATE INDEX IF NOT EXISTS tasks_case ON tasks(case_id)")


def enabled() -> bool:
    """Case sync rides the dashboard loop unless COORD_WORKFLOW_SYNC=0."""
    return os.environ.get("COORD_WORKFLOW_SYNC", "1").strip() not in ("0", "false", "off", "")


def _id() -> str:
    return uuid.uuid4().hex[:12]


def event(conn: sqlite3.Connection, case_id: str, ev: str, step: str | None = None, task_id: str | None = None,
          waits_on: str | None = None, detail: str | None = None, ts: int | None = None) -> None:
    conn.execute("INSERT INTO workflow_case_events(id, case_id, step, event, task_id, waits_on, detail, ts) "
                 "VALUES (?,?,?,?,?,?,?,?)", (_id(), case_id, step, ev, task_id, waits_on, detail, ts or now()))


def _root(conn: sqlite3.Connection, tid: str) -> str:
    seen = set()
    cur = tid
    while cur not in seen:
        seen.add(cur)
        r = conn.execute("SELECT parent_id FROM tasks WHERE id=?", (cur,)).fetchone()
        if r is None or not r["parent_id"]:
            return cur
        cur = r["parent_id"]
    return cur


def open_case(conn: sqlite3.Connection, root_id: str, origin: str = "sync") -> str:
    """The case for a root task, made if it has none. Tagged by tag.py."""
    r = conn.execute("SELECT id FROM workflow_cases WHERE root_task_id=?", (root_id,)).fetchone()
    if r:
        return r["id"]
    t = conn.execute("""SELECT t.*, i.source AS source, i.created_at AS intent_at
                        FROM tasks t LEFT JOIN intents i ON i.id = t.from_intent WHERE t.id=?""",
                     (root_id,)).fetchone()
    code, rule = tag_root(t["kind"], t["title"], t["spec"], t["assigned_to"], t["source"])
    cid = _id()
    start = min(x for x in (t["created_at"], t["intent_at"]) if x)
    conn.execute("""INSERT INTO workflow_cases(id, workflow, client, title, state, intent_id, root_task_id,
                                               origin, rule, created_at, updated_at)
                    VALUES (?,?,?,?, 'open', ?,?,?,?,?,?)""",
                 (cid, code, t["assigned_to"], t["title"], t["from_intent"], root_id, origin, rule, start, now()))
    event(conn, cid, "opened", detail=f"{code or 'untagged'} by rule {rule}", ts=start)
    return cid


def _attach(conn: sqlite3.Connection, origin: str) -> set[str]:
    """Give every task without a case its root's case. Returns the touched case ids."""
    touched: set[str] = set()
    for r in conn.execute("SELECT id FROM tasks WHERE case_id IS NULL ORDER BY created_at, id").fetchall():
        root = _root(conn, r["id"])
        cid = conn.execute("SELECT case_id FROM tasks WHERE id=?", (root,)).fetchone()["case_id"]
        if not cid:
            cid = open_case(conn, root, origin)
            conn.execute("UPDATE tasks SET case_id=? WHERE id=?", (cid, root))
        conn.execute("UPDATE tasks SET case_id=? WHERE id=?", (cid, r["id"]))
        touched.add(cid)
    return touched


def _step_tasks(conn: sqlite3.Connection, case: sqlite3.Row, d: dict[str, Any] | None) -> list[sqlite3.Row]:
    """The case's tasks, oldest first, each given its step if it has none."""
    rows = conn.execute("SELECT * FROM tasks WHERE case_id=? ORDER BY created_at, id", (case["id"],)).fetchall()
    step_of: dict[str, str | None] = {r["id"]: r["case_step"] for r in rows}
    for r in rows:
        if r["case_step"] is None and d:
            after = defs.step_index(d, step_of.get(r["parent_id"] or "")) if r["parent_id"] else -1
            try:
                spec = json.loads(r["spec"] or "{}")
            except ValueError:
                spec = {}
            st = defs.step_for(d, r["kind"], spec if isinstance(spec, dict) else {}, after)
            if st:
                conn.execute("UPDATE tasks SET case_step=? WHERE id=?", (st, r["id"]))
                step_of[r["id"]] = st
    return conn.execute("SELECT * FROM tasks WHERE case_id=? ORDER BY created_at, id", (case["id"],)).fetchall()


def _client_loose_end(who: str, client: str | None) -> bool:
    w = (who or "").strip().lower()
    return w != "ben" and w != "master"


def where(conn: sqlite3.Connection, case: sqlite3.Row, d: dict[str, Any] | None,
          tasks: list[sqlite3.Row]) -> dict[str, Any]:
    """{state, step, waits_on, since, task} for a case, from its tasks and loose ends."""
    steps = (d or {}).get("steps") or []
    idx = lambda key: defs.step_index(d, key) if d else -1  # noqa: E731
    open_tasks = [t for t in tasks if t["state"] == "open"]
    if open_tasks:
        t = max(open_tasks, key=lambda t: (idx(t["case_step"]), t["created_at"]))
        return {"state": "open", "step": t["case_step"], "waits_on": "agent", "since": t["created_at"],
                "task": t["id"]}
    ids = [t["id"] for t in tasks]
    marks = ",".join("?" * len(ids))
    fus = conn.execute(f"SELECT task_id, who, created_at FROM follow_ups WHERE state='open' AND task_id IN ({marks})",
                       ids).fetchall() if ids else []
    last = max(tasks, key=lambda t: (t["completed_at"] or 0, idx(t["case_step"])))
    reached = max((idx(t["case_step"]) for t in tasks if t["state"] == "done"), default=-1)
    nxt = steps[reached + 1] if 0 <= reached + 1 < len(steps) else None
    ben = [f for f in fus if (f["who"] or "").strip().lower() == "ben"]
    if ben:
        # Waiting on Ben: at the next Ben step if the definition has one there, else
        # at the step the loose end came from.
        key = nxt["key"] if nxt and nxt["who"] == "ben" else last["case_step"]
        return {"state": "open", "step": key, "waits_on": "ben", "since": min(f["created_at"] for f in ben),
                "task": ben[0]["task_id"]}
    client = [f for f in fus if _client_loose_end(f["who"], case["client"])]
    if client or last["state"] == "failed":
        key = nxt["key"] if nxt and client else last["case_step"]
        since = min(f["created_at"] for f in client) if client else last["completed_at"]
        return {"state": "open", "step": key, "waits_on": "master", "since": since, "task": last["id"]}
    return {"state": "done", "step": last["case_step"], "waits_on": None,
            "since": max(t["completed_at"] or t["created_at"] for t in tasks), "task": last["id"]}


def recompute(conn: sqlite3.Connection, case_id: str) -> dict[str, Any]:
    case = conn.execute("SELECT * FROM workflow_cases WHERE id=?", (case_id,)).fetchone()
    d = defs.get(case["workflow"])
    tasks = _step_tasks(conn, case, d)
    if not tasks:
        return {"state": case["state"]}
    w = where(conn, case, d, tasks)
    commitment = None
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='commitments'").fetchone():
        ids = [t["id"] for t in tasks]
        r = conn.execute(f"SELECT id FROM commitments WHERE task_id IN ({','.join('?' * len(ids))}) "
                         "ORDER BY created_at DESC LIMIT 1", ids).fetchone()
        commitment = r["id"] if r else None
    moved = (w["step"], w["waits_on"], w["state"]) != (case["step"], case["waits_on"], case["state"])
    if moved:
        if w["state"] == "done" and case["state"] != "done":
            event(conn, case_id, "closed", w["step"], w["task"], None, ts=w["since"])
        elif w["state"] == "open" and case["state"] == "done":
            event(conn, case_id, "reopened", w["step"], w["task"], w["waits_on"], ts=w["since"])
        else:
            event(conn, case_id, "step", w["step"], w["task"], w["waits_on"], ts=w["since"])
    conn.execute("""UPDATE workflow_cases SET state=?, step=?, step_at=?, waits_on=?, commitment_id=?,
                    closed_at=?, updated_at=? WHERE id=?""",
                 (w["state"], w["step"], w["since"] if moved or case["step_at"] is None else case["step_at"],
                  w["waits_on"], commitment, w["since"] if w["state"] == "done" else None, now(), case_id))
    return w


def sync(conn: sqlite3.Connection, origin: str = "sync", every: bool = False) -> dict[str, int]:
    """Attach new tasks to cases and re-derive where every open case stands
    (every case when `every`). Idempotent; one transaction."""
    ensure_workflows(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        touched = _attach(conn, origin)
        q = "SELECT id FROM workflow_cases" + ("" if every else " WHERE state='open'")
        ids = touched | {r["id"] for r in conn.execute(q)}
        # A loose end resolved or a task closed on a case already done can reopen it.
        if not every:
            ids |= {r["case_id"] for r in conn.execute(
                "SELECT DISTINCT t.case_id FROM follow_ups f JOIN tasks t ON t.id=f.task_id "
                "JOIN workflow_cases c ON c.id=t.case_id WHERE f.state='open' AND c.state='done'")}
        for cid in ids:
            recompute(conn, cid)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"attached_cases": len(touched), "recomputed": len(ids)}


def backfill(conn: sqlite3.Connection) -> dict[str, int]:
    """Every past task into a case, every case re-derived. Safe to re-run."""
    out = sync(conn, origin="backfill", every=True)
    out["cases"] = conn.execute("SELECT COUNT(*) FROM workflow_cases").fetchone()[0]
    out["open"] = conn.execute("SELECT COUNT(*) FROM workflow_cases WHERE state='open'").fetchone()[0]
    return out


def case_detail(conn: sqlite3.Connection, case_id: str) -> dict[str, Any] | None:
    """A case with its links: intent, tasks, decisions, latest draft, commitment, events."""
    ensure_workflows(conn)
    c = conn.execute("SELECT * FROM workflow_cases WHERE id=?", (case_id,)).fetchone()
    if c is None:
        return None
    tasks = [dict(r) for r in conn.execute(
        "SELECT id, parent_id, kind, title, state, case_step, created_at, completed_at FROM tasks "
        "WHERE case_id=? ORDER BY created_at, id", (case_id,))]
    ids = [t["id"] for t in tasks]
    marks = ",".join("?" * len(ids)) or "''"
    decisions = [dict(r) for r in conn.execute(
        f"SELECT id, task_id, statement, created_at FROM decisions WHERE task_id IN ({marks}) ORDER BY created_at",
        ids)]
    drafts = [t for t in tasks if t["kind"] == "draft_reply"]
    return {**dict(c), "tasks": tasks, "decisions": decisions, "draft_task_id": drafts[-1]["id"] if drafts else None,
            "events": [dict(r) for r in conn.execute(
                "SELECT step, event, task_id, waits_on, detail, ts FROM workflow_case_events WHERE case_id=? "
                "ORDER BY ts, rowid", (case_id,))]}


def list_cases(conn: sqlite3.Connection, open_only: bool = False, workflow: str | None = None,
               client: str | None = None) -> list[dict[str, Any]]:
    """Cases newest first, as plain dicts (the CLI and /workflows)."""
    ensure_workflows(conn)
    q, args = "SELECT * FROM workflow_cases WHERE 1=1", []
    if open_only:
        q += " AND state='open'"
    if workflow:
        q += " AND workflow=?"
        args.append(workflow)
    if client:
        q += " AND client=?"
        args.append(client)
    return [dict(r) for r in conn.execute(q + " ORDER BY created_at DESC, id", args)]
