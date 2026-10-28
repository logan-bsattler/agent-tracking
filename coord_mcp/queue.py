"""Ben's queue: keep it to what only Ben can do, and let him clear it in batches.

Measured 2026-10-27: tasks ran in minutes, while 214 loose ends sat on Ben
for a week on average, presented one question per turn. Three things here
attack that:

- Routing. A follow-up a client agent hands to Ben that is really agent work
  (a DB read, a query, a compile) or a reply an agent can draft goes back to
  that client as it lands (`route_new`, from `complete_task`). The minder
  already wakes master for an untasked client loose end, so it gets tasked
  without Ben seeing it. `backfill` does the same for the existing queue,
  dry-run unless told otherwise.
- The batch page (/queue). Every open Ben loose end on one page, grouped by
  client, with bulk Send to agent / Defer / Drop done here and typed answers
  sent to master as one prompt (`apply`).
- What master owes on the way out: finished drafts not yet in Outlook
  (`undrafted`) and pipeline-linked work that reached the client with no
  SharePoint note (`pipeline_owed`). Both feed the minder.

No contract or schema version change: routes live in their own table,
created on first use like task_deps.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from typing import Any

from . import contracts
from .db import connect, now

# ------------------------------------------------------------------ routing

# Replies are the agents' to draft (a draft_reply task); Ben reviews the
# draft, which is its own loose end. "Review and send ..." does not start
# with one of these verbs, so it stays his.
_REPLY = re.compile(r"^\s*(reply|respond|write back|answer|draft (a |an |the )?(reply|response|email|note)|e-?mail)\b",
                    re.I)
_DOES = r"(db read|database read|query|re-?run|run|pull|look up|lookup|grep|search|diff|compile|trace|reproduce" \
        r"|scan|read|count|list|inventory|map|extract)"
_CHECKS = r"(check|verify|confirm|find|determine|identify)"
_ON = (r"\b(tables?|data|db|database|records?|fields?|code|source|programs?|\w+\.(p|i|w|cls)|logs?|query|harness"
       r"|user-exits?|xx\w+|CIM|BOM|spec|file)\b")
_AGENT = re.compile(rf"^\s*({_DOES}\b|{_CHECKS}\b.*{_ON})", re.I)
# Anything that needs a person, a judgement or a conversation stays Ben's,
# whatever verb it opens with.
_PERSON = re.compile(
    r"\b(approve|approval|decide|decision|ask|raise|tell|send|sign[- ]?off|accept|choose|pick|prioriti[sz]e"
    r"|your call|Ben'?s call|with (the )?client|call|meeting|budget|scope|estimate|quote|contract|whether to"
    r"|VM|Kiro|AUX|manual(ly)?|by hand|WebUI|in the (UI|browser))\b", re.I)
_WITH_SOMEONE = re.compile(r"\bwith [A-Z][a-z]+\b")  # "with Darren": a person is in the loop
# Clients whose agent has no server leg: anything on a server is a person's.
NO_SERVER = {"lnk", "ts tech", "pbe", "ghsp", "jbss", "acument"}
_SERVER = re.compile(r"\b(DEVL|TEST|PROD|MIGR|server|CVC)\b")


def route_for(what: str, client: str | None) -> str | None:
    """'reply' or 'agent' when a loose end handed to Ben belongs to the
    client agent, else None. Conservative: when unsure, it stays Ben's."""
    if not client:
        return None
    if _REPLY.search(what):
        return "reply"
    if not _AGENT.search(what) or _PERSON.search(what) or _WITH_SOMEONE.search(what):
        return None
    if client.strip().casefold() in NO_SERVER and _SERVER.search(what):
        return None
    return "agent"


ROUTES_DDL = """
CREATE TABLE IF NOT EXISTS follow_up_routes (
  follow_up_id TEXT PRIMARY KEY REFERENCES follow_ups(id),
  from_who     TEXT NOT NULL,
  to_who       TEXT NOT NULL,
  why          TEXT NOT NULL,
  created_at   INTEGER NOT NULL
);
"""


def ensure_routes(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='follow_up_routes'").fetchone() is None:
        conn.executescript(ROUTES_DDL)


def reroute(conn: sqlite3.Connection, follow_up_id: str, to: str, why: str) -> None:
    """Hand an open loose end to `to`, remembering who it was for."""
    ensure_routes(conn)
    r = conn.execute("SELECT who, state FROM follow_ups WHERE id=?", (follow_up_id,)).fetchone()
    if r is None:
        raise KeyError(f"unknown follow-up '{follow_up_id}'")
    if r["state"] != "open":
        raise ValueError(f"follow-up '{follow_up_id}' is {r['state']}, not open")
    conn.execute("UPDATE follow_ups SET who=? WHERE id=?", (to, follow_up_id))
    conn.execute("INSERT OR REPLACE INTO follow_up_routes VALUES (?,?,?,?,?)",
                 (follow_up_id, r["who"], to, why, now()))


def _ben_rows(conn: sqlite3.Connection, task_id: str | None = None) -> list[sqlite3.Row]:
    sql = ("SELECT f.id, f.task_id, f.what, f.created_at, t.assigned_to client, t.title "
           "FROM follow_ups f JOIN tasks t ON t.id = f.task_id "
           "WHERE f.state='open' AND lower(trim(f.who))='ben'")
    args: tuple = ()
    if task_id:
        sql += " AND f.task_id=?"
        args = (task_id,)
    return conn.execute(sql + " ORDER BY f.created_at, f.idx", args).fetchall()


def route_new(conn: sqlite3.Connection, task_id: str) -> int:
    """Route a just-finished task's Ben loose ends that are agent work back
    to its client. Returns how many moved."""
    n = 0
    for r in _ben_rows(conn, task_id):
        why = route_for(r["what"], r["client"])
        if why:
            reroute(conn, r["id"], r["client"], why)
            n += 1
    return n


def backfill(conn: sqlite3.Connection, apply: bool = False) -> list[dict[str, Any]]:
    """Every open Ben loose end route_for would move, moved only if `apply`."""
    out = []
    for r in _ben_rows(conn):
        why = route_for(r["what"], r["client"])
        if why:
            out.append({"id": r["id"], "client": r["client"], "why": why, "what": r["what"]})
            if apply:
                reroute(conn, r["id"], r["client"], why)
    return out


# ------------------------------------------------------------- batch page

def ben_queue(conn: sqlite3.Connection) -> dict[str, Any]:
    """Ben's open loose ends grouped by client, the client with the oldest
    item first, plus what routing moved off his queue in the last week."""
    from . import store
    t = now()
    q = store.current_question(conn)
    by: dict[str, list[dict[str, Any]]] = {}
    for r in _ben_rows(conn):
        d = dict(r)
        d["route"] = route_for(r["what"], r["client"])
        d["presented"] = bool(q and q.get("follow_up_id") == r["id"])
        by.setdefault(r["client"] or "unassigned", []).append(d)
    groups = sorted(by.items(), key=lambda kv: kv[1][0]["created_at"])
    ensure_routes(conn)
    moved = [dict(r) for r in conn.execute(
        """SELECT r.follow_up_id id, r.to_who client, r.why, r.created_at, f.what, f.state, f.task_id
           FROM follow_up_routes r JOIN follow_ups f ON f.id = r.follow_up_id
           WHERE lower(trim(r.from_who))='ben' AND r.created_at >= ? ORDER BY r.created_at DESC""",
        (t - 7 * 86400,))]
    return {"groups": [{"client": c, "items": items} for c, items in groups],
            "total": sum(len(i) for _, i in groups), "moved": moved, "as_of": t}


BATCH_ACTIONS = ("agent", "defer", "drop", "answer")
ANSWER_MAX = 1000
ANSWERS_MAX = 6000


def apply(conn: sqlite3.Connection, action: str, ids: list[str],
          answers: dict[str, str] | None = None, note: str = "") -> dict[str, Any]:
    """Ben's batch from /queue.

    agent: hand each to its task's client (master tasks it on the minder's
    next pass). defer / drop: one decision in Ben's name, each resolved by
    it. answer: Ben's typed answers go to master as one prompt; master
    records and resolves each, exactly as for a button answer.
    """
    from . import store
    if action not in BATCH_ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    rows = {r["id"]: r for r in _ben_rows(conn)}
    ids = [i for i in dict.fromkeys(ids) if i]
    if action == "answer":
        answers = {k: (v or "").strip() for k, v in (answers or {}).items()}
        ids = [i for i in ids or list(answers) if answers.get(i)]
    gone = [i for i in ids if i not in rows]
    if gone:
        raise ValueError(f"not open loose ends for Ben: {', '.join(gone)}")
    if not ids:
        raise ValueError("select at least one item" + (" and type an answer" if action == "answer" else ""))
    note = (note or "").strip()[:300]
    if action == "agent":
        no_client = [i for i in ids if not rows[i]["client"]]
        if no_client:
            raise ValueError(f"no client to send to: {', '.join(no_client)}")
        for i in ids:
            reroute(conn, i, rows[i]["client"], "ben")
        return {"ok": True, "action": action, "count": len(ids)}
    if action in ("defer", "drop"):
        verb = "deferred" if action == "defer" else "dropped"
        dec = store.record_decision(
            conn, f"Ben {verb} {len(ids)} loose end(s) from /queue: {', '.join(ids)}"[:200],
            note or ("waits until Ben or later work raises it" if action == "defer" else "done or moot, per Ben"))
        for i in ids:
            store.resolve_follow_up(conn, i, decision_id=dec["decision_id"], defer=action == "defer")
        return {"ok": True, "action": action, "count": len(ids), "decision_id": dec["decision_id"]}
    lines = []
    for i in ids:
        a = answers[i]
        if len(a) > ANSWER_MAX:
            raise ValueError(f"answer on {i} is over {ANSWER_MAX} characters")
        r = rows[i]
        lines.append(f"- {i} ({r['client'] or 'unassigned'}, task {r['task_id']}; \"{r['what'][:120]}\"): {a}")
    text = store.OPERATOR_ACTIONS["batch"].format(n=len(ids), answers="\n".join(lines))
    if note:
        text += "\n\nBen's direction: " + note
    if len(text) > ANSWERS_MAX:
        raise ValueError("too many answers in one batch; send fewer at a time")
    return {**store._enqueue(conn, "batch", None, text), "action": action, "count": len(ids)}


# --------------------------------------------------------- owed downstream

DRAFT_WINDOW = 48 * 3600   # older finished drafts predate auto-drafting; Ben works them from /drafts
REPLY_LATE = 24 * 3600     # a client waiting longer than this on a reply is late


def undrafted(conn: sqlite3.Connection, now_ts: int, after: int = 10 * 60) -> list[str]:
    """Finished drafts, ready for Outlook (written, body on disk, not held by
    the review gate), not drafted and not queued: master drafts them unasked.
    Drafting never sends, so this is not outward-facing."""
    from . import store
    out = []
    for d in store.draft_replies(conn, days=3)["drafts"]:
        if d["state"] == "waiting" and now_ts - DRAFT_WINDOW <= d["completed_at"] <= now_ts - after:
            out.append(d["task_id"])
    return out


def pipeline_id(conn: sqlite3.Connection, task_id: str) -> str | None:
    """The pipeline item a task bills to: its own link, else its parent
    chain's, else (for a check-in) the code it checks in."""
    seen, tid = set(), task_id
    while tid and tid not in seen:
        seen.add(tid)
        r = conn.execute("SELECT parent_id, spec, pipeline_task_id FROM tasks WHERE id=?", (tid,)).fetchone()
        if r is None:
            return None
        if r["pipeline_task_id"]:
            return r["pipeline_task_id"]
        try:
            spec = json.loads(r["spec"] or "{}")
        except ValueError:
            spec = {}
        tid = r["parent_id"] or (spec.get("checkin_of") if isinstance(spec, dict) else None)
    return None


PIPELINE_NOTE = "Pipeline note"  # the decision master records once it has added the note


def pipeline_owed(conn: sqlite3.Connection, now_ts: int, days: int = 7) -> list[str]:
    """Work that has reached the client, linked to a pipeline item, with no
    note there yet: a promoted deliverable, or code checked in (a check-in
    task, or a code_change outside the CVC gate)."""
    from .db import ensure_promotions
    ensure_promotions(conn)
    out = []
    for r in conn.execute(
        """SELECT t.id, t.kind, t.spec FROM tasks t
           WHERE t.state='done' AND t.completed_at >= ? AND t.kind IN ('deliverable','code_change')
             AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.task_id = t.id AND d.statement LIKE ?)
           ORDER BY t.completed_at""", (now_ts - days * 86400, PIPELINE_NOTE + "%")):
        try:
            spec = json.loads(r["spec"] or "{}")
        except ValueError:
            spec = {}
        if r["kind"] == "deliverable":
            if not conn.execute("SELECT 1 FROM promotions WHERE task_id=?", (r["id"],)).fetchone():
                continue
        elif spec.get("cvc") and not spec.get("checkin_of"):
            continue  # built, not checked in: nothing has reached the client yet
        if pipeline_id(conn, r["id"]):
            out.append(r["id"])
    return out


# Work the client will see, which SharePoint should therefore show.
LINKED_KINDS = ("deliverable", "code_change", "draft_reply")


def create_warnings(conn: sqlite3.Connection, kind: str, spec: dict[str, Any],
                    pipeline_task_id: str | None, parent_id: str | None) -> list[str]:
    """What master should fix about a task it just created. Advice, never a refusal."""
    out = []
    if kind in LINKED_KINDS and not pipeline_task_id and not (parent_id and pipeline_id(conn, parent_id)) \
            and not (spec.get("checkin_of") and pipeline_id(conn, str(spec["checkin_of"]))):
        out.append("no pipeline_task_id: its progress cannot reach SharePoint. Link it with "
                   "coord_set_pipeline_task once you know the item.")
    if kind == "draft_reply" and not spec.get("reply_date"):
        out.append("spec has no reply_date: the client's wait cannot be measured from the email. "
                   "Add reply_date and reply_from when it answers a thread.")
    if kind == "deliverable":
        fmt = str(spec.get("format") or "")
        if not fmt or re.fullmatch(r"\.?(md|markdown)", fmt.strip(), re.I):
            out.append(f"spec format is {fmt or 'missing'}: {contracts.FORMAT_RULE}. "
                       "Name it as format (e.g. \"html\", \"html + xlsx\", \"docx\"); a Markdown path is refused at completion.")
    return out


# ------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m coord_mcp.queue",
                                description="Route agent work off Ben's queue.")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill", help="list (or with --apply, move) Ben loose ends that are agent work")
    b.add_argument("--apply", action="store_true")
    a = p.parse_args(argv)
    conn = connect()
    try:
        rows = backfill(conn, apply=a.apply)
    finally:
        conn.close()
    for r in rows:
        print(f"{r['id']}  {r['client']:<9} {r['why']:<5} {r['what'][:110]}")
    print(f"{len(rows)} {'moved' if a.apply else 'would move (dry run; --apply to move)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
