"""Check-in auto-approval (Ben, 2026-11-15): the `checkin_ok` step of A3/B1.

A CVC check-in reaches the client's source control, so it waited on Ben's
"approve the check-in". When the board can show the code is ready, that
approval is mechanical, and this rule gives it on his behalf, behind a veto
window. It is a code step with Ben as the fallback (workflow defs
`"who": "code", "fallback": "ben"`): whenever the rule is off or declines,
the step is Ben's exactly as before.

Inactive until the review gate starts (`gate.since`): before then there are
no dod_reviews to rely on, and this does nothing at all. Behind the switch
`auto_checkin` (`python -m coord_mcp.queue switch`, default on); the window
is the setting `auto_checkin_hours` (business hours, default 4).

A finished CVC code_change (gated, not itself a check-in) is approved when
all three hold:

  1. its newest dod_review approves it (gate.stage == "approved"),
  2. its files are unchanged since that review (the same stage check
     coord_create_task runs on a `checkin_of` task: "stale" fails it),
  3. its result says the build/dev-compile is clean: the optional
     `compile` field of the code_change contract reads "clean" and, when
     `tests` is present, none failed. The contract had no machine-readable
     compile field, so this one was added (additive); a result without it
     is never auto-approved, and the summary prose is not parsed.

Then, per code task, once:

  - decision `Check-in auto-approved for <id> (review <rid>); veto by <time>`
    (because ends "auto rule; Ben may override"), closing Ben's open
    check-in approval loose ends on the code and its reviews;
  - a veto window: gate.check_checkin refuses a check-in of that code
    until it passes. A Ben veto (the Veto button on /board/task, or a
    decision on the code task that supersedes the approval or starts
    "Veto") cancels it, reopens his item (or opens one when there was
    none), and the check-in waits on his own answer again;
  - when the window passes and the code is still approved and unchanged,
    decision `Check-in released for <id>` and a client loose end "Check in
    <id> ..." that master tasks as the `checkin_of` code_change. If the
    code went stale or lost its approval meanwhile, the approval lapses
    and Ben's item comes back.

Never pushes or checks in: only the approval is automatic. The client agent
still does the check-in as its own code_change, which master creates and
dispatches.

    python -m coord_mcp.checkin_auto status     # rows and whether the rule is active
    python -m coord_mcp.checkin_auto run [--dry-run]
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime
from typing import Any

from .db import now

DDL = """
CREATE TABLE IF NOT EXISTS checkin_auto (
  task_id      TEXT PRIMARY KEY REFERENCES tasks(id),
  review_id    TEXT NOT NULL,
  decision_id  TEXT NOT NULL,
  approved_at  INTEGER NOT NULL,
  veto_by      INTEGER NOT NULL,
  state        TEXT NOT NULL CHECK (state IN ('window','released','vetoed','lapsed')),
  closed       TEXT NOT NULL DEFAULT '[]',
  ended_by     TEXT,
  ended_at     INTEGER
);
"""
APPROVED = "Check-in auto-approved for"
RELEASED = "Check-in released for"
WHY = "auto rule; Ben may override"
# Ben's check-in approval item, as master or a review words it.
_CHECKIN = re.compile(r"check.?in", re.I)
_VETO = re.compile(r"^\s*veto", re.I)


def ensure(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkin_auto'").fetchone() is None:
        conn.executescript(DDL)


def _have(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkin_auto'").fetchone() is not None


def active(conn: sqlite3.Connection) -> bool:
    """Gate started and switch on. Before the gate starts there are no
    reviews to rely on, so the rule never acts."""
    from . import gate, queue
    return gate.since(conn) is not None and queue.switch(conn, "auto_checkin")


def window_hours(conn: sqlite3.Connection) -> float:
    from . import queue
    return float(queue.setting(conn, "auto_checkin_hours"))


def row(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    if not _have(conn):
        return None
    r = conn.execute("SELECT * FROM checkin_auto WHERE task_id=?", (task_id,)).fetchone()
    return dict(r) if r else None


def compile_clean(result: dict[str, Any]) -> tuple[bool, str]:
    """(clean, why not) from the machine-readable fields only."""
    c = result.get("compile")
    if c is None:
        return False, "its result has no compile field (the build/dev-compile is not machine-readable)"
    if c != "clean":
        return False, f"its compile reads {c!r}, not 'clean'"
    tests = result.get("tests")
    if isinstance(tests, dict) and tests.get("failed"):
        return False, f"{tests['failed']} test(s) failed"
    return True, ""


def check(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    """{ok, why, review_id} for one code task: are all three conditions met?"""
    from . import gate
    r = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if r is None:
        return {"ok": False, "why": "no such task"}
    spec = json.loads(r["spec"] or "{}")
    if r["kind"] != "code_change" or r["state"] != "done" or spec.get("checkin_of"):
        return {"ok": False, "why": "not a finished code_change awaiting check-in"}
    s = gate.stage(conn, task_id)
    if s is None:
        return {"ok": False, "why": "not gated CVC code"}
    if s["stage"] != "approved":
        return {"ok": False, "why": gate._WHY.get(s["stage"], s["stage"]), "review_id": s.get("review_id")}
    ok, why = compile_clean(json.loads(r["result"] or "{}"))
    return {"ok": ok, "why": why, "review_id": s["review_id"]}


def _when(ts: int) -> str:
    from .commitments import _tz
    return datetime.fromtimestamp(ts, _tz(None)).strftime("%Y-%m-%d %H:%M")


def _ben_items(conn: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
    """Ben's open check-in approval loose ends on the code and its reviews."""
    return [f for f in conn.execute(
        """SELECT f.id, f.what FROM follow_ups f JOIN tasks t ON t.id=f.task_id
           WHERE f.state='open' AND lower(trim(f.who))='ben'
             AND (t.id=? OR (t.parent_id=? AND t.kind='dod_review')) ORDER BY f.created_at, f.idx""",
        (task_id, task_id)) if _CHECKIN.search(f["what"] or "")]


def _add_follow_up(conn: sqlite3.Connection, task_id: str, who: str, what: str) -> str:
    from .store import _id
    n = conn.execute("SELECT COALESCE(MAX(idx), -1) + 1 FROM follow_ups WHERE task_id=?", (task_id,)).fetchone()[0]
    fid = _id()
    conn.execute("INSERT INTO follow_ups(id, task_id, idx, who, what, state, created_at) VALUES (?,?,?,?,?,'open',?)",
                 (fid, task_id, n, who, what[:300], now()))
    return fid


def _give_back(conn: sqlite3.Connection, a: dict[str, Any], why: str) -> list[str]:
    """Ben's item again: reopen what the approval closed, or open one."""
    closed = json.loads(a["closed"] or "[]")
    back = []
    for fid in closed:
        if conn.execute("UPDATE follow_ups SET state='open', resolved_by=NULL, resolved_at=NULL "
                        "WHERE id=? AND state='dropped'", (fid,)).rowcount:
            back.append(fid)
    if not back:
        back.append(_add_follow_up(conn, a["task_id"], "Ben",
                                   f"Approve the check-in of {a['task_id']} ({why})"))
    return back


def approve(conn: sqlite3.Connection, task_id: str, review_id: str, t: int) -> dict[str, Any]:
    from . import store
    from .workflows import sla
    from . import defer
    veto_by = sla.deadline(t, int(window_hours(conn) * sla.HOUR), off=defer.holidays(conn))
    dec = store.record_decision(
        conn, f"{APPROVED} {task_id} (review {review_id}); veto by {_when(veto_by)}",
        "review approves, files unchanged since, compile clean; check-in waits out the window; " + WHY, task_id)
    items = _ben_items(conn, task_id)
    for f in items:
        store.resolve_follow_up(conn, f["id"], decision_id=dec["decision_id"])
    conn.execute("INSERT INTO checkin_auto(task_id, review_id, decision_id, approved_at, veto_by, state, closed) "
                 "VALUES (?,?,?,?,?,'window',?)",
                 (task_id, review_id, dec["decision_id"], t, veto_by, json.dumps([f["id"] for f in items])))
    return {"task_id": task_id, "action": "approved", "decision_id": dec["decision_id"], "veto_by": veto_by,
            "closed": [f["id"] for f in items]}


def _checked_in(conn: sqlite3.Connection, task_id: str) -> bool:
    return any(json.loads(s or "{}").get("checkin_of") == task_id for (s,) in conn.execute(
        "SELECT spec FROM tasks WHERE kind='code_change' AND spec LIKE ?", (f"%{task_id}%",)))


def veto(conn: sqlite3.Connection, task_id: str, by: str = "Ben's Veto on /board/task",
         decision_id: str | None = None) -> dict[str, Any]:
    """Cancel an auto-approval still in its window (or released with no
    check-in yet): Ben's item comes back. `decision_id` is a veto decision
    already recorded; without one, one is recorded."""
    from . import store
    a = row(conn, task_id)
    if a is None or a["state"] not in ("window", "released"):
        raise ValueError(f"no auto-approved check-in of {task_id} to veto")
    if a["state"] == "released" and _checked_in(conn, task_id):
        raise ValueError(f"{task_id} already has its check-in task; stop that task instead")
    if decision_id is None:
        decision_id = store.record_decision(
            conn, f"Veto check-in auto-approval for {task_id}; Ben approves the check-in himself",
            f"Ben's veto ({by})", task_id, supersedes=a["decision_id"])["decision_id"]
    back = _give_back(conn, a, "auto-approval vetoed")
    # A released approval left a client loose end to check in: drop it.
    for (fid,) in conn.execute("SELECT id FROM follow_ups WHERE task_id=? AND state='open' AND what LIKE ?",
                               (task_id, f"Check in {task_id}%")).fetchall():
        store.resolve_follow_up(conn, fid, decision_id=decision_id)
    conn.execute("UPDATE checkin_auto SET state='vetoed', ended_by=?, ended_at=? WHERE task_id=?",
                 (decision_id, now(), task_id))
    return {"task_id": task_id, "action": "vetoed", "decision_id": decision_id, "ben_items": back}


def _veto_decision(conn: sqlite3.Connection, a: dict[str, Any]) -> str | None:
    """A decision on the code task, after the approval, that vetoes it."""
    for d in conn.execute("SELECT id, statement, supersedes FROM decisions WHERE task_id=? AND created_at>=? "
                          "AND id<>? ORDER BY created_at", (a["task_id"], a["approved_at"], a["decision_id"])):
        if d["supersedes"] == a["decision_id"] or _VETO.search(d["statement"] or ""):
            return d["id"]
    return None


def release(conn: sqlite3.Connection, a: dict[str, Any], t: int) -> dict[str, Any]:
    from . import store
    c = check(conn, a["task_id"])
    if not c["ok"] or c.get("review_id") != a["review_id"]:
        why = c["why"] or "a newer review replaced the one approved"
        dec = store.record_decision(conn, f"Check-in auto-approval lapsed for {a['task_id']}: {why}"[:200],
                                    "the code no longer meets the rule at the end of the window; " + WHY,
                                    a["task_id"], supersedes=a["decision_id"])
        back = _give_back(conn, a, f"auto-approval lapsed: {why}"[:120])
        conn.execute("UPDATE checkin_auto SET state='lapsed', ended_by=?, ended_at=? WHERE task_id=?",
                     (dec["decision_id"], t, a["task_id"]))
        return {"task_id": a["task_id"], "action": "lapsed", "why": why, "ben_items": back}
    from . import queue
    code = conn.execute("SELECT assigned_to, title, spec FROM tasks WHERE id=?", (a["task_id"],)).fetchone()
    client = code["assigned_to"]
    auto = bool(client) and queue.switch(conn, "auto_checkin_task")
    dec = store.record_decision(
        conn, f"{RELEASED} {a['task_id']}: veto window passed; "
              + ("the board creates the check-in (checkin_of)" if auto
                 else "master creates the check-in (checkin_of)"),
        f"approval {a['decision_id']} stood its window; " + WHY, a["task_id"])
    fid = _add_follow_up(conn, a["task_id"], client or "master",
                         f"Check in {a['task_id']} as its own code_change (spec checkin_of {a['task_id']}); "
                         f"approved by decision {a['decision_id']}")
    conn.execute("UPDATE checkin_auto SET state='released', ended_by=?, ended_at=? WHERE task_id=?",
                 (dec["decision_id"], t, a["task_id"]))
    out = {"task_id": a["task_id"], "action": "released", "decision_id": dec["decision_id"], "follow_up": fid}
    if auto:
        try:
            with store.savepoint(conn, "checkin_task"):
                out["checkin_task"] = create_checkin(conn, a, code, fid)
        except Exception as e:  # noqa: BLE001 - the loose end stays for master
            out["checkin_error"] = str(e)[:200]
    return out


def create_checkin(conn: sqlite3.Connection, a: dict[str, Any], code: sqlite3.Row, fid: str) -> str:
    """The check-in code_change for a released approval (switch
    auto_checkin_task): parent = the code, its loose end resolved with it.
    Auto-dispatch sends it to the client's primary once that is free."""
    from . import store
    spec = {k: v for k, v in json.loads(code["spec"] or "{}").items() if "cvc" in k or "ticket" in k}
    spec.update({"checkin_of": a["task_id"], "cvc": True,
                 "created_by": f"board (auto): check-in released by decision {a['decision_id']}",
                 "instructions": f"Check in task {a['task_id']}'s code exactly as reviewed and approved "
                                 f"(review {a['review_id']}). Do not change the files first. Return compile."})
    tid = store.create_task(conn, "code_change", f"Check in: {code['title']}"[:80], spec,
                            assigned_to=code["assigned_to"], parent_id=a["task_id"])["task_id"]
    store.resolve_follow_up(conn, fid, task_id=tid)
    return tid


def candidates(conn: sqlite3.Connection) -> list[str]:
    """Finished CVC code (not check-ins) with no auto row yet and no check-in."""
    from . import gate
    t0 = gate.since(conn)
    if t0 is None:
        return []
    seen = {r[0] for r in conn.execute("SELECT task_id FROM checkin_auto")} if _have(conn) else set()
    out = []
    for r in conn.execute("SELECT id, spec FROM tasks WHERE kind='code_change' AND state='done' AND created_at>=? "
                          "ORDER BY completed_at", (t0,)):
        spec = json.loads(r["spec"] or "{}")
        if spec.get("cvc") is True and not spec.get("checkin_of") and r["id"] not in seen \
                and not _checked_in(conn, r["id"]):
            out.append(r["id"])
    return out


def run(conn: sqlite3.Connection, t: int | None = None, dry_run: bool = False) -> list[dict[str, Any]]:
    """One pass: approve what qualifies, apply vetoes, release or lapse what
    has waited out its window. Does nothing before the gate starts. With the
    switch off, open windows are cancelled back to Ben (the old route)."""
    from . import gate, queue
    t = t or now()
    if gate.since(conn) is None:
        return []
    out: list[dict[str, Any]] = []
    on = queue.switch(conn, "auto_checkin")
    if not dry_run:
        ensure(conn)
    windows = [dict(r) for r in conn.execute("SELECT * FROM checkin_auto WHERE state='window'")] \
        if _have(conn) else []
    for a in windows:
        if not on:
            if dry_run:
                out.append({"task_id": a["task_id"], "action": "cancel (switch off)"})
                continue
            from . import store
            dec = store.record_decision(conn, f"Check-in auto-approval withdrawn for {a['task_id']}: switch "
                                        "auto_checkin is off", "switch off restores Ben's check-in approval",
                                        a["task_id"], supersedes=a["decision_id"])
            back = _give_back(conn, a, "auto rule switched off")
            conn.execute("UPDATE checkin_auto SET state='lapsed', ended_by=?, ended_at=? WHERE task_id=?",
                         (dec["decision_id"], t, a["task_id"]))
            out.append({"task_id": a["task_id"], "action": "withdrawn", "ben_items": back})
            continue
        vd = _veto_decision(conn, a)
        if vd:
            out.append({"task_id": a["task_id"], "action": "veto"} if dry_run
                       else veto(conn, a["task_id"], "a decision", decision_id=vd))
        elif t >= a["veto_by"]:
            out.append({"task_id": a["task_id"], "action": "release"} if dry_run else release(conn, a, t))
    if on:
        for tid in candidates(conn):
            c = check(conn, tid)
            if not c["ok"]:
                continue
            out.append({"task_id": tid, "action": "approve", "review_id": c["review_id"]} if dry_run
                       else approve(conn, tid, c["review_id"], t))
    if not dry_run and out:
        conn.commit()
    return out


def refuse_checkin(conn: sqlite3.Connection, code_id: str) -> None:
    """gate.check_checkin's part: no check-in while a window is open, or
    while Ben's item from a veto or lapse is open."""
    a = row(conn, code_id)
    if a is None:
        return
    if a["state"] == "window":
        raise ValueError(f"Check-in refused: {code_id} was auto-approved (decision {a['decision_id']}); "
                         f"its veto window runs to {_when(a['veto_by'])}. Create the check-in after that.")
    if a["state"] in ("vetoed", "lapsed"):
        back = _reopened(conn, a)
        mine = [r["id"] for r in conn.execute(
            "SELECT id FROM follow_ups WHERE state='open' AND lower(trim(who))='ben' AND task_id IN "
            "(SELECT ? UNION SELECT id FROM tasks WHERE parent_id=? AND kind='dod_review') ORDER BY created_at, idx",
            (code_id, code_id)) if r["id"] in back]
        if mine:
            raise ValueError(f"Check-in refused: the auto-approval of {code_id} was {a['state']}; "
                             f"Ben's {', '.join(mine)} is open. He approves the check-in himself.")


def _reopened(conn: sqlite3.Connection, a: dict[str, Any]) -> set[str]:
    """Ben's items the veto or lapse put back: the ones the approval closed,
    plus any opened for it on the code task."""
    ids = set(json.loads(a["closed"] or "[]"))
    ids |= {r[0] for r in conn.execute("SELECT id FROM follow_ups WHERE task_id=? AND lower(trim(who))='ben' "
                                       "AND what LIKE ?", (a["task_id"], f"Approve the check-in of {a['task_id']}%"))}
    return ids


def main(argv: list[str] | None = None) -> int:
    from .db import connect
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ("status", "run"):
        print(__doc__.strip())
        return 2
    conn = connect()
    try:
        if argv[0] == "run":
            for r in run(conn, dry_run="--dry-run" in argv):
                print(json.dumps(r))
            return 0
        from . import gate, queue
        print(f"gate {'started' if gate.since(conn) else 'not started'}; switch auto_checkin "
              f"{'on' if queue.switch(conn, 'auto_checkin') else 'off'}; window {window_hours(conn):g} business "
              f"hours; rule {'active' if active(conn) else 'inactive'}")
        if _have(conn):
            for r in conn.execute("SELECT * FROM checkin_auto ORDER BY approved_at"):
                print(f"{r['task_id']}  {r['state']:<8} review {r['review_id']}  veto by {_when(r['veto_by'])}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
