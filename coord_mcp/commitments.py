"""Commitments: what Ben promised whom, by when (docs/commitments.md).

Phase one: the dashboard (/commitments), the minder, Ben's queue order and
this CLI. No MCP tool reads or writes commitments yet, so an agent sees no
difference; master uses the CLI, as it does for projects.

    python -m coord_mcp.commitments add <client> --to WHO --what WHAT [--due TEXT] [--said ISO]
                                        [--to-email E] [--source teams|email|meeting|manual] [--ref R]
                                        [--intent I] [--task T] [--follow-up F] [--project P] [--none]
    python -m coord_mcp.commitments set <id> [--due TEXT] [--said ISO] [--decision D]
                                        [--to WHO] [--to-email E] [--what WHAT]
    python -m coord_mcp.commitments link <id> [--task T] [--follow-up F]
    python -m coord_mcp.commitments close <id> done|dropped --decision D
    python -m coord_mcp.commitments list [<client>] [--state open|slipped|done|dropped|live]
    python -m coord_mcp.commitments sweep

--due takes the words used ("Friday", "tomorrow", "end of week", "next
week") or an ISO date, resolved against --said (default now) in Ben's time
zone (COORD_TZ, else this machine's). No date words means an inferred date.
"""

from __future__ import annotations

import builtins
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone, tzinfo
from typing import Any

from .db import ensure_commitments

# Ben approved the design without answering its open questions; these are
# its recommended defaults, kept here so each is one edit to change.
INFERRED_WORKING_DAYS = 2      # "I'll get back to you": due two working days after it was said
COMING_DUE_WORKING_DAYS = 1    # the minder flags a commitment this far ahead of its due date
UNTRACKED_MINUTES = 30         # open this long with no task and no loose end: the minder asks
DEFAULT_BY = "Ben"             # phase one stores only Ben's own promises
DUE_HOUR = 17                  # a stated day means 17:00 local that day
CLOSE_STATES = ("done", "dropped")  # closed only by a decision: evidence, or Ben's button

STATES = ("open", "done", "slipped", "dropped")
LIVE = ("open", "slipped")
BASES = ("stated", "inferred", "none")
SOURCES = ("teams", "email", "meeting", "manual")
WHAT_MAX = 200
LINE_MAX = 200
FIELDS = ("id", "client", "project_id", "by_who", "to_who", "to_email", "what", "due_at", "due_basis",
          "due_text", "source", "source_ref", "source_at", "intent_id", "task_id", "follow_up_id", "state",
          "slipped_at", "due_history", "close_decision_id", "created_at", "closed_at")

DAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
        "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
        "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}


def _now() -> int:
    import time
    return int(time.time())


# ------------------------------------------------------------ dates


def _tz(tz: Any = None) -> tzinfo:
    """Ben's zone: a tzinfo, an IANA name, COORD_TZ, else this machine's."""
    if isinstance(tz, tzinfo):
        return tz
    name = tz or os.environ.get("COORD_TZ")
    if name:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    return datetime.now().astimezone().tzinfo or timezone.utc


def _epoch(v: Any) -> int | None:
    """An epoch int, or an ISO timestamp ('Z' allowed), or None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if s.isdigit():
        return int(s)
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=_tz())
    return int(d.timestamp())


def _at(day: date, z: tzinfo) -> int:
    return int(datetime.combine(day, dtime(DUE_HOUR), tzinfo=z).timestamp())


def _add_working(day: date, n: int) -> date:
    while n > 0:
        day += timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def working_days_after(ts: int, n: int, tz: Any = None) -> int:
    """`ts` moved forward `n` working days (weekends skipped), same local time."""
    z = _tz(tz)
    d = datetime.fromtimestamp(ts, z)
    day = _add_working(d.date(), n)
    return int(datetime.combine(day, d.time().replace(tzinfo=None), tzinfo=z).timestamp())


def resolve_due(text: str | None, said_at: Any = None, tz: Any = None) -> tuple[int, str]:
    """(due_at, due_basis) for the words used, against when they were said.

    ISO dates, today, tomorrow, "end of week" (Friday), "next week" (the
    following Friday) and day names (the next such day: a Friday said on a
    Friday is next Friday), all at DUE_HOUR local. No date words: inferred,
    INFERRED_WORKING_DAYS working days later, weekends skipped."""
    z = _tz(tz)
    said = datetime.fromtimestamp(_epoch(said_at) or _now(), z).date()
    s = (text or "").strip().lower()
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", s)
    if m:
        return _at(date.fromisoformat(m.group(1)), z), "stated"
    wd = said.weekday()
    friday = said + timedelta(days=4 - wd)  # this week's (Mon-Sun) Friday
    if re.search(r"\bnext week\b", s):
        return _at(friday + timedelta(days=7), z), "stated"
    if re.search(r"\b(end of (the )?week|eow|this week)\b", s):
        return _at(said + timedelta(days=(4 - wd) % 7), z), "stated"
    if re.search(r"\b(today|tonight|eod|end of (the )?day)\b", s):
        return _at(said, z), "stated"
    if re.search(r"\btomorrow\b", s):
        return _at(said + timedelta(days=1), z), "stated"
    for word in re.findall(r"[a-z]+", s):
        if word in DAYS:
            ahead = (DAYS[word] - wd) % 7 or 7
            return _at(said + timedelta(days=ahead), z), "stated"
    return _at(_add_working(said, INFERRED_WORKING_DAYS), z), "inferred"


# ------------------------------------------------------------ rows


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = {k: r[k] for k in FIELDS}
    d["due_history"] = json.loads(d["due_history"] or "[]")
    return d


def _text(name: str, v: str | None, cap: int) -> str | None:
    if v is None:
        return None
    v = v.strip()
    if len(v) > cap:
        raise ValueError(f"{name} is {len(v)} chars; cap is {cap}")
    return v or None


def get(conn: sqlite3.Connection, cid: str) -> dict[str, Any]:
    ensure_commitments(conn)
    r = conn.execute("SELECT * FROM commitments WHERE id=?", (cid,)).fetchone()
    if r is None:
        raise ValueError(f"no commitment {cid!r}")
    return _row(r)


def _check_links(conn: sqlite3.Connection, task_id: str | None, follow_up_id: str | None) -> None:
    if task_id and conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is None:
        raise ValueError(f"no task {task_id!r}")
    if follow_up_id and conn.execute("SELECT 1 FROM follow_ups WHERE id=?", (follow_up_id,)).fetchone() is None:
        raise ValueError(f"no loose end {follow_up_id!r}")


def _project_for(conn: sqlite3.Connection, task_id: str | None, follow_up_id: str | None) -> str | None:
    from .projects import project_of
    if not task_id and follow_up_id:
        r = conn.execute("SELECT task_id FROM follow_ups WHERE id=?", (follow_up_id,)).fetchone()
        task_id = r["task_id"] if r else None
    return project_of(conn, task_id) if task_id else None


def _decision(conn: sqlite3.Connection, decision_id: str | None) -> str | None:
    if decision_id and conn.execute("SELECT 1 FROM decisions WHERE id=?", (decision_id,)).fetchone() is None:
        raise ValueError(f"no decision {decision_id!r}")
    return decision_id or None


# ------------------------------------------------------------ writes


def add(conn: sqlite3.Connection, client: str, to_who: str, what: str, due: str | None = None,
        said_at: Any = None, *, no_date: bool = False, to_email: str | None = None, source: str = "manual",
        source_ref: str | None = None, intent_id: str | None = None, task_id: str | None = None,
        follow_up_id: str | None = None, project_id: str | None = None, by_who: str = DEFAULT_BY,
        tz: Any = None, now_ts: int | None = None) -> dict[str, Any]:
    """A new open commitment. `due` is the words used (or an ISO date);
    `no_date` marks a standing promise that never slips. Refuses a second
    live commitment with the same client, recipient and what: re-date that
    one instead (set)."""
    ensure_commitments(conn)
    client, to_who = (client or "").strip() or "internal", _text("to", to_who, LINE_MAX)
    what = _text("what", what, WHAT_MAX)
    if not to_who or not what:
        raise ValueError("to and what are required")
    if source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    dup = conn.execute("""SELECT id FROM commitments WHERE client=? AND to_who=? COLLATE NOCASE
                          AND what=? COLLATE NOCASE AND state IN ('open','slipped')""",
                       (client, to_who, what)).fetchone()
    if dup:
        raise ValueError(f"commitment {dup['id']} already holds this promise; re-date it with set")
    _check_links(conn, task_id, follow_up_id)
    if intent_id and conn.execute("SELECT 1 FROM intents WHERE id=?", (intent_id,)).fetchone() is None:
        raise ValueError(f"no intent {intent_id!r}")
    said = _epoch(said_at)
    due_at, basis = (None, "none") if no_date else resolve_due(due, said, tz)
    project_id = project_id or _project_for(conn, task_id, follow_up_id)
    cid = uuid.uuid4().hex[:12]
    conn.execute(
        """INSERT INTO commitments(id, client, project_id, by_who, to_who, to_email, what, due_at, due_basis,
               due_text, source, source_ref, source_at, intent_id, task_id, follow_up_id, state, due_history,
               created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open','[]',?)""",
        (cid, client, project_id, (by_who or DEFAULT_BY).strip(), to_who, _text("to_email", to_email, LINE_MAX),
         what, due_at, basis, _text("due_text", due, LINE_MAX), source, _text("ref", source_ref, 1000), said,
         intent_id, task_id, follow_up_id, now_ts or _now()))
    return get(conn, cid)


def set_(conn: sqlite3.Connection, cid: str, *, due: str | None = None, said_at: Any = None,
         decision_id: str | None = None, to_who: str | None = None, to_email: str | None = None,
         what: str | None = None, tz: Any = None, now_ts: int | None = None) -> dict[str, Any]:
    """Change the recipient, the what, or the date. A new date moves the old
    one onto due_history with the decision that moved it, and returns a
    slipped commitment to open. A closed one cannot be re-dated."""
    cur = get(conn, cid)
    sets: dict[str, Any] = {}
    if to_who is not None:
        sets["to_who"] = _text("to", to_who, LINE_MAX)
        if not sets["to_who"]:
            raise ValueError("to cannot be blank")
    if to_email is not None:
        sets["to_email"] = _text("to_email", to_email, LINE_MAX)
    if what is not None:
        sets["what"] = _text("what", what, WHAT_MAX)
        if not sets["what"]:
            raise ValueError("what cannot be blank")
    if due is not None:
        if cur["state"] in CLOSE_STATES:
            raise ValueError(f"commitment {cid} is {cur['state']}; add a new one instead")
        t = now_ts or _now()
        due_at, basis = resolve_due(due, said_at if said_at is not None else t, tz)
        hist = cur["due_history"] + [{"due_at": cur["due_at"], "due_basis": cur["due_basis"],
                                      "due_text": cur["due_text"], "decision_id": _decision(conn, decision_id),
                                      "at": t}]
        sets.update(due_at=due_at, due_basis=basis, due_text=_text("due_text", due, LINE_MAX),
                    due_history=json.dumps(hist), state="open")
    if not sets:
        return cur
    conn.execute(f"UPDATE commitments SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), cid))
    return get(conn, cid)


set = set_  # noqa: A001 -- the design's name; set_ keeps the builtin usable here


def link(conn: sqlite3.Connection, cid: str, task_id: str | None = None,
         follow_up_id: str | None = None) -> dict[str, Any]:
    """Point the commitment at the work that covers it. Pointers only: the
    task finishing never closes it."""
    cur = get(conn, cid)
    if not task_id and not follow_up_id:
        raise ValueError("give a task, a loose end, or both")
    _check_links(conn, task_id, follow_up_id)
    sets = {k: v for k, v in (("task_id", task_id), ("follow_up_id", follow_up_id)) if v}
    if not cur["project_id"]:
        pid = _project_for(conn, task_id or cur["task_id"], follow_up_id or cur["follow_up_id"])
        if pid:
            sets["project_id"] = pid
    conn.execute(f"UPDATE commitments SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), cid))
    return get(conn, cid)


def close(conn: sqlite3.Connection, cid: str, state: str, decision_id: str,
          now_ts: int | None = None) -> dict[str, Any]:
    """Done (kept) or dropped, by a decision. slipped_at stays, so an on-time
    rate can be read later."""
    cur = get(conn, cid)
    if state not in CLOSE_STATES:
        raise ValueError(f"close to one of {', '.join(CLOSE_STATES)}")
    if not decision_id:
        raise ValueError("a commitment closes only by a decision: give its id")
    _decision(conn, decision_id)
    if cur["state"] in CLOSE_STATES:
        raise ValueError(f"commitment {cid} is already {cur['state']}")
    conn.execute("UPDATE commitments SET state=?, close_decision_id=?, closed_at=? WHERE id=?",
                 (state, decision_id, now_ts or _now(), cid))
    return get(conn, cid)


def sweep(conn: sqlite3.Connection, now: int | None = None) -> list[str]:
    """Open commitments past due become slipped, slipped_at stamped the
    first time only. Nothing else changes and nothing is sent."""
    ensure_commitments(conn)
    t = now or _now()
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM commitments WHERE state='open' AND due_at IS NOT NULL AND due_at < ?", (t,))]
    if ids:
        conn.execute(f"UPDATE commitments SET state='slipped', slipped_at=COALESCE(slipped_at, ?) "
                     f"WHERE id IN ({','.join('?' * len(ids))})", (t, *ids))
    return ids


# ------------------------------------------------------------ reads


def list_commitments(conn: sqlite3.Connection, client: str | None = None,
                     state: str | None = None) -> list[dict[str, Any]]:
    """Live (open, slipped) first, each by due date, undated last. `state`
    is one state or 'live'."""
    ensure_commitments(conn)
    where, args = [], []
    if client:
        where.append("client=?")
        args.append(client)
    if state == "live":
        where.append("state IN ('open','slipped')")
    elif state:
        if state not in STATES:
            raise ValueError(f"state must be one of {', '.join(STATES)} or live")
        where.append("state=?")
        args.append(state)
    sql = "SELECT * FROM commitments" + (" WHERE " + " AND ".join(where) if where else "")
    sql += (" ORDER BY state NOT IN ('open','slipped'), due_at IS NULL, due_at, created_at")
    return [_row(r) for r in conn.execute(sql, args)]


def due(conn: sqlite3.Connection, now: int | None = None, tz: Any = None) -> list[dict[str, Any]]:
    """The minder's view: live dated commitments overdue or due within
    COMING_DUE_WORKING_DAYS, soonest first, each with `overdue`."""
    ensure_commitments(conn)
    t = now or _now()
    horizon = working_days_after(t, COMING_DUE_WORKING_DAYS, tz)
    out = []
    for r in conn.execute("""SELECT * FROM commitments WHERE state IN ('open','slipped') AND due_at IS NOT NULL
                             AND due_at <= ? ORDER BY due_at""", (horizon,)):
        d = _row(r)
        d["overdue"] = d["due_at"] < t
        out.append(d)
    return out


def _spoken_since(conn: sqlite3.Connection, c: dict[str, Any], since: int) -> bool:
    """A decision since `since` on the linked task, or naming the commitment."""
    if c["task_id"] and conn.execute("SELECT 1 FROM decisions WHERE task_id=? AND created_at >= ?",
                                     (c["task_id"], since)).fetchone():
        return True
    return conn.execute("SELECT 1 FROM decisions WHERE created_at >= ? AND (statement LIKE ? OR because LIKE ?)",
                        (since, f"%{c['id']}%", f"%{c['id']}%")).fetchone() is not None


def minder_owed(conn: sqlite3.Connection, now: int, tz: Any = None) -> list[str]:
    """Commitment ids master owes a look at, oldest due first:

    coming due within COMING_DUE_WORKING_DAYS while the work is not done (or
    nothing is linked); slipped with no decision since; open UNTRACKED_MINUTES
    with no task and no loose end. A decision on the linked task, or one
    naming the commitment, quiets each. Undated ones never show."""
    ensure_commitments(conn)
    horizon = working_days_after(now, COMING_DUE_WORKING_DAYS, tz)
    out: list[str] = []
    rows = [_row(r) for r in conn.execute(
        "SELECT * FROM commitments WHERE state IN ('open','slipped') AND due_basis != 'none' "
        "ORDER BY due_at, created_at")]
    for c in rows:
        if c["state"] == "slipped" or (c["due_at"] is not None and c["due_at"] < now):  # swept or not yet
            if not _spoken_since(conn, c, c["slipped_at"] or c["due_at"]):
                out.append(c["id"])
            continue
        if c["due_at"] is not None and now <= c["due_at"] <= horizon:
            work_done = False
            if c["task_id"]:
                r = conn.execute("SELECT state FROM tasks WHERE id=?", (c["task_id"],)).fetchone()
                work_done = bool(r and r["state"] == "done")
            elif c["follow_up_id"]:
                r = conn.execute("SELECT state FROM follow_ups WHERE id=?", (c["follow_up_id"],)).fetchone()
                work_done = bool(r and r["state"] != "open")
            # Coming due starts COMING_DUE_WORKING_DAYS before due_at; a decision since then quiets it.
            start = c["due_at"] - (horizon - now)
            if not work_done and not _spoken_since(conn, c, min(start, now)):
                out.append(c["id"])
            continue
        if (not c["task_id"] and not c["follow_up_id"] and c["created_at"] <= now - UNTRACKED_MINUTES * 60
                and not _spoken_since(conn, c, c["created_at"])):
            out.append(c["id"])
    return out


def due_map(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """task id -> {due_at, due_basis, commitment_id} of the earliest live
    dated commitment over it: linked to the task or a loose end of it, to
    an ancestor (parent chain, or the code a check-in checks in), or to its
    project (the commitment's project, or a task under the same project)."""
    ensure_commitments(conn)
    coms = conn.execute("""SELECT c.id, c.due_at, c.due_basis, c.task_id, c.project_id, f.task_id fu_task
                           FROM commitments c LEFT JOIN follow_ups f ON f.id = c.follow_up_id
                           WHERE c.state IN ('open','slipped') AND c.due_at IS NOT NULL""").fetchall()
    if not coms:
        return {}
    from .projects import _resolve_all, _up
    rows = {r["id"]: r for r in conn.execute("SELECT id, parent_id, spec, project_id FROM tasks")}
    proj = _resolve_all(rows)
    kids: dict[str, list[str]] = {}
    for tid, r in rows.items():
        up = _up(r)
        if up:
            kids.setdefault(up, []).append(tid)
    by_proj: dict[str, list[str]] = {}
    for tid, p in proj.items():
        if p:
            by_proj.setdefault(p, []).append(tid)
    out: dict[str, dict[str, Any]] = {}
    for c in coms:
        anchors = {a for a in (c["task_id"], c["fu_task"]) if a and a in rows}
        targets: set[str] = builtins.set()
        stack = list(anchors)
        while stack:
            t = stack.pop()
            if t not in targets:
                targets.add(t)
                stack.extend(kids.get(t, []))
        for p in {c["project_id"], *(proj.get(a) for a in anchors)} - {None}:
            targets.update(by_proj.get(p, []))
        for t in targets:
            if t not in out or c["due_at"] < out[t]["due_at"]:
                out[t] = {"due_at": c["due_at"], "due_basis": c["due_basis"], "commitment_id": c["id"]}
    return out


def effective_due(conn: sqlite3.Connection, task_id: str) -> int | None:
    """The earliest due_at of live commitments over this task (see due_map)."""
    d = due_map(conn).get(task_id)
    return d["due_at"] if d else None


def annotate(rows: Any, dues: dict[str, dict[str, Any]], depth: int = 4) -> Any:
    """Set `due` and `due_basis` on every dict with an `id` in `dues`, through
    nested lists and dicts, for the dashboard's due chips. Returns `rows`."""
    if depth < 0 or not dues:
        return rows
    if isinstance(rows, dict):
        tid = rows.get("id")
        if isinstance(tid, str) and tid in dues:
            rows["due"], rows["due_basis"] = dues[tid]["due_at"], dues[tid]["due_basis"]
        for v in rows.values():
            if isinstance(v, (list, dict)):
                annotate(v, dues, depth - 1)
    elif isinstance(rows, list):
        for v in rows:
            if isinstance(v, (list, dict)):
                annotate(v, dues, depth - 1)
    return rows


# ------------------------------------------------------------ Ben's buttons


def act(conn: sqlite3.Connection, action: str, cid: str, due_text: str | None = None,
        note: str = "") -> dict[str, Any]:
    """/commitments: Done, Drop or Re-date, in Ben's name. Each records a
    decision (on the linked task, so the minder hears it) and then applies it."""
    from . import store
    c = get(conn, cid)
    note = (note or "").strip()[:200] or None
    if action in ("done", "drop"):
        state = "done" if action == "done" else "dropped"
        verb = "kept" if state == "done" else "dropped"
        dec = store.record_decision(conn, f"Commitment {cid} {verb}, per Ben (/commitments): {c['what']}"[:200],
                                    note or "Ben's button on /commitments", task_id=c["task_id"])
        return {"ok": True, "action": action, "decision_id": dec["decision_id"],
                "commitment": close(conn, cid, state, dec["decision_id"])}
    if action == "redate":
        if not (due_text or "").strip():
            raise ValueError("type the new date")
        due_at, _ = resolve_due(due_text)
        when = datetime.fromtimestamp(due_at, _tz()).strftime("%a %Y-%m-%d")
        dec = store.record_decision(conn, f"Commitment {cid} re-dated to {when}, per Ben (/commitments)"[:200],
                                    note or "Ben's button on /commitments", task_id=c["task_id"])
        return {"ok": True, "action": action, "decision_id": dec["decision_id"],
                "commitment": set_(conn, cid, due=due_text.strip(), decision_id=dec["decision_id"])}
    raise ValueError(f"unknown action {action!r}")


# ------------------------------------------------------------ CLI


def _opts(args: list[str]) -> tuple[list[str], dict[str, str]]:
    flags = {"none"}
    pos, opt, i = [], {}, 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            key = a[2:].replace("-", "_")
            if key in flags:
                opt[key] = "1"
                i += 1
                continue
            if i + 1 >= len(args):
                raise ValueError(f"{a} needs a value")
            opt[key] = args[i + 1]
            i += 2
        else:
            pos.append(a)
            i += 1
    return pos, opt


def main(argv: list[str] | None = None) -> int:
    from .db import connect
    argv = sys.argv[1:] if argv is None else argv
    need = {"add": 1, "set": 1, "link": 1, "close": 2, "list": 0, "sweep": 0}
    if not argv or argv[0] not in need:
        print(__doc__.strip())
        return 2
    cmd = argv[0]
    conn = connect()
    try:
        pos, opt = _opts(argv[1:])
        if len(pos) < need[cmd]:
            print(__doc__.strip())
            return 2
        if cmd == "add":
            r: Any = add(conn, pos[0], opt.get("to", ""), opt.get("what", ""), opt.get("due"), opt.get("said"),
                         no_date="none" in opt, to_email=opt.get("to_email"), source=opt.get("source", "manual"),
                         source_ref=opt.get("ref"), intent_id=opt.get("intent"), task_id=opt.get("task"),
                         follow_up_id=opt.get("follow_up"), project_id=opt.get("project"))
        elif cmd == "set":
            r = set_(conn, pos[0], due=opt.get("due"), said_at=opt.get("said"), decision_id=opt.get("decision"),
                     to_who=opt.get("to"), to_email=opt.get("to_email"), what=opt.get("what"))
        elif cmd == "link":
            r = link(conn, pos[0], opt.get("task"), opt.get("follow_up"))
        elif cmd == "close":
            r = close(conn, pos[0], pos[1], opt.get("decision", ""))
        elif cmd == "list":
            r = list_commitments(conn, pos[0] if pos else None, opt.get("state"))
        else:
            r = {"slipped": sweep(conn)}
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(json.dumps(r, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
