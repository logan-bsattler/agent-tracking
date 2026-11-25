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
"""

from __future__ import annotations

import argparse
import json
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
    a = p.parse_args(argv)
    conn = connect()
    try:
        if a.cmd == "set":
            out = set_priority(conn, a.task_id, a.level, a.because)
            print(f"{out['task_id']} priority {out['priority']} (decision {out['decision_id']})")
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
