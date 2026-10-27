"""The review gate on CVC code and client emails (Ben, 2026-10-27).

Both are client deliverables but keep their own kinds, `code_change` and
`draft_reply`: their results carry what the Outlook flow and the check-in
run on. Their review is the same `dod_review` a deliverable gets, created
with `parent_id` = the task it judges. The board holds the gate at the two
points it controls:

- Draft in Outlook (`outlook.draft`) is refused for a gated draft_reply
  until its newest dod_review approves it and the body file is unchanged.
- A check-in is its own `code_change` whose spec names `checkin_of`, and
  `coord_create_task` refuses it until the code it checks in is approved
  the same way. The code itself is a `code_change` whose spec says
  `"cvc": true`: built and dev-compiled, never checked in.

The gate starts when Ben approves both checklists (`dod/cvc-code.md`,
`dod/client-email.md`). Tasks created before then keep the old route, so
nothing in flight is stranded:

    python -m coord_mcp.gate start <decision_id>   # the decision recording his approval
    python -m coord_mcp.gate status
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .db import now

SINCE_KEY = "review_gate_since"


def since(conn: sqlite3.Connection) -> int | None:
    """When the gate started, or None while it has not."""
    r = conn.execute("SELECT value FROM meta WHERE key=?", (SINCE_KEY,)).fetchone()
    return int(r["value"]) if r else None


def start(conn: sqlite3.Connection, decision_id: str) -> dict[str, Any]:
    """Start the gate from now. Needs the decision recording Ben's approval
    of the checklists, so the start is on the board with its reason. A
    second start keeps the first time: moving it would strand reviews."""
    d = conn.execute("SELECT id FROM decisions WHERE id=?", (decision_id,)).fetchone()
    if d is None:
        raise ValueError(f"no decision {decision_id!r}; record Ben's checklist approval first")
    t = since(conn)
    if t is not None:
        return {"ok": True, "since": t, "already": True}
    t = now()
    conn.execute("INSERT INTO meta(key, value) VALUES (?,?)", (SINCE_KEY, str(t)))
    return {"ok": True, "since": t, "already": False}


def _row(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT id, kind, spec, state, assigned_to, result, created_at FROM tasks WHERE id=?",
                        (task_id,)).fetchone()


def is_gated(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """A draft_reply, or a code_change bound for CVC, created since the start."""
    t = since(conn)
    if t is None or row["created_at"] < t:
        return False
    if row["kind"] == "draft_reply":
        return True
    return row["kind"] == "code_change" and json.loads(row["spec"] or "{}").get("cvc") is True


def _files(row: sqlite3.Row) -> list[Path]:
    """What the review judged, on disk: the body, or the files the code changed."""
    from .store import DOCS_ROOT, _resolve
    res = json.loads(row["result"] or "{}")
    if row["kind"] == "draft_reply":
        return [Path(res["body_path"]).expanduser()] if res.get("body_path") else []
    return [_resolve(f["path"], row["assigned_to"], DOCS_ROOT)
            for f in res.get("files_changed") or [] if isinstance(f, dict) and f.get("path")]


def stage(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    """Where a gated task stands: review, changes, escalated, stale or
    approved (store.STAGES labels them). None when the task is not gated."""
    from .store import latest_review
    row = _row(conn, task_id)
    if row is None or not is_gated(conn, row):
        return None
    rev = latest_review(conn, task_id)
    if rev is None:
        return {"stage": "review"}
    s = {"approve": "approved", "request_changes": "changes", "escalate": "escalated"}[rev["verdict"]]
    if s == "approved" and any(p.is_file() and int(p.stat().st_mtime) > rev["completed_at"]
                               for p in _files(row)):
        s = "stale"
    return {"stage": s, "review_id": rev["id"]}


_WHY = {"review": "has no dod_review yet", "changes": "its newest dod_review requests changes",
        "escalated": "its newest dod_review escalated it to Ben",
        "stale": "it changed after its dod_review approved it; review it again"}


def require_approved(conn: sqlite3.Connection, task_id: str, what: str) -> None:
    """Raise unless the task is ungated or approved and unchanged."""
    s = stage(conn, task_id)
    if s is not None and s["stage"] != "approved":
        raise ValueError(f"{what} refused: task {task_id} is a client deliverable and {_WHY[s['stage']]}. "
                         f"Create a dod_review with parent_id={task_id}.")


def check_checkin(conn: sqlite3.Connection, kind: str, spec: dict[str, Any]) -> None:
    """coord_create_task's check on a check-in: its code must be approved."""
    ref = spec.get("checkin_of")
    if ref is None:
        return
    if kind != "code_change":
        raise ValueError("checkin_of belongs on a code_change spec")
    row = _row(conn, str(ref))
    if row is None:
        raise ValueError(f"checkin_of names no task {ref!r}")
    if row["kind"] != "code_change" or row["state"] != "done":
        raise ValueError(f"checkin_of {ref} must be a finished code_change")
    require_approved(conn, row["id"], "Check-in")


def main(argv: list[str] | None = None) -> int:
    from .db import connect
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ("start", "status") or (argv[0] == "start" and len(argv) < 2):
        print(__doc__.strip())
        return 2
    conn = connect()
    try:
        if argv[0] == "start":
            r = start(conn, argv[1])
            conn.commit()
            print(("already started " if r["already"] else "started ") + str(r["since"]))
        else:
            t = since(conn)
            print(f"started {t}" if t else "not started")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
