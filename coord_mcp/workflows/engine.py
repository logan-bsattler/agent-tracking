"""The workflow engine: advance a case when a step's result lands.

Off unless COORD_WORKFLOW_ENGINE=1 (the dashboard reads it at each pass, like
the master-console mod reads COORD_AUTO_DISPATCH). It rides the dashboard
loop after cases.sync, and `python -m coord_mcp.workflows engine --dry-run`
shows what it would do without writing.

What it does, for a workflow whose definition says "engine": true:

  A step S with a `spawn` and the step A it waits on (`spawn.after`, else the
  step before S). When A's latest task finished done, nothing in the case is
  open, nothing later than A exists, and no Ben loose end is open on the case:
    on "result":           create S's task
    on "client_follow_up": take the first open loose end on A's task whose
                           `who` is the client (and matches takes_follow_up),
                           create S's task from it, resolve it as tasked
  The new task's parent is A's task (so it inherits the pipeline link), it
  carries case_id and case_step, and a decision records it ("workflow engine;
  Ben may override"). A matching client loose end is resolved as tasked by
  the new task, so master does not task it twice.

What it never does:
  - create a task at, or past, one of Ben's steps (a Ben step between A and S
    records a 'stopped' event and leaves the case to Ben),
  - create a check-in, a promotion or anything outside ENGINE_KINDS,
  - draft a reply when the case has no client message to answer (no
    reply_from on the root task's spec or its intent): a mis-tagged case
    must not grow an email,
  - dispatch: open tasks go out by auto-dispatch or master, as now,
  - move past a hold decision on the case (holds.case_hold) that is
    outward-facing or has no default; an internal hold with a default it
    proceeds past only under switch hold_rule, superseding the hold with a
    "master default; Ben may override" decision and gating Outlook/check-in,
  - act on a result that landed before it was first switched on, or more
    than LOOKBACK hours ago, so turning it on never sweeps up history.

Replies proceed past a deferral (Ben, 2026-11-14, decision REPLY_RULING):
drafting never sends, so a draft_reply step takes its reply loose end even
when that loose end is deferred, and an internal hold on the case does not
stop the draft. The hold stays standing; the engine records a decision
citing the ruling and registers the hold's open Ben items as a gate, so
Draft in Outlook and the send still wait on Ben (holds.pending_gate). An
outward-facing hold still stops the draft, as fix 6 has it.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from typing import Any

from ..db import now
from . import definitions as defs
from .cases import _step_tasks, ensure_workflows, event, recompute

SINCE_KEY = "workflow_engine_since"
REPLY_RULING = "f294e33095c1"
LOOKBACK = 72 * 3600
_PH = re.compile(r"\{(client|root_title|prev|intent|reply_from|reply_date|case|workflow|follow_up)\}")


def enabled() -> bool:
    return os.environ.get("COORD_WORKFLOW_ENGINE", "").strip() == "1"


def _since(conn: sqlite3.Connection, write: bool) -> int:
    r = conn.execute("SELECT value FROM meta WHERE key=?", (SINCE_KEY,)).fetchone()
    if r:
        return int(r["value"])
    t = now()
    if write:
        conn.execute("INSERT INTO meta(key, value) VALUES (?,?)", (SINCE_KEY, str(t)))
    return t


def _fill(v: Any, ctx: dict[str, str]) -> Any:
    if isinstance(v, str):
        return _PH.sub(lambda m: ctx.get(m.group(1), ""), v)
    if isinstance(v, list):
        return [_fill(x, ctx) for x in v]
    if isinstance(v, dict):
        return {k: _fill(x, ctx) for k, x in v.items()}
    return v


def _is_client(who: str | None) -> bool:
    return (who or "").strip().lower() not in ("ben", "master", "")


def _context(conn: sqlite3.Connection, case: sqlite3.Row, prev: sqlite3.Row, follow_up: str = "") -> dict[str, str]:
    from .. import store
    root = conn.execute("SELECT * FROM tasks WHERE id=?", (case["root_task_id"],)).fetchone()
    try:
        rspec = json.loads(root["spec"] or "{}")
    except ValueError:
        rspec = {}
    reply: dict[str, str] = {}
    if case["intent_id"]:
        i = conn.execute("SELECT context FROM intents WHERE id=?", (case["intent_id"],)).fetchone()
        if i:
            reply = store.status_request(i["context"])
            f = store.intent_fields(i["context"])
            reply.setdefault("reply_from", f.get("from_email", ""))
            reply.setdefault("reply_date", f.get("date", ""))
    rs = rspec if isinstance(rspec, dict) else {}
    return {"client": case["client"] or "", "root_title": root["title"], "prev": prev["id"],
            "intent": case["intent_id"] or "none", "case": case["id"], "workflow": case["workflow"] or "",
            "reply_from": str(rs.get("reply_from") or reply.get("reply_from") or ""),
            "reply_date": str(rs.get("reply_date") or reply.get("reply_date") or ""),
            "follow_up": follow_up}


def plan(conn: sqlite3.Connection, case: sqlite3.Row, t: int, since: int) -> dict[str, Any] | None:
    """What the engine would do for one case: {'spawn': ...}, {'stop': ...} or None."""
    d = defs.get(case["workflow"])
    if not d or not d.get("engine"):
        return None
    tasks = _step_tasks(conn, case, d)
    if not tasks or any(x["state"] == "open" for x in tasks):
        return None
    steps = d["steps"]
    done = [x for x in tasks if x["state"] == "done" and x["case_step"]]
    if not done:
        return None
    reached = max(defs.step_index(d, x["case_step"]) for x in done)
    prev = max((x for x in done if defs.step_index(d, x["case_step"]) == reached),
               key=lambda x: (x["completed_at"] or 0, x["created_at"]))
    if not prev["completed_at"] or prev["completed_at"] < max(since, t - LOOKBACK):
        return None
    ids = [x["id"] for x in tasks]
    marks = ",".join("?" * len(ids))
    fus = conn.execute(f"SELECT id, task_id, who, what FROM follow_ups WHERE state='open' AND task_id IN ({marks}) "
                       "ORDER BY created_at, id", ids).fetchall()
    if any((f["who"] or "").strip().lower() == "ben" for f in fus):
        return None  # Ben's turn: the engine never moves a case past him
    a_key = steps[reached]["key"]
    for j in range(reached + 1, len(steps)):
        st = steps[j]
        sp = st.get("spawn")
        if not sp or (sp.get("after") or steps[j - 1]["key"]) != a_key:
            continue
        between = steps[reached + 1:j + 1]
        if any(s["who"] == "ben" for s in between):
            return {"stop": st["key"], "prev": prev["id"],
                    "why": f"a Ben step ({next(s['name'] for s in between if s['who'] == 'ben')}) comes first"}
        if sp["kind"] not in defs.ENGINE_KINDS or (st.get("match") or {}).get("spec_has") == "checkin_of":
            return None
        if conn.execute("SELECT 1 FROM workflow_case_events WHERE case_id=? AND event='spawned' AND step=? "
                        "AND detail LIKE ?", (case["id"], st["key"], f"%from {prev['id']}%")).fetchone():
            return None
        rx = re.compile(sp["takes_follow_up"], re.I) if sp.get("takes_follow_up") else None
        mine = [f for f in fus if f["task_id"] == prev["id"] and _is_client(f["who"])]
        take = next((f for f in mine if not rx or rx.search(f["what"] or "")), None)
        if take is None and sp["kind"] == "draft_reply":
            # A deferred reply loose end still gets its draft (REPLY_RULING).
            parked = conn.execute("SELECT id, task_id, who, what FROM follow_ups WHERE state='deferred' "
                                  "AND task_id=? ORDER BY created_at, id", (prev["id"],)).fetchall()
            take = next((f for f in parked if _is_client(f["who"]) and (not rx or rx.search(f["what"] or ""))),
                        None)
        if sp["on"] == "client_follow_up" and take is None:
            return None
        ctx = _context(conn, case, prev, take["what"] if take else "")
        if sp["kind"] == "draft_reply" and not ctx["reply_from"]:
            return None  # no client message to answer: a reply is master's call
        spec = _fill(sp["spec"], ctx)
        for k, v in (sp.get("spec_defaults") or {}).items():
            spec.setdefault(k, v)
        if sp.get("cvc"):
            spec["cvc"] = True
        spec = {k: v for k, v in spec.items() if v not in ("", None)}
        if spec.get("checkin_of"):
            return None
        # A hold decision on the case (holds.py): outward or default-less holds
        # stop it; an internal hold with a default proceeds under hold_rule.
        from .. import holds
        held = holds.case_hold(conn, ids, sp["kind"])
        if held and "stop" in held:
            return {"stop": st["key"], "prev": prev["id"], "why": held["stop"]}
        title = _fill(sp["title"], ctx)
        title = title if len(title) <= 80 else title[:77] + "..."
        return {"spawn": st["key"], "kind": sp["kind"], "title": title, "spec": spec, "prev": prev["id"],
                "client": case["client"], "follow_up": take["id"] if take else None,
                "hold": held["proceed"]["decision_id"] if held and "proceed" in held else None,
                "draft_past": held["draft"]["decision_id"] if held and "draft" in held else None}
    return None


def _proceed_past(conn: sqlite3.Connection, hold_id: str, tid: str) -> None:
    """Supersede an internal hold the engine moved past with the default it
    proceeds on, and gate the new task's outward steps until Ben answers."""
    from .. import holds, store
    old = conn.execute("SELECT * FROM decisions WHERE id=?", (hold_id,)).fetchone()
    h = holds.classify(conn, old) if old is not None else None
    if h is None:
        return
    ben = h["open_ben_follow_ups"] or h["ben_follow_ups"]
    stmt = f"Proceed on master default for {tid}: {h['default'] or ''}"[:150]
    if ben:
        stmt += f"; Ben's {', '.join(ben)} stays open"
    dec = store.record_decision(conn, stmt[:200], f"workflow engine past internal hold {hold_id}; master default; "
                                "Ben may override", tid, supersedes=hold_id)
    holds.register(conn, dec["decision_id"], hold_id, ben, [w["ref"] for w in h["work"]] + [tid], tid)


def _draft_past(conn: sqlite3.Connection, hold_id: str, tid: str) -> None:
    """A reply drafted while an internal hold stands (REPLY_RULING): the hold
    is not superseded; Outlook drafting and the send wait on its Ben items."""
    from .. import holds, store
    old = conn.execute("SELECT * FROM decisions WHERE id=?", (hold_id,)).fetchone()
    h = holds.classify(conn, old) if old is not None else None
    ben = (h["open_ben_follow_ups"] or h["ben_follow_ups"]) if h else []
    # Worded so it does not read as a new hold itself (holds.is_hold).
    stmt = f"Engine drafted reply {tid} under ruling {REPLY_RULING}; decision {hold_id} still governs Outlook and the send"
    if ben:
        stmt += f" (Ben's {', '.join(ben)})"
    dec = store.record_decision(conn, stmt[:200], f"Ben's ruling {REPLY_RULING}: a deferred or held reply is still "
                                "drafted, since drafts never send; the earlier decision still governs the send", tid)
    holds.register(conn, dec["decision_id"], hold_id, ben, ([w["ref"] for w in h["work"]] if h else []) + [tid], tid)


def run(conn: sqlite3.Connection, dry_run: bool = False, t: int | None = None) -> list[dict[str, Any]]:
    """One pass over open and recently closed engine cases. Returns what it did
    (or would do). Each case in its own transaction, so one failure costs one case."""
    from .. import store
    ensure_workflows(conn)
    t = t or now()
    since = _since(conn, write=not dry_run) if not dry_run else 0
    engine_wfs = [c for c, d in defs.load().items() if d.get("engine")]
    if not engine_wfs:
        return []
    marks = ",".join("?" * len(engine_wfs))
    rows = conn.execute(f"SELECT * FROM workflow_cases WHERE workflow IN ({marks}) "
                        "AND (state='open' OR closed_at >= ?) ORDER BY created_at",
                        (*engine_wfs, t - LOOKBACK)).fetchall()
    out = []
    for case in rows:
        conn.execute("BEGIN IMMEDIATE")
        try:
            p = plan(conn, case, t, since)
            if p is None or dry_run:
                conn.execute("ROLLBACK")
                if p:
                    out.append({"case": case["id"], "workflow": case["workflow"], **p})
                continue
            if "stop" in p:
                seen = conn.execute("SELECT 1 FROM workflow_case_events WHERE case_id=? AND event='stopped' "
                                    "AND step=? AND task_id=?", (case["id"], p["stop"], p["prev"])).fetchone()
                if not seen:
                    event(conn, case["id"], "stopped", p["stop"], p["prev"], "ben", p["why"])
                    out.append({"case": case["id"], "workflow": case["workflow"], **p})
                conn.execute("COMMIT")
                continue
            from .. import queue
            new = store.create_task(conn, p["kind"], p["title"], p["spec"], assigned_to=p["client"],
                                    parent_id=p["prev"], pipeline_task_id=queue.pipeline_id(conn, p["prev"]))
            tid = new["task_id"]
            conn.execute("UPDATE tasks SET case_id=?, case_step=? WHERE id=?", (case["id"], p["spawn"], tid))
            dec = store.record_decision(
                conn, f"Workflow engine: {case['workflow']} case {case['id']} step {p['spawn']} -> "
                      f"{p['kind']} {tid} from task {p['prev']}",
                because="the step before landed; COORD_WORKFLOW_ENGINE on (workflow engine; Ben may override)",
                task_id=tid)
            if p.get("hold"):
                _proceed_past(conn, p["hold"], tid)
            if p.get("draft_past"):
                _draft_past(conn, p["draft_past"], tid)
            if p["follow_up"]:
                store.resolve_follow_up(conn, p["follow_up"], task_id=tid)
            event(conn, case["id"], "spawned", p["spawn"], tid, "agent",
                  f"{p['kind']} {tid} from {p['prev']}; decision {dec['decision_id']}")
            recompute(conn, case["id"])
            conn.execute("COMMIT")
            out.append({"case": case["id"], "workflow": case["workflow"], "task_id": tid, **p})
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return out
