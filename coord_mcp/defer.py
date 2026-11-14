"""Defer return dates: a deferred loose end comes back on its date.

Ben's ruling, 2026-11-14: defer means "pick up later", not "never". Before
this, a deferred loose end left the queue and came back only when someone
reopened it, so deferred items piled up (420 on the board that day).

- Every defer carries a return date (`coord_resolve_follow_up` return_on,
  /queue's Defer, the 14-day expiry). Without one it is DEFAULT_DAYS
  business days out (weekends skipped, Ben's zone: COORD_TZ, else this
  machine's).
- The dashboard loop (`reopen_due`) reopens every deferred item whose date
  has come, behind one decision per batch. Reopened, a Ben item is back on
  his queue; anyone else's is an open client loose end the minder lists.
- `python -m coord_mcp.defer backfill` dates the deferred items that have no
  return date, spread over the next BACKFILL_DAYS business days, Ben's
  first and oldest first. Dry run unless --apply.

The date lives in a side table (follow_up_returns), so the follow_ups table
is not rebuilt: additive only. A return row is history; the one that counts
is the row for a loose end that is still deferred by the same decision.
Switch `defer_return` (queue.SWITCHES, on) turns the reopen pass off.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from typing import Any

from .db import connect, now

DEFAULT_DAYS = 5
BACKFILL_DAYS = 10
MAX_DAYS = 366  # a return date further out than a year is a typo
DDL = """
CREATE TABLE IF NOT EXISTS follow_up_returns (
  follow_up_id TEXT PRIMARY KEY REFERENCES follow_ups(id),
  return_on    INTEGER NOT NULL,
  decision_id  TEXT,
  set_at       INTEGER NOT NULL,
  set_by       TEXT NOT NULL DEFAULT 'defer',
  returned_at  INTEGER
);
CREATE INDEX IF NOT EXISTS follow_up_returns_on ON follow_up_returns(return_on);
"""
PRESETS = {"tomorrow": 1, "3d": 3, "1w": 5, "2w": 10, "1m": 21}


def ensure(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='follow_up_returns'").fetchone() is None:
        conn.executescript(DDL)


def _tz():
    from .commitments import _tz as ctz
    return ctz()


def _start_of(d: date) -> int:
    return int(datetime.combine(d, datetime.min.time(), tzinfo=_tz()).timestamp())


def _add_business(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def business_days_out(n: int, now_ts: int | None = None) -> int:
    """Start of the local day `n` business days after today."""
    today = datetime.fromtimestamp(now() if now_ts is None else now_ts, _tz()).date()
    return _start_of(_add_business(today, max(1, int(n))))


def parse(value: Any, now_ts: int | None = None) -> int:
    """A return date as the start of its local day.

    Accepts an ISO date (2026-11-21), a preset (tomorrow, 3d, 1w, 2w, 1m),
    a count of business days ('5', '5bd'), an epoch, or None/'' for the
    default (DEFAULT_DAYS business days)."""
    t = now() if now_ts is None else now_ts
    if value is None or (isinstance(value, str) and not value.strip()):
        return business_days_out(DEFAULT_DAYS, t)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value > 10_000_000:  # an epoch
            d = datetime.fromtimestamp(int(value), _tz()).date()
            return _check(_start_of(d), t)
        return business_days_out(int(value), t)
    s = str(value).strip().lower()
    if s in PRESETS:
        return business_days_out(PRESETS[s], t)
    if m := re.fullmatch(r"(\d{1,3})\s*(bd|d|days?|business days?)?", s):
        return business_days_out(int(m.group(1)), t)
    try:
        d = date.fromisoformat(s[:10])
    except ValueError:
        raise ValueError(f"return_on {value!r}: give a date (YYYY-MM-DD), a preset "
                         f"({', '.join(PRESETS)}) or a number of business days") from None
    return _check(_start_of(d), t)


def _check(ts: int, t: int) -> int:
    today = _start_of(datetime.fromtimestamp(t, _tz()).date())
    if ts <= today:
        raise ValueError("return_on must be after today; to keep it on the board, do not defer it")
    if ts > t + MAX_DAYS * 86400:
        raise ValueError(f"return_on is more than {MAX_DAYS} days out")
    return ts


def day(ts: int | None) -> str:
    return datetime.fromtimestamp(ts, _tz()).date().isoformat() if ts else ""


def set_return(conn: sqlite3.Connection, follow_up_id: str, return_on: int, decision_id: str | None,
               set_by: str = "defer", t: int | None = None) -> None:
    ensure(conn)
    conn.execute("INSERT OR REPLACE INTO follow_up_returns(follow_up_id, return_on, decision_id, set_at, set_by, "
                 "returned_at) VALUES (?,?,?,?,?,NULL)",
                 (follow_up_id, int(return_on), decision_id, now() if t is None else t, set_by))


def returns_for(conn: sqlite3.Connection, ids: list[str] | None = None) -> dict[str, int]:
    """{follow_up_id: return_on} for loose ends still deferred by the decision that dated them."""
    ensure(conn)
    sql = ("SELECT r.follow_up_id, r.return_on FROM follow_up_returns r JOIN follow_ups f ON f.id = r.follow_up_id "
           "WHERE f.state='deferred' AND r.returned_at IS NULL "
           "AND (r.decision_id IS NULL OR r.decision_id = f.resolved_by)")
    args: list[Any] = []
    if ids is not None:
        if not ids:
            return {}
        sql += f" AND r.follow_up_id IN ({','.join('?' * len(ids))})"
        args = list(ids)
    return {r[0]: r[1] for r in conn.execute(sql, args)}


def _is_ben(who: Any) -> bool:
    return str(who or "").strip().casefold() == "ben"


def due(conn: sqlite3.Connection, now_ts: int | None = None) -> list[dict[str, Any]]:
    """Deferred loose ends whose return date has come, oldest date first."""
    ensure(conn)
    t = now() if now_ts is None else now_ts
    rows = conn.execute(
        """SELECT f.id, f.who, f.what, f.client, f.task_id, f.resolved_by, r.return_on,
                  COALESCE(f.client, t.assigned_to) owner
           FROM follow_up_returns r JOIN follow_ups f ON f.id = r.follow_up_id
           LEFT JOIN tasks t ON t.id = f.task_id
           WHERE f.state='deferred' AND r.returned_at IS NULL AND r.return_on <= ?
             AND (r.decision_id IS NULL OR r.decision_id = f.resolved_by)
           ORDER BY r.return_on, f.created_at""", (t,)).fetchall()
    return [dict(r) for r in rows]


def reopen_due(conn: sqlite3.Connection, now_ts: int | None = None, apply: bool = True) -> dict[str, Any]:
    """Reopen every deferred item whose date has come, behind one decision."""
    from . import queue, store
    t = now() if now_ts is None else now_ts
    rows = due(conn, t)
    ben = [r for r in rows if _is_ben(r["who"])]
    by: dict[str, int] = {}
    for r in rows:
        k = "Ben" if _is_ben(r["who"]) else (r["who"] or "unassigned")
        by[k] = by.get(k, 0) + 1
    out: dict[str, Any] = {"ok": True, "applied": False, "count": len(rows), "ben": len(ben), "by_who": by,
                           "items": rows}
    if not apply or not rows or not queue.switch(conn, "defer_return"):
        return out
    split = ", ".join(f"{k} {n}" for k, n in sorted(by.items(), key=lambda kv: (kv[0] != "Ben", -kv[1])))
    for r in rows:
        store.resolve_follow_up(conn, r["id"], reopen=True)
        conn.execute("UPDATE follow_up_returns SET returned_at=? WHERE follow_up_id=?", (t, r["id"]))
    ids = [r["id"] for r in rows]
    dec = store.record_decision(
        conn, f"Return dates: reopened {len(rows)} loose end(s) whose return date came: {split}"[:200],
        ("defer return dates, Ben 2026-11-14: a deferred item comes back on its date; Ben's go back to his "
         "queue, the rest are client loose ends for master. Reopened: " + ", ".join(ids))[:2000])
    conn.commit()
    return {**out, "applied": True, "decision_id": dec["decision_id"]}


def plan_backfill(conn: sqlite3.Connection, now_ts: int | None = None,
                  days: int = BACKFILL_DAYS) -> list[dict[str, Any]]:
    """A return date for each deferred loose end that has none: spread over
    the next `days` business days, Ben's first and oldest first, so they do
    not all come back on one day."""
    ensure(conn)
    t = now() if now_ts is None else now_ts
    dated = set(returns_for(conn))
    rows = [dict(r) for r in conn.execute(
        "SELECT id, who, what, client, created_at, resolved_by FROM follow_ups WHERE state='deferred'")
        if r["id"] not in dated]
    rows.sort(key=lambda r: (not _is_ben(r["who"]), r["created_at"], r["id"]))
    n = len(rows)
    for i, r in enumerate(rows):
        k = 1 + (i * days) // n if n else 1
        r["return_on"] = business_days_out(k, t)
    return rows


def backfill(conn: sqlite3.Connection, apply: bool = False, now_ts: int | None = None,
             days: int = BACKFILL_DAYS) -> dict[str, Any]:
    from . import store
    t = now() if now_ts is None else now_ts
    rows = plan_backfill(conn, t, days)
    per: dict[str, dict[str, int]] = {}
    for r in rows:
        d = per.setdefault(day(r["return_on"]), {"ben": 0, "other": 0})
        d["ben" if _is_ben(r["who"]) else "other"] += 1
    out: dict[str, Any] = {"ok": True, "applied": False, "count": len(rows),
                           "ben": sum(1 for r in rows if _is_ben(r["who"])), "per_day": per, "items": rows}
    if not apply or not rows:
        return out
    dec = store.record_decision(
        conn, f"Return dates backfill: dated {len(rows)} loose end(s) over the next {days} business days",
        "defer return dates, Ben 2026-11-14: existing deferred items return spread out, Ben's first and oldest "
        "first; python -m coord_mcp.defer backfill")
    for r in rows:
        # Keyed to the decision that deferred it, so a later re-defer replaces it.
        set_return(conn, r["id"], r["return_on"], r["resolved_by"], set_by=f"backfill {dec['decision_id']}", t=t)
    conn.commit()
    return {**out, "applied": True, "decision_id": dec["decision_id"]}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m coord_mcp.defer", description="Defer return dates.")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill", help="date deferred items that have no return date (dry run unless --apply)")
    b.add_argument("--apply", action="store_true")
    b.add_argument("--days", type=int, default=BACKFILL_DAYS)
    sub.add_parser("due", help="dry run: deferred items whose return date has come")
    sub.add_parser("list", help="every deferred item with its return date")
    a = p.parse_args(argv)
    conn = connect()
    try:
        if a.cmd == "backfill":
            out = backfill(conn, apply=a.apply, days=a.days)
            for d, n in sorted(out["per_day"].items()):
                print(f"{d}  Ben {n['ben']:>3}  others {n['other']:>3}  total {n['ben'] + n['other']:>3}")
            print(f"{out['count']} deferred item(s) without a return date ({out['ben']} Ben's): "
                  + (f"dated, decision {out['decision_id']}" if out["applied"] else "dry run; --apply to date them"))
            return 0
        if a.cmd == "due":
            out = reopen_due(conn, apply=False)
            for r in out["items"]:
                print(f"{r['id']}  {day(r['return_on'])}  {(r['who'] or ''):<9} {r['what'][:90]}")
            print(f"{out['count']} would reopen now (dry run)")
            return 0
        ret = returns_for(conn)
        for r in conn.execute("SELECT id, who, what FROM follow_ups WHERE state='deferred' ORDER BY created_at"):
            print(f"{r['id']}  {day(ret.get(r['id'])) or 'no date':<10}  {(r['who'] or ''):<9} {r['what'][:90]}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
