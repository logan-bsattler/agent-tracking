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
# A question for a client person is a draft the agent writes, never a chore
# for Ben (audit 2026-11-14: "Ask QAD ...", "Get X from Rajesh", "Confirm
# with Ari" sat on his queue). "Ask Ben" is not one.
_ASK_PERSON = re.compile(
    r"^\s*((ask|chase|ping|nudge|remind)\s+(?!ben\b)\S|(follow up|check|confirm|clarify|agree|align)\s+with\s+(?!ben\b)\S"
    r"|get\b.{1,80}\bfrom\s+(?!ben\b)(?-i:[A-Z][a-z]+\b))", re.I)  # a Name, not "the PROD table"
# Work the agent does itself: edits and drafts, besides reads and runs.
_EDITS = r"(draft|write|edit|update|fix|correct|revise|re-?point|rename|re-?generate|rebuild|re-?cut|reconcile" \
         r"|add|fill( in)?|test|compare|capture|export|re-?word|amend)"
# What only Ben can do, whatever verb it opens with (the "who: Ben" test,
# 2026-11-14): send or publish, approve or sign off, client-visible scope,
# cost or dates, access only he has, and judgement with no precedent. Tighter
# than before: ask, raise, tell, confirm, whether-to and "with <Name>" no
# longer keep an item on him; they are a draft or a read.
_PERSON = re.compile(
    r"\b(approve[sd]?|approval|sign[- ]?off|promote|publish|send|submit|post to|decide|decision|rule|ruling"
    r"|your call|Ben'?s call|choose|pick|prioriti[sz]e|accept|reject|override|scope|estimate[sd]?|quote"
    r"|pric(e|ing)|budget|contract|invoice|bill(ed|ing)?|staffing|timesheets?|commit to|credentials?|password"
    r"|access|licen[cs]e|VPN|sign[- ]?in|log ?in|MFA|SharePoint|pipeline|meeting|on (a|the) call|phone"
    r"|review|VM|Kiro|AUX|manual(ly)?|by hand|WebUI|in the (UI|browser))\b", re.I)
# Ben-only acts with no keyword above: a call or meeting with a named person,
# trusting a workspace or host, signing in. Anchored or person-scoped, so
# "Check the totals meet the spec" or "Call the API" stay agent work.
_BEN_ACTS = re.compile(
    r"^\s*(call|phone|ring|meet(\s+with)?|visit|walk)\s+(?-i:[A-Z][a-z]+)\b"
    r"|^\s*(trust|approve)\b.{0,40}\b(workspace|folder|repo(sitory)?|host|server|session|machine)s?\b"
    r"|^\s*(sign|log)\s*(in|on)(to)?\b", re.I)
_WITH_SOMEONE = re.compile(r"\bwith [A-Z][a-z]+\b")  # "Run it on the VM with Darren": a person is in the loop
_EDIT = re.compile(rf"^\s*{_EDITS}\b", re.I)
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
    if _ASK_PERSON.search(what) and not _PERSON.search(what):
        return "reply"
    if not (_AGENT.search(what) or _EDIT.search(what)) or _PERSON.search(what) or _WITH_SOMEONE.search(what):
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
    # A Ben item has no task (docs/ben-only-replies.md): its client and due
    # date are on the row, and it is grouped under that client.
    sql = ("SELECT f.id, f.task_id, f.what, f.created_at, COALESCE(t.assigned_to, f.client) client, "
           "COALESCE(t.title, '') title, f.intent_id, f.due_at, f.ref "
           "FROM follow_ups f LEFT JOIN tasks t ON t.id = f.task_id "
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
    tagged = reasons_for(conn, task_id)
    for r in _ben_rows(conn, task_id):
        if r["id"] in tagged:
            continue  # the agent said why it is Ben's (send, access, ...): routing it back would loop
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
    from . import commitments, store
    t = now()
    q = store.current_question(conn)
    dues = commitments.due_map(conn)
    by_fu = {r["follow_up_id"]: r for r in conn.execute(
        "SELECT follow_up_id, due_at, due_basis FROM commitments WHERE state IN ('open','slipped') "
        "AND due_at IS NOT NULL AND follow_up_id IS NOT NULL ORDER BY due_at DESC")} if dues else {}
    wired = store.client_keys(conn)
    by: dict[str, list[dict[str, Any]]] = {}
    for r in _ben_rows(conn):
        d = dict(r)
        # Send to agent needs a client session; an unwired client has none.
        d["agent_ok"] = bool(r["client"]) and r["client"].strip().casefold() in wired
        d["route"] = route_for(r["what"], r["client"]) if r["task_id"] else None
        d["presented"] = bool(q and q.get("follow_up_id") == r["id"])
        # The effective due date of the item's task (docs/commitments.md), or
        # of a commitment linked to this loose end itself if that is sooner.
        own = {"due_at": r["due_at"], "due_basis": "stated"} if r["due_at"] is not None else None
        cands = [x for x in (dues.get(r["task_id"]) if r["task_id"] else None, by_fu.get(r["id"]), own) if x]
        best = min(cands, key=lambda x: x["due_at"]) if cands else None
        d["due"] = best["due_at"] if best else None
        d["due_basis"] = best["due_basis"] if best else None
        by.setdefault(r["client"] or "unassigned", []).append(d)

    def order(d: dict[str, Any]) -> tuple:
        # Overdue first, then soonest due, then today's order (oldest first).
        if d["due"] is None:
            return (2, 0, d["created_at"])
        return (0 if d["due"] < t else 1, d["due"], d["created_at"])

    for items in by.values():
        items.sort(key=order)
    # Groups holding due items first, by their earliest due; the rest by oldest item, as before.
    groups = sorted(by.items(), key=lambda kv: (0, kv[1][0]["due"], 0) if kv[1][0]["due"] is not None
                    else (1, 0, min(i["created_at"] for i in kv[1])))
    ensure_routes(conn)
    moved = [dict(r) for r in conn.execute(
        """SELECT r.follow_up_id id, r.to_who client, r.why, r.created_at, f.what, f.state, f.task_id
           FROM follow_up_routes r JOIN follow_ups f ON f.id = r.follow_up_id
           WHERE lower(trim(r.from_who))='ben' AND r.created_at >= ? ORDER BY r.created_at DESC""",
        (t - 7 * 86400,))]
    from . import defer as defer_mod
    rets = defer_mod.returns_for(conn)
    deferred = sorted(({**dict(r), "return_on": rets.get(r["id"])} for r in conn.execute(
        """SELECT f.id, f.what, f.task_id, f.created_at, f.resolved_by, COALESCE(f.client, t.assigned_to) client
           FROM follow_ups f LEFT JOIN tasks t ON t.id = f.task_id
           WHERE f.state='deferred' AND lower(trim(f.who))='ben'""")),
        key=lambda d: (d["return_on"] is None, d["return_on"] or 0, d["created_at"]))
    return {"groups": [{"client": c, "items": items} for c, items in groups],
            "total": sum(len(i) for _, i in groups), "moved": moved, "deferred": deferred, "as_of": t}


BATCH_ACTIONS = ("agent", "defer", "drop", "answer")
ANSWER_MAX = 1000
ANSWERS_MAX = 6000


def apply(conn: sqlite3.Connection, action: str, ids: list[str],
          answers: dict[str, str] | None = None, note: str = "", return_on: Any = None) -> dict[str, Any]:
    """Ben's batch from /queue.

    agent: hand each to its task's client (master tasks it on the minder's
    next pass). defer / drop: one decision in Ben's name, each resolved by
    it; a defer comes back on return_on (defer.parse: a date, a preset, a
    count of business days; default 5 business days). answer: Ben's typed answers go to master as one prompt; master
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
        wired = store.client_keys(conn)
        unwired = [i for i in ids if rows[i]["client"].strip().casefold() not in wired]
        if unwired:
            raise ValueError("no agent session for "
                             + ", ".join(f"{i} ({rows[i]['client']})" for i in unwired)
                             + ": that client is not wired. Defer, drop or answer it instead.")
        for i in ids:
            reroute(conn, i, rows[i]["client"], "ben")
        return {"ok": True, "action": action, "count": len(ids)}
    if action in ("defer", "drop"):
        from . import defer as defer_mod
        verb = "deferred" if action == "defer" else "dropped"
        ret = defer_mod.parse(return_on, conn=conn) if action == "defer" else None  # a bad date fails before any write
        until = f" until {defer_mod.day(ret)}" if ret else ""
        dec = store.record_decision(
            conn, f"Ben {verb} {len(ids)} loose end(s) from /queue{until}: {', '.join(ids)}"[:200],
            note or (f"returns to his queue on {defer_mod.day(ret)}" if action == "defer" else "done or moot, per Ben"))
        for i in ids:
            store.resolve_follow_up(conn, i, decision_id=dec["decision_id"], defer=action == "defer",
                                    return_on=ret)
        out = {"ok": True, "action": action, "count": len(ids), "decision_id": dec["decision_id"]}
        if ret:
            out["return_on"] = defer_mod.day(ret)
        return out
    # A workflow proposal's approve / reject / defer is applied here, in
    # code (workflows/propose.answer); anything else goes to master.
    # The whole batch is validated before anything is applied: a proposal
    # adopted ahead of a later refusal could not be answered again.
    from .workflows import propose
    long = [i for i in ids if len(answers[i]) > ANSWER_MAX]
    if long:
        raise ValueError(f"answer on {long[0]} is over {ANSWER_MAX} characters")
    acts = {i: hit for i in ids if (hit := propose.answer_action(conn, i, answers[i]))}

    def line(i: str, extra: str = "") -> str:
        r = rows[i]
        src = f"task {r['task_id']}" if r["task_id"] else f"intent {r['intent_id']}"
        return f"- {i} ({r['client'] or 'unassigned'}, {src}; \"{r['what'][:120]}\"): {answers[i]}{extra}"

    def prompt(lines: list[str]) -> str:
        text = store.OPERATOR_ACTIONS["batch"].format(n=len(lines), answers="\n".join(lines))
        return text + ("\n\nBen's direction: " + note if note else "")

    # Worst case, every proposal answer fails and goes to master too: size
    # the prompt for that (with room for the failure notes) before applying.
    if len(prompt([line(i, " " * 200 if i in acts else "") for i in ids])) > ANSWERS_MAX:
        raise ValueError("too many answers in one batch; send fewer at a time")
    handled, failed, rest = [], [], [i for i in ids if i not in acts]
    for i, (pid, act) in acts.items():
        try:
            propose.apply_action(conn, pid, act)
            handled.append(i)
        except (ValueError, KeyError, OSError) as e:
            # One proposal that cannot be applied (its base moved, its file is
            # gone) does not drop the rest of the batch: master gets the
            # answer with the reason, and the item stays open.
            failed.append({"id": i, "error": str(e)[:200]})
            rest.append(i)
    out: dict[str, Any] = {"ok": True, "action": action, "count": len(handled) + len(rest)}
    if handled:
        out["proposals"] = handled
    if failed:
        out["proposal_errors"] = failed
    if not rest:
        return out
    why = {f["id"]: f" [proposal not applied: {f['error']}]" for f in failed}
    return {**out, **store._enqueue(conn, "batch", None, prompt([line(i, why.get(i, "")) for i in ids if i in rest]))}


# ------------------------------------------------- shrinking it at the source
#
# Ben's ruling, 2026-11-13: his queue grew faster than he could clear it
# (1,047 items raised in 14 days, 635 resolved). Two rules:
#
# - Grouping. A result that hands him three or more rulings of the same
#   shape (one per row, per item, per program) is refused at completion: the
#   agent writes the rows to one workbook and hands back one item naming it.
# - Expiry. A loose end of his that has sat open EXPIRE_DAYS is deferred, not
#   dropped: off the active queue, still in the Deferred lane, reopenable.
#   Dated items, replies waiting on him and the question on screen stay.

GROUP_MIN = 3
_FILE_NAMED = re.compile(r"\.(xlsx|xlsm|csv|html?|docx|pdf)\b", re.I)
_VARIES = re.compile(r"\S*\d\S*|\"[^\"]*\"|'[^']*'|`[^`]*`|\b\w+\.(p|i|w|cls)\b")


def _shape(what: str) -> str:
    """`what` with everything that varies row to row (numbers, ids, quoted
    names, program names) replaced, so two rulings on different rows match."""
    return " ".join(_VARIES.sub("#", what.lower()).split())


# "Review and draft to <name>" (server.py's example) and its variants: Ben
# reviewing one email thread's draft. Each is about its own thread, so two
# of them are never duplicates, rows of one shape, or covers of each other
# by wording alone; only the same task (or a revision of it) repeats one.
_REVIEW_DRAFT = re.compile(r"^\s*review\b[\w\s,&/+-]{0,30}?\b(draft(ed|s)?|repl(y|ies)|e-?mails?|responses?)\b",
                           re.I)


def review_draft(what: str) -> bool:
    return bool(_REVIEW_DRAFT.search(what or ""))


SHAPE_MATCH = 0.85


def same_shape(a: str, b: str) -> bool:
    """One ruling per row: the same template with only the row's numbers,
    ids, quoted names or programs changed. Review-and-draft items never
    count: each is its own thread."""
    from difflib import SequenceMatcher
    if review_draft(a) or review_draft(b):
        return False
    x, y = _shape(a), _shape(b)
    # Short distinct asks ("Approve the plan" / "Approve the checklist") look
    # alike as text; a per-row ruling also names its row, id or program.
    if "#" not in x or "#" not in y:
        return False
    # The template left after stripping must match nearly whole, not only
    # its opening words ("Decide whether ticket # is billable" is not the
    # same ruling as "Decide whether ticket # needs a CVC").
    return x == y or SequenceMatcher(None, x, y).ratio() >= SHAPE_MATCH


def ungrouped(follow_ups: list[dict[str, Any]], client: str | None) -> list[str]:
    """The largest set of same-shape items a result hands Ben one by one, if
    it has GROUP_MIN or more; else []. Items routing would hand back to the
    client, and ones that already name a file, do not count."""
    mine = [f["what"] for f in follow_ups
            if str(f.get("who", "")).strip().casefold() == "ben"
            and not route_for(f["what"], client) and not _FILE_NAMED.search(f["what"])]
    best: list[str] = []
    for i, w in enumerate(mine):
        group = [w] + [o for o in mine[i + 1:] if same_shape(w, o)]
        if len(group) > len(best):
            best = group
    return best if len(best) >= GROUP_MIN else []


def check_grouped(follow_ups: list[dict[str, Any]], client: str | None) -> None:
    """Refuse a result that hands Ben the same ruling row by row."""
    group = ungrouped(follow_ups, client)
    if group:
        raise ValueError(
            f"follow_ups hands Ben {len(group)} rulings of the same shape, one per row "
            f"(\"{group[0][:60]}\", \"{group[1][:60]}\", ...). Group them: write the rows to one workbook "
            "(.xlsx, a column for his answer) under work/ and hand back ONE follow-up for Ben that names "
            "its path, so he rules on all of them in one pass. Rulings that really differ may stay separate.")


# --------------------------------------------- what lands on Ben, 2026-11-14
#
# The queue audit (docs/ben-queue-audit-2026-11-14.html) found 358 of 770
# items on Ben's desk did not need him. Five checks at landing, each behind
# a switch on the board (meta `switch:<name>`, `python -m coord_mcp.queue
# switch`), so one can be turned off without a release:
#
#   ben_test        a Ben follow-up must be his: it names a send, approval,
#                   scope or cost, access, or a judgement, or carries a
#                   `reason` tag saying which. Else refused.            (on)
#   ben_dedupe      refuse a Ben item that repeats an open one of the same
#                   client, not just one in the same result.             (on)
#   ben_cover       when a task's result re-raises an ancestor's Ben item,
#                   or names open ones it covers, those close behind a
#                   decision citing the task, instead of expiring.       (on)
#   ben_group_wide  the workbook rule across the client's open Ben items of
#                   the last GROUP_DAYS, not only within one result.     (on)
#   ben_precedent   settle a new Ben item an earlier Ben ruling clearly
#                   implies, as "master default; Ben may override".     (off)
#   hold_rule       coord_record_decision refuses a new hold on internal
#                   work behind a Ben question that has a default; the
#                   engine proceeds past such holds (holds.py).          (on)
#   hold_apply      allows `python -m coord_mcp.holds apply --yes` on the
#                   existing holds.                                      (off)
#   case_checks     coord_create_task / coord_complete_task warn on a task
#                   that looks like a step of an open case but has none, or
#                   skips a step not yet done (workflows/checks.py). Advice
#                   only, never a refusal.                                (on)
#   defer_return    the dashboard reopens a deferred loose end on its return
#                   date (coord_mcp/defer.py); dates are still recorded
#                   when it is off.                                       (on)
#   auto_promote    an approving dod_review promotes its deliverable to
#                   <client>/deliverables (store.auto_promote), and Ben's
#                   promote-only loose ends on it close; Promote's refusals
#                   still apply, and a refusal leaves his item.       (on)
#   auto_estimate   a sizing result whose `estimate` (low/high hours) is
#                   inside the band closes its Ben approval item behind
#                   "Estimate auto-accepted ..." (coord_mcp/estimate.py;
#                   band in SETTINGS below).                            (on)
#   workflow_propose the dashboard runs `python -m coord_mcp.workflows propose
#                   --file` once a week, Monday 06:30 local: up to 3
#                   workflow proposals in Ben's queue.                  (on)

REASONS = ("send", "approve", "scope", "access", "judgement")
SWITCHES = {"ben_test": True, "ben_dedupe": True, "ben_cover": True, "ben_group_wide": True,
            "ben_precedent": False,
            # holds behind Ben (coord_mcp/holds.py): a new hold on internal work
            # with a default is refused; the apply on existing holds is Ben's.
            "hold_rule": True, "hold_apply": False,
            "case_checks": True,
            # defer return dates (coord_mcp/defer.py): the dashboard reopens a
            # deferred item on its return date.
            "defer_return": True,
            # the weekly workflow miner (workflows/propose.py) files its proposals
            "workflow_propose": True,
            # an approving dod_review promotes its deliverable (store.auto_promote)
            "auto_promote": True,
            # the estimate band (coord_mcp/estimate.py, Ben 2026-11-15)
            "auto_estimate": True}
# Numbers behind the switches, as working defaults Ben may change (meta
# `setting:<name>`, `python -m coord_mcp.queue setting <name> <value>`).
SETTINGS = {"estimate_max_ratio": 1.5, "estimate_max_high": 24.0}
GROUP_DAYS = 7
TAGS_DDL = """
CREATE TABLE IF NOT EXISTS follow_up_tags (
  follow_up_id TEXT PRIMARY KEY REFERENCES follow_ups(id),
  reason       TEXT NOT NULL,
  created_at   INTEGER NOT NULL
);
"""


def switch(conn: sqlite3.Connection, name: str) -> bool:
    if name not in SWITCHES:
        raise KeyError(f"unknown switch {name!r}; known: {', '.join(SWITCHES)}")
    r = conn.execute("SELECT value FROM meta WHERE key=?", (f"switch:{name}",)).fetchone()
    return SWITCHES[name] if r is None else r["value"] == "on"


def set_switch(conn: sqlite3.Connection, name: str, on: bool) -> None:
    switch(conn, name)  # validates the name
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (f"switch:{name}", "on" if on else "off"))
    conn.commit()


def setting(conn: sqlite3.Connection, name: str) -> float:
    if name not in SETTINGS:
        raise KeyError(f"unknown setting {name!r}; known: {', '.join(SETTINGS)}")
    r = conn.execute("SELECT value FROM meta WHERE key=?", (f"setting:{name}",)).fetchone()
    try:
        return float(r["value"]) if r is not None else SETTINGS[name]
    except (TypeError, ValueError):
        return SETTINGS[name]


def set_setting(conn: sqlite3.Connection, name: str, value: float) -> None:
    setting(conn, name)  # validates the name
    if not value > 0:
        raise ValueError(f"{name} must be above 0")
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (f"setting:{name}", repr(float(value))))
    conn.commit()


def ensure_tags(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='follow_up_tags'").fetchone() is None:
        conn.executescript(TAGS_DDL)


def tag(conn: sqlite3.Connection, follow_up_id: str, reason: str) -> None:
    conn.execute("INSERT OR REPLACE INTO follow_up_tags VALUES (?,?,?)", (follow_up_id, reason, now()))


def reasons_for(conn: sqlite3.Connection, task_id: str) -> dict[str, str]:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='follow_up_tags'").fetchone() is None:
        return {}
    return {r["follow_up_id"]: r["reason"] for r in conn.execute(
        "SELECT g.follow_up_id, g.reason FROM follow_up_tags g JOIN follow_ups f ON f.id = g.follow_up_id "
        "WHERE f.task_id=?", (task_id,))}


def is_ben(who: Any) -> bool:
    return str(who or "").strip().casefold() == "ben"


def bens_own(what: str) -> bool:
    """The text itself names something only Ben does (send, approve, scope,
    cost, access, judgement)."""
    return bool(_PERSON.search(what) or _BEN_ACTS.search(what))


BEN_TEST = ("A follow-up for Ben must be something only he can do: send or publish (send), approve or sign "
            "off (approve), client-visible scope, cost or dates (scope), access only he has (access), or a "
            "judgement with no precedent (judgement). A read, query, test, edit or draft is yours: do it in "
            "this task or give it to your own client key. A question for a client person is a draft you "
            "write (who = your key, \"Draft a note to <name> asking ...\"), never \"ask X\" for Ben.")


def _ben_new(follow_ups: list[dict[str, Any]], client: str | None) -> list[dict[str, Any]]:
    """The Ben items of a result that would stay on him: routing does not
    move a tagged item, and moves an untagged one route_for names."""
    return [f for f in follow_ups if is_ben(f.get("who"))
            and (f.get("reason") or not route_for(f["what"], client))]


def check_ben_test(follow_ups: list[dict[str, Any]], client: str | None) -> None:
    bad = [f["what"] for f in _ben_new(follow_ups, client) if not f.get("reason") and not bens_own(f["what"])]
    if bad:
        raise ValueError(
            f"{len(bad)} follow-up(s) for Ben read as agent work: " + "; ".join(f'"{w[:80]}"' for w in bad[:3])
            + ". " + BEN_TEST + " If it truly is his, add \"reason\": one of " + "|".join(REASONS)
            + " to that follow-up (a session started before 2026-11-14 refuses the field: reword it to say "
            "what he must send, approve or decide instead).")


_DATEISH = re.compile(r"^\d{1,2}/\d{1,2}(/\d{2,4})?$|^\d{4}-\d{2}-\d{2}$|^[\d.]+-?[\d.]*h$|^\d+-\d+$|^\d+%$|^\d{1,2}$")
_BOARD_ID = re.compile(r"\b[0-9a-f]{12}\b")


def anchors(what: str) -> set[str]:
    """What an ask is about: tickets, item numbers, file names (not dates,
    hours, small counts or board ids)."""
    out = set()
    for w in re.findall(r"[#\w][\w.#/-]*\w", what or ""):
        lw = w.lower().lstrip("#")
        if _BOARD_ID.fullmatch(lw) or _DATEISH.match(lw):
            continue
        if _YEAR.match(lw) and not w.startswith("#"):
            continue  # "2026" dates an ask, it is not what the ask is about ("#2026" is a ticket)
        if re.search(r"\d", lw) or _FILE_NAMED.search(lw) or re.search(r"\.(p|i|w|cls)$", lw):
            out.add(lw)
    return out


_YEAR = re.compile(r"^(19|20)\d{2}$")
# Jaccard over the words left once anchors are stripped (_words). Raised from
# 0.25 on precedent.shape (review 2026-11-14): at 0.25 one shared anchor and
# "decide whether ticket" made "is billable" and "needs a CVC" one ask.
DUP_MATCH = 0.6
# Closing an older item as covered by a newer one needs the same question,
# not a related one on the same ticket.
COVER_MATCH = 0.75


def _words(what: str) -> frozenset[str]:
    """The wording of an ask with its anchors (numbers, files, quotes, ids)
    removed: lowercased words of 3+ letters, stop words out. Unlike
    precedent.shape it keeps capitalised words (CVC, PROD, a person), which
    are often what tells two asks apart."""
    from .workflows.precedent import STOP, _ENT
    s = what or ""
    for rx, sub in _ENT:
        s = rx.sub(sub, s)
    return frozenset(w for w in (x.lower() for x in re.findall(r"[A-Za-z][A-Za-z'-]*", s))
                     if len(w) >= 3 and w not in STOP)


def _sim_words(a: str, b: str) -> float:
    x, y = _words(a), _words(b)
    return len(x & y) / len(x | y) if x and y else 0.0


def duplicate(a: str, b: str, same_task: bool = False) -> bool:
    """The same ask twice: they share an anchor (ticket, item, file) and
    their wording overlaps. Per-row rulings on different rows share no anchor.
    Two review-and-draft items are each their own thread: a repeat only
    within one task (same_task)."""
    if review_draft(a) and review_draft(b) and not same_task:
        return False
    if not anchors(a) & anchors(b):
        return _shape(a) == _shape(b) and "#" not in _shape(a)
    return _sim_words(a, b) >= DUP_MATCH


def same_ask(a: str, b: str) -> bool:
    """The newer item re-asks the older one (ben_cover on a lineage): the
    same anchors, or none on either side, and nearly the same wording. A
    shared ticket with a different question ("is billable" / "needs a CVC")
    is not a cover."""
    xa, xb = anchors(a), anchors(b)
    if (xa or xb) and not xa & xb:
        # A revision may bump a version ("v5" -> "v6") but keeps the ticket.
        return False
    return _sim_words(a, b) >= COVER_MATCH


def lineage(conn: sqlite3.Connection, task_id: str | None) -> list[str]:
    """task_id and its ancestors (parent chain, and the code a check-in checks in)."""
    out, tid = [], task_id
    while tid and tid not in out:
        out.append(tid)
        r = conn.execute("SELECT parent_id, spec FROM tasks WHERE id=?", (tid,)).fetchone()
        if r is None:
            break
        try:
            spec = json.loads(r["spec"] or "{}")
        except ValueError:
            spec = {}
        tid = r["parent_id"] or (spec.get("checkin_of") if isinstance(spec, dict) else None)
    return out


def _open_for(conn: sqlite3.Connection, client: str | None, since: int | None = None) -> list[sqlite3.Row]:
    if not client:
        return []
    rows = [r for r in _ben_rows(conn) if (r["client"] or "").strip().casefold() == client.strip().casefold()]
    return [r for r in rows if since is None or r["created_at"] >= since]


def check_landing(conn: sqlite3.Connection, follow_ups: list[dict[str, Any]], client: str | None,
                  lineage_ids: list[str]) -> None:
    """The refusals at completion, nothing written: ben_test, ben_dedupe,
    ben_group_wide. lineage_ids is the finishing task and its ancestors
    (a repeat of an ancestor's item is a revision, closed by settle_landing)."""
    if switch(conn, "ben_test"):
        check_ben_test(follow_ups, client)
    mine = [f["what"] for f in _ben_new(follow_ups, client)]
    if not mine:
        return
    if switch(conn, "ben_dedupe"):
        for i, w in enumerate(mine):
            for o in mine[i + 1:]:
                if duplicate(w, o, same_task=True):
                    raise ValueError(f'follow_ups hands Ben the same ask twice ("{w[:60]}", "{o[:60]}"). '
                                     "Merge them into one item.")
        ours = set(lineage_ids)
        for w in mine:
            for r in _open_for(conn, client):
                if r["task_id"] not in ours and duplicate(w, r["what"]):
                    raise ValueError(
                        f'follow-up for Ben "{w[:80]}" repeats his open loose end {r["id"]} '
                        f'(task {r["task_id"] or "none"}: "{r["what"][:80]}"). Leave it out: it is already on '
                        "his queue. If yours adds something it lacks, say so in your summary; if it is a "
                        "different ask, word it so it names what differs.")
    if switch(conn, "ben_group_wide"):
        pool = [f for f in mine if not _FILE_NAMED.search(f)]
        old = [r for r in _open_for(conn, client, now() - GROUP_DAYS * 86400)
               if not _FILE_NAMED.search(r["what"]) and route_for(r["what"], client) is None]
        for w in pool:
            same = [r for r in old if same_shape(w, r["what"])]
            if len(same) + sum(1 for o in pool if same_shape(w, o)) >= GROUP_MIN:
                ids = ", ".join(r["id"] for r in same[:6])
                raise ValueError(
                    f'follow-up for Ben "{w[:70]}" is one more ruling of the same shape as {len(same)} open '
                    f"one(s) of {client}'s from the last {GROUP_DAYS} days ({ids}). Group them: write these rows "
                    "and yours to one workbook (.xlsx, a column for his answer) under work/, and hand back ONE "
                    f"follow-up for Ben naming its path and saying it covers {ids or 'them'} (\"covers <id>, <id>\"); "
                    "those then close behind it.")


_COVERS = re.compile(r"\b(covers?|fold(s|ed)?|replac(es|ing)|supersed(es|ing))\b", re.I)


def settle_landing(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    """After a result's loose ends are written (inside its transaction):
    ben_cover, then ben_precedent. Returns what it closed, for the result."""
    from . import store
    out: dict[str, Any] = {}
    new = _ben_rows(conn, task_id)
    if not new:
        return out
    client = new[0]["client"]
    line = lineage(conn, task_id)
    if switch(conn, "ben_cover"):
        covered = []
        new_ids = {r["id"] for r in new}
        for old in _open_for(conn, client):
            if old["id"] in new_ids:
                continue
            by = None
            for r in new:
                named = set(_BOARD_ID.findall(r["what"]))
                if old["id"] in named and _COVERS.search(r["what"]):
                    by = r
                elif old["task_id"] in line[1:] and same_ask(r["what"], old["what"]):
                    by = r
                if by:
                    break
            if not by:
                continue
            dec = store.record_decision(
                conn, f"Covered: Ben loose end {old['id']} closed, task {task_id} re-raised it as {by['id']}"[:200],
                f"ben_cover: newer item on the same {'lineage' if old['task_id'] in line else 'client'}; "
                "the newer one stays on his queue", old["task_id"])
            store.resolve_follow_up(conn, old["id"], decision_id=dec["decision_id"])
            covered.append({"follow_up": old["id"], "by": by["id"], "decision": dec["decision_id"]})
        if covered:
            out["covered"] = covered
    if switch(conn, "ben_precedent"):
        settled = []
        for r in _ben_rows(conn, task_id):
            hit = ruling_for(conn, r["what"], client, line)
            if not hit:
                continue
            dec = store.record_decision(
                conn, f"Master default: Ben loose end {r['id']} settled by Ben's ruling {hit['decision_id']}"[:200],
                f"{hit['why']}; master default; Ben may override"[:200], task_id)
            store.resolve_follow_up(conn, r["id"], decision_id=dec["decision_id"])
            settled.append({"follow_up": r["id"], "ruling": hit["decision_id"], "decision": dec["decision_id"]})
        if settled:
            out["settled_by_ruling"] = settled
    if switch(conn, "auto_estimate"):
        from . import estimate
        if accepted := estimate.settle(conn, task_id):
            out["estimate_auto_accepted"] = accepted
    return out


# A decision that records Ben's ruling: his answer from a button or /queue,
# or master writing down what he said.
_RULING = re.compile(r"\b(Ben (ruled|rules|chose|approved|answered|said|decided|accepted|rejected|dropped|deferred)"
                     r"|per Ben|Ben's (ruling|answer|call|direction|decision))\b", re.I)
PRECEDENT_MATCH = 0.5


def ruling_for(conn: sqlite3.Connection, what: str, client: str | None,
               line: list[str]) -> dict[str, Any] | None:
    """An earlier Ben ruling that clearly settles `what`, or None. Clearly:
    either a ruled Ben loose end of the same client with the same shape and
    one outcome (workflows.precedent, a drop), or a ruling recorded on this
    task's lineage that shares its anchor and most of its wording."""
    from .workflows import precedent as p
    sh = p.shape(what, client)
    hits = [x for x in p.precedents(conn) if (x["client"] or "").casefold() == (client or "").casefold()
            and p._sim(sh, x["shape"]) >= p.MATCH]
    if hits and {x["state"] for x in hits} == {"dropped"}:
        x = max(hits, key=lambda x: p._sim(sh, x["shape"]))
        return {"decision_id": x["resolved_by"], "why": f"same ask as {x['id']}, which Ben's ruling closed"}
    if hits:
        return None  # mixed or tasked outcomes stay Ben's
    if not line:
        return None
    marks = ",".join("?" * len(line))
    ours = anchors(what)
    for d in conn.execute(
            f"""SELECT id, statement, because FROM decisions WHERE task_id IN ({marks})
                AND id NOT IN (SELECT supersedes FROM decisions WHERE supersedes IS NOT NULL)
                ORDER BY created_at DESC""", line):
        text = f"{d['statement']} {d['because'] or ''}"
        if not _RULING.search(text) or not ours & anchors(text):
            continue
        if p._sim(sh, p.shape(_RULING.sub(" ", d["statement"]), client)) >= PRECEDENT_MATCH:
            return {"decision_id": d["id"], "why": f"Ben's ruling {d['id']} on this task's lineage covers it"}
    return None


def landing_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """Read-only: what ben_cover and ben_precedent would close in Ben's open
    queue as it stands, if every item landed now. For Ben to read before a
    sweep or before turning ben_precedent on."""
    covered, settled = [], []
    rows = _ben_rows(conn)
    for old in rows:
        for r in rows:
            if r["id"] == old["id"] or r["created_at"] < old["created_at"] or not r["task_id"]:
                continue
            if old["task_id"] and old["task_id"] in lineage(conn, r["task_id"])[1:] and \
                    same_ask(r["what"], old["what"]):
                covered.append({"id": old["id"], "client": old["client"], "by": r["id"], "what": old["what"]})
                break
    for r in rows:
        hit = ruling_for(conn, r["what"], r["client"], lineage(conn, r["task_id"]))
        if hit:
            settled.append({"id": r["id"], "client": r["client"], "ruling": hit["decision_id"], "what": r["what"]})
    return {"covered": covered, "settled": settled}


EXPIRE_DAYS = 14
EXPIRE_RETURN_DAYS = 10  # business days an expired item stays off the queue before it returns
EXPIRE_EVERY = 86400
EXPIRE_META = "ben_expire_last"
EXPIRED_DDL = """
CREATE TABLE IF NOT EXISTS follow_up_expiry (
  follow_up_id TEXT PRIMARY KEY REFERENCES follow_ups(id),
  decision_id  TEXT NOT NULL,
  expired_at   INTEGER NOT NULL
);
"""


def ensure_expiry(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='follow_up_expiry'").fetchone() is None:
        conn.executescript(EXPIRED_DDL)


def stale(conn: sqlite3.Connection, now_ts: int | None = None, days: int = EXPIRE_DAYS) -> list[dict[str, Any]]:
    """Ben's open loose ends older than `days` that nothing dates or holds.

    Kept: anything with a due date (its own, its task's, a commitment's), a
    draft reply waiting on him, the question currently presented, and one
    already expired that Ben or master reopened by hand (--undo). One that
    came back on its return date (defer.py) may expire again, `days` after
    it returned (Ben, 2026-11-14)."""
    from . import commitments, store
    t = now() if now_ts is None else now_ts
    ensure_expiry(conn)
    q = store.current_question(conn)
    dues = commitments.due_map(conn)
    held = {r["follow_up_id"] for r in conn.execute(
        "SELECT follow_up_id FROM commitments WHERE state IN ('open','slipped') AND follow_up_id IS NOT NULL")} \
        if dues else set()
    from . import defer as defer_mod
    defer_mod.ensure(conn)
    once = {r["follow_up_id"] for r in conn.execute("SELECT follow_up_id FROM follow_up_expiry")}
    back = {r[0]: r[1] for r in conn.execute(
        "SELECT follow_up_id, returned_at FROM follow_up_returns WHERE returned_at IS NOT NULL")}
    replies = {r["id"] for r in conn.execute("SELECT id FROM tasks WHERE kind='draft_reply'")}
    out = []
    for r in _ben_rows(conn):
        since = max(r["created_at"], back.get(r["id"]) or 0)
        if since > t - days * 86400 or (r["id"] in once and r["id"] not in back) or r["id"] in held:
            continue
        if r["due_at"] is not None or (r["task_id"] and (r["task_id"] in dues or r["task_id"] in replies)):
            continue
        if q and q.get("follow_up_id") == r["id"]:
            continue
        out.append({"id": r["id"], "client": r["client"] or "unassigned", "what": r["what"],
                    "age_days": (t - since) // 86400})
    return out


def expire(conn: sqlite3.Connection, apply: bool = False, now_ts: int | None = None,
           days: int = EXPIRE_DAYS) -> dict[str, Any]:
    """Defer every stale Ben loose end behind one decision, each returning
    EXPIRE_RETURN_DAYS business days later. Dry run unless `apply`."""
    from . import defer as defer_mod, store
    t = now() if now_ts is None else now_ts
    rows = stale(conn, t, days)
    by: dict[str, int] = {}
    for r in rows:
        by[r["client"]] = by.get(r["client"], 0) + 1
    ret = defer_mod.business_days_out(EXPIRE_RETURN_DAYS, t)
    out: dict[str, Any] = {"ok": True, "applied": False, "count": len(rows), "by_client": by, "items": rows,
                           "return_on": defer_mod.day(ret)}
    if not apply or not rows:
        return out
    split = ", ".join(f"{c} {n}" for c, n in sorted(by.items(), key=lambda kv: -kv[1]))
    dec = store.record_decision(
        conn, f"Auto-deferred {len(rows)} Ben loose end(s) open over {days} days: {split}"[:200],
        f"queue expiry rule, Ben 2026-11-13: undated items leave the active queue and return on "
        f"{defer_mod.day(ret)} (defer return dates, 2026-11-14); "
        "python -m coord_mcp.queue expire --undo <this id>")
    for r in rows:
        store.resolve_follow_up(conn, r["id"], decision_id=dec["decision_id"], defer=True, return_on=ret)
        conn.execute("INSERT OR REPLACE INTO follow_up_expiry VALUES (?,?,?)", (r["id"], dec["decision_id"], t))
    conn.commit()
    return {**out, "applied": True, "decision_id": dec["decision_id"]}


def unexpire(conn: sqlite3.Connection, decision_id: str) -> dict[str, Any]:
    """Reopen what one expiry decision deferred and is still deferred. They
    stay marked as expired once, so the next pass leaves them alone."""
    from . import store
    ensure_expiry(conn)
    ids = [r["id"] for r in conn.execute(
        """SELECT f.id FROM follow_up_expiry e JOIN follow_ups f ON f.id = e.follow_up_id
           WHERE e.decision_id=? AND f.state='deferred' AND f.resolved_by=?""", (decision_id, decision_id))]
    for i in ids:
        store.resolve_follow_up(conn, i, reopen=True)
        # Reopened by hand: it stays, so it must not read as returned by date.
        conn.execute("UPDATE follow_up_returns SET returned_at=NULL WHERE follow_up_id=?", (i,))
    conn.commit()
    return {"ok": True, "reopened": len(ids), "decision_id": decision_id}


def expire_due(conn: sqlite3.Connection, now_ts: int | None = None) -> dict[str, Any] | None:
    """The dashboard's daily pass: expire once per EXPIRE_EVERY. The first
    pass on a board only stamps the clock, so the first sweep of an existing
    queue is one Ben runs by hand after reading the dry run."""
    t = now() if now_ts is None else now_ts
    r = conn.execute("SELECT value FROM meta WHERE key=?", (EXPIRE_META,)).fetchone()
    if r is not None and t - int(r["value"]) < EXPIRE_EVERY:
        return None
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (EXPIRE_META, str(t)))
    conn.commit()
    return expire(conn, apply=True, now_ts=t) if r is not None else None


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
            from .holds import pending_gate
            if pending_gate(conn, d["task_id"]):
                continue  # on a master default: Outlook waits for Ben's answer (holds.py)
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
    from .projects import single_pipeline  # the project's one item, when it has exactly one
    return single_pipeline(conn, task_id)


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


# --------------------------------------------------- pipeline notes in code
#
# The note master adds by hand (manual: Pipeline notes) is one factual line:
# what landed, its name or version, the date. The board writes it
# (pipeline_note_text), the master-console mod claims one at a time
# (/api/pipeline-note/claim), adds it with sharepoint-pipeline's
# add_task_note, and reports back (/api/pipeline-note/done); an ok records the
# same `Pipeline note <id>: <note>` decision that clears pipeline_owed.

NOTE_LEASE_KEY = "pipeline_note_lease"
NOTE_ERRORS_KEY = "pipeline_note_errors"
NOTE_LEASE = 120          # a claim not reported done within this is free again
NOTE_RETRY = 30 * 60      # a note SharePoint refused waits this long before another try
NOTE_BECAUSE = "added to SharePoint by the master-console mod from the board; master default"
_CVC = re.compile(r"\bCVC[\w-]*\d[\w-]*", re.I)
_TICKET_KEYS = ("cvc", "cvc_ticket", "ticket", "qsm", "jira")


def _day(ts: int | None) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts or now()).strftime("%Y-%m-%d")


def _ticket(spec: dict[str, Any], result: dict[str, Any]) -> str | None:
    """The CVC or ticket a code change names: a spec field, else the first
    CVC id in its result's summary, notes or branch, or its spec's text."""
    for k in _TICKET_KEYS:
        v = spec.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:40]
    for text in (result.get("summary"), result.get("notes"), result.get("branch"), json.dumps(spec)):
        if isinstance(text, str) and (m := _CVC.search(text)):
            return m.group(0)
    return None


def pipeline_note_text(conn: sqlite3.Connection, task_id: str) -> str:
    """One factual line for the task's pipeline item: the date, what landed,
    its name or version, and nothing else (never the agent's summary). A promoted deliverable names its promoted copy
    (<name>-vN); code names its CVC or ticket when the result or spec does.
    Short enough that the decision `Pipeline note <id>: <note>` fits 200."""
    from .db import ensure_promotions
    r = conn.execute("SELECT kind, title, spec, result, completed_at FROM tasks WHERE id=?", (task_id,)).fetchone()
    if r is None:
        raise KeyError(f"unknown task '{task_id}'")
    spec, result = json.loads(r["spec"] or "{}"), json.loads(r["result"] or "{}")
    if r["kind"] == "deliverable":
        ensure_promotions(conn)
        p = conn.execute("SELECT dest, version, created_at FROM promotions WHERE task_id=? "
                         "ORDER BY created_at DESC, rowid DESC LIMIT 1", (task_id,)).fetchone()
        if p is None:
            raise ValueError(f"task {task_id} has not been promoted")
        from pathlib import PureWindowsPath
        line = f"{_day(p['created_at'])}: delivered {PureWindowsPath(p['dest']).name} ({r['title']})"
    else:
        ticket = _ticket(spec, result)
        what = "checked in" if spec.get("checkin_of") or not spec.get("cvc") else "built"
        # Not the result's summary: SharePoint is client-visible and a summary
        # is written for master. The ticket is read from it, nothing else.
        line = f"{_day(r['completed_at'])}: {what} {r['title']}" + (f" ({ticket})" if ticket else "")
    pid = pipeline_id(conn, task_id) or ""
    room = 200 - len(f"{PIPELINE_NOTE} {pid}: ")
    return line if len(line) <= room else line[:room - 3].rstrip() + "..."


def _meta_json(conn: sqlite3.Connection, key: str) -> Any:
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    try:
        return json.loads(r["value"]) if r else None
    except ValueError:
        return None


def _set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    if value is None:
        conn.execute("DELETE FROM meta WHERE key=?", (key,))
    else:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, json.dumps(value)))


def pipeline_note_claim(conn: sqlite3.Connection, now_ts: int | None = None) -> dict[str, Any] | None:
    """One owed note under a lease, or None: a claim is live, nothing is
    owed, or what is owed has a pipeline id that is not a SharePoint item
    number (left for master) or was refused within NOTE_RETRY.
    {task_id, pipeline_id, item_id, note}."""
    from .db import ensure_projects, ensure_promotions
    t = now() if now_ts is None else now_ts
    ensure_promotions(conn)  # before BEGIN: executescript would commit it
    ensure_projects(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        lease = _meta_json(conn, NOTE_LEASE_KEY)
        if isinstance(lease, dict) and int(lease.get("until") or 0) > t:
            conn.execute("COMMIT")
            return None
        errors = _meta_json(conn, NOTE_ERRORS_KEY) or {}
        out = None
        for tid in pipeline_owed(conn, t):
            if t - int(errors.get(tid) or 0) < NOTE_RETRY:
                continue
            pid = pipeline_id(conn, tid) or ""
            if not pid.strip().isdigit():
                continue
            out = {"task_id": tid, "pipeline_id": pid, "item_id": int(pid), "note": pipeline_note_text(conn, tid)}
            _set_meta(conn, NOTE_LEASE_KEY, {**out, "until": t + NOTE_LEASE})
            break
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return out


def pipeline_note_done(conn: sqlite3.Connection, task_id: str, status: str, detail: str | None = None,
                       now_ts: int | None = None) -> dict[str, Any]:
    """Close the claim. ok records the decision `Pipeline note <id>: <note>`
    on the task, with the note exactly as claimed; error frees it to retry
    after NOTE_RETRY."""
    from . import store
    t = now() if now_ts is None else now_ts
    if status not in ("ok", "error"):
        raise ValueError(f"status must be ok or error, not {status!r}")
    lease = _meta_json(conn, NOTE_LEASE_KEY)
    if not isinstance(lease, dict) or lease.get("task_id") != task_id:
        raise ValueError(f"task {task_id!r} has no open pipeline-note claim")
    _set_meta(conn, NOTE_LEASE_KEY, None)
    if status == "error":
        errors = _meta_json(conn, NOTE_ERRORS_KEY) or {}
        errors = {k: v for k, v in errors.items() if t - int(v) < NOTE_RETRY}
        errors[task_id] = t
        _set_meta(conn, NOTE_ERRORS_KEY, errors)
        return {"ok": True, "task_id": task_id, "status": "error", "detail": (detail or "")[:200]}
    dec = store.record_decision(conn, f"{PIPELINE_NOTE} {lease['pipeline_id']}: {lease['note']}"[:200],
                                NOTE_BECAUSE, task_id)
    return {"ok": True, "task_id": task_id, "status": "ok", "decision_id": dec["decision_id"]}


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

def _print_settings(conn: sqlite3.Connection) -> None:
    for k, d in SETTINGS.items():
        v = setting(conn, k)
        print(f"{k:<19} {v:g}" + ("" if v == d else f"  (default {d:g})"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m coord_mcp.queue",
                                description="Route agent work off Ben's queue.")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill", help="list (or with --apply, move) Ben loose ends that are agent work")
    b.add_argument("--apply", action="store_true")
    e = sub.add_parser("expire", help="list (or with --apply, defer) Ben loose ends open too long with no date")
    e.add_argument("--apply", action="store_true")
    e.add_argument("--days", type=int, default=EXPIRE_DAYS)
    e.add_argument("--undo", metavar="DECISION_ID", help="reopen what that expiry decision deferred")
    w = sub.add_parser("switch", help="show the Ben-queue landing switches, or set one on/off")
    w.add_argument("name", nargs="?", choices=list(SWITCHES))
    w.add_argument("state", nargs="?", choices=["on", "off"])
    g = sub.add_parser("setting", help="show the numbers behind the switches, or set one")
    g.add_argument("name", nargs="?", choices=list(SETTINGS))
    g.add_argument("value", nargs="?", type=float)
    sub.add_parser("landing", help="dry run: Ben items ben_cover and ben_precedent would close now")
    a = p.parse_args(argv)
    conn = connect()
    try:
        if a.cmd == "switch":
            if a.name and a.state:
                set_switch(conn, a.name, a.state == "on")
            for k in SWITCHES:
                print(f"{k:<15} {'on' if switch(conn, k) else 'off'}" + ("" if switch(conn, k) == SWITCHES[k]
                                                                       else "  (default " + ("on" if SWITCHES[k] else "off") + ")"))
            _print_settings(conn)
            return 0
        if a.cmd == "setting":
            if a.name and a.value is not None:
                set_setting(conn, a.name, a.value)
            _print_settings(conn)
            return 0
        if a.cmd == "landing":
            rep = landing_report(conn)
            for r in rep["covered"]:
                print(f"covered  {r['id']}  {r['client'] or '':<9} by {r['by']}  {r['what'][:90]}")
            for r in rep["settled"]:
                print(f"ruling   {r['id']}  {r['client'] or '':<9} by {r['ruling']}  {r['what'][:90]}")
            print(f"{len(rep['covered'])} covered, {len(rep['settled'])} settled by a ruling (dry run; nothing changed)")
            return 0
        if a.cmd == "expire":
            if a.undo:
                print(f"{unexpire(conn, a.undo)['reopened']} reopened")
                return 0
            out = expire(conn, apply=a.apply, days=a.days)
            for r in out["items"]:
                print(f"{r['id']}  {r['client']:<9} {r['age_days']:>3}d  {r['what'][:100]}")
            for c, n in sorted(out["by_client"].items(), key=lambda kv: -kv[1]):
                print(f"  {c}: {n}")
            print(f"{out['count']} " + (f"deferred, decision {out['decision_id']}" if out["applied"]
                                        else "would be deferred (dry run; --apply to defer)")
                  + f", returning {out['return_on']}")
            return 0
        rows = backfill(conn, apply=a.apply)
    finally:
        conn.close()
    for r in rows:
        print(f"{r['id']}  {r['client']:<9} {r['why']:<5} {r['what'][:110]}")
    print(f"{len(rows)} {'moved' if a.apply else 'would move (dry run; --apply to move)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
