"""Task priority: orders undated work (Ben, 2026-11-25).

Auto-dispatch sends Ben's direct asks first, then the earliest-due
commitment, then the oldest task, and Ben's queue sorts overdue, then
soonest due, then oldest. Undated work was told apart by age alone.
``spec.priority`` (high, normal, low) breaks that tie in both: among items
with the same due date (in practice, none), high goes before normal before
low, and age decides within a level. It never puts an undated item ahead of
a dated one. A Ben item takes its task's priority; one with no task is
normal. On /queue, client groups with no due item are ordered by their
best priority, then their oldest item.

A task with no priority takes the nearest one above it (parent chain and
the code a check-in checks in), so a review or revision keeps its root's
level. No priority anywhere means normal.

Set it on create (coord_create_task ``priority``) or later:
``python -m coord_mcp.priority set <task id> high`` records a decision.

Rule P2 (Ben, 2026-11-25; switch ``require_priority``): coord_create_task
refuses a root client task with no priority, as it refuses one with no
pipeline line. A child (parent_id), a check-in (checkin_of) and Ben's
direct asks are exempt: they inherit, or go first anyway. high: Ben said it
is urgent, or a client is waiting. low: nice-to-have or internal tooling.
Otherwise normal. Rule P1 has no code: a task has a date only when a
commitment or the request states one.

Rule P3 (Ben, 2026-11-25; switch ``estimate_high``): an estimate is high, so
sizings come back fast. coord_create_task sets priority high on a task that
is a sizing (spec deliverable_type "sizing", or a title naming a sizing,
estimate or ballpark), whatever priority it was given, and says so in its
warnings. Code changes are left alone ("fix the estimate calc" is not one).
``python -m coord_mcp.priority estimates`` lists open estimate tasks below
high; ``--yes`` raises them.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from typing import Any, Iterable

LEVELS = ("high", "normal", "low")
DEFAULT = "normal"
RANK = {lvl: i for i, lvl in enumerate(LEVELS)}


def check(spec: Any) -> None:
    """Refuse a spec whose priority is not one of LEVELS."""
    if isinstance(spec, dict) and "priority" in spec and spec["priority"] not in LEVELS:
        raise ValueError(f"spec.priority must be one of {', '.join(LEVELS)}, not {spec['priority']!r}")


REFUSAL = ("no priority: every root client task names one (rule P2, Ben 2026-11-25). priority \"high\" when Ben "
           "said it is urgent or a client is waiting, \"low\" for a nice-to-have or internal tooling, otherwise "
           "\"normal\". A child task (parent_id) or check-in inherits its chain's level.")


def _required(conn: sqlite3.Connection) -> bool:
    from . import queue
    return queue.switch(conn, "require_priority")


def check_create(conn: sqlite3.Connection, spec: Any, assigned_to: str | None, parent_id: str | None) -> None:
    """Refuse a root client task with no priority (switch require_priority)."""
    from .pipeline import NOT_CLIENTS
    s = spec if isinstance(spec, dict) else {}
    if s.get("priority") or parent_id or s.get("checkin_of") or s.get("from") == "ben-direct":
        return
    if not assigned_to or assigned_to in NOT_CLIENTS:
        return
    if _required(conn):
        raise ValueError(REFUSAL)


ESTIMATE = re.compile(r"\bsizings?\b|\bsized\b|\bestimat\w*|\bballpark\b", re.I)
ESTIMATE_SWITCH = "estimate_high"


def is_estimate(kind: str | None, title: str | None, spec: Any) -> bool:
    """A sizing or estimate task (rule P3). Code is never one."""
    if kind == "code_change":
        return False
    s = spec if isinstance(spec, dict) else {}
    return str(s.get("deliverable_type") or "").lower() == "sizing" or bool(ESTIMATE.search(title or ""))


def raise_estimate(conn: sqlite3.Connection, kind: str, title: str, spec: Any) -> tuple[Any, list[str]]:
    """(spec, warnings): an estimate task's spec with priority high (rule P3)."""
    from . import queue
    if not isinstance(spec, dict) or spec.get("priority") == "high" or not is_estimate(kind, title, spec):
        return spec, []
    if not queue.switch(conn, ESTIMATE_SWITCH):
        return spec, []
    was = spec.get("priority")
    note = ((f"priority set to high (was {was}): " if was else "priority set to high: ")
            + "estimates go high so they come back fast (rule P3, switch estimate_high)")
    return {**spec, "priority": "high"}, [note]


def open_estimates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Open estimate tasks whose effective priority is below high."""
    rows = conn.execute("SELECT id, kind, title, spec, assigned_to FROM tasks WHERE state='open' "
                        "ORDER BY created_at").fetchall()
    out = []
    for r in rows:
        try:
            spec = json.loads(r["spec"] or "{}")
        except ValueError:
            spec = {}
        if is_estimate(r["kind"], r["title"], spec) and (p := of(conn, r["id"])) != "high":
            out.append({"task_id": r["id"], "client": r["assigned_to"], "title": r["title"], "priority": p})
    return out


def _own(raw: Any) -> str | None:
    try:
        s = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return None
    p = s.get("priority") if isinstance(s, dict) else None
    return p if p in LEVELS else None


def of(conn: sqlite3.Connection, task_id: str) -> str:
    """The task's own priority, else the nearest ancestor's, else normal."""
    from .queue import lineage
    for tid in lineage(conn, task_id):
        r = conn.execute("SELECT spec FROM tasks WHERE id=?", (tid,)).fetchone()
        if r is not None and (p := _own(r["spec"])):
            return p
    return DEFAULT


def ranks(conn: sqlite3.Connection, task_ids: Iterable[str]) -> dict[str, int]:
    """{task id: rank} for sorting, 0 = high. Skips the walk when no task on
    the board sets a priority."""
    ids = list(task_ids)
    if conn.execute("SELECT 1 FROM tasks WHERE spec LIKE '%\"priority\"%' LIMIT 1").fetchone() is None:
        return {t: RANK[DEFAULT] for t in ids}
    return {t: RANK[of(conn, t)] for t in ids}


def set_priority(conn: sqlite3.Connection, task_id: str, level: str, because: str = "") -> dict[str, Any]:
    """Set spec.priority on a task and record the decision. A finished task
    counts too: its open Ben items and any child tasks take its level."""
    from . import store
    if level not in LEVELS:
        raise ValueError(f"priority must be one of {', '.join(LEVELS)}, not {level!r}")
    r = conn.execute("SELECT spec FROM tasks WHERE id=?", (task_id,)).fetchone()
    if r is None:
        raise ValueError(f"no task {task_id!r}")
    try:
        spec = json.loads(r["spec"] or "{}")
    except ValueError:
        spec = {}
    spec = spec if isinstance(spec, dict) else {}
    spec["priority"] = level
    conn.execute("UPDATE tasks SET spec=? WHERE id=?", (json.dumps(spec), task_id))
    dec = store.record_decision(conn, f"Priority of {task_id} set to {level}",
                                because or "orders it among undated work", task_id=task_id)
    conn.commit()
    return {"task_id": task_id, "priority": level, "decision_id": dec["decision_id"]}


def main(argv: list[str] | None = None) -> int:
    from .db import connect
    p = argparse.ArgumentParser(prog="python -m coord_mcp.priority", description="Task priority.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set", help="set an open task's priority (records a decision)")
    s.add_argument("task_id")
    s.add_argument("level", choices=LEVELS)
    s.add_argument("--because", default="")
    g = sub.add_parser("get", help="a task's effective priority")
    g.add_argument("task_id")
    e = sub.add_parser("estimates", help="open estimate tasks below high (rule P3); --yes raises them")
    e.add_argument("--yes", action="store_true")
    a = p.parse_args(argv)
    conn = connect()
    try:
        if a.cmd == "set":
            out = set_priority(conn, a.task_id, a.level, a.because)
            print(f"{out['task_id']} priority {out['priority']} (decision {out['decision_id']})")
        elif a.cmd == "estimates":
            rows = open_estimates(conn)
            for r in rows:
                line = f"{r['task_id']} {r['client'] or '-'} {r['priority']}: {r['title']}"
                if a.yes:
                    d = set_priority(conn, r["task_id"], "high", "estimates go high (rule P3)")
                    line += f" -> high (decision {d['decision_id']})"
                print(line)
            if not rows:
                print("no open estimate tasks below high")
            elif not a.yes:
                print(f"{len(rows)} to raise; rerun with --yes")
        else:
            print(of(conn, a.task_id))
        return 0
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
