"""Client projects: the workstreams above the task (docs/client-projects.md).

Phase one: the dashboard (/clients, /client/<key>) and this CLI. No MCP tool
reads or writes projects yet, so an agent sees no difference; master and Ben
set them here or on the page.

    python -m coord_mcp.projects add <client> <title> [--goal G] [--pipeline id,id]
    python -m coord_mcp.projects set <project> [--title T] [--goal G] [--state S]
                                     [--summary S] [--next N] [--pipeline id,id]
    python -m coord_mcp.projects attach <project> <task>...   # root tasks; children inherit
    python -m coord_mcp.projects detach <task>...
    python -m coord_mcp.projects list [<client>]
    python -m coord_mcp.projects show <client>                # the /client/<key> rollup as JSON
    python -m coord_mcp.projects dump <client>                # root tasks, for the backfill investigation
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from typing import Any

from .db import ensure_deps, ensure_projects, ensure_promotions

STATES = ("active", "waiting", "done", "dropped")
LIVE = ("active", "waiting")
SUMMARY_MAX = 600
LINE_MAX = 200
UNFILED_DAYS = 30
STALE_SUMMARY_DAYS = 7
FIELDS = ("id", "client", "title", "goal", "state", "pipeline_ids", "summary", "summary_at", "summary_by",
          "next_step", "sp_snapshot", "created_at", "closed_at")


def _now() -> int:
    import time
    return int(time.time())


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = {k: r[k] for k in FIELDS}
    d["pipeline_ids"] = json.loads(d["pipeline_ids"] or "[]")
    d["sp_snapshot"] = json.loads(d["sp_snapshot"]) if d["sp_snapshot"] else None
    return d


def _pipeline_list(v: Any) -> list[str]:
    if v is None:
        return []
    items = v.split(",") if isinstance(v, str) else list(v)
    out = [str(x).strip() for x in items if str(x).strip()]
    for p in out:
        if len(p) > 40:
            raise ValueError(f"pipeline id {p[:20]!r}... is {len(p)} chars; cap is 40")
    return out


def _text(name: str, v: str | None, cap: int) -> str | None:
    if v is None:
        return None
    v = v.strip()
    if len(v) > cap:
        raise ValueError(f"{name} is {len(v)} chars; cap is {cap}")
    return v or None


# ------------------------------------------------------------ writes


def add(conn: sqlite3.Connection, client: str, title: str, goal: str | None = None,
        pipeline_ids: Any = None, state: str = "active") -> dict[str, Any]:
    ensure_projects(conn)
    client, title = (client or "").strip(), _text("title", title, LINE_MAX)
    if not client:
        raise ValueError("client is required")
    if not title:
        raise ValueError("title is required")
    if state not in STATES:
        raise ValueError(f"state must be one of {', '.join(STATES)}")
    if conn.execute("SELECT 1 FROM projects WHERE client=? AND title=? COLLATE NOCASE AND state IN ('active','waiting')",
                    (client, title)).fetchone():
        raise ValueError(f"{client} already has a live project titled {title!r}")
    pid = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO projects(id, client, title, goal, state, pipeline_ids, created_at) VALUES (?,?,?,?,?,?,?)",
        (pid, client, title, _text("goal", goal, LINE_MAX), state, json.dumps(_pipeline_list(pipeline_ids)), _now()))
    return get(conn, pid)


def get(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    ensure_projects(conn)
    r = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if r is None:
        raise ValueError(f"no project {project_id!r}")
    return _row(r)


def update(conn: sqlite3.Connection, project_id: str, *, by: str = "ben", **fields: Any) -> dict[str, Any]:
    """Set any of title, goal, state, summary, next_step, pipeline_ids. A
    summary stamps summary_at and summary_by; a terminal state stamps closed_at."""
    cur = get(conn, project_id)
    sets: dict[str, Any] = {}
    for k in ("title", "goal", "next_step"):
        if fields.get(k) is not None:
            sets[k] = _text(k, fields[k], LINE_MAX)
    if sets.get("title") is None and "title" in sets:
        raise ValueError("title cannot be blank")
    if fields.get("summary") is not None and _text("summary", fields["summary"], SUMMARY_MAX) != cur["summary"]:
        sets["summary"] = _text("summary", fields["summary"], SUMMARY_MAX)
        sets["summary_at"], sets["summary_by"] = _now(), by
    if fields.get("pipeline_ids") is not None:
        sets["pipeline_ids"] = json.dumps(_pipeline_list(fields["pipeline_ids"]))
    if fields.get("state") is not None:
        st = fields["state"]
        if st not in STATES:
            raise ValueError(f"state must be one of {', '.join(STATES)}")
        sets["state"] = st
        sets["closed_at"] = None if st in LIVE else (cur["closed_at"] or _now())
    sets = {k: v for k, v in sets.items() if k in ("summary_at", "summary_by") or v != cur.get(k)}
    if not sets:
        return cur
    conn.execute(f"UPDATE projects SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
                 (*sets.values(), project_id))
    return get(conn, project_id)


def attach(conn: sqlite3.Connection, project_id: str, task_ids: list[str]) -> dict[str, Any]:
    """File tasks under a project. Children inherit, so attach roots. A task
    of another client is refused: a project is one client's."""
    p = get(conn, project_id)
    done = []
    for tid in task_ids:
        r = conn.execute("SELECT assigned_to FROM tasks WHERE id=?", (tid,)).fetchone()
        if r is None:
            raise ValueError(f"no task {tid!r}")
        if (r["assigned_to"] or "") != p["client"]:
            raise ValueError(f"task {tid} is {r['assigned_to']}'s, project {project_id} is {p['client']}'s")
        conn.execute("UPDATE tasks SET project_id=? WHERE id=?", (project_id, tid))
        done.append(tid)
    return {"ok": True, "project_id": project_id, "attached": done}


def detach(conn: sqlite3.Connection, task_ids: list[str]) -> dict[str, Any]:
    ensure_projects(conn)
    for tid in task_ids:
        conn.execute("UPDATE tasks SET project_id=NULL WHERE id=?", (tid,))
    return {"ok": True, "detached": list(task_ids)}


# ------------------------------------------------------------ membership


def _up(r: sqlite3.Row) -> str | None:
    """The task a task inherits from: its parent, else the code it checks in."""
    if r["parent_id"]:
        return r["parent_id"]
    try:
        spec = json.loads(r["spec"] or "{}")
    except ValueError:
        return None
    return str(spec["checkin_of"]) if isinstance(spec, dict) and spec.get("checkin_of") else None


def project_of(conn: sqlite3.Connection, task_id: str) -> str | None:
    """A task's project: its own, else its parent chain's, else (for a
    check-in) the code it checks in. Same walk as queue.pipeline_id."""
    ensure_projects(conn)
    seen, tid = set(), task_id
    while tid and tid not in seen:
        seen.add(tid)
        r = conn.execute("SELECT parent_id, spec, project_id FROM tasks WHERE id=?", (tid,)).fetchone()
        if r is None:
            return None
        if r["project_id"]:
            return r["project_id"]
        tid = _up(r)
    return None


def single_pipeline(conn: sqlite3.Connection, task_id: str) -> str | None:
    """The pipeline item a task bills to through its project, when the
    project names exactly one. Several give none: the task must say which."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'").fetchone() is None:
        return None
    pid = project_of(conn, task_id)
    if not pid:
        return None
    r = conn.execute("SELECT pipeline_ids FROM projects WHERE id=?", (pid,)).fetchone()
    ids = json.loads(r["pipeline_ids"] or "[]") if r else []
    return ids[0] if len(ids) == 1 else None


def _resolve_all(rows: dict[str, sqlite3.Row]) -> dict[str, str | None]:
    """project_of for every task in `rows` at once (one client's tasks), so a
    page with 900 tasks does not walk each chain with its own queries."""
    memo: dict[str, str | None] = {}

    def walk(tid: str) -> str | None:
        path, cur = [], tid
        while cur and cur not in memo:
            r = rows.get(cur)
            if r is None or cur in path:
                break
            path.append(cur)
            if r["project_id"]:
                memo[cur] = r["project_id"]
                break
            cur = _up(r)
        found = memo.get(cur) if cur else None
        for t in path:
            memo.setdefault(t, found)
        return memo.get(tid)

    for tid in rows:
        walk(tid)
    return memo


# ------------------------------------------------------------ reads


def list_projects(conn: sqlite3.Connection, client: str | None = None) -> list[dict[str, Any]]:
    ensure_projects(conn)
    sql, args = "SELECT * FROM projects", ()
    if client:
        sql, args = sql + " WHERE client=?", (client,)
    return [_row(r) for r in conn.execute(sql + " ORDER BY client, state='done' OR state='dropped', created_at", args)]


def _verdict(result: str | None) -> str | None:
    try:
        return json.loads(result or "{}").get("verdict")
    except ValueError:
        return None


def client_view(conn: sqlite3.Connection, client: str, now_ts: int | None = None) -> dict[str, Any]:
    """Everything /client/<key> shows: each project with its open tasks,
    loose ends, recent decisions and deliverables, then the unfiled roots."""
    ensure_projects(conn)
    ensure_deps(conn)
    ensure_promotions(conn)
    now_ts = now_ts or _now()
    rows = {r["id"]: r for r in conn.execute(
        """SELECT id, parent_id, kind, title, spec, state, project_id, result, created_at, completed_at, picked_up_at
           FROM tasks WHERE assigned_to=?""", (client,))}
    proj_of = _resolve_all(rows)
    projects = list_projects(conn, client)
    by_pid = {p["id"]: p for p in projects}
    for p in projects:
        p.update(open=[], recent=[], loose=[], decisions=[], deliverables=[], task_count=0, last_ts=p["created_at"])

    waits: dict[str, list[str]] = {}
    for r in conn.execute("""SELECT d.task_id, d.blocked_by FROM task_deps d JOIN tasks t ON t.id=d.task_id
                             WHERE t.assigned_to=? AND d.cleared_at IS NULL""", (client,)):
        b = rows.get(r["blocked_by"])
        if b is None or b["state"] != "done" or b["kind"] == "draft_reply":
            waits.setdefault(r["task_id"], []).append(r["blocked_by"])

    reviews: dict[str, str] = {}  # deliverable id -> newest dod_review verdict
    for r in sorted(rows.values(), key=lambda r: r["created_at"]):
        if r["kind"] == "dod_review" and r["parent_id"] and r["state"] == "done":
            reviews[r["parent_id"]] = _verdict(r["result"]) or "done"
    promoted = {r["task_id"]: r["version"] for r in conn.execute(
        "SELECT p.task_id, MAX(p.version) version FROM promotions p JOIN tasks t ON t.id=p.task_id "
        "WHERE t.assigned_to=? GROUP BY p.task_id", (client,))}

    for tid, r in rows.items():
        p = by_pid.get(proj_of.get(tid) or "")
        if p is None:
            continue
        p["task_count"] += 1
        p["last_ts"] = max(p["last_ts"], r["completed_at"] or r["picked_up_at"] or r["created_at"])
        brief = {"id": tid, "title": r["title"], "kind": r["kind"], "state": r["state"],
                 "since": r["completed_at"] or r["created_at"], "picked_up": bool(r["picked_up_at"]),
                 "waits_on": waits.get(tid, [])}
        if r["state"] == "open":
            p["open"].append(brief)
        else:
            p["recent"].append(brief)
        if r["kind"] == "deliverable":
            if tid in promoted:
                stage = f"promoted v{promoted[tid]}"
            elif tid in reviews:
                stage = f"review: {reviews[tid]}"
            else:
                stage = "written" if r["state"] == "done" else r["state"]
            p["deliverables"].append({**brief, "stage": stage})

    ids = [t for t in rows if proj_of.get(t)]
    for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(f"""SELECT id, task_id, who, what, created_at FROM follow_ups
                                  WHERE state='open' AND task_id IN ({marks})""", chunk):
            by_pid[proj_of[r["task_id"]]]["loose"].append(dict(r))
        for r in conn.execute(f"""SELECT id, task_id, statement, because, created_at FROM decisions
                                  WHERE task_id IN ({marks})""", chunk):
            by_pid[proj_of[r["task_id"]]]["decisions"].append(dict(r))
    for p in projects:
        p["open"].sort(key=lambda d: d["since"])
        p["recent"] = sorted(p["recent"], key=lambda d: -d["since"])[:5]
        p["loose"].sort(key=lambda d: d["created_at"])
        p["decisions"] = sorted(p["decisions"], key=lambda d: -d["created_at"])[:3]
        p["deliverables"] = sorted(p["deliverables"], key=lambda d: -d["since"])[:6]
        p["summary_stale"] = (p["state"] in LIVE and bool(p["open"])
                              and now_ts - (p["summary_at"] or 0) > STALE_SUMMARY_DAYS * 86400)

    unfiled = []
    for tid, r in rows.items():
        if proj_of.get(tid) or _up(r) or r["created_at"] < now_ts - UNFILED_DAYS * 86400:
            continue
        unfiled.append({"id": tid, "title": r["title"], "kind": r["kind"], "state": r["state"],
                        "since": r["created_at"]})
    unfiled.sort(key=lambda d: -d["since"])
    live = sorted((p for p in projects if p["state"] in LIVE), key=lambda p: (p["state"] != "active", -p["last_ts"]))
    closed = sorted((p for p in projects if p["state"] not in LIVE), key=lambda p: -(p["closed_at"] or 0))
    return {"client": client, "as_of": now_ts, "projects": live, "closed": closed, "unfiled": unfiled,
            "task_count": len(rows), "filed": sum(1 for t in rows if proj_of.get(t))}


def clients_overview(conn: sqlite3.Connection, clients: list[str], now_ts: int | None = None) -> list[dict[str, Any]]:
    """One row per client for /clients: projects, open work, loose ends,
    unfiled roots, and the stalest summary among live projects."""
    ensure_projects(conn)
    now_ts = now_ts or _now()
    out = []
    for c in clients:
        v = client_view(conn, c, now_ts)
        live = v["projects"]
        open_tasks = conn.execute("SELECT COUNT(*) FROM tasks WHERE assigned_to=? AND state='open'", (c,)).fetchone()[0]
        loose = conn.execute("""SELECT COUNT(*) FROM follow_ups f JOIN tasks t ON t.id=f.task_id
                                WHERE t.assigned_to=? AND f.state='open'""", (c,)).fetchone()[0]
        ages = [now_ts - (p["summary_at"] or p["created_at"]) for p in live]
        out.append({"client": c, "active": sum(p["state"] == "active" for p in live),
                    "waiting": sum(p["state"] == "waiting" for p in live), "open_tasks": open_tasks,
                    "loose": loose, "unfiled": len(v["unfiled"]), "tasks": v["task_count"], "filed": v["filed"],
                    "stale": sum(p["summary_stale"] for p in live), "oldest_summary": max(ages) if ages else None,
                    "titles": [p["title"] for p in live][:4]})
    return out


def dump(conn: sqlite3.Connection, client: str) -> list[dict[str, Any]]:
    """A client's root tasks with what the backfill needs to group them."""
    ensure_projects(conn)
    out = []
    for r in conn.execute("""SELECT id, parent_id, kind, title, spec, state, project_id, pipeline_task_id, created_at,
                                    (SELECT COUNT(*) FROM tasks c WHERE c.parent_id=t.id) children
                             FROM tasks t WHERE assigned_to=? ORDER BY created_at""", (client,)):
        if _up(r):
            continue
        out.append({"id": r["id"], "title": r["title"], "kind": r["kind"], "state": r["state"],
                    "project_id": r["project_id"], "pipeline_task_id": r["pipeline_task_id"],
                    "children": r["children"], "created_at": r["created_at"]})
    return out


# ------------------------------------------------------------ CLI


def _opts(args: list[str]) -> tuple[list[str], dict[str, str]]:
    pos, opt, i = [], {}, 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            if i + 1 >= len(args):
                raise ValueError(f"{a} needs a value")
            opt[a[2:].replace("-", "_")] = args[i + 1]
            i += 2
        else:
            pos.append(a)
            i += 1
    return pos, opt


def main(argv: list[str] | None = None) -> int:
    from .db import connect
    argv = sys.argv[1:] if argv is None else argv
    need = {"add": 2, "set": 1, "attach": 2, "detach": 1, "list": 0, "show": 1, "dump": 1}
    if not argv or argv[0] not in need:
        print(__doc__.strip())
        return 2
    cmd = argv[0]
    conn = connect()
    try:
        pos, opt = _opts(argv[1:])
        if len(pos) < need[cmd]:
            print(__doc__.strip())
            return 2
        if cmd == "add":
            r: Any = add(conn, pos[0], " ".join(pos[1:]), opt.get("goal"), opt.get("pipeline"))
        elif cmd == "set":
            if "next" in opt:
                opt["next_step"] = opt.pop("next")
            if "pipeline" in opt:
                opt["pipeline_ids"] = opt.pop("pipeline")
            r = update(conn, pos[0], by=opt.pop("by", "master"), **opt)
        elif cmd == "attach":
            r = attach(conn, pos[0], pos[1:])
        elif cmd == "detach":
            r = detach(conn, pos)
        elif cmd == "list":
            r = list_projects(conn, pos[0] if pos else None)
        elif cmd == "show":
            r = client_view(conn, pos[0])
        else:
            r = dump(conn, pos[0])
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(json.dumps(r, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
