"""Pipeline lines: every task rolls up to one SharePoint pipeline item, the
same line Ben enters on his timesheet (Ben, 2026-11-15; docs/pipeline-lines.md).

Three pieces:

- **A cache** of the pipeline (`pipeline_items`), filled by the master-console
  mod from sharepoint-pipeline's get_pipeline every 30 minutes, so the
  dashboard can rank lines by priority, status and end date. Read only:
  SharePoint stays the system of record.
- **Proposals** (`pipeline_proposals`) for work that has no item yet. A task
  links to one as `pipeline_task_id = "p:<proposal id>"`, so inheritance,
  hours and the board treat it as a line at once. Ben approves it on
  /pipeline (Create, Use existing, Drop); the mod creates the SharePoint item
  and every task under the proposal is relinked to the real id.
- **The creation rule** (`check_create`, switch `require_pipeline`): a root
  task for a client must name a pipeline item or a proposal. Children and
  check-ins inherit theirs, so only the first task of a chain is asked.

    python -m coord_mcp.pipeline sync <get_pipeline.json>     # fill the cache by hand
    python -m coord_mcp.pipeline lines [<client>]             # the /pipeline ranking as text
    python -m coord_mcp.pipeline unfiled [<client>] [--days N]
    python -m coord_mcp.pipeline propose <client> <title> [--origin ID] [--priority P] [--task T]
    python -m coord_mcp.pipeline proposals [<client>]
    python -m coord_mcp.pipeline approve|drop <proposal>
    python -m coord_mcp.pipeline link <proposal> <item id>    # an item made by hand, or an existing one
    python -m coord_mcp.pipeline set <task> <item id|p:proposal|none>
    python -m coord_mcp.pipeline client-map [<key> <SharePoint client name>]
    python -m coord_mcp.pipeline backfill dump <client> [--days N] [--out file.csv]
    python -m coord_mcp.pipeline backfill apply <file.csv|json> [--yes]
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import sys
import time
import uuid
from datetime import date
from typing import Any

PREFIX = "p:"
STATES = ("proposed", "approved", "created", "linked", "dropped")
OPEN_STATES = ("proposed", "approved")
PRIORITIES = ("High", "Medium", "Low")
PRIORITY_RANK = {"High": 0, "Medium": 1, "Low": 2}
DONE_STATUS = ("Complete",)
NOT_CLIENTS = ("Master", "Workflows")
WINDOW_DAYS = 60
UNFILED_DAYS = 30
CREATE_LEASE = 120
CREATE_RETRY = 1800
TITLE_MAX = 200
SYNC_KEY = "pipeline_synced_at"
MAP_KEY = "pipeline_client_map"
LEASE_KEY = "pipeline_create_lease"
ERRORS_KEY = "pipeline_create_errors"
HOLD_KEY = "pipeline_create_hold"  # answered ok with no item id: never retried, Ben links it
BECAUSE_BEN = "Ben's click on /pipeline"

DDL = """
CREATE TABLE IF NOT EXISTS pipeline_items (
  id          TEXT PRIMARY KEY,           -- SharePoint item number, as text
  client      TEXT,                       -- board client key, when it maps
  sp_client   TEXT,                       -- SharePoint's client name
  title       TEXT NOT NULL,
  status      TEXT,
  priority    TEXT,
  developer   TEXT,
  start_date  TEXT,
  end_date    TEXT,
  est_low     REAL,
  est_high    REAL,
  pct         INTEGER,
  cvc         INTEGER,
  modified    TEXT,
  synced_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS pipeline_items_client ON pipeline_items(client, status);
CREATE TABLE IF NOT EXISTS pipeline_proposals (
  id          TEXT PRIMARY KEY,
  client      TEXT NOT NULL,
  title       TEXT NOT NULL,
  origin_id   TEXT,
  priority    TEXT,
  note        TEXT,
  state       TEXT NOT NULL CHECK (state IN ('proposed','approved','created','linked','dropped')),
  pipeline_id TEXT,
  task_id     TEXT,
  follow_up_id TEXT,
  decision_id TEXT,
  detail      TEXT,
  created_at  INTEGER NOT NULL,
  decided_at  INTEGER,
  done_at     INTEGER
);
CREATE INDEX IF NOT EXISTS pipeline_proposals_state ON pipeline_proposals(state, client);
"""

REFUSAL = ("no pipeline item: every task rolls up to the SharePoint pipeline item Ben bills it to (Ben, "
           "2026-11-15). Pass pipeline_task_id (the item number, e.g. \"231\"), or parent_id if this "
           "continues an existing chain, or pipeline_proposal {title, origin_id?, priority?, note?} when "
           "no item exists yet: the task proceeds on the proposal and Ben approves the item on /pipeline. "
           "`python -m coord_mcp.pipeline lines <client>` in agent-tracking/ lists the client's items.")


def _now() -> int:
    return int(time.time())


def _id() -> str:
    return uuid.uuid4().hex[:12]


def ensure_pipeline(conn: sqlite3.Connection) -> None:
    """Create the two tables on first use (no schema bump, like projects).
    executescript commits, so call it before any BEGIN."""
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('pipeline_items','pipeline_proposals')")}
    if len(have) < 2:
        conn.executescript(DDL)


def _meta(conn: sqlite3.Connection, key: str) -> Any:
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if r is None or r[0] is None:
        return None
    try:
        return json.loads(r[0])
    except ValueError:
        return r[0]


def _set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    if value is None:
        conn.execute("DELETE FROM meta WHERE key=?", (key,))
    else:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, json.dumps(value)))


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def is_proposal(pid: str | None) -> bool:
    return bool(pid) and str(pid).startswith(PREFIX)


# ------------------------------------------------------------ clients


def client_keys(conn: sqlite3.Connection) -> list[str]:
    from . import hours
    seen = list(hours.CLIENTS)
    for r in conn.execute("SELECT DISTINCT assigned_to FROM tasks WHERE assigned_to IS NOT NULL ORDER BY 1"):
        if r[0] not in seen and r[0] not in NOT_CLIENTS:
            seen.append(r[0])
    return seen


def client_map(conn: sqlite3.Connection) -> dict[str, str]:
    """SharePoint client name -> board key overrides (`client-map`)."""
    m = _meta(conn, MAP_KEY)
    return m if isinstance(m, dict) else {}


def map_client(sp_client: str | None, keys: list[str], overrides: dict[str, str]) -> str | None:
    """The board key a SharePoint client name belongs to: an override, else the
    key the name starts with or holds as a word ("Royal Technologies" -> Royal,
    "Cascade Die Casting" -> Cascade)."""
    if not sp_client:
        return None
    if sp_client in overrides:
        return overrides[sp_client]
    n = _norm(sp_client)
    for k in sorted(keys, key=len, reverse=True):
        nk = _norm(k)
        if nk and (n == nk or n.startswith(nk + " ") or f" {nk} " in f" {n} "):
            return k
    return None


# ------------------------------------------------------------ cache


def _items_of(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        raise ValueError("pipeline payload is not a JSON object or list")
    out: list[dict[str, Any]] = []
    for k in ("items", "unscheduledItems", "scheduled", "unscheduled"):
        v = payload.get(k)
        if isinstance(v, list):
            out += [x for x in v if isinstance(x, dict)]
    return out


def sync(conn: sqlite3.Connection, payload: Any, now_ts: int | None = None) -> dict[str, Any]:
    """Replace the cache with get_pipeline's answer (scheduled and
    unscheduled). A payload with no items is refused, so a failed read
    never empties the cache."""
    ensure_pipeline(conn)
    t = _now() if now_ts is None else now_ts
    rows = _items_of(payload)
    if not rows:
        raise ValueError("pipeline payload has no items; the cache is left as it was")
    keys, over = client_keys(conn), client_map(conn)
    unmapped: dict[str, int] = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM pipeline_items")
        for x in rows:
            if x.get("id") is None or not x.get("title"):
                continue
            key = map_client(x.get("client"), keys, over)
            if key is None and x.get("client"):
                unmapped[x["client"]] = unmapped.get(x["client"], 0) + 1
            conn.execute(
                "INSERT OR REPLACE INTO pipeline_items(id, client, sp_client, title, status, priority, developer, "
                "start_date, end_date, est_low, est_high, pct, cvc, modified, synced_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(x["id"]), key, x.get("client"), str(x["title"]).strip(), x.get("status"), x.get("priority"),
                 x.get("developer"), x.get("startDate"), x.get("endDate"), x.get("estHoursLow"),
                 x.get("estHoursHigh"), x.get("pct"), x.get("cvcTicket"), x.get("modified"), t))
        _set_meta(conn, SYNC_KEY, t)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    n = conn.execute("SELECT COUNT(*) FROM pipeline_items").fetchone()[0]
    return {"ok": True, "items": n, "unmapped_clients": unmapped, "synced_at": t}


def synced_at(conn: sqlite3.Connection) -> int | None:
    v = _meta(conn, SYNC_KEY)
    return int(v) if isinstance(v, (int, float)) else None


def items(conn: sqlite3.Connection, client: str | None = None) -> dict[str, dict[str, Any]]:
    ensure_pipeline(conn)
    sql, args = "SELECT * FROM pipeline_items", ()
    if client:
        sql, args = sql + " WHERE client=?", (client,)
    return {r["id"]: dict(r) for r in conn.execute(sql, args)}


def label(item: dict[str, Any] | None, pid: str) -> str:
    """The timesheet line: the item's title, or for a proposal its origin id
    and title, as the SharePoint item will be named."""
    if item is None:
        return f"#{pid}"
    if "origin_id" in item:
        return f"{item['origin_id']} {item['title']}".strip() if item.get("origin_id") else item["title"]
    return item["title"]


# ------------------------------------------------------------ proposals


def _proposal(conn: sqlite3.Connection, pid: str) -> dict[str, Any]:
    ensure_pipeline(conn)
    pid = pid[len(PREFIX):] if pid.startswith(PREFIX) else pid
    r = conn.execute("SELECT * FROM pipeline_proposals WHERE id=?", (pid,)).fetchone()
    if r is None:
        raise KeyError(f"unknown pipeline proposal '{pid}'")
    return dict(r)


def proposals(conn: sqlite3.Connection, client: str | None = None, states: tuple[str, ...] = OPEN_STATES
              ) -> list[dict[str, Any]]:
    ensure_pipeline(conn)
    q = f"SELECT * FROM pipeline_proposals WHERE state IN ({','.join('?' * len(states))})"
    args: list[Any] = list(states)
    if client:
        q += " AND client=?"
        args.append(client)
    out = [dict(r) for r in conn.execute(q + " ORDER BY created_at", args)]
    for p in out:
        p["tasks"] = conn.execute("SELECT COUNT(*) FROM tasks WHERE pipeline_task_id=?",
                                  (PREFIX + p["id"],)).fetchone()[0]
    return out


def _clean_proposal(v: Any) -> dict[str, Any]:
    if isinstance(v, str):
        v = {"title": v}
    if not isinstance(v, dict):
        raise ValueError("pipeline_proposal must be an object {title, origin_id?, priority?, note?}")
    extra = set(v) - {"title", "origin_id", "priority", "note"}
    if extra:
        raise ValueError(f"pipeline_proposal has unknown keys {sorted(extra)}; it takes title, origin_id, "
                         "priority, note")
    title = str(v.get("title") or "").strip()
    if not title or len(title) > TITLE_MAX:
        raise ValueError(f"pipeline_proposal title must be 1-{TITLE_MAX} characters")
    pri = v.get("priority")
    if pri not in (None, "") and pri not in PRIORITIES:
        raise ValueError(f"pipeline_proposal priority must be one of {PRIORITIES}")
    origin = str(v.get("origin_id") or "").strip() or None
    if origin and len(origin) > 40:
        raise ValueError("pipeline_proposal origin_id is over 40 characters")
    note = str(v.get("note") or "").strip() or None
    return {"title": title, "origin_id": origin, "priority": pri or None, "note": (note or "")[:1000] or None}


def propose(conn: sqlite3.Connection, client: str, proposal: Any, task_id: str | None = None,
            now_ts: int | None = None) -> dict[str, Any]:
    """Open a proposal, or reuse the open one for the same client and title.
    Ben's queue gets one item per client ("Approve N proposed pipeline items
    for <client> on /pipeline"), kept current as proposals come and go."""
    ensure_pipeline(conn)
    p = _clean_proposal(proposal)
    if not client or client in NOT_CLIENTS:
        raise ValueError("a pipeline proposal needs a client (assigned_to)")
    t = _now() if now_ts is None else now_ts
    want = _norm(label(p, ""))
    for r in conn.execute("SELECT * FROM pipeline_proposals WHERE client=? AND state IN ('proposed','approved')",
                          (client,)):
        if _norm(label(dict(r), "")) == want:
            row = dict(r)
            if task_id and not row["task_id"]:
                conn.execute("UPDATE pipeline_proposals SET task_id=? WHERE id=?", (task_id, row["id"]))
            _ben_item(conn, client, task_id or row["task_id"], t)
            return {**row, "reused": True, "pipeline_task_id": PREFIX + row["id"]}
    pid = _id()
    conn.execute("INSERT INTO pipeline_proposals(id, client, title, origin_id, priority, note, state, task_id, "
                 "created_at) VALUES (?,?,?,?,?,?,'proposed',?,?)",
                 (pid, client, p["title"], p["origin_id"], p["priority"], p["note"], task_id, t))
    _ben_item(conn, client, task_id, t)
    return {**_proposal(conn, pid), "reused": False, "pipeline_task_id": PREFIX + pid}


def _ben_item(conn: sqlite3.Connection, client: str, task_id: str | None, t: int) -> str | None:
    """One open Ben item per client for its proposals awaiting him; its text
    carries the count. Closed when none await (see _settle_ben_item)."""
    ref = f"pipeline:{client}"
    n = conn.execute("SELECT COUNT(*) FROM pipeline_proposals WHERE client=? AND state='proposed'",
                     (client,)).fetchone()[0]
    what = (f"Approve {n} proposed pipeline item{'s' if n != 1 else ''} for {client} on /pipeline "
            "(Create, Use existing or Drop)")
    r = conn.execute("SELECT id FROM follow_ups WHERE ref=? AND state='open'", (ref,)).fetchone()
    if r is not None:
        conn.execute("UPDATE follow_ups SET what=? WHERE id=?", (what, r["id"]))
        fid = r["id"]
    elif n and task_id:
        fid = _id()
        idx = conn.execute("SELECT COALESCE(MAX(idx), -1) + 1 FROM follow_ups WHERE task_id=?",
                           (task_id,)).fetchone()[0]
        conn.execute("INSERT INTO follow_ups(id, task_id, idx, who, what, state, created_at, client, ref) "
                     "VALUES (?,?,?,'Ben',?,'open',?,?,?)", (fid, task_id, idx, what, t, client, ref))
    else:
        return None
    conn.execute("UPDATE pipeline_proposals SET follow_up_id=? WHERE client=? AND state='proposed'", (fid, client))
    return fid


def _settle_ben_item(conn: sqlite3.Connection, client: str, decision_id: str) -> None:
    left = conn.execute("SELECT COUNT(*) FROM pipeline_proposals WHERE client=? AND state='proposed'",
                        (client,)).fetchone()[0]
    r = conn.execute("SELECT id FROM follow_ups WHERE ref=? AND state='open'", (f"pipeline:{client}",)).fetchone()
    if r is None:
        return
    if left:
        _ben_item(conn, client, None, _now())
        return
    from . import store
    store.resolve_follow_up(conn, r["id"], decision_id=decision_id)


def relink(conn: sqlite3.Connection, proposal_id: str, item_id: str | None) -> int:
    cur = conn.execute("UPDATE tasks SET pipeline_task_id=? WHERE pipeline_task_id=?",
                       (item_id, PREFIX + proposal_id))
    return cur.rowcount


def decide(conn: sqlite3.Connection, proposal_id: str, action: str, item_id: str | None = None,
           by: str = BECAUSE_BEN, now_ts: int | None = None) -> dict[str, Any]:
    """Ben's answer on a proposal. approve: the mod creates the SharePoint
    item next tick. link: an existing item covers it, so its tasks move there
    now. drop: no line; its tasks fall back to No pipeline item."""
    from . import store
    p = _proposal(conn, proposal_id)
    t = _now() if now_ts is None else now_ts
    if action == "link":
        return link(conn, p["id"], item_id or "", by=by, now_ts=t)
    if p["state"] not in OPEN_STATES:
        raise ValueError(f"proposal {p['id']} is {p['state']}; nothing to decide")
    if action == "approve":
        if p["state"] == "approved":
            return {"ok": True, "proposal_id": p["id"], "state": "approved", "unchanged": True}
        dec = store.record_decision(conn, f"Pipeline proposal {p['id']} approved: create '{label(p, '')}' "
                                          f"for {p['client']}"[:200], by, p["task_id"])
        conn.execute("UPDATE pipeline_proposals SET state='approved', decided_at=?, decision_id=? WHERE id=?",
                     (t, dec["decision_id"], p["id"]))
    elif action == "drop":
        dec = store.record_decision(conn, f"Pipeline proposal {p['id']} dropped: '{label(p, '')}' "
                                          f"({p['client']})"[:200], by, p["task_id"])
        conn.execute("UPDATE pipeline_proposals SET state='dropped', decided_at=?, decision_id=? WHERE id=?",
                     (t, dec["decision_id"], p["id"]))
        relink(conn, p["id"], None)
    else:
        raise ValueError(f"unknown action {action!r}; approve, link or drop")
    _settle_ben_item(conn, p["client"], dec["decision_id"])
    return {"ok": True, "proposal_id": p["id"], "state": "approved" if action == "approve" else "dropped",
            "decision_id": dec["decision_id"]}


def link(conn: sqlite3.Connection, proposal_id: str, item_id: str, by: str = "master",
         now_ts: int | None = None, created: bool = False) -> dict[str, Any]:
    """Point a proposal at a real item (one Ben names, or one the mod or
    master just created) and move its tasks there."""
    from . import store
    item_id = str(item_id).strip().lstrip("#")
    if not item_id.isdigit():
        raise ValueError(f"item id {item_id!r} is not a SharePoint item number")
    p = _proposal(conn, proposal_id)
    if p["state"] in ("created", "linked", "dropped"):
        raise ValueError(f"proposal {p['id']} is {p['state']} already")
    t = _now() if now_ts is None else now_ts
    n = relink(conn, p["id"], item_id)
    word = "created as" if created else "linked to"
    dec = store.record_decision(conn, f"Pipeline proposal {p['id']} {word} item {item_id}: '{label(p, '')}' "
                                      f"({p['client']}), {n} task(s) relinked"[:200], by, p["task_id"])
    conn.execute("UPDATE pipeline_proposals SET state=?, pipeline_id=?, done_at=?, decided_at=COALESCE(decided_at, ?), "
                 "decision_id=? WHERE id=?",
                 ("created" if created else "linked", item_id, t, t, dec["decision_id"], p["id"]))
    if created and not conn.execute("SELECT 1 FROM pipeline_items WHERE id=?", (item_id,)).fetchone():
        conn.execute("INSERT INTO pipeline_items(id, client, title, status, priority, synced_at) "
                     "VALUES (?,?,?,'New',?,?)", (item_id, p["client"], label(p, ""), p["priority"], t))
    _settle_ben_item(conn, p["client"], dec["decision_id"])
    return {"ok": True, "proposal_id": p["id"], "pipeline_id": item_id, "relinked": n,
            "decision_id": dec["decision_id"]}


def sp_client(conn: sqlite3.Connection, key: str) -> str:
    """The SharePoint client name for a board key: the override, else the name
    its items already use, else the key itself."""
    for name, k in client_map(conn).items():
        if k == key:
            return name
    r = conn.execute("SELECT sp_client, COUNT(*) n FROM pipeline_items WHERE client=? AND sp_client IS NOT NULL "
                     "GROUP BY 1 ORDER BY n DESC LIMIT 1", (key,)).fetchone()
    return r["sp_client"] if r else key


def claim_create(conn: sqlite3.Connection, now_ts: int | None = None) -> dict[str, Any] | None:
    """One approved proposal for the mod to create in SharePoint, under a
    lease. Off with switch pipeline_create."""
    from . import queue
    ensure_pipeline(conn)
    if not queue.switch(conn, "pipeline_create"):
        return None
    t = _now() if now_ts is None else now_ts
    conn.execute("BEGIN IMMEDIATE")
    try:
        lease = _meta(conn, LEASE_KEY)
        if isinstance(lease, dict) and int(lease.get("until") or 0) > t:
            conn.execute("COMMIT")
            return None
        errors = _meta(conn, ERRORS_KEY) or {}
        held = set(_meta(conn, HOLD_KEY) or [])
        out = None
        for r in conn.execute("SELECT * FROM pipeline_proposals WHERE state='approved' ORDER BY decided_at"):
            if r["id"] in held or t - int(errors.get(r["id"]) or 0) < CREATE_RETRY:
                continue
            p = dict(r)
            note = (f"Proposed on the coord board (proposal {p['id']}"
                    + (f", first task {p['task_id']}" if p["task_id"] else "") + ")."
                    + (f" {p['note']}" if p["note"] else ""))
            out = {"proposal_id": p["id"], "title": label(p, ""), "client": sp_client(conn, p["client"]),
                   "priority": p["priority"], "note": note}
            _set_meta(conn, LEASE_KEY, {**out, "until": t + CREATE_LEASE})
            break
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return out


def _item_id_in(text: str) -> str | None:
    """create_task's answer names the new item: JSON with id, or text."""
    try:
        v = json.loads(text)
        for k in ("id", "itemId", "item_id"):
            if isinstance(v, dict) and str(v.get(k) or "").isdigit():
                return str(v[k])
        if isinstance(v, dict):
            for sub in ("item", "task", "created"):
                if isinstance(v.get(sub), dict) and str(v[sub].get("id") or "").isdigit():
                    return str(v[sub]["id"])
    except ValueError:
        pass
    m = re.search(r'"id"\s*:\s*(\d+)', text) or re.search(r"(?:\b(?:id|item)\b|#)\s*:?\s*#?(\d{1,6})\b", text, re.I)
    return m.group(1) if m else None


def create_done(conn: sqlite3.Connection, proposal_id: str, status: str, detail: str = "",
                item_id: str | None = None, now_ts: int | None = None) -> dict[str, Any]:
    t = _now() if now_ts is None else now_ts
    if status not in ("ok", "error"):
        raise ValueError(f"status must be ok or error, not {status!r}")
    lease = _meta(conn, LEASE_KEY)
    if not isinstance(lease, dict) or lease.get("proposal_id") != proposal_id:
        raise ValueError(f"proposal {proposal_id!r} has no open create claim")
    _set_meta(conn, LEASE_KEY, None)
    iid = (item_id or "").strip() or (_item_id_in(detail or "") if status == "ok" else None)
    if status == "ok" and not iid:
        # SharePoint may well have made the item: a retry could make a second
        # one, so hold it until Ben points the proposal at it (Use existing).
        held = [x for x in (_meta(conn, HOLD_KEY) or []) if x != proposal_id] + [proposal_id]
        _set_meta(conn, HOLD_KEY, held[-200:])
        msg = ("SharePoint answered ok but named no item id; not retried. Find the item and press Use existing. "
               f"Answer: {detail or ''}")
        conn.execute("UPDATE pipeline_proposals SET detail=? WHERE id=?", (msg[:300], proposal_id))
        return {"ok": True, "proposal_id": proposal_id, "status": "error", "held": True, "detail": msg[:200]}
    if status == "error" or not iid:
        errors = {k: v for k, v in (_meta(conn, ERRORS_KEY) or {}).items() if t - int(v) < CREATE_RETRY}
        errors[proposal_id] = t
        _set_meta(conn, ERRORS_KEY, errors)
        conn.execute("UPDATE pipeline_proposals SET detail=? WHERE id=?",
                     ((detail or "no item id in the answer")[:300], proposal_id))
        return {"ok": True, "proposal_id": proposal_id, "status": "error", "detail": (detail or "")[:200]}
    return {**link(conn, proposal_id, iid, by="master-console created it in SharePoint", now_ts=t, created=True),
            "status": "ok"}


# ------------------------------------------------------------ the rule


def check_create(conn: sqlite3.Connection, kind: str, spec: dict[str, Any], assigned_to: str | None,
                 parent_id: str | None, pipeline_task_id: str | None, proposal: Any) -> list[str]:
    """Refuse a root client task with no line (switch require_pipeline).
    Returns warnings for what passes. The proposal itself is opened by
    store.create_task once the task has an id."""
    from . import queue
    warnings: list[str] = []
    if proposal is not None and pipeline_task_id:
        raise ValueError("pass pipeline_task_id or pipeline_proposal, not both")
    if proposal is not None:
        _clean_proposal(proposal)
        if not assigned_to or assigned_to in NOT_CLIENTS:
            raise ValueError("pipeline_proposal needs assigned_to: a proposal belongs to a client")
        return warnings
    if is_proposal(pipeline_task_id):
        p = _proposal(conn, pipeline_task_id)
        if p["state"] == "dropped":
            raise ValueError(f"pipeline proposal {p['id']} was dropped by Ben; link another item")
        if p["state"] in ("created", "linked"):
            raise ValueError(f"pipeline proposal {p['id']} is now item {p['pipeline_id']}; pass "
                             f"pipeline_task_id \"{p['pipeline_id']}\"")
        return warnings
    if pipeline_task_id:
        ensure_pipeline(conn)
        if synced_at(conn) and not conn.execute("SELECT 1 FROM pipeline_items WHERE id=?",
                                                (pipeline_task_id,)).fetchone():
            warnings.append(f"pipeline_task_id {pipeline_task_id} is not in the cached pipeline: check the "
                            "item number (the cache refreshes every 30 minutes)")
        return warnings
    inherited = bool(parent_id) or bool(isinstance(spec, dict) and spec.get("checkin_of"))
    if inherited or not assigned_to or assigned_to in NOT_CLIENTS:
        return warnings
    if _switch(conn, "require_pipeline"):
        raise ValueError(REFUSAL)
    return warnings


# ------------------------------------------------------------ the view


def _resolver(conn: sqlite3.Connection):
    """task id -> pipeline id for every task at once, with the same walk as
    queue.pipeline_id (own, parent chain, checkin_of), then the project's one
    item."""
    rows: dict[str, tuple[str | None, str | None, str | None]] = {}
    for r in conn.execute("SELECT id, parent_id, spec, pipeline_task_id FROM tasks"):
        up = r["parent_id"]
        if not up and r["spec"] and "checkin_of" in r["spec"]:
            try:
                up = json.loads(r["spec"]).get("checkin_of")
            except (ValueError, AttributeError):
                up = None
        rows[r["id"]] = (up, r["pipeline_task_id"], None)
    memo: dict[str, str | None] = {}

    def resolve(tid: str) -> str | None:
        path: list[str] = []
        cur: str | None = tid
        out: str | None = None
        while True:
            if cur is None or cur not in rows or cur in path:
                break
            if cur in memo:
                out = memo[cur]
                break
            path.append(cur)
            up, own, _ = rows[cur]
            if own:
                out = own
                break
            cur = up
        for p in path:
            memo[p] = out
        return out

    return resolve, rows


def lines(conn: sqlite3.Connection, client: str | None = None, now_ts: int | None = None,
          days: int = WINDOW_DAYS) -> dict[str, Any]:
    """The /pipeline view: each line ranked (priority, overdue, end date,
    latest activity) with its board work, the proposals awaiting Ben, and the
    root tasks with no line."""
    ensure_pipeline(conn)
    t = _now() if now_ts is None else now_ts
    since = t - days * 86400
    resolve, _ = _resolver(conn)
    try:
        from .projects import single_pipeline
    except ImportError:  # pragma: no cover
        single_pipeline = None
    cache = items(conn)
    props = {PREFIX + p["id"]: p for p in proposals(conn, states=OPEN_STATES)}
    agg: dict[str, dict[str, Any]] = {}
    ben = {r["task_id"]: r["n"] for r in conn.execute(
        "SELECT task_id, COUNT(*) n FROM follow_ups WHERE state='open' AND who='Ben' AND task_id IS NOT NULL "
        "GROUP BY task_id")}
    for r in conn.execute("SELECT id, assigned_to, state, created_at, completed_at, title FROM tasks "
                          "WHERE created_at>=? OR completed_at>=? OR state='open'", (since, since)):
        if r["assigned_to"] in NOT_CLIENTS:
            continue
        pid = resolve(r["id"])
        if not pid and single_pipeline is not None:
            try:
                pid = single_pipeline(conn, r["id"])
            except sqlite3.OperationalError:
                pid = None
        if not pid:
            continue
        a = agg.setdefault(pid, {"open": 0, "done": 0, "tasks": 0, "ben": 0, "last": 0, "clients": set(),
                                 "open_titles": []})
        a["tasks"] += 1
        a["clients"].add(r["assigned_to"])
        a["ben"] += ben.get(r["id"], 0)
        if r["state"] == "open":
            a["open"] += 1
            if len(a["open_titles"]) < 4:
                a["open_titles"].append((r["id"], r["title"]))
        elif r["state"] == "done":
            a["done"] += 1
        a["last"] = max(a["last"], r["completed_at"] or 0, r["created_at"] or 0)
    today = date.fromtimestamp(t).isoformat()
    out = []
    keys = set(agg) | {k for k, v in cache.items() if (v.get("status") not in DONE_STATUS and v.get("client"))}
    keys |= set(props)
    for pid in keys:
        a = agg.get(pid, {"open": 0, "done": 0, "tasks": 0, "ben": 0, "last": 0, "clients": set(),
                          "open_titles": []})
        it = cache.get(pid)
        prop = props.get(pid)
        if is_proposal(pid) and prop is None:
            continue  # a dropped or finished proposal: its tasks moved
        cl = (it or {}).get("client") or (prop or {}).get("client") or (sorted(a["clients"])[0] if a["clients"] else None)
        if client and cl != client and client not in a["clients"]:
            continue
        pri = (it or prop or {}).get("priority")
        end = (it or {}).get("end_date")
        status = (it or {}).get("status") or ("Proposed" if prop else "unknown")
        overdue = bool(end and end < today and status not in DONE_STATUS)
        out.append({"pipeline_id": pid, "label": label(prop or it, pid) if (prop or it) else f"#{pid}",
                    "client": cl, "status": status, "priority": pri, "end_date": end, "overdue": overdue,
                    "est_low": (it or {}).get("est_low"), "est_high": (it or {}).get("est_high"),
                    "pct": (it or {}).get("pct"), "developer": (it or {}).get("developer"),
                    "proposal": prop, "known": bool(it or prop), "open": a["open"], "done": a["done"],
                    "tasks": a["tasks"], "ben": a["ben"], "last": a["last"] or None,
                    "open_titles": a["open_titles"]})
    out.sort(key=lambda x: (PRIORITY_RANK.get(x["priority"] or "", 3), not x["overdue"],
                            x["end_date"] or "9999", -(x["last"] or 0)))
    return {"lines": out, "proposals": [p for p in props.values() if p["state"] == "proposed"
                                        and (not client or p["client"] == client)],
            "approved": [p for p in props.values() if p["state"] == "approved"
                         and (not client or p["client"] == client)],
            "unfiled": unfiled(conn, client, t), "synced_at": synced_at(conn), "now": t,
            "require": _switch(conn, "require_pipeline")}


def _switch(conn: sqlite3.Connection, name: str) -> bool:
    from . import queue
    return queue.switch(conn, name)


def unfiled(conn: sqlite3.Connection, client: str | None = None, now_ts: int | None = None,
            days: int = UNFILED_DAYS) -> list[dict[str, Any]]:
    """Root client tasks from the last `days` (or still open) whose chain
    names no line."""
    t = _now() if now_ts is None else now_ts
    resolve, _ = _resolver(conn)
    try:
        from .projects import single_pipeline
    except ImportError:  # pragma: no cover
        single_pipeline = None
    q = ("SELECT id, kind, title, assigned_to, state, created_at, spec FROM tasks WHERE parent_id IS NULL "
         "AND assigned_to IS NOT NULL AND (created_at>=? OR state='open')")
    args: list[Any] = [t - days * 86400]
    if client:
        q += " AND assigned_to=?"
        args.append(client)
    out = []
    for r in conn.execute(q + " ORDER BY created_at DESC", args):
        if r["assigned_to"] in NOT_CLIENTS or (r["spec"] and '"checkin_of"' in r["spec"]):
            continue
        if resolve(r["id"]):
            continue
        if single_pipeline is not None:
            try:
                if single_pipeline(conn, r["id"]):
                    continue
            except sqlite3.OperationalError:
                pass
        out.append({"task_id": r["id"], "kind": r["kind"], "title": r["title"], "client": r["assigned_to"],
                    "state": r["state"], "created_at": r["created_at"]})
    return out


def set_task(conn: sqlite3.Connection, task_id: str, value: str | None) -> dict[str, Any]:
    """Link a task (normally a chain's root) to an item or a proposal; 'none'
    or empty unlinks it."""
    from . import store
    v = (value or "").strip().lstrip("#")
    if v.lower() in ("", "none"):
        return store.set_pipeline_task(conn, task_id, None)
    if is_proposal(v):
        p = _proposal(conn, v)
        if p["state"] not in OPEN_STATES:
            raise ValueError(f"proposal {p['id']} is {p['state']}")
        v = PREFIX + p["id"]
    elif not v.isdigit():
        raise ValueError(f"{v!r} is neither an item number nor p:<proposal>")
    return store.set_pipeline_task(conn, task_id, v)


# ------------------------------------------------------------ backfill


BACKFILL_COLS = ("task_id", "client", "kind", "title", "created", "tasks_in_chain", "pipeline_id",
                 "proposal_title", "origin_id", "priority")


def backfill_dump(conn: sqlite3.Connection, client: str, days: int = 90) -> list[dict[str, Any]]:
    """The client's unfiled root tasks with their chain size, for an
    investigation to map to items (fill pipeline_id) or proposals (fill
    proposal_title, origin_id). The same columns go back into apply."""
    roots = unfiled(conn, client, days=days)
    kids: dict[str, list[str]] = {}
    for r in conn.execute("SELECT id, parent_id FROM tasks WHERE parent_id IS NOT NULL"):
        kids.setdefault(r["parent_id"], []).append(r["id"])

    def size(tid: str) -> int:
        n, stack = 0, [tid]
        while stack:
            cur = stack.pop()
            n += 1
            stack += kids.get(cur, [])
        return n

    return [{"task_id": r["task_id"], "client": r["client"], "kind": r["kind"], "title": r["title"],
             "created": date.fromtimestamp(r["created_at"]).isoformat(), "tasks_in_chain": size(r["task_id"]),
             "pipeline_id": "", "proposal_title": "", "origin_id": "", "priority": ""} for r in roots]


def _read_rows(path: str) -> list[dict[str, Any]]:
    if path.lower().endswith(".json"):
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, list) else v.get("rows", [])
    if path.lower().endswith(".xlsx"):
        import openpyxl  # optional
        ws = openpyxl.load_workbook(path, read_only=True, data_only=True).active
        it = ws.iter_rows(values_only=True)
        head = [str(h or "").strip() for h in next(it)]
        return [{head[i]: ("" if v is None else str(v)) for i, v in enumerate(row) if i < len(head)} for row in it]
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def backfill_apply(conn: sqlite3.Connection, rows: list[dict[str, Any]], yes: bool = False) -> dict[str, Any]:
    """Link each row's root task to its pipeline_id, or to a proposal opened
    from proposal_title (rows naming the same title share one). Dry run
    unless yes."""
    plan, errors = [], []
    for i, r in enumerate(rows, 1):
        tid = str(r.get("task_id") or "").strip()
        pid = str(r.get("pipeline_id") or "").strip().lstrip("#")
        title = str(r.get("proposal_title") or "").strip()
        if not tid or not (pid or title):
            continue
        t = conn.execute("SELECT assigned_to FROM tasks WHERE id=?", (tid,)).fetchone()
        if t is None:
            errors.append(f"row {i}: unknown task {tid}")
            continue
        if pid and not pid.isdigit():
            errors.append(f"row {i}: pipeline_id {pid!r} is not an item number")
            continue
        plan.append({"task_id": tid, "client": t["assigned_to"], "pipeline_id": pid or None,
                     "proposal": None if pid else {"title": title, "origin_id": r.get("origin_id") or None,
                                                   "priority": r.get("priority") or None}})
    if not yes:
        return {"dry_run": True, "would_link": len(plan), "errors": errors, "plan": plan[:50]}
    done = 0
    for p in plan:
        if p["pipeline_id"]:
            set_task(conn, p["task_id"], p["pipeline_id"])
        else:
            prop = propose(conn, p["client"], p["proposal"], task_id=p["task_id"])
            set_task(conn, p["task_id"], prop["pipeline_task_id"])
        done += 1
    return {"dry_run": False, "linked": done, "errors": errors}


# ------------------------------------------------------------ CLI


def _opts(args: list[str]) -> tuple[list[str], dict[str, str]]:
    pos, opt, i = [], {}, 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            if a in ("--yes",):
                opt[a[2:]] = "1"
            else:
                opt[a[2:]] = args[i + 1] if i + 1 < len(args) else ""
                i += 1
        else:
            pos.append(a)
        i += 1
    return pos, opt


def main(argv: list[str] | None = None) -> int:
    from .db import connect
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, (pos, opt) = args[0], _opts(args[1:])
    conn = connect()
    ensure_pipeline(conn)
    try:
        if cmd == "sync":
            with open(pos[0], encoding="utf-8") as f:
                out: Any = sync(conn, f.read())
        elif cmd == "lines":
            v = lines(conn, pos[0] if pos else None)
            for x in v["lines"]:
                flag = " OVERDUE" if x["overdue"] else ""
                print(f"{x['priority'] or '-':<6} {x['pipeline_id']:<16} {x['client'] or '?':<9} "
                      f"{x['status']:<16} {x['end_date'] or '':<10} open {x['open']:<3} {x['label'][:70]}{flag}")
            print(f"\n{len(v['unfiled'])} root task(s) with no line; {len(v['proposals'])} proposal(s) await Ben")
            return 0
        elif cmd == "unfiled":
            out = unfiled(conn, pos[0] if pos else None, days=int(opt.get("days") or UNFILED_DAYS))
        elif cmd == "propose":
            out = propose(conn, pos[0], {"title": " ".join(pos[1:]), "origin_id": opt.get("origin"),
                                         "priority": opt.get("priority")}, task_id=opt.get("task"))
            if opt.get("task"):
                set_task(conn, opt["task"], out["pipeline_task_id"])
        elif cmd == "proposals":
            out = proposals(conn, pos[0] if pos else None)
        elif cmd in ("approve", "drop"):
            out = decide(conn, pos[0], cmd, by="master")
        elif cmd == "link":
            out = link(conn, pos[0], pos[1])
        elif cmd == "set":
            out = set_task(conn, pos[0], pos[1] if len(pos) > 1 else None)
        elif cmd == "client-map":
            m = client_map(conn)
            if len(pos) >= 2:
                m[" ".join(pos[1:])] = pos[0]
                _set_meta(conn, MAP_KEY, m)
            out = m
        elif cmd == "backfill" and pos and pos[0] == "dump":
            rows = backfill_dump(conn, pos[1], days=int(opt.get("days") or 90))
            if opt.get("out"):
                with open(opt["out"], "w", encoding="utf-8", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=BACKFILL_COLS)
                    w.writeheader()
                    w.writerows(rows)
                out = {"rows": len(rows), "out": opt["out"]}
            else:
                out = rows
        elif cmd == "backfill" and pos and pos[0] == "apply":
            out = backfill_apply(conn, _read_rows(pos[1]), yes="yes" in opt)
        else:
            print(__doc__)
            return 2
    except (KeyError, ValueError, IndexError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
