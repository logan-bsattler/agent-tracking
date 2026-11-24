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
    python -m coord_mcp.sessions dry-run     # what auto-dispatch would send now
    python -m coord_mcp.sessions idle        # free sessions beside owed work 15+ min
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


def _candidates(conn: sqlite3.Connection, t: int, *, paced: bool = True) -> list[sqlite3.Row]:
    """Open, never picked up, never parked tasks with a client, earliest-due
    commitment first (commitments.due_map), then oldest. paced adds the
    send timers: DISPATCH_AFTER old, not sent in RESEND_AFTER, no attempt in
    RETRY_AFTER."""
    from .commitments import due_map
    sql = """SELECT t.id, t.kind, t.assigned_to, t.created_at FROM tasks t
             WHERE t.state='open' AND t.picked_up_at IS NULL AND t.assigned_to IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM task_parks p WHERE p.task_id = t.id)"""
    args: tuple[Any, ...] = ()
    if paced:
        sql += """ AND t.created_at <= ? AND (t.dispatched_at IS NULL OR t.dispatched_at <= ?)
                   AND NOT EXISTS (SELECT 1 FROM dispatch_attempts a WHERE a.task_id = t.id AND a.created_at > ?)"""
        args = (t - DISPATCH_AFTER, t - RESEND_AFTER, t - RETRY_AFTER)
    from .direct import direct_ids
    direct = direct_ids(conn)
    if paced and direct:
        # Ben's direct asks skip the DISPATCH_AFTER wait (direct.py); the
        # resend and retry timers still hold.
        sql = sql.replace("t.created_at <= ? AND", "(t.created_at <= ? OR t.id IN (%s)) AND"
                          % ",".join("?" * len(direct)))
        args = (args[0], *direct, *args[1:])
    rows = conn.execute(sql + " ORDER BY t.created_at, t.rowid", args).fetchall()
    try:
        dues = due_map(conn)
    except sqlite3.Error:
        dues = {}
    inf = float("inf")
    # Direct asks first, then earliest due, then oldest. sorted() is stable:
    # equal keys keep the created_at order.
    return sorted(rows, key=lambda r: (r["id"] not in direct, (dues.get(r["id"]) or {}).get("due_at") or inf))


def next_dispatch(conn: sqlite3.Connection, now_ts: int | None = None,
                  exclude_sessions: set[str] | frozenset[str] = frozenset()) -> dict[str, Any] | None:
    """At most one {task_id, client, role, session_id, message}: the
    earliest-due, then oldest, undelivered task (minder's set: open, never
    picked up, never parked, not held, not waiting; not sent in RESEND_AFTER,
    no attempt in RETRY_AFTER, at least DISPATCH_AFTER old) whose target
    session is free. exclude_sessions counts as busy (the dry run)."""
    from . import store
    t = now() if now_ts is None else now_ts
    ensure_dispatch(conn)
    reg = registry(conn)
    if not reg:
        return None
    held = store.held_tasks(conn, t)
    waiting = store.waiting_on(conn)
    busy = _busy_sessions(conn, reg, t)
    # A session waiting on its restart after a coord update gets nothing new
    # until it has restarted (upkeep.py); nor one with an upkeep send in flight.
    from . import upkeep
    upkeep.ensure_upkeep(conn)
    exclude_sessions = set(exclude_sessions) | upkeep.restarting_sessions(conn)
    for u in conn.execute("SELECT task_id, session_id FROM upkeep WHERE task_id IS NOT NULL AND phase IN (%s)"
                          % ",".join("?" * len(upkeep.ACTIVE)), upkeep.ACTIVE):
        busy.setdefault(u["session_id"], set()).add(u["task_id"])
    for r in _candidates(conn, t):
        if r["id"] in held or r["id"] in waiting:
            continue
        tg = target(reg, r["assigned_to"], r["kind"])
        if tg is None:
            continue
        role, sid = tg
        if sid in exclude_sessions or busy.get(sid, set()) - {r["id"]}:
            continue
        return {"task_id": r["id"], "client": r["assigned_to"], "role": role, "session_id": sid,
                "kind": r["kind"], "message": MESSAGE.format(task_id=r["id"])}
    return None


def dry_run(conn: sqlite3.Connection, now_ts: int | None = None) -> dict[str, Any]:
    """What auto-dispatch would send over the next ticks if nothing else
    changed (one per tick, each session at most once), and why every other
    undelivered task is left. Reads only; claims nothing."""
    from . import store
    from .db import ensure_deps
    t = now() if now_ts is None else now_ts
    ensure_dispatch(conn)
    ensure_deps(conn)
    sends: list[dict[str, Any]] = []
    used: set[str] = set()
    while (d := next_dispatch(conn, t, used)) is not None:
        sends.append(d)
        used.add(d["session_id"])
    reg = registry(conn)
    held = store.held_tasks(conn, t)
    waiting = store.waiting_on(conn)
    busy = _busy_sessions(conn, reg, t)
    paced = {r["id"] for r in _candidates(conn, t)}
    chosen = {d["task_id"] for d in sends}
    left: list[dict[str, Any]] = []
    for r in _candidates(conn, t, paced=False):
        if r["id"] in chosen:
            continue
        tg = target(reg, r["assigned_to"], r["kind"])
        if r["id"] in held:
            why = f"held by decision {held[r['id']]['id']}"
        elif r["id"] in waiting:
            why = "waiting on " + ", ".join(b["id"] for b in waiting[r["id"]])
        elif tg is None:
            why = ("review with no side session: primary must clear first (master)"
                   if r["kind"] == "dod_review" and (reg.get(r["assigned_to"].casefold()) or {})
                   else "no session registered for this client (master)")
        elif r["id"] not in paced:
            why = "pacing: too new, just sent, or just attempted"
        elif busy.get(tg[1], set()) - {r["id"]}:
            why = "session busy with " + ", ".join(sorted(busy[tg[1]] - {r["id"]}))
        else:
            why = "session gets an earlier task first"
        left.append({"task_id": r["id"], "client": r["assigned_to"], "kind": r["kind"], "why": why})
    for r in conn.execute("SELECT t.id, t.kind, t.assigned_to FROM tasks t WHERE t.state='open' "
                          "AND t.picked_up_at IS NULL AND EXISTS (SELECT 1 FROM task_parks p WHERE p.task_id=t.id)"):
        left.append({"task_id": r["id"], "client": r["assigned_to"], "kind": r["kind"],
                     "why": "parked: upkeep re-dispatches it once the session is idle "
                            "(python -m coord_mcp.upkeep status), else master"})
    return {"sends": sends, "left": left}


IDLE_AFTER = 15 * 60  # a free session beside dispatchable work this long is an alert


def idle_sessions(conn: sqlite3.Connection, now_ts: int | None = None,
                  after: int = IDLE_AFTER) -> list[dict[str, Any]]:
    """Registered client sessions with nothing in flight on the board while
    their client has owed, dispatchable work (open, undelivered, never
    parked, not held, not waiting), both true for `after` seconds: since the
    later of the oldest such task's creation and the client's last finished
    task on that session. [{session_id, client, role, idle_since, task_ids}].
    A review with no side session counts against the primary (master clears
    it first)."""
    from . import store
    from .db import ensure_deps
    t = now() if now_ts is None else now_ts
    ensure_dispatch(conn)
    ensure_deps(conn)
    reg = registry(conn)
    if not reg:
        return []
    held = store.held_tasks(conn, t)
    waiting = store.waiting_on(conn)
    busy = _busy_sessions(conn, reg, t)
    owed: dict[str, dict[str, Any]] = {}
    for r in _candidates(conn, t, paced=False):
        if r["id"] in held or r["id"] in waiting:
            continue
        sess = reg.get(r["assigned_to"].casefold()) or {}
        tg = target(reg, r["assigned_to"], r["kind"])
        role, sid = tg if tg else ("primary", sess.get("primary"))
        if not sid or busy.get(sid):
            continue
        o = owed.setdefault(sid, {"session_id": sid, "client": r["assigned_to"], "role": role,
                                  "first": r["created_at"], "task_ids": []})
        o["first"] = min(o["first"], r["created_at"])
        o["task_ids"].append(r["id"])
    out = []
    for sid, o in owed.items():
        last = conn.execute("SELECT MAX(completed_at) FROM tasks WHERE assigned_to = ? COLLATE NOCASE "
                            "AND (dispatch_session = ? OR dispatch_session IS NULL)",
                            (o["client"], sid)).fetchone()[0] or 0
        since = max(o["first"], last)
        if since <= t - after:
            out.append({"session_id": sid, "client": o["client"], "role": o["role"],
                        "idle_since": since, "task_ids": o["task_ids"]})
    return sorted(out, key=lambda o: o["idle_since"])


MANUAL = "manual"        # claimer of a hand-send's pending claim
MANUAL_LEASE = 180       # a pending hand-send not confirmed within this is free again
MANUAL_PHASES = ("pending", "sent", "failed")


def _live_auto_claim(conn: sqlite3.Connection, t: int, task_id: str, session_id: str) -> sqlite3.Row | None:
    """A live auto-dispatch claim on this task or on this session, if any."""
    return conn.execute(
        "SELECT task_id, session_id FROM dispatch_attempts WHERE status='claimed' AND lease_until > ? "
        "AND COALESCE(claimer, '') <> ? AND (task_id = ? OR (session_id = ? AND session_id <> '')) "
        "ORDER BY created_at DESC LIMIT 1", (t, MANUAL, task_id, session_id)).fetchone()


def mark_manual(conn: sqlite3.Connection, task_id: str, session_id: str | None = None,
                now_ts: int | None = None, phase: str = "sent", detail: str | None = None) -> dict[str, Any]:
    """Master sends 'Do task <id>' by hand (coord-rules sees the send_message).

    phase "pending", before the send: under the same write lock auto-dispatch
    claims with, refuse ({"ok": False, "conflict": ...}) when auto-dispatch
    holds a live claim on this task or this session (it is sending right now),
    else take a MANUAL_LEASE claim on the session, so no auto tick gives it a
    second task while the send runs. phase "sent", after a delivered or queued
    result: close that claim and stamp the task as dispatched to the session,
    so code counts it busy until the client picks it up; a task code itself
    sent within RESEND_AFTER keeps code's stamp. phase "failed": close the
    claim as an error and stamp nothing."""
    if phase not in MANUAL_PHASES:
        raise ValueError(f"phase must be one of {', '.join(MANUAL_PHASES)}, not {phase!r}")
    t = now() if now_ts is None else now_ts
    ensure_dispatch(conn)
    r = conn.execute("SELECT id, kind, assigned_to, state, picked_up_at, dispatched_at, dispatch_status "
                     "FROM tasks WHERE id=?", (task_id,)).fetchone()
    if r is None:
        raise ValueError(f"no task {task_id!r}")
    reg = registry(conn)
    sid = (session_id or "").strip()
    tg = target(reg, r["assigned_to"], r["kind"])
    if not sid:
        sid = tg[1] if tg else ((reg.get((r["assigned_to"] or "").casefold()) or {}).get("primary") or "")
    pend = ("SELECT id FROM dispatch_attempts WHERE task_id=? AND status='claimed' AND claimer=? "
            "ORDER BY created_at DESC LIMIT 1")
    if phase == "failed":
        a = conn.execute(pend, (task_id, MANUAL)).fetchone()
        if a is not None:
            conn.execute("UPDATE dispatch_attempts SET status='error', detail=?, sent_at=? WHERE id=?",
                         ((detail or "hand-send not delivered")[:500], t, a["id"]))
        return {"ok": True, "task_id": task_id, "stamped": False}
    if r["state"] != "open" or r["picked_up_at"]:
        if phase == "sent":  # close any pending claim; nothing to stamp
            conn.execute("UPDATE dispatch_attempts SET status='delivered', sent_at=? WHERE task_id=? "
                         "AND status='claimed' AND claimer=?", (t, task_id, MANUAL))
        return {"ok": True, "task_id": task_id, "stamped": False}
    if phase == "pending":
        conn.execute("BEGIN IMMEDIATE")
        try:
            c = _live_auto_claim(conn, t, task_id, sid)
            if c is None:
                from .store import _id
                conn.execute("INSERT INTO dispatch_attempts(id, task_id, client, role, session_id, status, claimer, "
                             "created_at, lease_until) VALUES (?,?,?,?,?,'claimed',?,?,?)",
                             (_id(), task_id, r["assigned_to"] or "", tg[0] if tg else "primary", sid, MANUAL,
                              t, t + MANUAL_LEASE))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        if c is not None:
            what = ("is sending this task now" if c["task_id"] == task_id
                    else f"is sending task {c['task_id']} to this session now")
            return {"ok": False, "conflict": True, "task_id": task_id, "session_id": sid or None,
                    "claimed_task": c["task_id"],
                    "error": f"auto-dispatch {what}; one task in flight per session. Check coord_board "
                             "before sending by hand."}
        return {"ok": True, "task_id": task_id, "pending": True, "session_id": sid or None}
    # phase == "sent"
    a = conn.execute(pend, (task_id, MANUAL)).fetchone()
    if a is not None:
        conn.execute("UPDATE dispatch_attempts SET status='delivered', detail=?, sent_at=? WHERE id=?",
                     ((detail or "")[:500] or None, t, a["id"]))
    code_sent = (r["dispatch_status"] in ("delivered", "queued") and r["dispatched_at"]
                 and r["dispatched_at"] > t - RESEND_AFTER)
    if code_sent or _live_auto_claim(conn, t, task_id, "") is not None:
        # Auto-dispatch already has this task in flight: keep its stamp, add no second one.
        return {"ok": True, "task_id": task_id, "stamped": False, "auto": True}
    conn.execute("UPDATE tasks SET dispatched_at=?, dispatch_status='manual', dispatch_session=? WHERE id=?",
                 (t, sid or None, task_id))
    return {"ok": True, "task_id": task_id, "stamped": True, "session_id": sid or None}


# ------------------------------------------------------------ claim and report

def _claim_fingerprint(conn: sqlite3.Connection) -> tuple:
    """Moves whenever a claim is added or changes status, or a task is stamped
    dispatched (a hand-send included)."""
    a = conn.execute("SELECT COUNT(*), MAX(rowid), SUM(status='claimed'), "
                     "MAX(COALESCE(sent_at, 0)) FROM dispatch_attempts").fetchone()
    return (*a, conn.execute("SELECT MAX(dispatched_at) FROM tasks").fetchone()[0])


def _still_dispatchable(conn: sqlite3.Connection, task_id: str) -> bool:
    r = conn.execute("SELECT state, picked_up_at FROM tasks WHERE id=?", (task_id,)).fetchone()
    return bool(r) and r["state"] == "open" and r["picked_up_at"] is None


def claim(conn: sqlite3.Connection, claimer: str | None = None, now_ts: int | None = None) -> dict[str, Any] | None:
    """next_dispatch under a LEASE-second claim, so a second process asking
    at the same moment gets the next one or nothing. The claim is the
    attempt row; dispatch_sent closes it."""
    t = now() if now_ts is None else now_ts
    from .db import ensure_deps
    ensure_dispatch(conn)  # before BEGIN: executescript would commit it
    ensure_deps(conn)      # (waiting_on, inside next_dispatch)
    from .db import ensure_commitments
    ensure_commitments(conn)  # (due_map, inside next_dispatch)
    # next_dispatch reads the whole board (4.5 s on an idle machine, far
    # longer in the busy dashboard process), so it runs before the write lock,
    # not under it: holding BEGIN IMMEDIATE through it starved every other
    # writer past busy_timeout ("database is locked", 2026-11-18). Under the
    # lock, the pick stands only if no claim landed and the task did not move
    # since the read; otherwise it is re-picked there, as before.
    seen = _claim_fingerprint(conn)
    d = next_dispatch(conn, t)
    if d is None:
        return None
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _claim_fingerprint(conn) != seen or not _still_dispatchable(conn, d["task_id"]):
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
                     "AND COALESCE(claimer, '') <> ? ORDER BY created_at DESC LIMIT 1", (task_id, MANUAL)).fetchone()
    if a is None:
        raise ValueError(f"task {task_id!r} has no open dispatch claim")
    detail = (detail or "")[:500] or None
    conn.execute("UPDATE dispatch_attempts SET status=?, detail=?, sent_at=? WHERE id=?", (status, detail, t, a["id"]))
    if status == "error":
        conn.execute("UPDATE tasks SET dispatch_status='error' WHERE id=?", (task_id,))
    else:
        conn.execute("UPDATE tasks SET dispatched_at=?, dispatch_status=?, dispatch_session=? WHERE id=?",
                     (t, status, a["session_id"], task_id))
        # Did the session's clear fire? (upkeep.py, switch clear_check)
        from . import upkeep
        upkeep.after_send(conn, task_id, a["session_id"], None, status, t)
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
    sub.add_parser("dry-run", help="what auto-dispatch would send now, and why the rest waits")
    sub.add_parser("idle", help="free sessions beside owed work for 15+ minutes")
    a = p.parse_args(argv)
    conn = connect()
    try:
        if a.cmd == "set":
            r = set_session(conn, a.client, a.role, a.session_id, a.title)
            print(f"{r['client']} {r['role']}: {r['session_id']} ({r['title']})")
        elif a.cmd == "seed":
            print(f"{len(seed(conn))} primary sessions seeded")
        elif a.cmd == "dry-run":
            r = dry_run(conn)
            print(f"would send ({len(r['sends'])}, one per 10s tick):")
            for d in r["sends"]:
                print(f"  {d['task_id']}  {d['client']:<10} {d['role']:<8} {d['kind']:<14} -> {d['session_id']}")
            print(f"left ({len(r['left'])}):")
            for x in r["left"]:
                print(f"  {x['task_id']}  {(x['client'] or ''):<10} {x['kind']:<14} {x['why']}")
        elif a.cmd == "idle":
            rows = idle_sessions(conn)
            for o in rows:
                mins = (now() - o["idle_since"]) // 60
                print(f"{o['client']:<10} {o['role']:<8} {o['session_id']}  idle {mins} min  "
                      f"tasks {', '.join(o['task_ids'])}")
            print(f"{len(rows)} idle")
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
