"""Dispatch in code: the client-to-session registry and the next task to send.

Master's manual (Dispatching) has it create a task, then send_message the
client's session `Do task <id>. Start with coord_get_task.`, one task in
flight per session. The board now holds which session is each client's
(client_sessions) and picks the next safe send (next_dispatch); the
master-console mod claims it (/api/dispatch/claim), sends it through the
app's send_message, and reports back (/api/dispatch/sent). A claim is a
short lease, so the desk and worker processes cannot both send one.

Left for master, never chosen here:
- a parked task (awaiting re-dispatch): its clear must be confirmed first;
  and while one is parked its session counts as busy, since a send would
  cancel that clear;
- a dod_review whose only target is the primary: the primary must clear
  first so the reviewer did not write the file;
- a client with no registered session for the role;
- a held task (a decision on it this week) or one waiting on a blocker.

    python -m coord_mcp.sessions list
    python -m coord_mcp.sessions set <client> <primary|side> <session_id> [--title T]
    python -m coord_mcp.sessions seed        # the primary table from the master manual
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from typing import Any

from .db import ensure_dispatch, now

ROLES = ("primary", "side")
# The manual's side-session kinds: work touching neither the server nor the
# primary's source tree.
SIDE_KINDS = frozenset({"draft_reply", "investigation", "data_pull", "dod_review"})
MESSAGE = "Do task {task_id}. Start with coord_get_task."


def _env_secs(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "")))
    except ValueError:
        return default


# A new task waits this long so master's own send (the manual's step two)
# lands first; a client picks up within seconds of a delivered message.
DISPATCH_AFTER = _env_secs("COORD_DISPATCH_AFTER", 120)
RESEND_AFTER = 15 * 60   # delivered or queued and still not picked up: send again
RETRY_AFTER = 120        # any attempt on a task (an error, a claim) waits this long before the next
LEASE = 60               # a claim not reported sent within this is free again
MINDER_QUIET = 30 * 60   # minder's 'never delivered' skips a task code sent this recently
ERRORS_TROUBLE = 2       # this many failed sends in a row to one session is a health problem

# The primary table in C:\development\agents\CLAUDE.md, 2026-11-13.
SEED = (
    ("Moog", "local_4a57a8e3-795c-44b6-a427-325db3d1f4ac"),
    ("Royal", "local_00af8403-98c1-4593-868b-fd37f668900b"),
    ("Cascade", "local_f779b381-a827-4e09-b068-96c606b00707"),
    ("LNK", "local_df114493-a3bd-43f0-9b7b-caa1720f0464"),
    ("TS Tech", "local_fe1a3778-030e-490a-b3c2-2442832bb556"),
    ("PBE", "local_1b20885b-48c7-4ddd-bf99-303defc72c19"),
    ("MAG", "local_ec170f58-ce1f-4744-8ee5-44f84a2f5cb5"),
    ("Furlani", "local_df66b5a7-13da-4656-9aa4-0a84f73db3e5"),
    ("Yamamoto", "local_02d1d8ac-f051-45a8-84d2-249ed9e01136"),
    ("GHSP", "local_7665f332-224e-413a-a51e-e0c347f94182"),
    ("JBSS", "local_f29062a7-4821-42da-a1da-b975fac94758"),
    ("Acument", "local_9a6c84aa-5cf1-4a47-a122-dbc9ba101085"),
    ("P&C", "local_12ca855c-2ea1-4d76-93e9-9a3ce626255d"),
)
SEED_TITLES = {"P&C": "P&C Local session"}


# ------------------------------------------------------------ registry

def set_session(conn: sqlite3.Connection, client: str, role: str, session_id: str,
                title: str | None = None) -> dict[str, Any]:
    client, session_id = (client or "").strip(), (session_id or "").strip()
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}, not {role!r}")
    if not client or not session_id:
        raise ValueError("client and session_id are required")
    ensure_dispatch(conn)
    title = title or (f"{client} - Local" if role == "primary" else f"{client} - Local 2")
    conn.execute("INSERT OR REPLACE INTO client_sessions(client, role, session_id, title, updated_at) "
                 "VALUES (?,?,?,?,?)", (client, role, session_id, title, now()))
    return {"client": client, "role": role, "session_id": session_id, "title": title}


def list_sessions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ensure_dispatch(conn)
    return [dict(r) for r in conn.execute(
        "SELECT client, role, session_id, title, updated_at FROM client_sessions ORDER BY client, role")]


def seed(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [set_session(conn, c, "primary", s, SEED_TITLES.get(c)) for c, s in SEED]


def registry(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    """client (casefolded) -> {role: session_id}."""
    out: dict[str, dict[str, str]] = {}
    for r in list_sessions(conn):
        out.setdefault(r["client"].casefold(), {})[r["role"]] = r["session_id"]
    return out


def target(reg: dict[str, dict[str, str]], client: str | None, kind: str) -> tuple[str, str] | None:
    """(role, session_id) a task of this kind goes to, or None when code must
    leave it for master: no session registered for the role, or a dod_review
    with no side session (the primary must clear first)."""
    sess = reg.get((client or "").casefold()) or {}
    role = "side" if kind in SIDE_KINDS and "side" in sess else "primary"
    if kind == "dod_review" and role != "side":
        return None
    return (role, sess[role]) if role in sess else None


# ------------------------------------------------------------ choosing

def _busy_sessions(conn: sqlite3.Connection, reg: dict[str, dict[str, str]], t: int) -> dict[str, set[str]]:
    """session_id -> the tasks occupying it: open and picked up, sent by code,
    under a live claim, or parked awaiting re-dispatch (its clear is pending)."""
    from .store import _open_status
    out: dict[str, set[str]] = {}
    for r in conn.execute(
        """SELECT t.id, t.kind, t.assigned_to, t.picked_up_at, t.dispatched_at, t.dispatch_session,
                  (SELECT MAX(p.created_at) FROM task_parks p WHERE p.task_id = t.id) last_park,
                  (SELECT a.session_id FROM dispatch_attempts a WHERE a.task_id = t.id AND a.status='claimed'
                     AND a.lease_until > ? ORDER BY a.created_at DESC LIMIT 1) claim
           FROM tasks t WHERE t.state = 'open' AND t.assigned_to IS NOT NULL""", (t,)):
        status = _open_status(r["last_park"], r["picked_up_at"])
        if not (r["picked_up_at"] or r["dispatched_at"] or r["claim"] or status == "parked"):
            continue
        sid = r["claim"] or r["dispatch_session"]
        if not sid:
            tg = target(reg, r["assigned_to"], r["kind"])
            sess = reg.get(r["assigned_to"].casefold()) or {}
            sid = tg[1] if tg else sess.get("primary")
        if sid:
            out.setdefault(sid, set()).add(r["id"])
    return out


def next_dispatch(conn: sqlite3.Connection, now_ts: int | None = None) -> dict[str, Any] | None:
    """At most one {task_id, client, role, session_id, message}: the oldest
    undelivered task (minder's set: open, never picked up, never parked, not
    held, not waiting; not sent in RESEND_AFTER, no attempt in RETRY_AFTER,
    at least DISPATCH_AFTER old) whose target session is free."""
    from . import store
    t = now() if now_ts is None else now_ts
    ensure_dispatch(conn)
    reg = registry(conn)
    if not reg:
        return None
    held = store.held_tasks(conn, t)
    waiting = store.waiting_on(conn)
    busy = _busy_sessions(conn, reg, t)
    for r in conn.execute(
        """SELECT t.id, t.kind, t.assigned_to FROM tasks t
           WHERE t.state='open' AND t.picked_up_at IS NULL AND t.assigned_to IS NOT NULL AND t.created_at <= ?
             AND NOT EXISTS (SELECT 1 FROM task_parks p WHERE p.task_id = t.id)
             AND (t.dispatched_at IS NULL OR t.dispatched_at <= ?)
             AND NOT EXISTS (SELECT 1 FROM dispatch_attempts a WHERE a.task_id = t.id AND a.created_at > ?)
           ORDER BY t.created_at, t.rowid""", (t - DISPATCH_AFTER, t - RESEND_AFTER, t - RETRY_AFTER)):
        if r["id"] in held or r["id"] in waiting:
            continue
        tg = target(reg, r["assigned_to"], r["kind"])
        if tg is None:
            continue
        role, sid = tg
        if busy.get(sid, set()) - {r["id"]}:
            continue
        return {"task_id": r["id"], "client": r["assigned_to"], "role": role, "session_id": sid,
                "kind": r["kind"], "message": MESSAGE.format(task_id=r["id"])}
    return None


# ------------------------------------------------------------ claim and report

def claim(conn: sqlite3.Connection, claimer: str | None = None, now_ts: int | None = None) -> dict[str, Any] | None:
    """next_dispatch under a LEASE-second claim, so a second process asking
    at the same moment gets the next one or nothing. The claim is the
    attempt row; dispatch_sent closes it."""
    t = now() if now_ts is None else now_ts
    from .db import ensure_deps
    ensure_dispatch(conn)  # before BEGIN: executescript would commit it
    ensure_deps(conn)      # (waiting_on, inside next_dispatch)
    conn.execute("BEGIN IMMEDIATE")
    try:
        d = next_dispatch(conn, t)
        if d is not None:
            from .store import _id
            d["attempt_id"] = _id()
            conn.execute("INSERT INTO dispatch_attempts(id, task_id, client, role, session_id, status, claimer, "
                         "created_at, lease_until) VALUES (?,?,?,?,?,'claimed',?,?,?)",
                         (d["attempt_id"], d["task_id"], d["client"], d["role"], d["session_id"],
                          (claimer or "")[:40] or None, t, t + LEASE))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return d


SENT_STATUSES = ("delivered", "queued", "error")


def dispatch_sent(conn: sqlite3.Connection, task_id: str, status: str, detail: str | None = None,
                  now_ts: int | None = None) -> dict[str, Any]:
    """Close the task's newest claim. delivered and queued stamp the task as
    dispatched to that session; error frees it for a retry after RETRY_AFTER."""
    t = now() if now_ts is None else now_ts
    if status not in SENT_STATUSES:
        raise ValueError(f"status must be one of {', '.join(SENT_STATUSES)}, not {status!r}")
    ensure_dispatch(conn)
    a = conn.execute("SELECT id, session_id FROM dispatch_attempts WHERE task_id=? AND status='claimed' "
                     "ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
    if a is None:
        raise ValueError(f"task {task_id!r} has no open dispatch claim")
    detail = (detail or "")[:500] or None
    conn.execute("UPDATE dispatch_attempts SET status=?, detail=?, sent_at=? WHERE id=?", (status, detail, t, a["id"]))
    if status == "error":
        conn.execute("UPDATE tasks SET dispatch_status='error' WHERE id=?", (task_id,))
    else:
        conn.execute("UPDATE tasks SET dispatched_at=?, dispatch_status=?, dispatch_session=? WHERE id=?",
                     (t, status, a["session_id"], task_id))
    return {"ok": True, "task_id": task_id, "status": status, "attempt_id": a["id"]}


def recently_dispatched(conn: sqlite3.Connection, now_ts: int, window: int = MINDER_QUIET) -> set[str]:
    """Tasks code sent within the window: minder does not wake master for them."""
    ensure_dispatch(conn)
    return {r[0] for r in conn.execute("SELECT id FROM tasks WHERE state='open' AND dispatched_at > ?",
                                       (now_ts - window,))}


def trouble(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """store.health problems: a session whose last ERRORS_TROUBLE sends all failed."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dispatch_attempts'").fetchone() is None:
        return []
    out = []
    for (sid,) in conn.execute("SELECT DISTINCT session_id FROM dispatch_attempts"):
        last = conn.execute("SELECT client, status, detail FROM dispatch_attempts WHERE session_id=? "
                            "AND status != 'claimed' ORDER BY COALESCE(sent_at, created_at) DESC, rowid DESC "
                            "LIMIT ?", (sid, ERRORS_TROUBLE)).fetchall()
        if len(last) == ERRORS_TROUBLE and all(r["status"] == "error" for r in last):
            out.append({"key": "dispatch", "detail": str(last[0]["detail"] or ""),
                        "text": f"auto-dispatch to {last[0]['client']} ({sid}) failed {ERRORS_TROUBLE} times "
                                "running; check the session id (python -m coord_mcp.sessions list)"})
    return out


# ------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    from .db import connect
    p = argparse.ArgumentParser(prog="python -m coord_mcp.sessions", description="Client session registry.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="every registered session")
    s = sub.add_parser("set", help="register or replace one client's session for a role")
    s.add_argument("client")
    s.add_argument("role", choices=ROLES)
    s.add_argument("session_id")
    s.add_argument("--title")
    sub.add_parser("seed", help="load the primary sessions from the master manual's table")
    a = p.parse_args(argv)
    conn = connect()
    try:
        if a.cmd == "set":
            r = set_session(conn, a.client, a.role, a.session_id, a.title)
            print(f"{r['client']} {r['role']}: {r['session_id']} ({r['title']})")
        elif a.cmd == "seed":
            print(f"{len(seed(conn))} primary sessions seeded")
        else:
            rows = list_sessions(conn)
            for r in rows:
                print(f"{r['client']:<10} {r['role']:<8} {r['session_id']}  {r['title'] or ''}")
            print(f"{len(rows)} registered")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
