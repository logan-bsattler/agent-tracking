"""Billable hours: daily minutes per client per task, drawn from what the board
and the usage ingest already record, plus the operator's own logged time.

Agent time is measured, not reported. Each client works in its own folder, so
a request's cwd names the client; the gaps between a client's consecutive
requests are its active time, each capped at IDLE_GAP. A task owns the time
between its pickup and its next park or close; client time outside every task
lands on an `unassigned` row for that client.

These numbers are a draft for the operator to edit before billing, never the
bill. SharePoint stays the system of record, and the push to it ships off.

Minutes are stored and summed unrounded. Rounding to 0.25h happens only at
display (`quarter_hours`).
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta
from typing import Any

from .db import now

# A longer gap between two requests counts as this much, not zero, so the
# work a request did before the session went quiet is not lost.
IDLE_GAP = 300

# Client keys, as the board's assigned_to spells them. Distinct assignees on
# the board are added at run time, so a newly wired client counts without an
# edit here. Folders that are not clients (Posey, the master) never match.
CLIENTS = ("Moog", "Royal", "Cascade", "LNK", "TS Tech", "PBE", "MAG", "Furlani", "Yamamoto", "GHSP")

PUSH_FLAG = "hours_push_enabled"
UNASSIGNED = "unassigned"
ROW_FIELDS = ("day", "client", "task_id", "title", "pipeline_task_id", "agent_min", "operator_min",
              "meeting_min", "total_min", "estimated")

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ------------------------------------------------------------ helpers


def parse_day(s: str) -> date:
    if not isinstance(s, str) or not _DAY.match(s):
        raise ValueError(f"day '{s}' is not YYYY-MM-DD")
    return date.fromisoformat(s)


def _midnight(d: date) -> int:
    """Local midnight at the start of d, as epoch seconds."""
    return int(datetime.combine(d, dtime()).timestamp())


def _day_of(ts: float) -> date:
    return datetime.fromtimestamp(ts).date()


def quarter_hours(minutes: float) -> float:
    """Display rounding: nearest 0.25h. Never applied to stored values."""
    return round(minutes / 15) / 4


def _clients(conn: sqlite3.Connection) -> dict[str, str]:
    """casefolded folder name -> canonical client key."""
    keys = {c.casefold(): c for c in CLIENTS}
    for r in conn.execute("SELECT DISTINCT assigned_to FROM tasks WHERE assigned_to IS NOT NULL"):
        keys.setdefault(r[0].casefold(), r[0])
    return keys


def client_of(cwd: str | None, keys: dict[str, str]) -> str | None:
    """The client a working directory belongs to: the folder right under
    Documents (or a client repo under C:/development), when it is a client key.
    Subfolders count for their client."""
    if not cwd:
        return None
    parts = [p for p in re.split(r"[\\/]+", cwd) if p]
    for i, p in enumerate(parts[:-1]):
        if p.casefold() in ("documents", "development"):
            return keys.get(parts[i + 1].casefold())
    return None


# ---------------------------------------------------------- active time


def active_segments(timestamps: list[int], until: int | None = None) -> list[tuple[int, int]]:
    """[start, end) spans of active time from a sorted list of request times.

    Each request owns the time up to the next one, capped at IDLE_GAP. The last
    request owns IDLE_GAP, cut short at `until` (now, for a live session).
    Overlapping spans cannot occur: each ends at or before the next start.
    """
    segs = []
    for i, t in enumerate(timestamps):
        nxt = timestamps[i + 1] if i + 1 < len(timestamps) else None
        end = t + (min(nxt - t, IDLE_GAP) if nxt is not None else IDLE_GAP)
        if until is not None:
            end = min(end, max(until, t))
        if end > t:
            segs.append((t, end))
    return segs


# ---------------------------------------------------------- attribution


def task_intervals(conn: sqlite3.Connection, client: str, lo: int, hi: int, t_now: int
                   ) -> list[dict[str, Any]]:
    """Every span in which `client` held a task, overlapping [lo, hi).

    From task_events: a pickup opens a span, the next park, completion or
    failure closes it; a re-read while open does not reopen it. A span still
    open runs to now. Tasks from before task_events existed fall back to
    created_at -> completed_at and are flagged estimated; so is a close with no
    recorded pickup (a task picked up before the events table, closed after).
    """
    out: list[dict[str, Any]] = []
    rows = conn.execute(
        """SELECT id, title, pipeline_task_id, created_at, completed_at, picked_up_at
           FROM tasks WHERE assigned_to = ? COLLATE NOCASE""", (client,)).fetchall()
    for t in rows:
        evs = conn.execute("SELECT event, ts FROM task_events WHERE task_id=? ORDER BY ts, rowid",
                           (t["id"],)).fetchall()
        spans: list[tuple[int, int, bool]] = []
        if not evs:
            if t["completed_at"]:
                spans.append((t["created_at"], t["completed_at"], True))
        else:
            start: int | None = None
            last_close: int | None = None
            for e in evs:
                if e["event"] == "picked_up":
                    if start is None:
                        start = e["ts"]
                elif start is not None:
                    spans.append((start, e["ts"], False))
                    start, last_close = None, e["ts"]
                else:
                    # Closed with no pickup on record: the pickup predates the
                    # events table. Best guess is the last pickup, else creation.
                    guess = t["picked_up_at"] if t["picked_up_at"] and t["picked_up_at"] < e["ts"] else t["created_at"]
                    if last_close is not None:
                        guess = max(guess, last_close)
                    spans.append((guess, e["ts"], True))
                    last_close = e["ts"]
            if start is not None:
                spans.append((start, t_now, False))
        for a, b, est in spans:
            if b > a and b > lo and a < hi:
                out.append({"task_id": t["id"], "title": t["title"], "pipeline_task_id": t["pipeline_task_id"],
                            "start": a, "end": b, "estimated": est})
    return out


def _split(seg: tuple[int, int], cuts: list[int]) -> list[tuple[int, int]]:
    a, b = seg
    pts = [a] + sorted(c for c in set(cuts) if a < c < b) + [b]
    return list(zip(pts, pts[1:]))


def _midnights(a: int, b: int) -> list[int]:
    out, d = [], _day_of(a) + timedelta(days=1)
    while (m := _midnight(d)) < b:
        out.append(m)
        d += timedelta(days=1)
    return out


def agent_minutes(conn: sqlite3.Connection, start: date, end: date, client: str | None = None,
                  t_now: int | None = None) -> dict[tuple[str, str, str], dict[str, Any]]:
    """(day, client, task_id|'unassigned') -> {minutes, estimated, title, pipeline_task_id}."""
    t_now = t_now or now()
    lo, hi = _midnight(start), _midnight(end + timedelta(days=1))
    keys = _clients(conn)
    by_client: dict[str, list[int]] = defaultdict(list)
    for r in conn.execute("SELECT ts, cwd FROM requests WHERE ts >= ? AND ts < ? ORDER BY ts",
                          (lo - IDLE_GAP, hi)):
        c = client_of(r["cwd"], keys)
        if c and (client is None or c.casefold() == client.casefold()):
            by_client[c].append(r["ts"])
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for c, stamps in by_client.items():
        ivs = task_intervals(conn, c, lo, hi, t_now)
        cuts = [x for iv in ivs for x in (iv["start"], iv["end"])]
        for seg in active_segments(stamps, until=t_now):
            a, b = max(seg[0], lo), min(seg[1], hi)
            if b <= a:
                continue
            for pa, pb in _split((a, b), cuts + _midnights(a, b)):
                mins = (pb - pa) / 60
                day = _day_of(pa).isoformat()
                cover = [iv for iv in ivs if iv["start"] <= pa and pb <= iv["end"]]
                if not cover:
                    k = (day, c, UNASSIGNED)
                    row = out.setdefault(k, {"minutes": 0.0, "estimated": False, "title": "",
                                             "pipeline_task_id": None})
                    row["minutes"] += mins
                    continue
                # Two tasks held at once on one client: split the time evenly.
                share = mins / len(cover)
                for iv in cover:
                    k = (day, c, iv["task_id"])
                    row = out.setdefault(k, {"minutes": 0.0, "estimated": False, "title": iv["title"],
                                             "pipeline_task_id": iv["pipeline_task_id"]})
                    row["minutes"] += share
                    row["estimated"] = row["estimated"] or iv["estimated"]
    return out


# -------------------------------------------------------- operator time


def log_time(conn: sqlite3.Connection, minutes: float, day: str | None = None, task_id: str | None = None,
             client: str | None = None, note: str | None = None) -> dict[str, Any]:
    """Record the operator's own time. With a task, the client defaults to its assignee."""
    if not (0 < minutes <= 24 * 60):
        raise ValueError(f"minutes is {minutes}; must be above 0 and at most 1440")
    day = parse_day(day).isoformat() if day else date.today().isoformat()
    if note and len(note) > 200:
        raise ValueError(f"note is {len(note)} chars; cap is 200")
    if task_id:
        row = conn.execute("SELECT assigned_to FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown task '{task_id}'")
        if client and row["assigned_to"] and client.casefold() != row["assigned_to"].casefold():
            raise ValueError(f"task '{task_id}' belongs to {row['assigned_to']}, not {client}")
        client = client or row["assigned_to"]
    if not client:
        raise ValueError("give a client, or a task_id whose assignee is the client")
    client = _clients(conn).get(client.casefold(), client)
    eid = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO time_entries(id, task_id, client, day, minutes, note, created_at) VALUES (?,?,?,?,?,?,?)",
        (eid, task_id, client, day, float(minutes), note, now()))
    return {"entry_id": eid, "client": client, "day": day, "minutes": float(minutes), "task_id": task_id}


def operator_minutes(conn: sqlite3.Connection, start: date, end: date, client: str | None = None
                     ) -> dict[tuple[str, str, str], dict[str, Any]]:
    sql = """SELECT e.day, e.client, e.task_id, SUM(e.minutes) m, t.title, t.pipeline_task_id
             FROM time_entries e LEFT JOIN tasks t ON t.id = e.task_id
             WHERE e.day BETWEEN ? AND ?"""
    args: list[Any] = [start.isoformat(), end.isoformat()]
    if client:
        sql += " AND e.client = ? COLLATE NOCASE"
        args.append(client)
    out = {}
    for r in conn.execute(sql + " GROUP BY e.day, e.client, e.task_id", args):
        out[(r["day"], r["client"], r["task_id"] or UNASSIGNED)] = {
            "minutes": r["m"], "title": r["title"] or "", "pipeline_task_id": r["pipeline_task_id"]}
    return out


# --------------------------------------------------------------- output


def breakdown(conn: sqlite3.Connection, start: date | str, end: date | str, client: str | None = None,
              t_now: int | None = None) -> list[dict[str, Any]]:
    """One row per (day, client, task), agent, operator and meeting minutes side
    by side. Meetings are one `meetings` row per client per day: no task, so no
    pipeline item, so never in the push until that is decided."""
    from . import meetings
    start = parse_day(start) if isinstance(start, str) else start
    end = parse_day(end) if isinstance(end, str) else end
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    agent = agent_minutes(conn, start, end, client, t_now)
    op = operator_minutes(conn, start, end, client)
    mt = {(d, c, meetings.MEETINGS): m for (d, c), m in meetings.meeting_minutes(conn, start, end, client).items()}
    rows = []
    for k in sorted(set(agent) | set(op) | set(mt)):
        a, o = agent.get(k, {}), op.get(k, {})
        am, om, mm = a.get("minutes", 0.0), o.get("minutes", 0.0), mt.get(k, 0.0)
        rows.append({"day": k[0], "client": k[1], "task_id": k[2],
                     "title": a.get("title") or o.get("title") or "",
                     "pipeline_task_id": a.get("pipeline_task_id") or o.get("pipeline_task_id"),
                     "agent_min": am, "operator_min": om, "meeting_min": mm, "total_min": am + om + mm,
                     "estimated": bool(a.get("estimated"))})
    return rows


def rollup(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sum rows by (day, client, pipeline item). Rows with no item go under 'unassigned'."""
    acc: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in rows:
        k = (r["day"], r["client"], r["pipeline_task_id"] or UNASSIGNED)
        d = acc.setdefault(k, {"day": k[0], "client": k[1], "pipeline_task_id": k[2], "agent_min": 0.0,
                               "operator_min": 0.0, "meeting_min": 0.0, "total_min": 0.0, "estimated": False, "tasks": []})
        for f in ("agent_min", "operator_min", "meeting_min", "total_min"):
            d[f] += r[f]
        d["estimated"] = d["estimated"] or r["estimated"]
        if r["task_id"] != UNASSIGNED:
            d["tasks"].append(r["task_id"])
    return [acc[k] for k in sorted(acc)]


# ---------------------------------------------------------------- push


def push_enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (PUSH_FLAG,)).fetchone()
    return bool(row) and str(row["value"]).strip().lower() in ("1", "true", "on", "yes")


def push_payload(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per pipeline item per day: the hours and a note naming the board tasks.

    Rows with no pipeline item cannot be pushed and are left out; the page
    shows them so they can be linked first.
    """
    out = []
    for r in rollup(rows):
        if r["pipeline_task_id"] == UNASSIGNED or r["total_min"] <= 0:
            continue
        note = f"{quarter_hours(r['agent_min']):g}h agent, {quarter_hours(r['operator_min']):g}h operator"
        if r["tasks"]:
            note += "; board " + ", ".join(sorted(set(r["tasks"])))
        out.append({"pipeline_task_id": r["pipeline_task_id"], "date": r["day"],
                    "hours": quarter_hours(r["total_min"]), "note": note[:250]})
    return out


def push_to_pipeline(conn: sqlite3.Connection, rows: list[dict[str, Any]], dry_run: bool = True
                     ) -> dict[str, Any]:
    """Build what would be sent to SharePoint and return it. Never calls out.

    Gated twice: the `hours_push_enabled` meta key (absent = off) and dry_run.
    The send itself is not wired: turning the push on is a later decision
    (8f4ed8b96bf9), and it will be made alongside the send, not before it.
    """
    payload = push_payload(rows)
    enabled = push_enabled(conn)
    if not enabled or dry_run:
        return {"enabled": enabled, "dry_run": True, "sent": 0, "payload": payload,
                "note": "push disabled" if not enabled else "dry run: nothing sent"}
    raise NotImplementedError("the SharePoint send is not wired yet; the payload above is what it would send")
