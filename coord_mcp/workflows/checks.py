"""Case-step checks on coord_create_task and coord_complete_task (2026-11-14).

Advice, never a refusal: each check returns warnings the tool result carries,
the way coord_create_task already warns about a missing reply_date or
pipeline link. Two of them:

  no case    a root task (no parent, no spec case_id) for a client that has
             an open case of the same workflow it looks like a step of: same
             intent, same pipeline item, or a near-identical title (same dates). Master
             should have hung it under that case, so the case does not split
             in two and its step limit keeps counting the right work.
  skipped    a task in a case whose step comes after an agent step of the
             case that has not happened (no done task at it). A reply
             drafted before the status read, a check-in before the fix.
             Ben's steps are not checked (no task carries them), and the
             review step only once the review gate is on.

A task may name its case outright with spec "case_id" (create_task attaches
it). After the fact, `python -m coord_mcp.workflows attach <task> <case>`
does the same.

Switch: queue switch case_checks (on by default). Every check is wrapped so a
fault here can never stop a task from being created or closed.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from . import definitions as defs

SWITCH = "case_checks"
TITLE_MATCH = 0.6
_WORD = re.compile(r"[a-z0-9#][a-z0-9#._-]{2,}")
_STOP = {"the", "and", "for", "with", "from", "into", "draft", "reply", "task", "check", "read", "status"}


def on(conn: sqlite3.Connection) -> bool:
    from .. import queue
    try:
        return queue.switch(conn, SWITCH)
    except KeyError:
        return True


def _have_cases(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_cases'").fetchone() \
        is not None and "case_id" in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}


def _words(title: str, client: str | None) -> set[str]:
    from .tag import strip_client
    c = (client or "").casefold()
    return {w for w in _WORD.findall(strip_client(title or "").casefold()) if w not in _STOP and w != c}


_DATE = re.compile(r"(?<![\d/])\d{1,4}[/-]\d{1,2}(?:[/-]\d{2,4})?(?![\d/])")


def _similar(a: str, b: str, client: str | None) -> bool:
    # A recurring task dated differently (cleanup check 10/29 vs 11/08) is a
    # new instance, not a step of the old one.
    if set(_DATE.findall(a or "")) != set(_DATE.findall(b or "")):
        return False
    wa, wb = _words(a, client), _words(b, client)
    if len(wa) < 2 or len(wb) < 2:
        return False
    return len(wa & wb) / len(wa | wb) >= TITLE_MATCH


def _spec(v: Any) -> dict[str, Any]:
    if isinstance(v, dict):
        return v
    try:
        s = json.loads(v or "{}")
    except (TypeError, ValueError):
        return {}
    return s if isinstance(s, dict) else {}


def _root(conn: sqlite3.Connection, tid: str) -> str:
    from .cases import _root as root
    return root(conn, tid)


def case_of(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    """The case a task belongs to: its own case_id, else its root's case."""
    r = conn.execute("SELECT case_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    cid = r["case_id"] if r else None
    if not cid:
        root = _root(conn, task_id)
        r = conn.execute("SELECT case_id FROM tasks WHERE id=?", (root,)).fetchone()
        cid = r["case_id"] if r and r["case_id"] else None
        if not cid:
            c = conn.execute("SELECT id FROM workflow_cases WHERE root_task_id=?", (root,)).fetchone()
            cid = c["id"] if c else None
    return conn.execute("SELECT * FROM workflow_cases WHERE id=?", (cid,)).fetchone() if cid else None


def matching_cases(conn: sqlite3.Connection, kind: str, title: str, spec: dict[str, Any], client: str | None,
                   from_intent: str | None, pipeline_task_id: str | None,
                   exclude: str | None = None) -> list[tuple[sqlite3.Row, str]]:
    """Open cases for this client the new root task looks like a step of, with why."""
    if not client:
        return []
    from .tag import tag_root
    source = None
    if from_intent:
        r = conn.execute("SELECT source FROM intents WHERE id=?", (from_intent,)).fetchone()
        source = r["source"] if r else None
    code, _ = tag_root(kind, title, spec, client, source)
    rows = conn.execute(
        "SELECT c.*, t.title AS root_title, t.pipeline_task_id AS root_pipeline FROM workflow_cases c "
        "JOIN tasks t ON t.id=c.root_task_id WHERE c.state='open' AND lower(c.client)=lower(?) "
        "ORDER BY c.updated_at DESC", (client,)).fetchall()
    out = []
    for c in rows:
        if exclude and c["id"] == exclude:
            continue
        if from_intent and c["intent_id"] == from_intent and c["workflow"] == code:
            out.append((c, f"same intent {from_intent}"))
        elif pipeline_task_id and c["root_pipeline"] == pipeline_task_id and c["workflow"] == code:
            out.append((c, f"same pipeline item {pipeline_task_id}"))
        elif code and c["workflow"] == code and (_similar(title, c["root_title"], client)
                                                 or _similar(title, c["title"], client)):
            out.append((c, "same workflow, near-identical title"))
    return out[:3]


def _last_task(conn: sqlite3.Connection, case_id: str) -> str | None:
    r = conn.execute("SELECT id FROM tasks WHERE case_id=? ORDER BY COALESCE(completed_at, created_at) DESC, "
                     "created_at DESC LIMIT 1", (case_id,)).fetchone()
    return r["id"] if r else None


def skipped(conn: sqlite3.Connection, case: sqlite3.Row, kind: str, spec: dict[str, Any],
            parent_id: str | None, task_id: str | None = None) -> tuple[str | None, list[dict[str, Any]]]:
    """(the step this task carries, the agent steps before it that have not happened)."""
    d = defs.get(case["workflow"])
    if not d:
        return None, []
    steps = d["steps"]
    pstep = None
    if parent_id:
        p = conn.execute("SELECT case_step FROM tasks WHERE id=?", (parent_id,)).fetchone()
        pstep = p["case_step"] if p else None
    mine = None
    if task_id:
        r = conn.execute("SELECT case_step FROM tasks WHERE id=?", (task_id,)).fetchone()
        mine = r["case_step"] if r else None
    step = mine or defs.step_for(d, kind, spec, defs.step_index(d, pstep) if pstep else -1)
    at = defs.step_index(d, step)
    if at <= 0:
        return step, []
    done: set[str] = set()
    for t in conn.execute("SELECT id, kind, spec, parent_id, case_step FROM tasks WHERE case_id=? AND state='done'",
                          (case["id"],)).fetchall():
        if t["id"] == task_id:
            continue
        done.add(t["case_step"] or defs.step_for(d, t["kind"], _spec(t["spec"])) or "")
    reached = max((defs.step_index(d, k) for k in done), default=-1)
    if at <= reached:
        return step, []  # a loop back (a fix of a fix) or a step already passed
    from .. import gate
    gate_on = gate.since(conn) is not None
    miss = []
    for st in steps[max(reached, 0):at]:
        if st["key"] in done or not st.get("kinds") or st.get("optional"):
            continue
        if st["who"] == "agent" or (st["who"] == "code" and gate_on):
            miss.append(st)
    return step, miss


def _skip_text(case: sqlite3.Row, step: str | None, miss: list[dict[str, Any]], verb: str) -> str:
    names = ", ".join(f"{s['name']} ({s['key']})" for s in miss)
    return (f"case {case['id']} ({case['workflow']}): this task {verb} step {step}, but {names} has not happened "
            f"in the case (no done task at it). If that step is not needed here, record why on the case's task; "
            "otherwise task it first.")


def _no_case_text(matches: list[tuple[sqlite3.Row, str]], task_id: str) -> str:
    c, why = matches[0]
    return (f"no case: client {c['client']} has open case {c['id']} ({c['workflow']}, at {c['step'] or '?'}, "
            f"'{c['title'][:60]}') and this task looks like a step of it ({why}). If it is, run "
            f"`python -m coord_mcp.workflows attach {task_id} {c['id']}` in agent-tracking/ (next time: "
            f"parent_id = a task of that case, or spec case_id); if not, ignore this."
            + (f" Also matches {', '.join(m[0]['id'] for m in matches[1:])}." if len(matches) > 1 else ""))


def create_warnings(conn: sqlite3.Connection, task_id: str, kind: str, title: str, spec: dict[str, Any],
                    client: str | None, parent_id: str | None, from_intent: str | None,
                    pipeline_task_id: str | None) -> list[str]:
    """Case-step advice for a task just created. Never raises."""
    try:
        if not on(conn) or not _have_cases(conn):
            return []
        case = None
        if spec.get("case_id"):
            case = conn.execute("SELECT * FROM workflow_cases WHERE id=?", (str(spec["case_id"]),)).fetchone()
            if case is None:
                return [f"spec case_id {spec['case_id']!r} is not a case; the task was left without one."]
            conn.execute("UPDATE tasks SET case_id=? WHERE id=?", (case["id"], task_id))
        elif parent_id:
            case = case_of(conn, parent_id)
        if case is None:
            if parent_id:
                return []
            m = matching_cases(conn, kind, title, spec, client, from_intent, pipeline_task_id)
            return [_no_case_text(m, task_id)] if m else []
        if case["state"] == "dropped":
            return []
        step, miss = skipped(conn, case, kind, spec, parent_id)
        return [_skip_text(case, step, miss, "would be")] if miss else []
    except Exception as e:  # advice only: never block the create
        return [f"case check skipped ({type(e).__name__}: {str(e)[:80]})"]


def complete_warnings(conn: sqlite3.Connection, task_id: str) -> list[str]:
    """Case-step advice for a task that just closed done. Never raises."""
    try:
        if not on(conn) or not _have_cases(conn):
            return []
        t = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if t is None or t["state"] != "done":
            return []
        spec = _spec(t["spec"])
        case = case_of(conn, task_id)
        # A parentless task is "no case" only while it roots its own case (or has
        # none): one attached into another case (attach) is a step of that case.
        attached = case is not None and case["root_task_id"] != task_id
        if not t["parent_id"] and not spec.get("case_id") and not attached:
            m = matching_cases(conn, t["kind"], t["title"], spec, t["assigned_to"], t["from_intent"],
                               t["pipeline_task_id"], exclude=case["id"] if case is not None else None)
            if m:
                return [_no_case_text(m, task_id)]
            return []
        if case is None or case["state"] == "dropped":
            return []
        step, miss = skipped(conn, case, t["kind"], spec, t["parent_id"], task_id)
        return [_skip_text(case, step, miss, "carried")] if miss else []
    except Exception as e:
        return [f"case check skipped ({type(e).__name__}: {str(e)[:80]})"]


def attach(conn: sqlite3.Connection, task_id: str, case_id: str) -> dict[str, Any]:
    """Move a task into a case (master's fix for a 'no case' warning)."""
    from .cases import ensure_workflows, recompute
    ensure_workflows(conn)
    t = conn.execute("SELECT id, case_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    if t is None:
        raise KeyError(f"unknown task '{task_id}'")
    c = conn.execute("SELECT id FROM workflow_cases WHERE id=?", (case_id,)).fetchone()
    if c is None:
        raise KeyError(f"unknown case '{case_id}'")
    old = t["case_id"]
    conn.execute("UPDATE tasks SET case_id=?, case_step=NULL WHERE id=?", (case_id, task_id))
    out = {"task_id": task_id, "case_id": case_id, "was": old}
    out["case"] = recompute(conn, case_id)
    if old and old != case_id:
        left = conn.execute("SELECT COUNT(*) n FROM tasks WHERE case_id=?", (old,)).fetchone()["n"]
        if left == 0:
            conn.execute("UPDATE workflow_cases SET state='dropped', updated_at=strftime('%s','now') WHERE id=?",
                         (old,))
        else:
            recompute(conn, old)
    return out
