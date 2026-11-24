"""Where is X, and what is this id: two read-only helpers for the dashboard.

Ben spends about 8% of his messages to master asking where something stands
("is xx902.p done?") and about 7% asking what a bare id means. Both are
answered from rows the board already keeps, so nothing here writes:

- find() takes any id (or its first few characters), a title word, a program
  or file name, or a SharePoint item number, and returns one status line per
  case: its state, step, who has it, what it waits on and the last thing
  that happened. /find shows it; /find?client=X with no query is the compact
  per-client view; the task page shows its own case's line.
- labels() and annotate() put a short label next to every 12-character id
  in a piece of text (a task title, a decision statement, a loose end's
  text), at the moment the text is shown. Stored text never changes.
"""

from __future__ import annotations

import re
import sqlite3
import time
from html import escape
from typing import Any, Callable, Iterable
from urllib.parse import quote

# A board id: 12 lowercase hex characters standing alone, not inside a URL or
# a longer word; or q<n>, a presented question.
ID_RE = re.compile(r"(?<![0-9A-Za-z_/#=.-])([0-9a-f]{12}|q\d{1,6})(?![0-9A-Za-z_])")
LABEL_MAX = 48


def _short(s: Any, n: int = LABEL_MAX) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _in(conn: sqlite3.Connection, sql: str, ids: list[str]) -> list[sqlite3.Row]:
    out: list[sqlite3.Row] = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        out += conn.execute(sql.format(marks=",".join("?" * len(chunk))), chunk).fetchall()
    return out


def ids_in(*texts: Any) -> list[str]:
    """Every id-shaped token in the texts, in order, once each."""
    seen: dict[str, None] = {}
    for t in texts:
        for m in ID_RE.finditer(str(t or "")):
            seen.setdefault(m.group(1))
    return list(seen)


def labels(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, dict[str, str]]:
    """{id: {kind, label, href}} for the ids the board knows. Unknown ones are left out."""
    hexes = [i for i in dict.fromkeys(ids) if i and not i.startswith("q")]
    qs = [i for i in dict.fromkeys(ids) if i and i.startswith("q")]
    out: dict[str, dict[str, str]] = {}
    if hexes:
        for r in _in(conn, "SELECT id, title, assigned_to FROM tasks WHERE id IN ({marks})", hexes):
            out[r["id"]] = {"kind": "task", "label": r["title"], "href": f"/board/task/{r['id']}"}
        for r in _in(conn, "SELECT id, task_id, statement FROM decisions WHERE id IN ({marks})", hexes):
            out.setdefault(r["id"], {"kind": "decision", "label": r["statement"],
                                     "href": f"/find?q={r['id']}"})
        for r in _in(conn, "SELECT id, task_id, what FROM follow_ups WHERE id IN ({marks})", hexes):
            out.setdefault(r["id"], {"kind": "loose end", "label": r["what"], "href": f"/find?q={r['id']}"})
        for table, col, kind in (("workflow_cases", "title", "case"), ("intents", "summary", "intent"),
                                 ("commitments", "what", "commitment")):
            if _has(conn, table):
                for r in _in(conn, f"SELECT id, {col} AS t FROM {table} WHERE id IN ({{marks}})", hexes):
                    out.setdefault(r["id"], {"kind": kind, "label": r["t"], "href": f"/find?q={r['id']}"})
    if qs:
        nums = [q[1:] for q in qs]
        for r in _in(conn, "SELECT id, task_id, question FROM questions WHERE id IN ({marks})", nums):
            out[f"q{r['id']}"] = {"kind": "question", "label": r["question"],
                                  "href": f"/board/task/{r['task_id']}" if r["task_id"] else "/board"}
    return out


def annotate(conn: sqlite3.Connection, text: str, known: dict[str, dict[str, str]] | None = None,
             n: int = LABEL_MAX) -> str:
    """Plain text with ("label") after each known id. An id whose label
    already follows it is left alone, so annotating twice changes nothing."""
    if not text:
        return text
    known = labels(conn, ids_in(text)) if known is None else known

    def one(m: re.Match) -> str:
        k = known.get(m.group(1))
        if not k:
            return m.group(0)
        lab = _short(k["label"], n)
        tail = text[m.end():m.end() + len(lab) + 4]
        return m.group(0) if tail.startswith(f' ("{lab[:12]}') else f'{m.group(0)} ("{lab}")'
    return ID_RE.sub(one, text)


def html(text: Any, known: dict[str, dict[str, str]], plain: Callable[[str], str] = escape) -> str:
    """HTML for text with each known id linked and its label beside it.
    `plain` renders the text between ids (the task page passes its file linker)."""
    s = str(text if text is not None else "")
    if not known:
        return plain(s)
    out, at = [], 0
    for m in ID_RE.finditer(s):
        k = known.get(m.group(1))
        if not k:
            continue
        out += [plain(s[at:m.start()]), ref(m.group(1), k)]
        at = m.end()
    return "".join(out) + plain(s[at:])


def ref(i: str, k: dict[str, str] | None) -> str:
    """One id as a link with its label after it."""
    if not k:
        return f"<code>{escape(i)}</code>"
    return (f'<a class="idref" href="{escape(k["href"])}" title="{escape(k["kind"])}: {escape(k["label"][:300])}">'
            f'<code>{escape(i)}</code></a> <span class="idlabel">{escape(_short(k["label"]))}</span>')


# ------------------------------------------------------------ where is X

def _ago(ts: int | None, now: int) -> str:
    if not ts:
        return ""
    s = max(0, now - ts)
    return ("just now" if s < 60 else f"{s // 60}m ago" if s < 3600 else f"{s // 3600}h ago" if s < 172800
            else f"{s // 86400}d ago")


def _open_task_state(conn: sqlite3.Connection, t: sqlite3.Row) -> str:
    from .store import _open_status
    p = conn.execute("SELECT MAX(created_at) m FROM task_parks WHERE task_id=?", (t["id"],)).fetchone()["m"]
    return {"parked": "parked, owed a re-dispatch", "running": "picked up",
            "open": "not picked up yet"}[_open_status(p, t["picked_up_at"])]


def _last_event(conn: sqlite3.Connection, tasks: list[sqlite3.Row]) -> dict[str, Any] | None:
    """The newest thing that happened to any of these tasks: a pickup, park,
    close or failure, or a decision recorded on one."""
    if not tasks:
        return None
    ids = [t["id"] for t in tasks]
    title = {t["id"]: t["title"] for t in tasks}
    best = None
    for r in _in(conn, "SELECT task_id, event, ts FROM task_events WHERE task_id IN ({marks}) "
                       "ORDER BY ts DESC, rowid DESC LIMIT 1", ids):
        best = {"ts": r["ts"], "text": f'{r["event"].replace("_", " ")}: {_short(title[r["task_id"]], 60)}',
                "task": r["task_id"]}
    for r in _in(conn, "SELECT task_id, statement, created_at FROM decisions WHERE task_id IN ({marks}) "
                       "ORDER BY created_at DESC LIMIT 1", ids):
        if best is None or r["created_at"] >= best["ts"]:
            best = {"ts": r["created_at"], "text": f'decision: {_short(r["statement"], 80)}', "task": r["task_id"]}
    if best is None:
        t = max(tasks, key=lambda t: t["created_at"])
        best = {"ts": t["created_at"], "text": f'created: {_short(t["title"], 60)}', "task": t["id"]}
    return best


def _waits(conn: sqlite3.Connection, tasks: list[sqlite3.Row], waits_on: str | None, client: str | None) -> str:
    """What the work waits on, in words."""
    if not tasks:
        return ""
    open_t = [t for t in tasks if t["state"] == "open"]
    if open_t and waits_on in (None, "agent"):
        t = max(open_t, key=lambda t: t["created_at"])
        return f'{t["assigned_to"] or "agent"} on "{_short(t["title"], 60)}" ({_open_task_state(conn, t)})'
    ids = [t["id"] for t in tasks]
    fus = _in(conn, "SELECT who, what FROM follow_ups WHERE state='open' AND task_id IN ({marks}) "
                    "ORDER BY created_at", ids)
    ben = [f for f in fus if (f["who"] or "").strip().lower() == "ben"]
    if ben and waits_on in (None, "ben"):
        more = f" (+{len(ben) - 1} more)" if len(ben) > 1 else ""
        return f'Ben: "{_short(ben[0]["what"], 80)}"{more}'
    others = [f for f in fus if (f["who"] or "").strip().lower() != "ben"]
    if others:
        return f'master to task: "{_short(others[0]["what"], 80)}"' + (
            f" (+{len(others) - 1} more)" if len(others) > 1 else "")
    if any(t["state"] == "failed" for t in tasks) and waits_on == "master":
        return "master: a failed task to re-dispatch or decide"
    return ""


def _who(waits_on: str | None, client: str | None) -> str:
    return {"agent": f"{client or 'agent'} agent", "ben": "Ben", "master": "master"}.get(waits_on or "", "")


def case_status(conn: sqlite3.Connection, case: sqlite3.Row | dict[str, Any], now: int | None = None) -> dict[str, Any]:
    """One case's status: state, step, who has it, what it waits on, last event."""
    from .workflows import definitions as defs
    now = now or int(time.time())
    tasks = conn.execute("SELECT * FROM tasks WHERE case_id=? ORDER BY created_at", (case["id"],)).fetchall()
    d = defs.get(case["workflow"])
    steps = (d or {}).get("steps") or []
    names = {s["key"]: s.get("name") or s["key"] for s in steps}
    n = defs.step_index(d, case["step"]) + 1 if d and case["step"] else 0
    step = names.get(case["step"], case["step"] or "")
    if step and n:
        step += f" ({n} of {len(steps)})"
    last = _last_event(conn, tasks)
    st = {"kind": "case", "id": case["id"], "root": case["root_task_id"], "client": case["client"],
          "title": case["title"], "state": case["state"], "workflow": case["workflow"],
          "workflow_name": (d or {}).get("name") or case["workflow"] or "", "step": step,
          "who": _who(case["waits_on"], case["client"]) if case["state"] == "open" else "",
          "waits": _waits(conn, tasks, case["waits_on"], case["client"]) if case["state"] == "open" else "",
          "since": case["step_at"], "closed_at": case["closed_at"], "last": last,
          "href": f"/board/task/{case['root_task_id']}", "tasks": len(tasks),
          "open_tasks": sum(t["state"] == "open" for t in tasks), "updated": (last or {}).get("ts") or 0}
    st["line"] = line(st, now)
    return st


def task_status(conn: sqlite3.Connection, t: sqlite3.Row, now: int | None = None) -> dict[str, Any]:
    """A task with no case (an old board, or one sync has not reached): the
    same fields, from the task alone."""
    now = now or int(time.time())
    waits = _waits(conn, [t], "agent" if t["state"] == "open" else None, t["assigned_to"])
    last = _last_event(conn, [t])
    st = {"kind": "task", "id": t["id"], "root": t["id"], "client": t["assigned_to"], "title": t["title"],
          "state": t["state"], "workflow": None, "workflow_name": "", "step": t["kind"],
          "who": (f'{t["assigned_to"]} agent' if t["state"] == "open" else ("Ben" if waits.startswith("Ben") else
                  "master" if waits.startswith("master") else "")),
          "waits": waits, "since": t["created_at"], "closed_at": t["completed_at"], "last": last,
          "href": f"/board/task/{t['id']}", "tasks": 1, "open_tasks": int(t["state"] == "open"),
          "updated": (last or {}).get("ts") or 0}
    st["line"] = line(st, now)
    return st


def line(st: dict[str, Any], now: int) -> str:
    """The status as one line of text: 'open · Review (4 of 6) · with Ben · waits on ... · last ...'."""
    if st["state"] == "open":
        bits = ["open"]
    else:
        bits = [f'{st["state"]} {_ago(st["closed_at"], now)}'.strip()]
    if st["step"]:
        bits.append(f'at {st["step"]}' if st["state"] == "open" else f'last step {st["step"]}')
    if st["who"]:
        age = _ago(st["since"], now) if st["since"] else ""
        bits.append(f'with {st["who"]}' + (f' for {age.replace(" ago", "")}' if age.endswith(" ago") else ""))
    if st["waits"]:
        bits.append(f'waits on {st["waits"]}')
    if st["last"]:
        bits.append(f'last: {st["last"]["text"]} ({_ago(st["last"]["ts"], now)})')
    return " · ".join(bits)


def status_for_task(conn: sqlite3.Connection, task_id: str, now: int | None = None) -> dict[str, Any] | None:
    """The status line for whatever case this task is in (its own when it has none)."""
    from .workflows import cases
    t = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if t is None:
        return None
    if cases.have_workflows(conn) and t["case_id"]:
        c = conn.execute("SELECT * FROM workflow_cases WHERE id=?", (t["case_id"],)).fetchone()
        if c is not None:
            return case_status(conn, c, now)
    return task_status(conn, t, now)


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def find(conn: sqlite3.Connection, q: str, client: str | None = None, limit: int = 30,
         now: int | None = None) -> dict[str, Any]:
    """Everything matching q, one status line per case.

    q may be an id or its first 4+ characters (task, decision, loose end,
    case, intent, commitment), q<n> for a question, a SharePoint item number,
    or any text: a title word, a program or file name (matched in task
    titles, specs and results), a pipeline item title. Decisions match by id,
    or by text when nothing else does. Returns {query, hits, notes}: each hit
    is a case (or a task with no case), or a pipeline item, with `matched`
    saying why it came up and `line` the status."""
    from .workflows import cases
    now = now or int(time.time())
    q = (q or "").strip()
    wf = cases.have_workflows(conn)
    task_ids: dict[str, str] = {}   # task id -> why it matched
    notes: list[dict[str, Any]] = []  # decisions, loose ends, questions, intents, commitments: shown as themselves
    pipeline_ids: list[str] = []
    lo = q.lower()
    if not q:
        if not client:
            return {"query": q, "client": client, "hits": [], "notes": []}
        return {"query": q, "client": client, "hits": client_status(conn, client, now), "notes": []}
    if re.fullmatch(r"q\d{1,6}", lo):
        r = conn.execute("SELECT id, task_id, question, created_at, closed_at, superseded_at FROM questions "
                         "WHERE id=?", (lo[1:],)).fetchone()
        if r:
            state = "answered" if r["closed_at"] else "superseded" if r["superseded_at"] else "open"
            notes.append({"kind": "question", "id": lo, "text": r["question"], "state": state,
                          "at": r["created_at"], "task": r["task_id"] or None})
            if r["task_id"]:
                task_ids[r["task_id"]] = f"question {lo}"
    if re.fullmatch(r"[0-9a-f]{4,12}", lo):
        pre = lo + "%"
        for r in conn.execute("SELECT id FROM tasks WHERE id LIKE ? LIMIT 20", (pre,)):
            task_ids.setdefault(r["id"], "id")
        for r in conn.execute("SELECT id, task_id, statement, because, created_at FROM decisions WHERE id LIKE ? "
                              "LIMIT 10", (pre,)):
            notes.append({"kind": "decision", "id": r["id"], "text": r["statement"], "because": r["because"],
                          "at": r["created_at"], "task": r["task_id"]})
            if r["task_id"]:
                task_ids.setdefault(r["task_id"], f"decision {r['id']}")
        for r in conn.execute("SELECT id, task_id, who, what, state, created_at FROM follow_ups WHERE id LIKE ? "
                              "LIMIT 10", (pre,)):
            notes.append({"kind": "loose end", "id": r["id"], "text": r["what"], "who": r["who"],
                          "state": r["state"], "at": r["created_at"], "task": r["task_id"]})
            if r["task_id"]:
                task_ids.setdefault(r["task_id"], f"loose end {r['id']}")
        if wf:
            for r in conn.execute("SELECT root_task_id FROM workflow_cases WHERE id LIKE ? LIMIT 10", (pre,)):
                task_ids.setdefault(r["root_task_id"], "case id")
        if _has(conn, "intents"):
            for r in conn.execute("SELECT id, summary, state, task_ids, created_at FROM intents WHERE id LIKE ? "
                                  "LIMIT 10", (pre,)):
                notes.append({"kind": "intent", "id": r["id"], "text": r["summary"], "state": r["state"],
                              "at": r["created_at"], "task": None})
                for tid in re.findall(r"[0-9a-f]{12}", r["task_ids"] or ""):
                    task_ids.setdefault(tid, f"intent {r['id']}")
        if _has(conn, "commitments"):
            for r in conn.execute("SELECT id, what, to_who, state, due_at, task_id, created_at FROM commitments "
                                  "WHERE id LIKE ? LIMIT 10", (pre,)):
                notes.append({"kind": "commitment", "id": r["id"], "text": f'{r["what"]} (to {r["to_who"]})',
                              "state": r["state"], "at": r["created_at"], "task": r["task_id"]})
                if r["task_id"]:
                    task_ids.setdefault(r["task_id"], f"commitment {r['id']}")
    if _has(conn, "pipeline_items"):
        pq = lo.lstrip("#")
        rows = (conn.execute("SELECT id FROM pipeline_items WHERE id=?", (pq,)).fetchall() if pq.isdigit() else [])
        if len(q) >= 3:
            rows += conn.execute("SELECT id FROM pipeline_items WHERE title LIKE ? ESCAPE '\\' LIMIT 10",
                                 (_like(q),)).fetchall()
        pipeline_ids = list(dict.fromkeys(r["id"] for r in rows))
    if len(q) >= 3:
        like = _like(q)
        for r in conn.execute("SELECT id, CASE WHEN title LIKE ?1 ESCAPE '\\' THEN 'title' "
                              "WHEN spec LIKE ?1 ESCAPE '\\' THEN 'spec' ELSE 'result' END why FROM tasks "
                              "WHERE title LIKE ?1 ESCAPE '\\' OR spec LIKE ?1 ESCAPE '\\' "
                              "OR result LIKE ?1 ESCAPE '\\' ORDER BY created_at DESC LIMIT 200", (like,)):
            task_ids.setdefault(r["id"], r["why"])
        if wf:
            for r in conn.execute("SELECT root_task_id FROM workflow_cases WHERE title LIKE ? ESCAPE '\\' LIMIT 50",
                                  (like,)):
                task_ids.setdefault(r["root_task_id"], "case title")
        if not task_ids and not pipeline_ids and not notes:
            for r in conn.execute("SELECT id, task_id, statement, because, created_at FROM decisions "
                                  "WHERE statement LIKE ? ESCAPE '\\' ORDER BY created_at DESC LIMIT 10", (like,)):
                notes.append({"kind": "decision", "id": r["id"], "text": r["statement"], "because": r["because"],
                              "at": r["created_at"], "task": r["task_id"]})
    for pid in pipeline_ids:
        for r in conn.execute("SELECT id FROM tasks WHERE pipeline_task_id=? ORDER BY created_at DESC LIMIT 50",
                              (pid,)):
            task_ids.setdefault(r["id"], f"pipeline {pid}")

    hits: dict[str, dict[str, Any]] = {}
    for tid, why in task_ids.items():
        t = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if t is None:
            continue
        key = t["case_id"] if wf and t["case_id"] else tid
        if key in hits:
            if len(hits[key]["matched"]) < 4:
                hits[key]["matched"].append({"task": tid, "title": t["title"], "why": why})
            continue
        st = status_for_task(conn, tid, now)
        if client and (st["client"] or "") != client:
            continue
        st["matched"] = [{"task": tid, "title": t["title"], "why": why}]
        hits[key] = st
    ordered = sorted(hits.values(), key=lambda h: (h["state"] != "open", -h["updated"]))
    pipe = [pipeline_status(conn, pid) for pid in pipeline_ids]
    pipe = [p for p in pipe if p and (not client or p["client"] == client)]
    for n in notes:
        n["task_title"] = None
        if n.get("task"):
            r = conn.execute("SELECT title FROM tasks WHERE id=?", (n["task"],)).fetchone()
            n["task_title"] = r["title"] if r else None
    return {"query": q, "client": client, "hits": ordered[:limit], "more": max(0, len(ordered) - limit),
            "pipeline": pipe, "notes": notes}


def pipeline_status(conn: sqlite3.Connection, pid: str) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM pipeline_items WHERE id=?", (pid,)).fetchone()
    if r is None:
        return None
    bits = [r["status"] or "no status"]
    if r["pct"] is not None:
        bits.append(f'{r["pct"]}%')
    if r["developer"]:
        bits.append(r["developer"])
    if r["end_date"]:
        bits.append(f'ends {r["end_date"]}')
    return {"id": r["id"], "client": r["client"], "title": r["title"], "line": " · ".join(bits)}


def client_status(conn: sqlite3.Connection, client: str, now: int | None = None, days: int = 7) -> list[dict[str, Any]]:
    """The compact per-client view: every open case for the client, then those
    closed in the last `days`, one status line each."""
    from .workflows import cases
    now = now or int(time.time())
    if cases.have_workflows(conn):
        rows = conn.execute("SELECT * FROM workflow_cases WHERE client=? AND (state='open' OR closed_at>=?) "
                            "ORDER BY state!='open', updated_at DESC", (client, now - days * 86400)).fetchall()
        out = [case_status(conn, c, now) for c in rows]
        loose = conn.execute("SELECT * FROM tasks WHERE assigned_to=? AND case_id IS NULL AND "
                             "(state='open' OR completed_at>=?)", (client, now - days * 86400)).fetchall()
    else:
        out, loose = [], conn.execute("SELECT * FROM tasks WHERE assigned_to=? AND (state='open' OR completed_at>=?)",
                                      (client, now - days * 86400)).fetchall()
    out += [task_status(conn, t, now) for t in loose]
    return sorted(out, key=lambda h: (h["state"] != "open", -h["updated"]))


# ------------------------------------------------------------ the page

FIND_CSS = """
.find-hit { padding: 8px 0; border-bottom: 1px solid var(--line, #8883); }
.find-hit:last-child { border-bottom: 0; }
.find-hit .st { font-size: 13px; margin-top: 2px; }
.find-hit .why { font-size: 12px; }
.idlabel { color: var(--muted, #888); font-size: 12px; }
a.idref { text-decoration: none; }
"""


def _st_class(state: str) -> str:
    return {"open": "st-running", "done": "st-done", "failed": "st-failed", "dropped": "st-failed"}.get(state, "")


def hit_html(h: dict[str, Any], now: int, client: bool = True) -> str:
    why = ""
    if h.get("matched"):
        why = '<div class="why muted">matched ' + ", ".join(
            f'{escape(m["why"])} on <a href="/board/task/{escape(m["task"])}">{escape(_short(m["title"], 50))}</a>'
            for m in h["matched"]) + "</div>"
    wf = f' · {escape(h["workflow"])} {escape(h["workflow_name"])}' if h.get("workflow") else ""
    who = f'<span class="muted">{escape(h["client"] or "")}</span> ' if client else ""
    return (f'<div class="find-hit">{who}<a class="drill" href="{escape(h["href"])}">{escape(h["title"])}</a> '
            f'<span class="state {_st_class(h["state"])}">{escape(h["state"])}</span>'
            f'<span class="muted" style="font-size:12px">{wf} · <code>{escape(h["root"])}</code></span>'
            f'<div class="st">{escape(h["line"])}</div>{why}</div>')


def search_form(q: str = "", client: str | None = None, clients: list[str] | None = None) -> str:
    opts = "".join(f'<option value="{escape(c)}"{" selected" if c == client else ""}>{escape(c)}</option>'
                   for c in clients or [])
    sel = (f'<select name="client"><option value="">all clients</option>{opts}</select>' if clients else "")
    return (f'<form class="go" method="get" action="/find" style="display:flex;gap:8px;align-items:center">'
            f'<input name="q" value="{escape(q)}" size="40" autofocus '
            f'placeholder="id, title, program or file (xx902.p), pipeline #">{sel}'
            f'<button class="primary">Find</button></form>')


def find_page(r: dict[str, Any], clients: list[str], known: dict[str, dict[str, str]]) -> str:
    from .report import esc, page, when
    now = int(time.time())
    q, client = r["query"], r["client"]
    cards = []
    if r.get("notes"):
        items = []
        for n in r["notes"]:
            task = (f' · on <a class="drill" href="/board/task/{esc(n["task"])}">{esc(_short(n["task_title"], 60))}</a>'
                    if n.get("task") and n.get("task_title") else "")
            state = f' · {esc(n["state"])}' if n.get("state") else ""
            who = f'<b>{esc(n["who"])}</b>: ' if n.get("who") else ""
            because = (f'<div class="muted">because {html(n["because"], known)}</div>' if n.get("because") else "")
            items.append(f'<li>{esc(n["kind"])} <code>{esc(n["id"])}</code>{state} · {when(n["at"])}{task}'
                         f'<div>{who}{html(n["text"], known)}</div>{because}</li>')
        cards.append(f'<div class="card"><h2>What the id is<span class="count">{len(r["notes"])}</span></h2>'
                     f'<ul class="vals">{"".join(items)}</ul></div>')
    if r.get("pipeline"):
        items = "".join(f'<li><a class="drill" href="/pipeline?client={quote(p["client"] or "", safe="")}">'
                        f'#{esc(p["id"])} {esc(p["title"])}</a> <span class="muted">{esc(p["client"] or "")}</span>'
                        f'<div class="st">SharePoint: {esc(p["line"])}</div></li>' for p in r["pipeline"])
        cards.append(f'<div class="card"><h2>Pipeline items<span class="count">{len(r["pipeline"])}</span></h2>'
                     f'<ul class="vals">{items}</ul></div>')
    if r["hits"]:
        more = f'<p class="muted">+{r["more"]} more; narrow the search.</p>' if r.get("more") else ""
        title = f"{esc(client)}: open, then closed this week" if client and not q else "Where it stands"
        cards.append(f'<div class="card"><h2>{title}<span class="count">{len(r["hits"])}</span></h2>'
                     + "".join(hit_html(h, now, client=not client) for h in r["hits"]) + more + "</div>")
    if not cards:
        cards.append('<p class="empty">' + ("Nothing on the board matches." if q or client
                                            else "Type an id, a title word, a program or file name, or a pipeline item number.")
                     + "</p>")
    chips = " ".join(f'<a class="tab" href="/find?client={quote(c, safe="")}"'
                     f'{" aria-current=page" if c == client and not q else ""}>{esc(c)}</a>' for c in clients)
    sub = ("Where any task, case, program or pipeline item stands, and what any id is. "
           "Pick a client for its compact status.")
    body = (search_form(q, client, clients) + f'<div style="margin:8px 0">{chips}</div>'
            + f"<style>{FIND_CSS}</style>" + "".join(cards))
    return page("Find", f"Find: {q}" if q else "Find", "Find", sub, body)


def label_question(conn: sqlite3.Connection, q: dict[str, Any] | None) -> dict[str, Any] | None:
    """The open question as the band and /board show it: `question_labeled`
    with each id's label beside it, and `ref_label` for what the answer lands
    on. `question`, `options` and `prompts` are unchanged, so the answer
    prompt master parses is the same text as before."""
    if not q:
        return q
    known = labels(conn, ids_in(q["question"], q["ref"]))
    q = dict(q)
    q["question_labeled"] = annotate(conn, q["question"], known)
    k = known.get(q["ref"])
    q["ref_label"] = f'{k["kind"]}: {_short(k["label"], 80)}' if k else ""
    return q
