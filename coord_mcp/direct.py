"""Ben's direct asks: top priority, handed back finished (Ben, 2026-11-16).

When Ben asks the Desk or a client session for something himself, the board
carries it first and brings it back to him done, without his chasing it.

1. The flag. A task is *direct* when its spec says ``"from": "ben-direct"``,
   or any task above it does (parent chain, and the code a check-in checks
   in). So a review, a revision or a follow-up of a direct ask is direct too.
   The Desk sets it with coord_create_task(ben_direct=true); a client session
   files finished work Ben asked it for with coord_file_work (any kind) or
   coord_file_draft (a reply), both of which stamp it.
2. Priority. Auto-dispatch sends direct tasks first, ahead of commitments
   and oldest-first, with no DISPATCH_AFTER wait (sessions._candidates). The
   minder lists a direct task as owed the moment it is undelivered, its
   result unread, or a client follow-up of it untasked, and that list fires
   past the minder's rate limit when it holds an id the last prompt did not.
3. Hand-back. When a direct task lands, its agent-shaped loose ends become
   child tasks at once (``autotask``). When the whole chain is finished
   (``chain_state``), it becomes one *ready* item: a toast from the
   master-console mod and a "Ready for you" card on /board, not a question.
   Ben's "review and draft" items in the chain close behind it.

Completed state, per kind (``_work_done``):

- draft_reply: done, the review gate clear if the gate covers it, and typed
  into Outlook Drafts (draft_replies reads ``drafted``). The minder lists a
  direct draft as owed for Outlook the moment it is ready.
- deliverable: done and promoted to ``<client>/deliverables`` (the approving
  dod_review promotes it, store.auto_promote).
- code_change: done, and if the gate covers it, its newest dod_review
  approves. Built and reviewed; the check-in is its own step, as always.
- investigation, data_pull, review: done.

And the chain as a whole: no open task, no open client loose end, and no
failed task that nothing replaced. A chain that is waiting on Ben (an
escalation, a refused promote) is not ready: it is already in his queue.

Tables made on first use (no SCHEMA_VERSION bump): ``ready_items``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from .db import now

BEN_DIRECT = "ben-direct"
WORK_KINDS = ("investigation", "data_pull", "code_change", "deliverable", "draft_reply", "review")
# Kinds a client may file finished with coord_file_work. Reviews are the
# board's own step (an independent session), never something Ben asks for.
FILE_KINDS = ("investigation", "data_pull", "code_change", "deliverable", "draft_reply")

READY_DDL = """
CREATE TABLE IF NOT EXISTS ready_items (
  root_id      TEXT PRIMARY KEY REFERENCES tasks(id),
  task_id      TEXT NOT NULL REFERENCES tasks(id),
  client       TEXT,
  title        TEXT NOT NULL,
  what         TEXT NOT NULL,
  created_at   INTEGER NOT NULL,
  announced_at INTEGER,
  seen_at      INTEGER
);
"""


def ensure_ready(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ready_items'").fetchone() is None:
        conn.executescript(READY_DDL)


def _spec(raw: Any) -> dict[str, Any]:
    try:
        s = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return s if isinstance(s, dict) else {}


def marked(spec: Any) -> bool:
    """The spec itself carries the flag."""
    return isinstance(spec, dict) and spec.get("from") == BEN_DIRECT


def root_of(conn: sqlite3.Connection, task_id: str | None) -> str | None:
    """The topmost task in task_id's lineage whose spec is flagged, or None
    when nothing in it is direct."""
    from .queue import lineage
    found = None
    for tid in lineage(conn, task_id):
        r = conn.execute("SELECT spec FROM tasks WHERE id=?", (tid,)).fetchone()
        if r is not None and marked(_spec(r["spec"])):
            found = tid
    return found


def is_direct(conn: sqlite3.Connection, task_id: str | None) -> bool:
    return root_of(conn, task_id) is not None


def direct_ids(conn: sqlite3.Connection) -> set[str]:
    """Every direct task on the board: the flagged ones and all below them.
    LIKE is only a prefilter; the flag is the parsed field, so a spec that
    merely quotes the words is not a direct ask."""
    out: set[str] = set()
    for r in conn.execute("SELECT id, spec FROM tasks WHERE spec LIKE '%ben-direct%'").fetchall():
        if marked(_spec(r["spec"])) and r["id"] not in out:
            out.update(c["id"] for c in chain(conn, r["id"]))
    return out


def chain(conn: sqlite3.Connection, root_id: str) -> list[sqlite3.Row]:
    """root_id and every task below it (parent chain and check-ins)."""
    return conn.execute(
        """WITH RECURSIVE d(id) AS (
             SELECT ?
           UNION
             SELECT t.id FROM tasks t JOIN d ON t.parent_id = d.id
                OR (t.spec LIKE '%checkin_of%' AND json_extract(t.spec, '$.checkin_of') = d.id)
           ) SELECT t.* FROM tasks t JOIN d ON d.id = t.id ORDER BY t.created_at, t.rowid""",
        (root_id,)).fetchall()


# ------------------------------------------------------------ priority


def minder_owed(conn: sqlite3.Connection, now_ts: int, sent: set[str] | frozenset[str] = frozenset(),
                waiting: dict[str, Any] | None = None) -> list[str]:
    """Direct work master owes now, no thresholds: '<id> undelivered',
    '<id> unread', '<follow-up id> untasked'. sent and waiting are the
    minder's own exclusions (code is delivering it; a dependency holds it)."""
    ids = direct_ids(conn)
    if not ids:
        return []
    waiting = waiting or {}
    clients = _clients(conn)
    out: list[str] = []
    marks = ",".join("?" * len(ids))
    for r in conn.execute(
            f"""SELECT t.id FROM tasks t WHERE t.id IN ({marks}) AND t.state='open' AND t.picked_up_at IS NULL
                  AND NOT EXISTS (SELECT 1 FROM task_parks p WHERE p.task_id = t.id)
                ORDER BY t.created_at""", list(ids)):
        if r["id"] not in sent and r["id"] not in waiting:
            out.append(f"{r['id']} undelivered")
    for r in conn.execute(
            f"""SELECT t.id FROM tasks t WHERE t.id IN ({marks}) AND t.state IN ('done','failed')
                  AND t.completed_at <= ?
                  AND (t.result_read_at IS NULL OR t.result_read_at < t.completed_at)
                  AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.task_id = t.id AND d.created_at >= t.completed_at)
                  AND NOT EXISTS (SELECT 1 FROM tasks c WHERE c.parent_id = t.id AND c.created_at >= t.completed_at)
                ORDER BY t.completed_at""", [*ids, now_ts]):
        if not _finished_alone(conn, r["id"]):
            out.append(f"{r['id']} unread")
    for r in conn.execute(
            f"""SELECT f.id, f.who FROM follow_ups f WHERE f.task_id IN ({marks}) AND f.state='open'
                  AND NOT EXISTS (SELECT 1 FROM tasks c WHERE c.parent_id = f.task_id AND c.state = 'open')
                ORDER BY f.created_at, f.idx""", list(ids)):
        if (r["who"] or "").strip().casefold() in clients:
            out.append(f"{r['id']} untasked")
    return out


def _finished_alone(conn: sqlite3.Connection, task_id: str) -> bool:
    """A direct result that needs nothing from master: its chain reached a
    ready item already. Reading it would be busywork."""
    root = root_of(conn, task_id)
    if root is None:
        return False
    ensure_ready(conn)
    return conn.execute("SELECT 1 FROM ready_items WHERE root_id=? AND created_at >= "
                        "(SELECT completed_at FROM tasks WHERE id=?)", (root, task_id)).fetchone() is not None


def _clients(conn: sqlite3.Connection) -> set[str]:
    from .store import client_keys
    return client_keys(conn)


# ------------------------------------------------------------ hand-back


def _pick_kind(src: sqlite3.Row, what: str) -> str:
    """The kind of the child task a client loose end becomes."""
    from .workflows.engine import _REPLYISH
    if _REPLYISH.search(what or ""):
        return "draft_reply"
    if src["kind"] in ("investigation", "data_pull", "code_change", "deliverable"):
        return src["kind"]
    return "investigation"


def autotask(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    """Turn a just-finished direct task's open client loose ends into child
    tasks, and resolve each with its task. Returns [{follow_up, task_id, kind}].

    A dod_review that requests changes revises the task it reviewed: the
    child is that task's kind, parent = that task, spec copied with the
    findings. Anything else: a reply-shaped item is a draft_reply, otherwise
    the source's own kind (investigation for a review). A check-in is never
    created here (checkin_auto owns it), nor a step the workflow engine owns:
    a task in an engine case leaves its loose ends to the engine."""
    from . import store
    if not is_direct(conn, task_id):
        return []
    src = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if src is None or src["state"] != "done" or not src["assigned_to"]:
        return []
    if _engine_case(conn, src):
        return []
    clients = _clients(conn)
    fus = [f for f in conn.execute("SELECT id, who, what FROM follow_ups WHERE task_id=? AND state='open' "
                                   "ORDER BY idx, rowid", (task_id,))
           if (f["who"] or "").strip().casefold() in clients]
    if not fus:
        return []
    result = _spec(src["result"])
    base, parent = src, src["id"]
    if src["kind"] == "dod_review" and result.get("verdict") == "request_changes" and src["parent_id"]:
        p = conn.execute("SELECT * FROM tasks WHERE id=?", (src["parent_id"],)).fetchone()
        if p is not None:
            base, parent = p, p["id"]
    out = []
    revision = parent != src["id"]
    groups: list[list[sqlite3.Row]] = [fus] if revision else [[f] for f in fus]
    for group in groups:
        what = "; ".join(f["what"] for f in group)
        kind = base["kind"] if revision else _pick_kind(base, what)
        spec = {k: v for k, v in _spec(base["spec"]).items()
                if k not in ("checkin_of", "created_by", "follow_up", "findings")} if kind == base["kind"] else {}
        spec.update({"from": BEN_DIRECT, "created_by": f"ben-direct hand-back (from task {src['id']})"})
        if revision:
            spec["findings"] = [f["what"] for f in group][:10]
            spec["revises"] = base["id"]
            spec["instructions"] = (f"Revise task {base['id']}'s work to meet every finding of review {src['id']}. "
                                    "Write in work/, never deliverables/.")
        else:
            spec["follow_up"] = what
            spec["instructions"] = (f"Ben asked for this directly (task chain {root_of(conn, task_id)}). "
                                    f"Do the follow-up task {src['id']} named: {what}")
        if kind == "draft_reply":
            for k in ("reply_from", "reply_date", "asked"):
                if _spec(base["spec"]).get(k) or result.get(k):
                    spec.setdefault(k, _spec(base["spec"]).get(k) or result.get(k))
        title = (("Revise: " if revision else "") + (base["title"] if revision else what))[:80]
        new = store.create_task(conn, kind, title, spec, assigned_to=src["assigned_to"], parent_id=parent)
        tid = new["task_id"]
        for f in group:
            store.resolve_follow_up(conn, f["id"], task_id=tid)
        store.record_decision(conn, f"Ben-direct hand-back: {kind} {tid} from task {src['id']}",
                              because="direct ask; agent-shaped follow-ups are tasked at once (Ben, 2026-11-16)",
                              task_id=tid)
        out.append({"follow_ups": [f["id"] for f in group], "task_id": tid, "kind": kind})
    return out


def _engine_case(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    keys = row.keys()
    if "case_id" not in keys or not row["case_id"]:
        return False
    try:
        from .workflows import engine
        from .workflows import definitions as defs
        c = conn.execute("SELECT workflow FROM workflow_cases WHERE id=?", (row["case_id"],)).fetchone()
        return bool(c and engine.enabled() and (defs.get(c["workflow"]) or {}).get("engine"))
    except Exception:  # noqa: BLE001 - no workflow tables: no engine case
        return False


def _drafted(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    from . import store
    for d in store.draft_replies(conn, days=60, client=row["assigned_to"])["drafts"]:
        if d["task_id"] == row["id"]:
            return d["state"] == "drafted"
    return False


def _promoted(conn: sqlite3.Connection, task_id: str) -> bool:
    from .db import ensure_promotions
    ensure_promotions(conn)
    return conn.execute("SELECT 1 FROM promotions WHERE task_id=?", (task_id,)).fetchone() is not None


def _approved(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    from . import gate, store
    if not gate.is_gated(conn, row):
        return True
    rv = store.latest_review(conn, row["id"])
    return bool(rv and rv["verdict"] == "approve")


def _work_done(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[bool, str]:
    """Is this piece of work in its finished state (module docstring)? And if
    not, what it waits on, in a few words."""
    if row["state"] != "done":
        return False, f"{row['kind']} {row['id']} is {row['state']}"
    if not _spec(row["result"]).get("done", True):
        return False, f"{row['kind']} {row['id']} came back not done"
    k = row["kind"]
    if k == "draft_reply":
        if not _approved(conn, row):
            return False, f"draft {row['id']} waits on its review"
        if not _drafted(conn, row):
            return False, f"draft {row['id']} is not in Outlook Drafts yet"
    elif k == "deliverable":
        if not _promoted(conn, row["id"]):
            return False, f"deliverable {row['id']} is not promoted yet"
    elif k == "code_change":
        if not _approved(conn, row):
            return False, f"code {row['id']} waits on its review"
    return True, ""


def chain_state(conn: sqlite3.Connection, root_id: str) -> dict[str, Any]:
    """{ready, waits: [...], last: task id, work: [task ids]} for a direct chain."""
    rows = chain(conn, root_id)
    ids = [r["id"] for r in rows]
    waits: list[str] = []
    for r in rows:
        if r["state"] == "open":
            waits.append(f"{r['kind']} {r['id']} is open")
    clients = _clients(conn)
    if ids:
        marks = ",".join("?" * len(ids))
        for f in conn.execute(f"SELECT id, who FROM follow_ups WHERE state='open' AND task_id IN ({marks})", ids):
            if (f["who"] or "").strip().casefold() in clients:
                waits.append(f"loose end {f['id']} untasked")
    # The work that stands: each task with no child of its own kind (a
    # revision or retry replaces its parent). Reviews are judged through the
    # work they review, never on their own.
    kids: dict[str, set[str]] = {}
    for r in rows:
        if r["parent_id"]:
            kids.setdefault(r["parent_id"], set()).add(r["kind"])
    work = [r for r in rows if r["kind"] != "dod_review" and r["kind"] not in kids.get(r["id"], set())]
    for r in work:
        if r["state"] == "open":
            continue  # counted above
        if r["state"] == "failed":
            if not kids.get(r["id"]):
                waits.append(f"{r['kind']} {r['id']} failed")
            continue
        ok, why = _work_done(conn, r)
        if not ok:
            waits.append(why)
    done = [r for r in rows if r["state"] == "done"]
    # Latest to finish; ties (one second) go to the later-created task.
    last = max(enumerate(done), key=lambda x: (x[1]["completed_at"] or 0, x[0]))[1]["id"] if done else root_id
    return {"ready": bool(rows) and not waits, "waits": waits, "last": last, "work": [r["id"] for r in work]}


def _what(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> str:
    """One line for the toast and the card: what Ben gets, per piece of work."""
    parts = []
    for r in rows:
        res = _spec(r["result"])
        if r["kind"] == "draft_reply":
            parts.append(f"draft '{res.get('subject', r['title'])}' is in your Outlook Drafts")
        elif r["kind"] == "deliverable":
            p = conn.execute("SELECT dest, version FROM promotions WHERE task_id=? ORDER BY version DESC LIMIT 1",
                             (r["id"],)).fetchone()
            parts.append(f"{p['dest'].replace(chr(92), '/').split('/')[-1]} promoted" if p else "deliverable done")
        elif r["kind"] == "code_change":
            parts.append(f"code built and reviewed ({res.get('summary', r['title'])})"[:160])
        else:
            parts.append(str(res.get("verdict") or res.get("summary") or r["title"])[:160])
    return "; ".join(parts)[:400] or "finished"


def sweep(conn: sqlite3.Connection, now_ts: int | None = None) -> list[dict[str, Any]]:
    """Record a ready item for every direct chain that is finished and has
    none since its last completion. Closes Ben's "review and draft" items in
    the chain behind it. Returns the items it recorded."""
    from . import queue, store
    ensure_ready(conn)
    t = now() if now_ts is None else now_ts
    roots = [r["id"] for r in conn.execute("SELECT id, spec FROM tasks WHERE spec LIKE '%ben-direct%'")
             if marked(_spec(r["spec"])) and root_of(conn, r["id"]) == r["id"]]
    out = []
    for root in roots:
        st = chain_state(conn, root)
        if not st["ready"]:
            continue
        last = conn.execute("SELECT completed_at FROM tasks WHERE id=?", (st["last"],)).fetchone()
        have = conn.execute("SELECT created_at FROM ready_items WHERE root_id=?", (root,)).fetchone()
        if have is not None and have["created_at"] >= (last["completed_at"] or 0):
            continue
        rows = [r for r in chain(conn, root) if r["id"] in set(st["work"])]
        rt = conn.execute("SELECT title, assigned_to, spec FROM tasks WHERE id=?", (root,)).fetchone()
        what = _what(conn, rows)
        conn.execute("INSERT OR REPLACE INTO ready_items(root_id, task_id, client, title, what, created_at) "
                     "VALUES (?,?,?,?,?,?)", (root, st["last"], rt["assigned_to"], rt["title"], what, t))
        dec = store.record_decision(conn, f"Ready for Ben: {rt['title']}"[:200],
                                    because="direct ask finished; handed back, not asked (Ben, 2026-11-16)",
                                    task_id=root)
        ids = [r["id"] for r in chain(conn, root)]
        marks = ",".join("?" * len(ids))
        closed = []
        for f in conn.execute(f"SELECT id, what FROM follow_ups WHERE state='open' AND lower(trim(who))='ben' "
                              f"AND task_id IN ({marks})", ids).fetchall():
            if queue.review_draft(f["what"]):
                store.resolve_follow_up(conn, f["id"], decision_id=dec["decision_id"])
                closed.append(f["id"])
        out.append({"root_id": root, "task_id": st["last"], "client": rt["assigned_to"], "title": rt["title"],
                    "what": what, "decision_id": dec["decision_id"], "closed": closed})
    return out


def ready_items(conn: sqlite3.Connection, include_seen: bool = False, limit: int = 20) -> list[dict[str, Any]]:
    ensure_ready(conn)
    sql = "SELECT * FROM ready_items" + ("" if include_seen else " WHERE seen_at IS NULL")
    return [dict(r) for r in conn.execute(sql + " ORDER BY created_at DESC LIMIT ?", (limit,))]


def claim_announce(conn: sqlite3.Connection, claimer: str = "master",
                   now_ts: int | None = None) -> dict[str, Any] | None:
    """The oldest ready item not yet toasted, stamped as toasted. The desk
    toasts while it is live; the worker only when no desk is."""
    from . import store
    ensure_ready(conn)
    t = now() if now_ts is None else now_ts
    if claimer != "desk" and store.desk_live(conn, t):
        return None
    r = conn.execute("SELECT * FROM ready_items WHERE announced_at IS NULL AND seen_at IS NULL "
                     "ORDER BY created_at LIMIT 1").fetchone()
    if r is None:
        return None
    conn.execute("UPDATE ready_items SET announced_at=? WHERE root_id=?", (t, r["root_id"]))
    return dict(r)


def mark_seen(conn: sqlite3.Connection, root_id: str, now_ts: int | None = None) -> bool:
    ensure_ready(conn)
    cur = conn.execute("UPDATE ready_items SET seen_at=? WHERE root_id=? AND seen_at IS NULL",
                       (now() if now_ts is None else now_ts, root_id))
    return cur.rowcount > 0


_ID = re.compile(r"^[0-9a-f]{12}$")


def valid_id(s: str) -> bool:
    return bool(_ID.match(s or ""))
