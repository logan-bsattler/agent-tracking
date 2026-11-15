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
    on "client_follow_up": take the client loose end on A's task that fits
                           the step (_pick: first in the result's order to
                           match takes_follow_up, else the step's kind;
                           ambiguous ones are left for master), create S's
                           task from it, resolve it as tasked
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
from .cases import _step_tasks, ensure_workflows, event, have_workflows, recompute

SINCE_KEY = "workflow_engine_since"
REPLY_RULING = "f294e33095c1"
LOOKBACK = 72 * 3600
# A loose end that asks for a reply, not for the work a non-reply step does.
_REPLYISH = re.compile(r"\b(repl(y|ies|ied)|respond|response|e-?mail|draft (a |the )?(note|message|answer))\b", re.I)
# What a loose end for each kind's step reads like, when the step names no
# takes_follow_up of its own.
KIND_WORDS = {
    "code_change": re.compile(r"\b(fix|build|code|compile|patch|implement|change|program|cvc)", re.I),
    "deliverable": re.compile(r"\b(finding|write|document|report|deliverable|workbook|evidence)", re.I),
    "investigation": re.compile(r"\b(investigat|verif|check|confirm|re-?run|retest|test)", re.I),
    "data_pull": re.compile(r"\b(pull|extract|export|query|data)", re.I),
    "draft_reply": _REPLYISH,
}
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


def _pick(fus: list[Any], sp: dict[str, Any]) -> Any:
    """The client loose end a spawn takes, in the order the result listed
    them, or None. With takes_follow_up, the first it matches. Without one,
    a loose end that reads like the step's kind (and, for any step but a
    reply, not like a reply); failing that, for a client_follow_up spawn,
    the only non-reply loose end when there is exactly one. Anything less
    clear is left for master rather than guessed."""
    if sp.get("takes_follow_up"):
        rx = re.compile(sp["takes_follow_up"], re.I)
        return next((f for f in fus if rx.search(f["what"] or "")), None)
    pool = fus if sp["kind"] == "draft_reply" else [f for f in fus if not _REPLYISH.search(f["what"] or "")]
    words = KIND_WORDS.get(sp["kind"])
    hit = next((f for f in pool if words and words.search(f["what"] or "")), None)
    if hit is not None:
        return hit
    if sp["on"] == "client_follow_up" and len(pool) == 1:
        return pool[0]
    return None


def plan(conn: sqlite3.Connection, case: sqlite3.Row, t: int, since: int,
         write: bool = True) -> dict[str, Any] | None:
    """What the engine would do for one case: {'spawn': ...}, {'stop': ...} or
    None. With write=False it stores nothing (steps it would assign stay in memory)."""
    d = defs.get(case["workflow"])
    if not d or not d.get("engine"):
        return None
    tasks = _step_tasks(conn, case, d, write=write)
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
    # In the order the result listed them: one result's loose ends share
    # created_at and have random ids, so idx (then rowid) is the order.
    fus = conn.execute(f"SELECT id, task_id, who, what FROM follow_ups WHERE state='open' AND task_id IN ({marks}) "
                       "ORDER BY created_at, idx, rowid", ids).fetchall()
    if any((f["who"] or "").strip().lower() == "ben" for f in fus):
        return None  # Ben's turn: the engine never moves a case past him
    a_key = steps[reached]["key"]
    for j in range(reached + 1, len(steps)):
        st = steps[j]
        sp = st.get("spawn")
        if not sp or (sp.get("after") or steps[j - 1]["key"]) != a_key:
            continue
        # An optional Ben step (a mid-case ruling) applies only when Ben has a loose
        # end open, and that already returned above; so only required ones stop here.
        between = [s for s in steps[reached + 1:j + 1] if s["who"] == "ben" and not s.get("optional")]
        if between:
            return {"stop": st["key"], "prev": prev["id"],
                    "why": f"a Ben step ({between[0]['name']}) comes first"}
        if sp["kind"] not in defs.ENGINE_KINDS or (st.get("match") or {}).get("spec_has") == "checkin_of":
            return None
        if conn.execute("SELECT 1 FROM workflow_case_events WHERE case_id=? AND event='spawned' AND step=? "
                        "AND detail LIKE ?", (case["id"], st["key"], f"%from {prev['id']}%")).fetchone():
            return None
        mine = [f for f in fus if f["task_id"] == prev["id"] and _is_client(f["who"])]
        take = _pick(mine, sp)
        if take is None and sp["kind"] == "draft_reply":
            # A deferred reply loose end still gets its draft (REPLY_RULING).
            parked = conn.execute("SELECT id, task_id, who, what FROM follow_ups WHERE state='deferred' "
                                  "AND task_id=? ORDER BY created_at, idx, rowid", (prev["id"],)).fetchall()
            take = _pick([f for f in parked if _is_client(f["who"])], sp)
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
        # Every hold passed is gated, not only the first of each sort.
        proceeds = [h["decision_id"] for h in (held or {}).get("proceeds", [])]
        drafts = [h["decision_id"] for h in (held or {}).get("drafts", [])]
        return {"spawn": st["key"], "kind": sp["kind"], "title": title, "spec": spec, "prev": prev["id"],
                "client": case["client"], "follow_up": take["id"] if take else None,
                "hold": proceeds[0] if proceeds else None, "holds_past": proceeds,
                "draft_past": drafts[0] if drafts else None, "drafts_past": drafts}
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
    # A hold with no Ben item of its own still gates Outlook and the send
    # until it is lifted (holds.hold_live), so the gate never registers empty.
    gate = ben or [holds.HOLD_REF + hold_id]
    # Worded so it does not read as a new hold itself (holds.is_hold).
    stmt = f"Engine drafted reply {tid} under ruling {REPLY_RULING}; decision {hold_id} still governs Outlook and the send"
    if ben:
        stmt += f" (Ben's {', '.join(ben)})"
    dec = store.record_decision(conn, stmt[:200], f"Ben's ruling {REPLY_RULING}: a deferred or held reply is still "
                                "drafted, since drafts never send; the earlier decision still governs the send", tid)
    holds.register(conn, dec["decision_id"], hold_id, gate, ([w["ref"] for w in h["work"]] if h else []) + [tid], tid)


def run(conn: sqlite3.Connection, dry_run: bool = False, t: int | None = None) -> list[dict[str, Any]]:
    """One pass over open and recently closed engine cases. Returns what it did
    (or would do). Each case in its own transaction, so one failure costs one case."""
    from .. import store
    if dry_run:
        if not have_workflows(conn):
            return []  # a dry run writes nothing, schema included
    else:
        ensure_workflows(conn)
    t = t or now()
    # The stored switch-on time in both modes (a dry run with none stored
    # gets now(), which is what the first real pass would use).
    since = _since(conn, write=not dry_run)
    engine_wfs = [c for c, d in defs.load().items() if d.get("engine")]
    if not engine_wfs:
        return []
    marks = ",".join("?" * len(engine_wfs))
    rows = conn.execute(f"SELECT * FROM workflow_cases WHERE workflow IN ({marks}) "
                        "AND (state='open' OR closed_at >= ?) ORDER BY created_at",
                        (*engine_wfs, t - LOOKBACK)).fetchall()
    out = []
    failed: list[tuple[str, Exception]] = []
    for case in rows:
        if dry_run:
            try:
                p = plan(conn, case, t, since, write=False)
            except Exception as exc:
                failed.append((case["id"], exc))
                continue
            if p:
                out.append({"case": case["id"], "workflow": case["workflow"], **p})
            continue
        try:
            # Inside the try: a lock timeout here costs this case, not the pass.
            conn.execute("BEGIN IMMEDIATE")
            p = plan(conn, case, t, since)
            if p is None:
                conn.execute("ROLLBACK")
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
            for hid in p.get("holds_past") or []:
                _proceed_past(conn, hid, tid)
            for hid in p.get("drafts_past") or []:
                _draft_past(conn, hid, tid)
            if p["follow_up"]:
                store.resolve_follow_up(conn, p["follow_up"], task_id=tid)
            event(conn, case["id"], "spawned", p["spawn"], tid, "agent",
                  f"{p['kind']} {tid} from {p['prev']}; decision {dec['decision_id']}")
            recompute(conn, case["id"])
            conn.execute("COMMIT")
            out.append({"case": case["id"], "workflow": case["workflow"], "task_id": tid, **p})
        except Exception as exc:  # one bad case must not skip the cases after it
            if conn.in_transaction:  # BEGIN may have failed, or something committed already
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            failed.append((case["id"], exc))
    if failed:
        # The spawned cases are committed; raise so the caller's loop_error records the rest.
        raise EngineCaseErrors(failed, out)
    return out


class EngineCaseErrors(RuntimeError):
    """run() finished every case it could; these ones raised. `done` is what it did."""

    def __init__(self, failed: list[tuple[str, Exception]], done: list[dict[str, Any]]):
        self.failed, self.done = failed, done
        super().__init__(f"{len(failed)} case(s) failed, {len(done)} done: " + "; ".join(
            f"{cid}: {type(e).__name__}: {e}" for cid, e in failed))
