"""Holds behind Ben become working defaults (the queue audit's sixth item).

The 2026-11-14 audit found 158 live decisions holding work until Ben ruled or
acted, 71 of them recorded as "master default" holds. Ben is in meetings most
of the day; internal work should not wait on him when the question already
has a default. The rule (Ben, 2026-11-14):

  A hold behind Ben is kept only for outward-facing work that turns on his
  answer: a send, Outlook drafting of a reply whose content depends on the
  ruling, a check-in, a Promote, SharePoint status, a tracker post, anything
  client-visible. Internal work (a draft, an investigation, a revision, a
  data pull, code built but not checked in) proceeds on the default, behind a
  decision "master default; Ben may override" that supersedes the hold.
  Ben's question stays in his queue, so he can still overrule.

What is here:

- Detection (`find_holds`, read-only): non-superseded decisions that hold
  or defer work until Ben rules or acts, the work they hold, the default the
  question carries (the presented question's recommended option, else the
  text of his loose end or of the hold), and whether the held work is
  internal or outward-facing. `python -m coord_mcp.holds report` lists it;
  `--html/--xlsx` write the report.
- Going forward (`check_new_hold`, switch `hold_rule`, on): coord_record_decision
  refuses a new hold on internal work whose Ben question has a default, and
  says what to record instead. A decision that says "master default; Ben may
  override" and names his still-open question is registered as proceeding on
  a default (`register`), which gates the outward steps below.
- Gates (`pending_gate`): while the Ben question behind a default is open,
  Draft in Outlook and a check-in of the work it proceeds on are refused, and
  the minder does not list such a draft as owed. A Promote stays Ben's own
  button and is not gated.
- Existing holds (`apply`, switch `hold_apply`, off): for each internal hold
  with a default, record the superseding decision, reopen the loose ends it
  deferred, and register the gate. `python -m coord_mcp.holds apply` is a dry
  run; `--yes` writes, and refuses while the switch is off.
- The workflow engine calls `case_hold` before spawning: an outward hold, or
  a hold with no default, stops the case; an internal hold with a default
  proceeds only under `hold_rule`, and the engine records the default.

Schema: one side table, `hold_defaults`, created on first use. No version bump.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import sys
from datetime import datetime
from typing import Any

from .db import connect, now

# ------------------------------------------------------------- detection

# Snapshots of state, not holds: the handoff a master writes before a clear
# lists what waits, but the hold itself is in an earlier decision.
_SNAPSHOT = re.compile(r"^\s*(handoff|hand-off|master state|worker|state|cleared|auto-clear|self-clear|"
                       r"board cleanup|cleanup)\b", re.I)
_HOLD_VERB = re.compile(r"\b(hold|holds|held|holding|defer|defers|deferred|deferring|park|parks|parked|"
                        r"keep\b[^.;]{0,60}\b(open|back)|waits?|waiting|stays? (open|held|parked)|blocked|gated)\b", re.I)
_LIFT = re.compile(r"\b(lift(s|ed)?|release[sd]?|unhold|supersedes?\b[^.;]{0,40}\bhold|no longer held)\b", re.I)
# The clause after the trigger word: "until Ben rules ...", "pending Ben's ...", "waits on Ben q55".
_TRIGGER = re.compile(r"\b(until|till|pending|after|once|when|waits? (on|for)|waiting (on|for)|behind|"
                      r"gated (on|by)|blocked (on|by)|for)\b(?P<rest>[^.;]{0,160})", re.I)
_BEN = re.compile(r"\bBen(?:'s)?\b")
_ID = re.compile(r"(?<![0-9a-f])[0-9a-f]{12}(?![0-9a-f])")
_QID = re.compile(r"(?<![0-9A-Za-z])q(\d{1,5})(?![0-9])")
# What Ben must DO (not decide): no answer can stand in for these.
_ACTION = re.compile(r"\bBen(?:'s)?\s+(?:\w+\s+){0,2}?(sync|syncs|synced|add|adds|added|get|gets|refresh|refreshes|"
                     r"grant|grants|install|installs|sign|signs|apply|applies|applied|upload|uploads|log|logs|"
                     r"send|sends|sent|promote|promotes|promoted|swap|swaps|replace|replaces|open|opens|call|"
                     r"calls|meet|meets|share|shares|provide|provides|request|requests|enable|enables|connect|"
                     r"credential|access|login|VPN|session|restart|restarts|post|posts|email|emails|reply|replies|"
                     r"talks?|asks?|hears?|pushes|checks? in)\b", re.I)
_MD = re.compile(r"\bmaster default\b", re.I)
# A decision proceeding on a default is the opposite of a hold, whatever verbs
# its tail carries ("...; Ben's <id> stays open").
_PROCEED_STMT = re.compile(r"^\s*proceed(s|ing)? on (the |a )?master default\b|"
                           r"\bmaster default; Ben may overrid", re.I)
# A decision recording Ben's own ruling, e.g. that the work should wait for
# him: "Per Ben's answer: hold X until Ben rules q5". The hold is his call, so
# no rule turns it into a default. The ruling markers of queue._RULING plus
# "Ben:" and "Answer on <id>"; present-tense "Ben rules" is left out, since it
# is the trigger of an ordinary hold ("until Ben rules").
_BEN_RULING = re.compile(
    r"\b(Ben (ruled|chose|approved|answered|said|decided|accepted|rejected|dropped|deferred|asked|directed|wants)\b"
    r"|per Ben\b|as Ben (said|asked|directed)\b|Ben's (ruling|answer|call|direction|decision|instruction)\b"
    r"|Ben:|Answer on\b)", re.I)
_RULING_LEAD = re.compile(r"^\s*(?:(?:desk|master|worker)\s*:\s*)?(?:on\s+)?" + _BEN_RULING.pattern, re.I)

# What makes work outward-facing: it leaves Logan, or the client can see it.
_OUTWARD = re.compile(
    r"\b(send|sends|sent|sending|e-?mail (it|the)|post(s|ed|ing)? (to|on|in)|publish|"
    r"check[- ]?ins?|checked in|check it in|promote[sd]?|promotion|SharePoint|Jira|ServiceNow|RAID comment|"
    r"tracker (post|comment|update)|Outlook|deliver(s|ed|y)? to|go(es)? out|client[- ]visible|"
    r"outward[- ]facing|in front of (the )?client|walk(ed)? through with|to the client|QSM|ticket comment|"
    r"status (field|update) (in|on) SharePoint|pipeline (status|dates|estimate))\b", re.I)
# Internal work words, used when the held work names no task kind.
_INTERNAL = re.compile(
    r"\b(draft|drafts|drafting|revise|revision|rework|re-?cut|rerun|re-?run|investigat\w*|read|query|pull|"
    r"build|compile|fix|fixes|update|edit|reconcile|analy[sz]\w*|script|spec|r\d+\b|round|write|rewrite|"
    r"re-?generate|test|harness|research|check|verify|DEVL|review)\b", re.I)

INTERNAL_KINDS = ("investigation", "data_pull", "dod_review")

# A default in the words of a Ben loose end or a hold.
_DEFAULT = re.compile(
    r"(?P<lead>\b(master default|default(s)?|recommend(s|ed|ation)?|propos(e|ed|al)|suggest(s|ed)?|"
    r"accept (the )?(README )?defaults?|as[- ]is|keep (the|it)|agent[- ]proposed|working figure)\b)"
    r"(?P<rest>[^.;)]{0,140})", re.I)


_NOT_A_DEFAULT = re.compile(r"\s*(defer|later|wait|hold|drop|park|not now|ask|escalate|saved|(yes,? )?I('ll| will|'ve| have))\b", re.I)


def _clause(text: str, m: re.Match) -> str:
    """The clause a default match sits in: back to the previous separator."""
    start = max(text.rfind(c, 0, m.start()) for c in ",;:?(")
    start = start + 1 if start >= 0 and m.start() - start <= 100 else m.start()
    if len(text[start:m.start()].strip()) < 3 and start > 0:  # "A (recommended)": the option is before the mark
        prev = max(text.rfind(c, 0, start - 1) for c in ",;:?(")
        start = prev + 1 if prev >= 0 else 0
    return (text[start:m.start()] + m.group("lead") + m.group("rest")).strip(" )")[:160]


def _ids(text: str) -> list[str]:
    seen: list[str] = []
    for i in _ID.findall(text or ""):
        if i not in seen:
            seen.append(i)
    return seen


def trigger_clauses(statement: str) -> list[str]:
    return [m.group("rest") for m in _TRIGGER.finditer(statement or "")]


def _fu(conn: sqlite3.Connection, fid: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM follow_ups WHERE id=?", (fid,)).fetchone()


def _task(conn: sqlite3.Connection, tid: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()


def _is_ben(who: Any) -> bool:
    return str(who or "").strip().casefold() == "ben"


def waits_on_ben(conn: sqlite3.Connection, statement: str) -> dict[str, Any] | None:
    """The Ben side of a hold: which of his loose ends or questions it waits
    on, and whether it waits on a ruling or an action. None when the hold's
    trigger does not involve Ben."""
    clauses = trigger_clauses(statement)
    if not clauses:
        return None
    ben_ids, qids, named, tasks = [], [], False, []
    for cl in clauses:
        if _BEN.search(cl):
            named = True
        for i in _ids(cl):
            f = _fu(conn, i)
            if f is not None and _is_ben(f["who"]) and i not in ben_ids:
                ben_ids.append(i)
            elif f is None and _BEN.search(cl) and _task(conn, i) is not None:
                tasks.append(i)  # "until Ben rules the round-3 question on <task>"
        qids += [int(q) for q in _QID.findall(cl)]
    if not (named or ben_ids or qids):
        return None
    trig = " ".join(clauses)
    kind = "action" if _ACTION.search(trig) and not re.search(r"\bBen(?:'s)?\s+(?:\w+\s+){0,2}?(rul|decid|answer|"
                                                              r"call|choice|confirm|go\b|approv|pick|chooses)",
                                                              trig, re.I) else "ruling"
    return {"ben_follow_ups": ben_ids, "questions": qids, "tasks": tasks, "type": kind}


def is_hold(statement: str) -> bool:
    s = statement or ""
    return (bool(_HOLD_VERB.search(s)) and not _SNAPSHOT.search(s) and not _LIFT.search(s)
            and not _PROCEED_STMT.search(s))


def records_ben_ruling(statement: str) -> bool:
    """True when the decision writes down Ben's own ruling: a ruling marker at
    the start, or anywhere outside the hold's trigger clauses ("pending Ben's
    ruling" is a trigger, not a ruling)."""
    s = statement or ""
    if _RULING_LEAD.search(s):
        return True
    # A trigger clause ends at its comma: "until Ben rules q5, as Ben asked".
    spans = []
    for m in _TRIGGER.finditer(s):
        a, b = m.span("rest")
        comma = s.find(",", a, b)
        spans.append((a, comma if comma >= 0 else b))
    return any(not any(a <= m.start() < b for a, b in spans) for m in _BEN_RULING.finditer(s))


def held_work(conn: sqlite3.Connection, d: sqlite3.Row, ben_ids: list[str]) -> list[dict[str, Any]]:
    """Open work a hold keeps back: loose ends it deferred, loose ends and
    tasks it names (not Ben's), and its own task when that is still open."""
    out: dict[str, dict[str, Any]] = {}
    for f in conn.execute("SELECT * FROM follow_ups WHERE resolved_by=? AND state='deferred'", (d["id"],)):
        out[f["id"]] = {"ref": f["id"], "type": "follow_up", "state": f["state"], "who": f["who"],
                        "what": f["what"], "client": f["client"], "task_id": f["task_id"]}
    for i in _ids(d["statement"]):
        if i in out or i in ben_ids:
            continue
        f = _fu(conn, i)
        if f is not None:
            if f["state"] in ("open", "deferred") and not _is_ben(f["who"]):
                out[i] = {"ref": i, "type": "follow_up", "state": f["state"], "who": f["who"], "what": f["what"],
                          "client": f["client"], "task_id": f["task_id"]}
            continue
        t = _task(conn, i)
        if t is not None and t["state"] == "open":
            out[i] = {"ref": i, "type": "task", "state": t["state"], "kind": t["kind"], "what": t["title"],
                      "client": t["assigned_to"], "spec": t["spec"]}
    if d["task_id"] and d["task_id"] not in out:
        t = _task(conn, d["task_id"])
        if t is not None and t["state"] == "open" and t["picked_up_at"] is None:
            out[t["id"]] = {"ref": t["id"], "type": "task", "state": t["state"], "kind": t["kind"],
                            "what": t["title"], "client": t["assigned_to"], "spec": t["spec"]}
    return list(out.values())


def facing(work: list[dict[str, Any]], statement: str, because: str) -> tuple[str, str]:
    """'internal' or 'outward', and why. Outward if any held item is, or the
    hold says so; a draft_reply is internal (writing the body), its Outlook
    step is gated separately."""
    for w in work:
        spec = {}
        if w.get("spec"):
            try:
                spec = json.loads(w["spec"]) or {}
            except ValueError:
                spec = {}
        if w.get("kind") == "code_change" and isinstance(spec, dict) and spec.get("checkin_of"):
            return "outward", f"{w['ref']} is a check-in"
        if w.get("kind") in INTERNAL_KINDS or w.get("kind") in ("draft_reply", "deliverable", "code_change"):
            continue
        if _OUTWARD.search(w.get("what") or "") and not re.match(r"\s*draft\b", w.get("what") or "", re.I):
            return "outward", f"{w['ref']}: {_OUTWARD.search(w['what']).group(0)}"
    held_part = _TRIGGER.sub(" ", statement or "")  # "until Ben promotes" is his step, not the held work
    m = _OUTWARD.search(held_part)
    if m:
        return "outward", f"hold: {m.group(0)}"
    if re.search(r"\boutward[- ]facing\b", because or "", re.I):
        return "outward", "hold says outward-facing"
    if not work:
        return "none", "holds nothing open"
    kinds = sorted({w.get("kind") or "loose end" for w in work})
    return "internal", "held: " + ", ".join(kinds)


def default_for(conn: sqlite3.Connection, ben_ids: list[str], qids: list[int], statement: str,
                because: str, ben_tasks: list[str] | None = None) -> dict[str, str] | None:
    """The working default for the question a hold waits on: the
    recommended option of the question presented to Ben on it, else a
    default named in his loose end's text, else one named in the hold."""
    rows = []
    for fid in ben_ids:
        rows += conn.execute("SELECT * FROM questions WHERE follow_up_id=? ORDER BY id DESC", (fid,)).fetchall()
    for q in qids:
        rows += conn.execute("SELECT * FROM questions WHERE id=?", (q,)).fetchall()
    for tid in ben_tasks or []:
        rows += conn.execute("SELECT * FROM questions WHERE task_id=? ORDER BY id DESC",
                             (tid,)).fetchall()
    for q in rows:
        if q["recommended"] is None:
            continue
        try:
            pick = str(json.loads(q["options"])[int(q["recommended"])])
        except (ValueError, IndexError, TypeError):
            continue
        if _NOT_A_DEFAULT.match(pick):
            return None  # the recommendation is to wait: nothing to proceed on
        return {"default": pick[:160], "source": f"q{q['id']} recommended"}
    for fid in ben_ids:
        f = _fu(conn, fid)
        if f is not None and (m := _DEFAULT.search(f["what"])):
            return {"default": _clause(f["what"], m), "source": f"loose end {fid}"}
    for text, src in ((statement, "hold"), (because, "hold's because")):
        for m in _DEFAULT.finditer(text or ""):
            if m.group("lead").lower() == "master default":
                continue  # marks the hold itself as master's call, not an answer to the question
            return {"default": _clause(text, m), "source": src}
    return None


def classify(conn: sqlite3.Connection, d: sqlite3.Row) -> dict[str, Any] | None:
    """One decision as a hold behind Ben, or None when it is not one."""
    if not is_hold(d["statement"]):
        return None
    ben = waits_on_ben(conn, d["statement"])
    if ben is None:
        return None
    work = held_work(conn, d, ben["ben_follow_ups"])
    face, why = facing(work, d["statement"], d["because"] or "")
    dflt = default_for(conn, ben["ben_follow_ups"], ben["questions"], d["statement"], d["because"] or "",
                       ben["tasks"])
    open_ben = [f for f in ben["ben_follow_ups"] if ben_open(conn, f)]
    qs = [f"q{q}" for q in ben["questions"]] + [f"q{r['id']}" for tid in ben["tasks"] for r in conn.execute(
        "SELECT id FROM questions WHERE task_id=? AND closed_at IS NULL", (tid,))]
    open_ben += [q for q in dict.fromkeys(qs) if ben_open(conn, q)]
    client = next((w.get("client") for w in work if w.get("client")), None)
    if client is None and d["task_id"] and (t := _task(conn, d["task_id"])) is not None:
        client = t["assigned_to"]
    if records_ben_ruling(d["statement"]):
        verdict, reason = "keep", "records Ben's own ruling to hold; only he lifts it"
    elif ben["type"] == "action":
        verdict, reason = "keep", "waits on something Ben must do; no answer stands in for it"
    elif face == "outward":
        verdict, reason = "keep", f"outward-facing ({why})"
    elif face == "none":
        verdict, reason = "moot", "nothing it holds is still open"
    elif not dflt:
        verdict, reason = "keep", "no default stated or recommended; stays Ben's"
    else:
        verdict, reason = "proceed", f"internal work; default from {dflt['source']}"
    return {"decision_id": d["id"], "task_id": d["task_id"], "client": client, "created_at": d["created_at"],
            "statement": d["statement"], "because": d["because"] or "", "type": ben["type"],
            "ben_follow_ups": ben["ben_follow_ups"], "open_ben_follow_ups": open_ben,
            "questions": ben["questions"], "master_default_hold": bool(_MD.search(d["because"] or "")),
            "work": [{k: v for k, v in w.items() if k != "spec"} for w in work],
            "facing": face, "facing_why": why, "default": dflt["default"] if dflt else None,
            "default_source": dflt["source"] if dflt else None, "verdict": verdict, "reason": reason}


def find_holds(conn: sqlite3.Connection, live_only: bool = True, since: int = 0) -> list[dict[str, Any]]:
    """Every non-superseded hold behind Ben; with live_only, those still
    holding open work (the report's rows)."""
    out = []
    for d in conn.execute("""SELECT * FROM decisions WHERE created_at >= ? AND id NOT IN
                               (SELECT supersedes FROM decisions WHERE supersedes IS NOT NULL)
                             ORDER BY created_at""", (since,)):
        h = classify(conn, d)
        if h is None or (live_only and h["verdict"] == "moot"):
            continue
        out.append(h)
    return out


# ---------------------------------------------------------- side table

DDL = """
CREATE TABLE IF NOT EXISTS hold_defaults (
  decision_id    TEXT PRIMARY KEY REFERENCES decisions(id),
  hold_id        TEXT REFERENCES decisions(id),
  ben_follow_ups TEXT NOT NULL,
  work           TEXT NOT NULL,
  task_id        TEXT,
  created_at     INTEGER NOT NULL
);
"""


def ensure(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hold_defaults'").fetchone() is None:
        conn.execute(DDL)  # one statement: no implicit COMMIT inside a caller's transaction


def register(conn: sqlite3.Connection, decision_id: str, hold_id: str | None, ben_follow_ups: list[str],
             work: list[str], task_id: str | None) -> None:
    ensure(conn)
    conn.execute("INSERT OR REPLACE INTO hold_defaults VALUES (?,?,?,?,?,?)",
                 (decision_id, hold_id, json.dumps(ben_follow_ups), json.dumps(work), task_id, now()))


def _lineage(conn: sqlite3.Connection, task_id: str) -> list[str]:
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


def pending_gate(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    """The default a task's work proceeds on while Ben's question is still
    open, or None. A task is covered when it, or a task above it, is held
    work of a registered default, or tasked a loose end that was."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hold_defaults'").fetchone() is None:
        return None
    line = set(_lineage(conn, task_id))
    for r in conn.execute("SELECT * FROM hold_defaults ORDER BY created_at DESC"):
        refs = set(json.loads(r["work"]))
        if r["task_id"]:
            refs.add(r["task_id"])
        tasked = {f["resolved_by"] for f in conn.execute(
            f"SELECT resolved_by FROM follow_ups WHERE state='tasked' AND id IN ({','.join('?' * len(refs))})",
            list(refs))} if refs else set()
        if not (line & (refs | tasked)):
            continue
        ben = [f for f in json.loads(r["ben_follow_ups"]) if ben_open(conn, f)]
        if ben:
            return {"decision_id": r["decision_id"], "hold_id": r["hold_id"], "ben_follow_ups": ben}
    return None


def ben_open(conn: sqlite3.Connection, ref: str) -> bool:
    """A Ben loose end still open or deferred, or a question (q<id>) not closed."""
    if m := re.fullmatch(r"q(\d+)", ref):
        q = conn.execute("SELECT closed_at FROM questions WHERE id=?", (int(m.group(1)),)).fetchone()
        return q is not None and q["closed_at"] is None
    f = _fu(conn, ref)
    return f is not None and f["state"] in ("open", "deferred")


def require_ruled(conn: sqlite3.Connection, task_id: str, what: str) -> None:
    g = pending_gate(conn, task_id)
    if g:
        raise ValueError(
            f"{what} refused: task {task_id} proceeds on a master default (decision {g['decision_id']}) while "
            f"Ben's question {', '.join(g['ben_follow_ups'])} is open. Outward steps wait for his answer: once "
            "he rules, record it and resolve that loose end, revising the work first if he overruled the default.")


# ------------------------------------------------------- going forward

HOLD_RULE = ("Holds behind Ben are kept only for outward-facing work (a send, Outlook drafting of a reply that "
             "turns on his answer, a check-in, Promote, SharePoint status, a tracker post, anything "
             "client-visible) or for something only he can do. Internal work proceeds on the default.")


def check_new_hold(conn: sqlite3.Connection, statement: str, because: str | None, task_id: str | None) -> None:
    """coord_record_decision's check (switch hold_rule): refuse a new hold on
    internal work when Ben's question already carries a default."""
    from . import queue
    if not queue.switch(conn, "hold_rule"):
        return
    row = {"id": "", "statement": statement, "because": because, "task_id": task_id, "created_at": now()}
    h = classify(conn, row)  # sqlite3.Row-compatible mapping
    if h is None or h["verdict"] != "proceed":
        return
    ben = ", ".join(h["open_ben_follow_ups"] or h["ben_follow_ups"]) or "his question"
    work = ", ".join(w["ref"] for w in h["work"])
    raise ValueError(
        f"hold refused: {HOLD_RULE} This one holds internal work ({work}) behind Ben's {ben}, which has a default "
        f"({h['default_source']}: \"{h['default'][:80]}\"). Record instead: \"Proceed on master default "
        f"<the default> for {work}; Ben's {ben} stays open\" with because \"... master default; Ben may override\", "
        "then task or dispatch the work. Outward steps on it (Outlook, check-in) stay gated until he answers. "
        "If the work is outward-facing, say which step in the statement (send, check-in, Promote, SharePoint, "
        "tracker post).")


SINCE_KEY = "hold_rule_since"


def minder_owed(conn: sqlite3.Connection) -> list[str]:
    """Holds recorded since the rule went live that it would have refused
    (an older session, or a path that skips the tool): master owes the
    default decision that supersedes each, and the task."""
    from . import queue
    if not queue.switch(conn, "hold_rule"):
        return []
    r = conn.execute("SELECT value FROM meta WHERE key=?", (SINCE_KEY,)).fetchone()
    if r is None:
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?,?)", (SINCE_KEY, str(now())))
        return []
    return [h["decision_id"] for h in find_holds(conn, since=int(r["value"])) if h["verdict"] == "proceed"]


_PROCEEDS = re.compile(r"\bmaster default\b[^.]{0,20}\bBen may overrid", re.I)


def after_decision(conn: sqlite3.Connection, decision_id: str, statement: str, because: str | None,
                   task_id: str | None, supersedes: str | None) -> dict[str, Any] | None:
    """After a decision is written: when it proceeds on a master default while
    naming Ben's still-open question, register it so outward steps wait."""
    text = f"{statement} {because or ''}"
    if not _PROCEEDS.search(text):
        return None
    ben, work = [], []
    for i in _ids(statement):
        f = _fu(conn, i)
        if f is not None and _is_ben(f["who"]) and f["state"] in ("open", "deferred"):
            ben.append(i)
        elif f is not None or _task(conn, i) is not None:
            work.append(i)
    hold_id = None
    if supersedes:
        old = conn.execute("SELECT * FROM decisions WHERE id=?", (supersedes,)).fetchone()
        if old is not None and (h := classify(conn, old)) is not None:
            hold_id = supersedes
            ben = ben or [f for f in h["open_ben_follow_ups"]]
            work = work or [w["ref"] for w in h["work"]]
    if not ben or not (work or task_id):
        return None
    register(conn, decision_id, hold_id, ben, work, task_id)
    return {"gated_until_ben": ben}


# ---------------------------------------------------- existing holds

APPLY_BECAUSE = "hold {hold} was on internal work; master default; Ben may override"


def apply(conn: sqlite3.Connection, write: bool = False) -> dict[str, Any]:
    """For each live internal hold with a default: record a decision that
    supersedes it, reopen the loose ends it deferred (master tasks them as
    client loose ends), and register the gate. Dry run unless write; write
    refuses while switch hold_apply is off."""
    from . import queue, store
    if write and not queue.switch(conn, "hold_apply"):
        raise ValueError("hold_apply is off: run `python -m coord_mcp.queue switch hold_apply on` first "
                         "(Ben's call, after reading the holds report)")
    rows = [h for h in find_holds(conn) if h["verdict"] == "proceed"]
    done = []
    if write:
        ensure(conn)
        conn.execute("BEGIN IMMEDIATE")
    try:
        for h in rows:
            work = [w["ref"] for w in h["work"]]
            ben = h["open_ben_follow_ups"] or h["ben_follow_ups"]
            item = {"hold": h["decision_id"], "client": h["client"], "default": h["default"], "work": work,
                    "reopen": [w["ref"] for w in h["work"] if w["type"] == "follow_up" and w["state"] == "deferred"],
                    "ben_follow_ups": ben}
            if write:
                stmt = f"Proceed on master default for {', '.join(work)}: {h['default']}"
                stmt = (stmt[:150] + f"; Ben's {', '.join(ben)} stays open")[:200] if ben else stmt[:200]
                dec = store.record_decision(conn, stmt, APPLY_BECAUSE.format(hold=h["decision_id"]),
                                            h["task_id"], supersedes=h["decision_id"])
                for fid in item["reopen"]:
                    store.resolve_follow_up(conn, fid, reopen=True)
                register(conn, dec["decision_id"], h["decision_id"], ben, work, h["task_id"])
                item["decision"] = dec["decision_id"]
            done.append(item)
        if write:
            conn.execute("COMMIT")
    except Exception:
        if write:
            conn.execute("ROLLBACK")
        raise
    return {"applied": write, "count": len(done), "items": done}


# --------------------------------------------------- workflow engine

def case_hold(conn: sqlite3.Connection, task_ids: list[str], spawn_kind: str) -> dict[str, Any] | None:
    """What a live hold on any of a case's tasks (or their loose ends) means
    for the engine's next spawn: {'stop': why}, {'proceed': hold},
    {'draft': hold} (a reply drafted past a hold short of the send) or None.
    An outward or default-less hold stops it; so does any hold when the
    spawn is outward (a check-in); an internal hold with a default proceeds
    only under hold_rule."""
    from . import queue
    if not task_ids:
        return None
    ids = set(task_ids)
    ids |= {f["id"] for f in conn.execute(
        f"SELECT id FROM follow_ups WHERE task_id IN ({','.join('?' * len(task_ids))})", task_ids)}
    # Every hold on the case is weighed, and a stop wins: an outward hold
    # recorded after an internal one must still stop the spawn.
    passed: dict[str, dict[str, Any]] = {}
    for h in find_holds(conn):
        refs = {w["ref"] for w in h["work"]} | {w.get("task_id") for w in h["work"] if w.get("task_id")}
        if h["task_id"]:
            refs.add(h["task_id"])
        refs |= set(_ids(h["statement"]))
        if not refs & ids:
            continue
        if h["verdict"] == "proceed" and spawn_kind != "checkin" and queue.switch(conn, "hold_rule"):
            passed.setdefault("proceed", h)
        elif spawn_kind == "draft_reply" and h["facing"] != "outward":
            # Ben 2026-11-14 (engine.REPLY_RULING): drafts never send, so a
            # hold short of the send does not stop the draft.
            passed.setdefault("draft", h)
        else:
            return {"stop": f"held by decision {h['decision_id']} ({h['reason']})", "hold": h["decision_id"]}
    if "proceed" in passed:
        return {"proceed": passed["proceed"]}
    if "draft" in passed:
        return {"draft": passed["draft"]}
    return None


# --------------------------------------------------------------- report

def _when(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by = {}
    for v in ("proceed", "keep"):
        by[v] = sum(1 for r in rows if r["verdict"] == v)
    return {"holds": len(rows), **by,
            "ruling": sum(1 for r in rows if r["type"] == "ruling"),
            "action": sum(1 for r in rows if r["type"] == "action"),
            "master_default_hold": sum(1 for r in rows if r["master_default_hold"]),
            "outward": sum(1 for r in rows if r["facing"] == "outward"),
            "internal": sum(1 for r in rows if r["facing"] == "internal"),
            "with_default": sum(1 for r in rows if r["default"]),
            "keep_reasons": _count(r["reason"].split(" (")[0] for r in rows if r["verdict"] == "keep")}


def _count(xs) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


COLUMNS = ("Hold", "Date", "Client", "Waits on", "Ben item(s)", "Held work", "Facing", "Why", "Default",
           "Default from", "Hold said master default", "Verdict", "Reason", "Statement", "Because", "Ben: OK?")


def row_values(r: dict[str, Any]) -> list[Any]:
    work = "; ".join(f"{w['ref']} ({w.get('kind') or ('loose end, ' + str(w.get('who')))}): {w['what'][:80]}"
                     for w in r["work"])
    return [r["decision_id"], _when(r["created_at"]), r["client"] or "", r["type"],
            ", ".join(r["ben_follow_ups"] + [f"q{q}" for q in r["questions"]]), work, r["facing"], r["facing_why"],
            r["default"] or "", r["default_source"] or "", "yes" if r["master_default_hold"] else "",
            r["verdict"], r["reason"], r["statement"], r["because"], ""]


def write_xlsx(rows: list[dict[str, Any]], path: str) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "Holds"
    ws.append(list(COLUMNS))
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="DDE4EE")
    for r in rows:
        ws.append(row_values(r))
    widths = (14, 11, 10, 9, 22, 60, 9, 30, 50, 18, 9, 9, 40, 70, 50, 12)
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(65 + i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions
    s = wb.create_sheet("Summary")
    for k, v in summary(rows).items():
        if isinstance(v, dict):
            for kk, vv in v.items():
                s.append([f"keep: {kk}", vv])
        else:
            s.append([k, v])
    wb.save(path)


def write_html(rows: list[dict[str, Any]], path: str, xlsx_name: str, day: str) -> None:
    s = summary(rows)
    e = html.escape

    def table(sel: list[dict[str, Any]], limit: int) -> str:
        out = ["<table><tr><th>Hold</th><th>Client</th><th>Holds</th><th>Default</th><th>Why</th></tr>"]
        for r in sel[:limit]:
            work = "<br>".join(f"<code>{e(w['ref'])}</code> {e((w.get('kind') or 'loose end'))}: {e(w['what'][:90])}"
                               for w in r["work"])
            out.append(f"<tr><td><code>{e(r['decision_id'])}</code><br><small>{_when(r['created_at'])}</small></td>"
                       f"<td>{e(r['client'] or '')}</td><td>{work}</td>"
                       f"<td>{e(r['default'] or '—')}<br><small>{e(r['default_source'] or '')}</small></td>"
                       f"<td>{e(r['reason'])}</td></tr>")
        out.append("</table>")
        if len(sel) > limit:
            out.append(f"<p class=muted>{len(sel) - limit} more in the workbook.</p>")
        return "".join(out)

    proceed = sorted([r for r in rows if r["verdict"] == "proceed"], key=lambda r: -r["created_at"])
    keep = sorted([r for r in rows if r["verdict"] == "keep"], key=lambda r: -r["created_at"])
    keeps = "".join(f"<tr><td>{e(k)}</td><td class=n>{v}</td></tr>" for k, v in s["keep_reasons"].items())
    page = f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Ben Holds Defaults</title>
<style>
:root{{--bg:#fff;--fg:#1d2330;--muted:#5d6675;--line:#dde2ea;--card:#f5f7fa;--accent:#2457a6}}
@media (prefers-color-scheme:dark){{:root:not([data-theme=light]){{--bg:#14171c;--fg:#e4e8ee;--muted:#9aa3b0;--line:#2c323b;--card:#1b1f26;--accent:#7aa7ec}}}}
:root[data-theme=dark]{{--bg:#14171c;--fg:#e4e8ee;--muted:#9aa3b0;--line:#2c323b;--card:#1b1f26;--accent:#7aa7ec}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,Segoe UI,sans-serif;margin:0 auto;max-width:1100px;padding:24px 16px}}
h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:18px;margin:28px 0 8px}} .muted,small{{color:var(--muted)}}
.kpis{{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}} .kpis div{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;min-width:150px}}
.kpis b{{display:block;font-size:22px;color:var(--accent)}} table{{border-collapse:collapse;width:100%;font-size:13px;display:block;overflow-x:auto}}
th,td{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}} th{{background:var(--card)}} td.n{{text-align:right}}
code{{font-size:12px}} .bluf{{background:var(--card);border-left:4px solid var(--accent);padding:10px 14px;border-radius:4px}}
</style></head><body>
<h1>Holds behind Ben: working defaults</h1>
<p class=muted>{day} · coord-mcp · read-only report: nothing on the board was changed. Every hold with its verdict is in
<a href="{e(xlsx_name)}">{e(xlsx_name)}</a> beside this page, with a column for Ben's OK.</p>
<p class=bluf><b>{s['proceed']} of {s['holds']} live holds behind Ben could proceed on a default today</b>: the work they hold is
internal and Ben's question already carries a default (most often the recommended option of the question presented to him).
The other {s['keep']} stay held: outward-facing, waiting on something only Ben can do, or with no default to proceed on.
Nothing proceeds until Ben turns on <code>hold_apply</code> and runs the apply command.</p>
<div class=kpis><div><b>{s['holds']}</b>live holds behind Ben</div><div><b>{s['proceed']}</b>could proceed on a default</div>
<div><b>{s['keep']}</b>stay held</div><div><b>{s['ruling']}</b>wait on a ruling</div><div><b>{s['action']}</b>wait on an action of his</div>
<div><b>{s['master_default_hold']}</b>holds recorded as "master default"</div></div>
<h2>How a hold was read</h2>
<ul>
<li><b>A hold behind Ben</b>: a standing (not superseded) decision that holds, defers, parks or keeps work waiting, whose trigger
("until …", "pending …", "waits on …") names Ben, his loose end or a question presented to him. Handoff snapshots are skipped.</li>
<li><b>Live</b>: it still holds something open: a loose end it deferred, an open loose end or undelivered task it names, or its own task.</li>
<li><b>Default</b>: the recommended option of the question presented on his loose end; else a default, recommendation or proposal
named in his loose end; else one named in the hold. A "master default" in a hold's reason marks the hold as master's own call,
not an answer to the question, so it is counted separately and is not used as the default.</li>
<li><b>Outward</b>: the held work is a check-in, or names a send, Outlook, Promote, SharePoint, a tracker post or the client; or the
hold says outward-facing. A draft reply is internal (its body is a file); its Outlook step is gated until Ben answers.</li>
</ul>
<h2>Why the rest stay held</h2><table><tr><th>Reason</th><th>Holds</th></tr>{keeps}</table>
<h2>Would proceed on a default ({s['proceed']})</h2>{table(proceed, 40)}
<h2>Stay held ({s['keep']})</h2>{table(keep, 25)}
<h2>Limits</h2>
<ul><li>Holds and defaults are read from wording; each row carries its reason so Ben can override it in the workbook.</li>
<li>The audit counted 158 live holds from the dashboard pages (no database read). This report reads the board directly, so its
count differs: it counts holds whose trigger names Ben and that still hold open work.</li>
<li>A hold on a whole client ("hold MAG until …") with no item named is reported only when its own task is still undelivered.</li></ul>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m coord_mcp.holds", description="Holds behind Ben as working defaults.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="read-only: live holds behind Ben, their defaults and verdicts")
    r.add_argument("--html")
    r.add_argument("--xlsx")
    r.add_argument("--json", action="store_true")
    a = sub.add_parser("apply", help="dry run (or --yes, with hold_apply on): proceed internal holds on their default")
    a.add_argument("--yes", action="store_true")
    args = p.parse_args(argv)
    conn = connect()
    try:
        if args.cmd == "report":
            rows = find_holds(conn)
            if args.json:
                print(json.dumps(rows, indent=1, default=str))
                return 0
            if args.xlsx:
                write_xlsx(rows, args.xlsx)
            if args.html:
                import os
                write_html(rows, args.html, os.path.basename(args.xlsx or ""), datetime.now().strftime("%Y-%m-%d"))
            for h in rows:
                print(f"{h['decision_id']}  {(h['client'] or ''):<9} {h['verdict']:<7} {h['facing']:<8} "
                      f"{h['statement'][:90]}")
            print(json.dumps(summary(rows)))
            return 0
        out = apply(conn, write=args.yes)
        for i in out["items"]:
            print(f"{i['hold']}  {(i['client'] or ''):<9} work {', '.join(i['work'])}  reopen {len(i['reopen'])}  "
                  f"default: {(i['default'] or '')[:70]}" + (f"  -> {i['decision']}" if i.get("decision") else ""))
        print(f"{out['count']} " + ("applied" if out["applied"] else "would proceed (dry run; --yes with hold_apply on)"))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
