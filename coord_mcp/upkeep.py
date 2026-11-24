"""Session upkeep in code: the four chores Ben used to type to Master.

1. VPN watch (switch vpn_watch). Every setting vpn_interval_minutes the
   dashboard opens a TCP connection to port 22 of each VPN client's server
   (VPN_HOSTS; no login). A task that failed with a retryable "VPN" reason
   waits on that client's VPN; once the server has answered continuously
   since after the failure, the task is reopened (open, never picked up) and
   auto-dispatch sends it like any other. A parked task whose park says the
   VPN is down waits the same way before it is re-dispatched. Furlani and
   Yamamoto are not watched: their "handoff not configured" is not a VPN wait.
   A task reopened VPN_RELEASES_MAX times is left to master.

2. Parked re-dispatch (switch auto_redispatch). A parked task (fewer than
   PARKS_MAX parks; more is a spec to split, master's) gets an upkeep row:
   the mod checks get_session until the session is idle (its clear fired as
   the turn ended), then sends the same "Do task <id>" line.

3. Clear check (switch clear_check). After any delivered send (auto-dispatch
   or 2.), the mod reads get_usage until the session reports its context.
   A Messages category over setting clear_max_messages_k (thousands of
   tokens) means the clear did not fire: the session is told to clear itself
   and end its turn, and once get_session reads idle the task is sent again.
   CLEARS_MAX cycles that do not help flag the row to master (minder).

4. Restart after a coord update (switch code_restart). Every coord server
   records the hash of the code it runs (reaper.record_self). A registered
   client session whose live server runs other code than is on disk now,
   and that the reaper's rules call idle (no task in flight, quiet for
   setting restart_idle_minutes, no tool running), gets a restart row; once
   the mod's get_session reads not running, the board ends its claude.exe
   (the app starts it again, on the new code, at its next message). Never
   Master, the desk, or any unregistered session.

The board plans and keeps the state; the master-console mod only does the
app calls (/api/upkeep/claim, /api/upkeep/report), one row per tick.

    python -m coord_mcp.upkeep status     # VPN, stale sessions, upkeep rows
    python -m coord_mcp.upkeep vpn        # probe now
    python -m coord_mcp.upkeep drop <key> # forget one upkeep row (a flag master has handled)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from .db import ensure_dispatch, now

VPN_HOSTS = {"Moog": "fcp0ks1xdb03.odqad.com", "Royal": "fcp0hg5xdb09.odqad.com",
             "Cascade": "awo0et4xdb03.odqad.com"}
VPN_PORT = 22
VPN_TIMEOUT = 4.0
VPN_REASON = re.compile(r"\bVPN\b", re.I)
VPN_PARK = re.compile(r"\bVPN\b[^.;]{0,40}\b(down|off|unreachable|not (up|connected|reachable)|disconnected|dropped)\b"
                      r"|\bno VPN\b", re.I)
VPN_RELEASES_MAX = 3
PARKS_MAX = 3            # parked this often: master splits the spec
PARK_SETTLE = 60         # a park this fresh: the client is still ending its turn
LEASE = 60               # a claimed row not reported within this is offered again
POLL = 20                # get_session again after a running answer
VERIFY_AFTER = 15        # first get_usage this long after a delivered send
VERIFY_FOR = 180         # give up reading usage after this (unverified, not a flag)
CLEAR_WAIT_FOR = 20 * 60  # the session must go idle after the clear request within this
CLEARS_MAX = 2
SEND_ERRORS_MAX = 3
ACTIVE = ("idle_check", "send", "verify", "clear", "clear_wait")
MESSAGE = "Do task {task_id}. Start with coord_get_task."
CLEAR_MESSAGE = ("master-console upkeep: your context did not clear before task {task_id}. Run "
                 'clear_session "self" now and end the turn without starting work. The task will be sent again.')
REVIEW_CLEAR_MESSAGE = ("master-console upkeep: a definition-of-done review of work this session did not write "
                        "comes next (task {task_id}). Run clear_session \"self\" now and end the turn without "
                        "starting work. The review will be sent once you are idle.")
DECISION_PREFIXES = ("VPN watch:", "Upkeep:")  # bookkeeping, never a hold (store.NOT_HOLD_PREFIXES)

DDL = """
CREATE TABLE IF NOT EXISTS vpn_state (
  client     TEXT PRIMARY KEY,
  host       TEXT NOT NULL,
  up         INTEGER NOT NULL,
  checked_at INTEGER NOT NULL,
  up_since   INTEGER,
  down_since INTEGER,
  detail     TEXT
);
CREATE TABLE IF NOT EXISTS vpn_releases (
  task_id     TEXT NOT NULL,
  client      TEXT,
  released_at INTEGER NOT NULL,
  reason      TEXT,
  PRIMARY KEY (task_id, released_at)
);
CREATE TABLE IF NOT EXISTS upkeep (
  key         TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,
  task_id     TEXT,
  client      TEXT,
  session_id  TEXT NOT NULL,
  phase       TEXT NOT NULL,
  sends       INTEGER NOT NULL DEFAULT 0,
  clears      INTEGER NOT NULL DEFAULT 0,
  errors      INTEGER NOT NULL DEFAULT 0,
  next_at     INTEGER NOT NULL DEFAULT 0,
  deadline    INTEGER,
  lease_until INTEGER NOT NULL DEFAULT 0,
  claimer     TEXT,
  detail      TEXT,
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);
"""


def ensure_upkeep(conn: sqlite3.Connection) -> None:
    """Tables on first use. Never inside a SAVEPOINT the caller needs."""
    ensure_dispatch(conn)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='upkeep'").fetchone() is None:
        conn.executescript(DDL)


def _on(conn: sqlite3.Connection, name: str) -> bool:
    from .queue import switch
    return switch(conn, name)


def _setting(conn: sqlite3.Connection, name: str) -> float:
    from .queue import setting
    return setting(conn, name)


# ------------------------------------------------------------ 1. VPN watch

def probe(host: str, port: int = VPN_PORT, timeout: float = VPN_TIMEOUT) -> tuple[bool, str]:
    """Open and close a TCP connection: no login, nothing sent."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"port {port} answers"
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"[:200]


def probe_all(prober: Callable[[str], tuple[bool, str]] = probe) -> dict[str, tuple[bool, str]]:
    """Every VPN host at once, on threads, so a dead VPN costs one timeout."""
    out: dict[str, tuple[bool, str]] = {}

    def one(client: str, host: str) -> None:
        out[client] = prober(host)
    threads = [threading.Thread(target=one, args=(c, h), daemon=True) for c, h in VPN_HOSTS.items()]
    for th in threads:
        th.start()
    for th in threads:
        th.join(VPN_TIMEOUT + 2)
    return {c: out.get(c, (False, "probe did not return")) for c in VPN_HOSTS}


def record_vpn(conn: sqlite3.Connection, results: dict[str, tuple[bool, str]], t: int) -> None:
    for client, (up, detail) in results.items():
        r = conn.execute("SELECT up, up_since, down_since FROM vpn_state WHERE client=?", (client,)).fetchone()
        was = None if r is None else bool(r["up"])
        up_since = (r["up_since"] if r is not None and was else t) if up else None
        down_since = (r["down_since"] if r is not None and was is False else t) if not up else None
        conn.execute("INSERT OR REPLACE INTO vpn_state(client, host, up, checked_at, up_since, down_since, detail) "
                     "VALUES (?,?,?,?,?,?,?)", (client, VPN_HOSTS[client], int(up), t, up_since, down_since, detail))
    conn.commit()


def vpn_state(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """casefolded client -> its last probe row."""
    ensure_upkeep(conn)
    return {r["client"].casefold(): dict(r) for r in conn.execute("SELECT * FROM vpn_state")}


def vpn_answered_since(conn: sqlite3.Connection, client: str | None, since: int | None) -> bool | None:
    """True when the client's server has answered continuously since after
    `since`; False when it is down or was up across it; None when the client
    is not watched or never probed."""
    if not client or client.casefold() not in {c.casefold() for c in VPN_HOSTS}:
        return None
    st = vpn_state(conn).get(client.casefold())
    if st is None:
        return None
    return bool(st["up"]) and (st["up_since"] or 0) > (since or 0)


def _last_park_text(conn: sqlite3.Connection, task_id: str) -> str:
    r = conn.execute("SELECT progress FROM task_parks WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
                     (task_id,)).fetchone()
    if r is None:
        return ""
    try:
        p = json.loads(r["progress"])
    except ValueError:
        return ""
    return " ".join(str(p.get(k) or "") for k in ("next_step", "notes"))


def vpn_waits(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Tasks of watched clients that wait on their VPN: failed with a
    retryable VPN reason, or open and parked saying the VPN is down.
    [{task_id, client, how ('failed'|'parked'), since, reason, releases}]."""
    ensure_upkeep(conn)
    watched = tuple(VPN_HOSTS)
    q = ",".join("?" * len(watched))
    out = []
    for r in conn.execute(f"SELECT id, assigned_to, result, completed_at FROM tasks WHERE state='failed' "
                          f"AND assigned_to COLLATE NOCASE IN ({q})", watched):
        try:
            res = json.loads(r["result"] or "{}")
        except ValueError:
            continue
        reason = str(res.get("reason") or "")
        if res.get("done") is False and res.get("retryable") is True and VPN_REASON.search(reason):
            out.append({"task_id": r["id"], "client": r["assigned_to"], "how": "failed",
                        "since": r["completed_at"] or 0, "reason": reason})
    from .store import parked_tasks
    for p in parked_tasks(conn):
        if p["client"].casefold() in {c.casefold() for c in watched}:
            text = _last_park_text(conn, p["id"])
            if VPN_PARK.search(text):
                out.append({"task_id": p["id"], "client": p["client"], "how": "parked",
                            "since": p["parked_at"], "reason": text[:200]})
    for w in out:
        w["releases"] = conn.execute("SELECT COUNT(*) FROM vpn_releases WHERE task_id=?",
                                     (w["task_id"],)).fetchone()[0]
    return out


def reopen(conn: sqlite3.Connection, task_id: str, client: str, reason: str, t: int) -> bool:
    """A failed VPN task back to open and undelivered. The failure is kept
    in vpn_releases and the decision."""
    from .store import record_decision
    n = conn.execute("UPDATE tasks SET state='open', result=NULL, result_version=NULL, completed_at=NULL, "
                     "picked_up_at=NULL, dispatched_at=NULL, dispatch_status=NULL, dispatch_session=NULL, "
                     "reopened_at=? WHERE id=? AND state='failed'", (t, task_id)).rowcount
    if not n:
        return False
    conn.execute("INSERT OR REPLACE INTO vpn_releases(task_id, client, released_at, reason) VALUES (?,?,?,?)",
                 (task_id, client, t, reason[:200]))
    record_decision(conn, f"VPN watch: {client} server answers again; reopened failed task {task_id} "
                          "for auto-dispatch", because=f"failed with: {reason}"[:200], task_id=task_id)
    return True


def release_vpn_waits(conn: sqlite3.Connection, t: int | None = None) -> list[str]:
    """Reopen every failed VPN wait whose server has answered since. Parked
    waits need nothing here: plan_redispatch checks the same test."""
    t = now() if t is None else t
    out = []
    for w in vpn_waits(conn):
        if w["how"] != "failed" or w["releases"] >= VPN_RELEASES_MAX:
            continue
        if vpn_answered_since(conn, w["client"], w["since"]) and reopen(conn, w["task_id"], w["client"],
                                                                         w["reason"], t):
            out.append(w["task_id"])
    conn.commit()
    return out


def vpn_tick(conn: sqlite3.Connection, t: int | None = None, prober: Callable[[str], tuple[bool, str]] = probe,
             force: bool = False) -> dict[str, Any] | None:
    """The dashboard's call: probe every vpn_interval_minutes, then release."""
    t = now() if t is None else t
    ensure_upkeep(conn)
    if not _on(conn, "vpn_watch"):
        return None
    r = conn.execute("SELECT value FROM meta WHERE key='upkeep:vpn_last'").fetchone()
    if not force and r is not None and t - int(r["value"] or 0) < _setting(conn, "vpn_interval_minutes") * 60:
        return None
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('upkeep:vpn_last', ?)", (str(t),))
    conn.commit()
    results = probe_all(prober)
    record_vpn(conn, results, t)
    return {"probed": {c: up for c, (up, _) in results.items()}, "reopened": release_vpn_waits(conn, t)}


# ------------------------------------------------------------ rows

def _row(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM upkeep WHERE key=?", (key,)).fetchone()
    return dict(r) if r else None


def _put(conn: sqlite3.Connection, key: str, kind: str, session_id: str, phase: str, t: int, *,
         task_id: str | None = None, client: str | None = None, next_at: int | None = None,
         deadline: int | None = None, detail: str | None = None) -> None:
    old = _row(conn, key)
    conn.execute("INSERT OR REPLACE INTO upkeep(key, kind, task_id, client, session_id, phase, sends, clears, errors, "
                 "next_at, deadline, lease_until, claimer, detail, created_at, updated_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,NULL,?,?,?)",
                 (key, kind, task_id, client, session_id, phase, (old or {}).get("sends", 0) if old else 0,
                  (old or {}).get("clears", 0) if old else 0, 0, t if next_at is None else next_at, deadline,
                  detail, (old or {}).get("created_at", t) if old else t, t))


def _set(conn: sqlite3.Connection, key: str, t: int, **cols: Any) -> None:
    cols = {**cols, "updated_at": t, "lease_until": 0}
    conn.execute(f"UPDATE upkeep SET {', '.join(f'{k}=?' for k in cols)} WHERE key=?", (*cols.values(), key))


def _flag(conn: sqlite3.Connection, key: str, t: int, why: str) -> None:
    _set(conn, key, t, phase="flagged", detail=why[:300])


def active_keys(conn: sqlite3.Connection) -> set[str]:
    q = ",".join("?" * len(ACTIVE))
    return {r[0] for r in conn.execute(f"SELECT key FROM upkeep WHERE phase IN ({q})", ACTIVE)}


def restarting_sessions(conn: sqlite3.Connection) -> set[str]:
    """Sessions with a restart pending: next_dispatch sends them nothing."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='upkeep'").fetchone() is None:
        return set()
    return {r[0] for r in conn.execute("SELECT session_id FROM upkeep WHERE kind='restart' AND phase='idle_check'")}


# ------------------------------------------------------------ 2. parked re-dispatch

def _held_since(conn: sqlite3.Connection, task_id: str, since: int) -> bool:
    """A decision master recorded on the task after its last park: master's call."""
    from .store import NOT_HOLD_PREFIXES
    not_holds = " ".join("AND statement NOT LIKE ?" for _ in NOT_HOLD_PREFIXES)
    return conn.execute(f"SELECT 1 FROM decisions WHERE task_id=? AND created_at >= ? {not_holds} LIMIT 1",
                        (task_id, since, *(f"{p}%" for p in NOT_HOLD_PREFIXES))).fetchone() is not None


def redispatchable(conn: sqlite3.Connection, t: int | None = None) -> list[dict[str, Any]]:
    """Parked tasks code re-dispatches now: {task_id, client, session_id}.
    Left to master: PARKS_MAX parks, no registered session, a decision since
    the park, a VPN park whose server has not answered since."""
    from . import sessions
    from .store import parked_tasks
    t = now() if t is None else t
    ensure_upkeep(conn)
    if not _on(conn, "auto_redispatch"):
        return []
    reg = sessions.registry(conn)
    busy = sessions._busy_sessions(conn, reg, t)
    vpn = {w["task_id"] for w in vpn_waits(conn) if w["how"] == "parked"}
    keys = set(r[0] for r in conn.execute("SELECT key FROM upkeep"))
    out = []
    for p in parked_tasks(conn):
        if p["parks"] >= PARKS_MAX or p["parked_at"] > t - PARK_SETTLE or p["id"] in keys:
            continue
        kind = conn.execute("SELECT kind FROM tasks WHERE id=?", (p["id"],)).fetchone()[0]
        tg = sessions.target(reg, p["client"], kind)
        if tg is None or busy.get(tg[1], set()) - {p["id"]} or _held_since(conn, p["id"], p["parked_at"]):
            continue
        if p["id"] in vpn and not (_on(conn, "vpn_watch") and vpn_answered_since(conn, p["client"], p["parked_at"])):
            continue
        out.append({"task_id": p["id"], "client": p["client"], "session_id": tg[1]})
    return out


def auto_parked(conn: sqlite3.Connection, t: int | None = None) -> set[str]:
    """Parked tasks upkeep is handling or will handle: the minder leaves them.
    A VPN park waits on the watch, so it counts too while the watch is on."""
    ensure_upkeep(conn)
    ids = {d["task_id"] for d in redispatchable(conn, t)} if _on(conn, "auto_redispatch") else set()
    ids |= {r[0] for r in conn.execute("SELECT task_id FROM upkeep WHERE task_id IS NOT NULL AND phase IN (%s)"
                                       % ",".join("?" * len(ACTIVE)), ACTIVE)}
    if _on(conn, "auto_redispatch") and _on(conn, "vpn_watch"):
        ids |= {w["task_id"] for w in vpn_waits(conn) if w["how"] == "parked" and w["releases"] < VPN_RELEASES_MAX}
    return ids


def vpn_waiting_failed(conn: sqlite3.Connection) -> set[str]:
    """Failed tasks the VPN watch will reopen: the minder leaves them."""
    if not _on(conn, "vpn_watch"):
        return set()
    return {w["task_id"] for w in vpn_waits(conn) if w["how"] == "failed" and w["releases"] < VPN_RELEASES_MAX}


def plan_redispatch(conn: sqlite3.Connection, t: int) -> None:
    for d in redispatchable(conn, t):
        _put(conn, d["task_id"], "redispatch", d["session_id"], "idle_check", t,
             task_id=d["task_id"], client=d["client"])


def after_send(conn: sqlite3.Connection, task_id: str, session_id: str, client: str | None, status: str,
               t: int | None = None) -> None:
    """sessions.dispatch_sent's hook: a delivered auto-dispatch is checked
    for a clear that did not fire. A queued one is not: the usage read would
    be of whatever the session is doing now."""
    t = now() if t is None else t
    ensure_upkeep(conn)
    if status != "delivered" or not _on(conn, "clear_check"):
        return
    _put(conn, task_id, "dispatch", session_id, "verify", t, task_id=task_id, client=client,
         next_at=t + VERIFY_AFTER, deadline=t + VERIFY_FOR)
    conn.execute("UPDATE upkeep SET sends = sends + 1 WHERE key=?", (task_id,))


# ------------------------------------------------------------ 5. reviews on a cleared session

REVIEW_SETTLE = 60  # a review this fresh: its producer is still ending the turn that finished it


def review_session(conn: sqlite3.Connection, reg: dict[str, dict[str, str]], task_id: str,
                   client: str | None) -> tuple[str, bool] | None:
    """(session_id, must_clear) for an undelivered dod_review: the side
    session when there is one that did not produce the work under review,
    else the primary, cleared first. None: no session registered."""
    sess = reg.get((client or "").casefold()) or {}
    r = conn.execute("SELECT p.dispatch_session FROM tasks t LEFT JOIN tasks p ON p.id = t.parent_id "
                     "WHERE t.id=?", (task_id,)).fetchone()
    producer = r["dispatch_session"] if r else None
    if sess.get("side") and sess["side"] != producer:
        return sess["side"], False
    if sess.get("primary"):
        return sess["primary"], True
    return None


def reviewable(conn: sqlite3.Connection, t: int | None = None) -> list[dict[str, Any]]:
    """Undelivered dod_reviews code dispatches through a cleared session:
    {task_id, client, session_id}. Under switch auto_review_dispatch. A
    review with a side session of its own goes by plain auto-dispatch."""
    from . import sessions, store
    t = now() if t is None else t
    ensure_upkeep(conn)
    if not _on(conn, "auto_review_dispatch"):
        return []
    reg = sessions.registry(conn)
    busy = sessions._busy_sessions(conn, reg, t)
    keys = {r[0] for r in conn.execute("SELECT key FROM upkeep")}
    held = store.held_tasks(conn, t)
    waiting = store.waiting_on(conn)
    restarting = restarting_sessions(conn)
    active: dict[str, set[str]] = {}
    for u in conn.execute("SELECT task_id, session_id FROM upkeep WHERE phase IN (%s)"
                          % ",".join("?" * len(ACTIVE)), ACTIVE):
        active.setdefault(u["session_id"], set()).add(u["task_id"])
    out, used = [], set()
    for r in conn.execute("""SELECT t.id, t.assigned_to FROM tasks t WHERE t.state='open' AND t.kind='dod_review'
                               AND t.picked_up_at IS NULL AND t.dispatched_at IS NULL AND t.created_at <= ?
                               AND NOT EXISTS (SELECT 1 FROM task_parks p WHERE p.task_id = t.id)
                             ORDER BY t.created_at""", (t - REVIEW_SETTLE,)):
        if r["id"] in keys or r["id"] in held or r["id"] in waiting:
            continue
        rs = review_session(conn, reg, r["id"], r["assigned_to"])
        if rs is None or not rs[1]:
            continue
        sid = rs[0]
        if sid in used or sid in restarting or busy.get(sid, set()) - {r["id"]} or active.get(sid):
            continue
        used.add(sid)
        out.append({"task_id": r["id"], "client": r["assigned_to"], "session_id": sid})
    return out


def plan_reviews(conn: sqlite3.Connection, t: int) -> None:
    for d in reviewable(conn, t):
        _put(conn, d["task_id"], "review", d["session_id"], "idle_check", t,
             task_id=d["task_id"], client=d["client"])


def auto_undelivered(conn: sqlite3.Connection, t: int | None = None) -> set[str]:
    """Undelivered tasks upkeep is sending or will send: the minder leaves them."""
    ensure_upkeep(conn)
    ids = {d["task_id"] for d in reviewable(conn, t)}
    ids |= {r[0] for r in conn.execute("SELECT task_id FROM upkeep WHERE task_id IS NOT NULL AND phase IN (%s)"
                                       % ",".join("?" * len(ACTIVE)), ACTIVE)}
    return ids


# ------------------------------------------------------------ 4. restart after a coord update

def code_version(root: Path | None = None) -> str:
    """Hash of every .py under the coord_mcp package at `root` (default: this
    one). Read from disk each call, so a long-lived process sees new code."""
    root = Path(root) if root else Path(__file__).resolve().parent
    h = hashlib.sha1()
    for f in sorted(root.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        try:
            h.update(str(f.relative_to(root)).replace("\\", "/").encode())
            h.update(f.read_bytes())
        except OSError:
            continue
    return h.hexdigest()[:12]


def stale_sessions(conn: sqlite3.Connection, procs: dict[int, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Registered client sessions and the code their newest live coord server
    runs: [{client, role, session_id, server_pid, started_at, version,
    current, stale}]. With no process listing the newest recorded server
    counts as live. A server recorded before versions were kept is stale."""
    from . import reaper, sessions
    reaper.ensure_procs(conn)
    cur: dict[str, str] = {}
    out = []
    for s in sessions.list_sessions(conn):
        rows = [dict(r) for r in conn.execute("SELECT * FROM session_procs WHERE host_session_id=? "
                                              "ORDER BY started_at DESC", (s["session_id"],))]
        if procs:
            rows = [r for r in rows if r["server_pid"] in procs]
        if not rows:
            continue
        r = rows[0]
        root = r.get("code_root") or str(Path(__file__).resolve().parent)
        if root not in cur:
            cur[root] = code_version(Path(root)) if Path(root).is_dir() else ""
        out.append({"client": s["client"], "role": s["role"], "session_id": s["session_id"],
                    "server_pid": r["server_pid"], "started_at": r["started_at"], "version": r.get("code_version"),
                    "current": cur[root], "stale": bool(cur[root]) and r.get("code_version") != cur[root]})
    return out


def plan_restarts(conn: sqlite3.Connection, procs: dict[int, dict[str, Any]], t: int, **plan_kw: Any) -> list[str]:
    """Restart rows for stale sessions the reaper's rules call idle; rows for
    sessions that no longer qualify are dropped. Keeps the last stale list
    in meta for /upkeep."""
    from . import reaper
    stale = {s["session_id"]: s for s in stale_sessions(conn, procs)}
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('upkeep:stale', ?)",
                 (json.dumps({"at": t, "sessions": list(stale.values())}),))
    want: list[str] = []
    if _on(conn, "code_restart") and stale and any(s["stale"] for s in stale.values()):
        from .queue import switch
        plan = reaper.plan(conn, procs, idle_minutes=_setting(conn, "restart_idle_minutes"),
                           keep_rc=switch(conn, "idle_reaper_keep_rc"), **plan_kw)
        for p in plan:
            s = stale.get(p["session_id"])
            if s and s["stale"] and p["end"]:
                want.append(p["session_id"])
                if _row(conn, f"restart:{p['session_id']}") is None:
                    _put(conn, f"restart:{p['session_id']}", "restart", p["session_id"], "idle_check", t,
                         client=p["client"], detail=f"pid {p['pid']}, code {s['version'] or 'unversioned'} "
                                                    f"-> {s['current']}")
    conn.execute("DELETE FROM upkeep WHERE kind='restart' AND phase='idle_check' AND session_id NOT IN (%s)"
                 % ",".join("?" * len(want)) if want else
                 "DELETE FROM upkeep WHERE kind='restart' AND phase='idle_check'", want)
    conn.commit()
    return want


def restart_now(conn: sqlite3.Connection, session_id: str, *, lister: Callable[[], dict] | None = None,
                killer: Callable[[int], tuple[bool, str]] | None = None, **plan_kw: Any) -> tuple[bool, str]:
    """The mod read the session as not running: list again, check every rule
    again for this session, end its claude.exe if they still hold."""
    from . import reaper
    from .queue import switch
    lister = lister or reaper.list_processes
    killer = killer or reaper.kill_tree
    procs = lister()
    stale = {s["session_id"]: s for s in stale_sessions(conn, procs)}
    if not (stale.get(session_id) or {}).get("stale"):
        return False, "no longer running old code"
    rows = [p for p in reaper.plan(conn, procs, idle_minutes=_setting(conn, "restart_idle_minutes"),
                                   keep_rc=switch(conn, "idle_reaper_keep_rc"), **plan_kw)
            if p["session_id"] == session_id]
    if not rows or not rows[0]["end"]:
        return False, f"left alone: {rows[0]['why'] if rows else 'not registered'}"
    p = rows[0]
    ok, detail = killer(p["pid"])
    import time as _time
    reaper._log([f"{_time.strftime('%Y-%m-%d %H:%M')} {'RESTART' if ok else 'FAIL '} {p['client']} {p['role']} "
                 f"{session_id} pid {p['pid']} (old coord code{'' if ok else ': ' + detail})"])
    return ok, (f"ended pid {p['pid']}; the app restarts it on the new code at its next message"
                if ok else f"kill failed: {detail}")


def tick(conn: sqlite3.Connection, t: int | None = None) -> None:
    """The dashboard loop's call (not under pytest): the VPN probe on its
    interval, and the restart plan every 3 minutes."""
    t = now() if t is None else t
    ensure_upkeep(conn)
    vpn_tick(conn, t)
    r = conn.execute("SELECT value FROM meta WHERE key='upkeep:restart_last'").fetchone()
    # The process listing is a CIM query (seconds): every 3 minutes, and only
    # with the switch on.
    if not _on(conn, "code_restart") or (r is not None and t - int(r["value"] or 0) < 180):
        return
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('upkeep:restart_last', ?)", (str(t),))
    conn.commit()
    from . import reaper
    procs = reaper.list_processes()
    if procs:
        plan_restarts(conn, procs, t)


# ------------------------------------------------------------ the mod's claim and report

def _action(row: dict[str, Any]) -> dict[str, Any]:
    out = {"key": row["key"], "kind": row["kind"], "task_id": row["task_id"], "client": row["client"],
           "session_id": row["session_id"], "phase": row["phase"]}
    if row["phase"] in ("idle_check", "clear_wait"):
        out["action"] = "get_session"
    elif row["phase"] == "verify":
        out["action"] = "get_usage"
    else:
        out["action"] = "send"
        clear = REVIEW_CLEAR_MESSAGE if row["kind"] == "review" and not row["clears"] else CLEAR_MESSAGE
        out["message"] = (MESSAGE if row["phase"] == "send" else clear).format(task_id=row["task_id"])
    return out


def claim(conn: sqlite3.Connection, claimer: str | None = None, t: int | None = None) -> dict[str, Any] | None:
    """Plan the parked re-dispatches, then lease the first due row to this
    caller and say what app call it needs. A row whose task has closed is
    finished here instead."""
    t = now() if t is None else t
    ensure_upkeep(conn)
    from .db import ensure_commitments, ensure_deps
    ensure_deps(conn)
    ensure_commitments(conn)
    plan_redispatch(conn, t)
    plan_reviews(conn, t)
    conn.commit()
    q = ",".join("?" * len(ACTIVE))
    conn.execute("BEGIN IMMEDIATE")
    try:
        picked = None
        for r in conn.execute(f"SELECT * FROM upkeep WHERE phase IN ({q}) AND next_at <= ? AND lease_until <= ? "
                              "ORDER BY next_at, created_at", (*ACTIVE, t, t)).fetchall():
            r = dict(r)
            if r["task_id"]:
                st = conn.execute("SELECT state FROM tasks WHERE id=?", (r["task_id"],)).fetchone()
                if st is None or st["state"] != "open":
                    _set(conn, r["key"], t, phase="done", detail=f"task {st['state'] if st else 'gone'}")
                    continue
            picked = r
            conn.execute("UPDATE upkeep SET lease_until=?, claimer=? WHERE key=?",
                         (t + LEASE, (claimer or "")[:40] or None, r["key"]))
            break
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _action(picked) if picked else None


def _usage_messages_k(usage: dict[str, Any]) -> float | None:
    """Thousands of tokens in the Messages category of a get_usage answer;
    None when the session's context could not be read."""
    ctx = usage.get("context") if isinstance(usage.get("context"), dict) else usage
    if not isinstance(ctx, dict) or ctx.get("status") not in (None, "ok"):
        return None
    for c in ctx.get("categories") or []:
        if isinstance(c, dict) and str(c.get("name", "")).lower() == "messages":
            return float(c.get("tokens") or 0) / 1000
    if ctx.get("tokensUsed") is not None:
        return None  # a reading without categories: no basis to judge
    return None


def report(conn: sqlite3.Connection, key: str, *, running: str | None = None, status: str | None = None,
           usage: dict[str, Any] | None = None, detail: str | None = None, t: int | None = None,
           restarter: Callable[..., tuple[bool, str]] | None = None) -> dict[str, Any]:
    """The mod's answer for a claimed row. running: 'true'|'false'|'' (from
    get_session); status: delivered|queued|error (from send_message); usage:
    get_usage's JSON. Moves the row on and says to what phase."""
    t = now() if t is None else t
    ensure_upkeep(conn)
    row = _row(conn, key)
    if row is None:
        raise ValueError(f"no upkeep row {key!r}")
    phase = row["phase"]
    detail = (detail or "")[:300]
    if phase in ("idle_check", "clear_wait"):
        if running == "false":
            if row["kind"] == "restart":
                ok, why = (restarter or restart_now)(conn, row["session_id"])
                _set(conn, key, t, phase="done" if ok else "skipped", detail=why)
            else:
                if phase == "clear_wait":
                    _set(conn, key, t, clears=row["clears"] + 1)
                    _set(conn, key, t, phase="send", next_at=t, errors=0)
                elif row["kind"] == "review" and not row["clears"]:
                    # A review on the producer's session: clear it first (step 1 of
                    # the clear-and-confirm), then the send and its usage check.
                    _set(conn, key, t, phase="clear", next_at=t, errors=0)
                else:
                    _set(conn, key, t, phase="send", next_at=t, errors=0)
        elif row["deadline"] and t > row["deadline"]:
            _flag(conn, key, t, f"session still running {CLEAR_WAIT_FOR // 60} min after the clear request")
        else:
            errs = row["errors"] + (running not in ("true", "false"))
            _set(conn, key, t, next_at=t + (POLL if running == "true" else 3 * POLL), errors=errs,
                 detail=detail or row["detail"])
    elif phase in ("send", "clear"):
        if status in ("delivered", "queued"):
            if phase == "send":
                conn.execute("UPDATE tasks SET dispatched_at=?, dispatch_status=?, dispatch_session=? WHERE id=?",
                             (t, status, row["session_id"], row["task_id"]))
                if status == "delivered" and _on(conn, "clear_check"):
                    _set(conn, key, t, phase="verify", sends=row["sends"] + 1, errors=0,
                         next_at=t + VERIFY_AFTER, deadline=t + VERIFY_FOR)
                else:
                    _set(conn, key, t, phase="done", sends=row["sends"] + 1, detail=f"sent ({status})")
            else:
                _set(conn, key, t, phase="clear_wait", next_at=t + POLL, deadline=t + CLEAR_WAIT_FOR, errors=0)
        elif row["errors"] + 1 >= SEND_ERRORS_MAX:
            _flag(conn, key, t, f"send_message failed {SEND_ERRORS_MAX} times: {detail}")
        else:
            _set(conn, key, t, errors=row["errors"] + 1, next_at=t + 120, detail=detail)
    elif phase == "verify":
        k = _usage_messages_k(usage or {})
        limit = _setting(conn, "clear_max_messages_k")
        if k is None:
            if row["deadline"] and t > row["deadline"]:
                _set(conn, key, t, phase="done", detail="context never readable: unverified")
            else:
                _set(conn, key, t, next_at=t + 10)
        elif k <= limit:
            _set(conn, key, t, phase="done", detail=f"fresh: Messages {k:.0f}k")
        elif row["clears"] >= CLEARS_MAX:
            _flag(conn, key, t, f"Messages {k:.0f}k after {row['clears']} clear requests: the clear does not fire")
        else:
            _set(conn, key, t, phase="clear", next_at=t, detail=f"Messages {k:.0f}k > {limit:g}k: clear did not fire")
    else:
        raise ValueError(f"upkeep row {key!r} is {phase}, not waiting on a report")
    conn.commit()
    return {"ok": True, "key": key, "phase": _row(conn, key)["phase"]}


# ------------------------------------------------------------ master's view

def flagged(conn: sqlite3.Connection) -> list[str]:
    """Minder 'upkeep': flagged rows whose task is still open with no decision
    since the flag, and VPN waits past VPN_RELEASES_MAX."""
    ensure_upkeep(conn)
    out = []
    for r in conn.execute("SELECT u.key, u.task_id, u.client, u.detail, u.updated_at FROM upkeep u "
                          "LEFT JOIN tasks t ON t.id = u.task_id WHERE u.phase='flagged' "
                          "AND (u.task_id IS NULL OR (t.state='open' AND NOT EXISTS (SELECT 1 FROM decisions d "
                          "WHERE d.task_id=u.task_id AND d.created_at >= u.updated_at)))"):
        out.append(f"{r['key']} ({r['client']}: {r['detail']})")
    out += [f"{w['task_id']} ({w['client']}: VPN failure after {w['releases']} reopens)"
            for w in vpn_waits(conn) if w["releases"] >= VPN_RELEASES_MAX]
    return out


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    ensure_upkeep(conn)
    m = conn.execute("SELECT value FROM meta WHERE key='upkeep:stale'").fetchone()
    stale = json.loads(m["value"]) if m else {"at": None, "sessions": []}
    from .queue import switch
    return {"switches": {s: switch(conn, s) for s in ("vpn_watch", "auto_redispatch", "clear_check", "code_restart")},
            "vpn": [dict(r) for r in conn.execute("SELECT * FROM vpn_state ORDER BY client")],
            "vpn_waits": vpn_waits(conn), "stale": stale,
            "rows": [dict(r) for r in conn.execute("SELECT * FROM upkeep ORDER BY updated_at DESC LIMIT 50")]}


def page_html(conn: sqlite3.Connection) -> str:
    """/upkeep: VPN per client, sessions on old code, the upkeep rows."""
    from datetime import datetime
    from html import escape as esc
    s = status(conn)
    when = lambda x: datetime.fromtimestamp(x).strftime("%m-%d %H:%M") if x else "—"  # noqa: E731
    sw = " · ".join(f"{k} <b>{'on' if v else 'off'}</b>" for k, v in s["switches"].items())
    vpn = "".join(f"<tr><td>{esc(r['client'])}</td><td>{'<b>up</b>' if r['up'] else '<b style=color:#c33>down</b>'}"
                  f"</td><td>{when(r['up_since'] if r['up'] else r['down_since'])}</td><td>{when(r['checked_at'])}"
                  f"</td><td class=age>{esc(r['host'])} · {esc(r['detail'] or '')}</td></tr>" for r in s["vpn"])
    waits = "".join(f"<tr><td><a href='/board/task/{esc(w['task_id'])}'>{esc(w['task_id'])}</a></td>"
                    f"<td>{esc(w['client'])}</td><td>{esc(w['how'])}</td><td>{when(w['since'])}</td>"
                    f"<td>{w['releases']}</td><td class=age>{esc(w['reason'][:120])}</td></tr>" for w in s["vpn_waits"])
    stale = "".join(f"<tr><td>{esc(x['client'])} {esc(x['role'])}</td><td>{esc(x['session_id'])}</td>"
                    f"<td>{when(x['started_at'])}</td><td>{esc(x['version'] or 'unversioned')}</td>"
                    f"<td>{'<b>old code</b>' if x['stale'] else 'current'}</td></tr>"
                    for x in s["stale"]["sessions"])
    rows = "".join(f"<tr><td>{esc(r['key'])}</td><td>{esc(r['kind'])}</td><td>{esc(r['client'] or '')}</td>"
                   f"<td><b>{esc(r['phase'])}</b></td><td>{r['sends']}/{r['clears']}/{r['errors']}</td>"
                   f"<td>{when(r['updated_at'])}</td><td class=age>{esc(r['detail'] or '')}</td></tr>"
                   for r in s["rows"])

    def card(title: str, head: str, body: str, empty: str) -> str:
        return (f'<div class="card"><h2>{title}</h2>' + (f"<table><tr>{head}</tr>{body}</table>" if body
                                                         else f'<p class="age">{empty}</p>') + "</div>")
    th = lambda *c: "".join(f"<th>{x}</th>" for x in c)  # noqa: E731
    return (f'<p class="sub">Switches: {sw} (<code>python -m coord_mcp.queue switch &lt;name&gt; on|off</code>)</p>'
            + card("VPN", th("Client", "State", "Since", "Checked", "Host"), vpn, "not probed yet (switch vpn_watch)")
            + card("Tasks waiting on a VPN", th("Task", "Client", "How", "Since", "Reopens", "Reason"), waits, "none")
            + card(f"Client sessions' coord code (as of {when(s['stale']['at'])})",
                   th("Client", "Session", "Server started", "Code", "State"), stale, "no listing yet")
            + card("Upkeep rows (newest 50)", th("Key", "Kind", "Client", "Phase", "Sends/clears/errors", "Updated",
                                                 "Detail"), rows, "none yet"))


def main(argv: list[str] | None = None) -> int:
    from .db import connect
    p = argparse.ArgumentParser(prog="python -m coord_mcp.upkeep", description="Session upkeep in code.")
    p.add_argument("cmd", choices=["status", "vpn", "drop"], nargs="?", default="status")
    p.add_argument("key", nargs="?")
    a = p.parse_args(argv)
    conn = connect()
    try:
        if a.cmd == "vpn":
            print(json.dumps(vpn_tick(conn, force=True), indent=1))
        elif a.cmd == "drop":
            if not a.key:
                print("drop needs a key", file=sys.stderr)
                return 1
            ensure_upkeep(conn)
            n = conn.execute("DELETE FROM upkeep WHERE key=?", (a.key,)).rowcount
            conn.commit()
            print(f"{n} row dropped")
        else:
            print(json.dumps(status(conn), indent=1, default=str))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
