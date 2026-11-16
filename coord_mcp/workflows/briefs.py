"""Per-meeting briefs (C4 Meeting preparation): /brief/<meeting id>.

Read-only. Built from the meetings table (meetings.py; Ben's calendar as
last imported) on each request, so a brief opened shortly before a meeting
is as current as the board. For the meeting's client:

  - open cases and where each stands (workflow_cases)
  - commitments live, soonest due first (commitments.py)
  - drafts waiting on Ben (store.draft_replies, not yet in Outlook)
  - Ben's open loose ends (questions he owes), on that client's tasks or
    filed under that client as Ben items
  - what changed since the last meeting with the same client or organizer:
    tasks finished, decisions on that client's tasks, intents that arrived

The calendar is imported, not live: when no meetings row was pulled today the
dashboard says "calendar not imported for today" rather than showing an empty
day. Who imports it each morning is Ben's call (docs/workflow-build-*.html).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from .cases import have_workflows

NO_CLIENT = ("Internal", "", None)
DEFAULT_SINCE = 7 * 86400
CAP = 25  # rows per card; MAG alone has hundreds of open rulings


def _tz():
    from ..commitments import _tz as tz
    return tz()


def day_bounds(t: int) -> tuple[int, int]:
    d = datetime.fromtimestamp(t, _tz()).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp()), int((d + timedelta(days=1)).timestamp())


def href(meeting_id: str) -> str:
    return "/brief/" + quote(meeting_id, safe="")


def today(conn: sqlite3.Connection, t: int) -> dict[str, Any]:
    """{imported, last_pull, meetings: [...]} for the /board card: today's
    meetings Ben attends (not cancelled, not skipped as focus time or lunch)."""
    lo, hi = day_bounds(t)
    last = conn.execute("SELECT MAX(pulled_at) FROM meetings").fetchone()[0]
    rows = [dict(r) for r in conn.execute(
        "SELECT id, start_ts, end_ts, subject, organizer, client, status FROM meetings "
        "WHERE start_ts >= ? AND start_ts < ? AND cancelled=0 AND all_day=0 AND status != 'skipped' "
        "ORDER BY start_ts", (lo, hi))]
    return {"imported": bool(last and last >= lo), "last_pull": last, "meetings": rows, "as_of": t}


def _previous(conn: sqlite3.Connection, m: sqlite3.Row) -> sqlite3.Row | None:
    """The last earlier meeting with the same client (not all-day, not
    cancelled or skipped). An unmapped meeting matches on organizer, but only
    among meetings equally unmapped, so the window never anchors on a meeting
    with some other client."""
    base = ("SELECT * FROM meetings WHERE start_ts < ? AND cancelled=0 AND all_day=0 AND id != ? "
            "AND status != 'skipped' AND ")
    if m["client"] not in NO_CLIENT:
        return conn.execute(base + "client = ? ORDER BY start_ts DESC LIMIT 1",
                            (m["start_ts"], m["id"], m["client"])).fetchone()
    if not m["organizer"]:
        return None
    return conn.execute(base + "organizer = ? AND COALESCE(client, '') = COALESCE(?, '') "
                        "ORDER BY start_ts DESC LIMIT 1", (m["start_ts"], m["id"], m["organizer"],
                                                           m["client"])).fetchone()


def brief(conn: sqlite3.Connection, meeting_id: str, t: int) -> dict[str, Any] | None:
    from .. import commitments, store
    from . import definitions as defs, name
    # Read-only: a board without the case or commitment tables shows none,
    # it is not migrated by opening a brief.
    m = conn.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
    if m is None:
        return None
    client = m["client"] if m["client"] not in NO_CLIENT else None
    prev = _previous(conn, m)
    since = prev["end_ts"] if prev else m["start_ts"] - DEFAULT_SINCE
    out: dict[str, Any] = {"meeting": dict(m), "client": client, "previous": dict(prev) if prev else None,
                           "since": since, "as_of": t, "domains": json.loads(m["domains"] or "[]")}
    if not client:
        out.update(cases=[], commitments=[], drafts=[], questions=[], changed={"done": [], "decisions": [],
                                                                               "intents": []})
        return out

    cases = []
    rows = conn.execute("SELECT * FROM workflow_cases WHERE state='open' AND client=? ORDER BY step_at",
                        (client,)).fetchall() if have_workflows(conn) else []
    for c in rows:
        d = defs.get(c["workflow"])
        st = next((s for s in (d or {}).get("steps") or [] if s["key"] == c["step"]), None)
        cases.append({**dict(c), "workflow_name": name(c["workflow"]),
                      "step_name": st["name"] if st else (c["step"] or "—")})
    out["cases"] = cases
    have_com = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='commitments'").fetchone()
    out["commitments"] = commitments.list_commitments(conn, client, "live") if have_com else []
    out["drafts"] = [d for d in store.draft_replies(conn, days=30, client=client)["drafts"]
                     if d.get("state") not in ("drafted", store.SET_ASIDE)]
    out["questions"] = [dict(r) for r in conn.execute(
        """SELECT f.id, f.task_id, f.what, f.created_at, f.due_at, t.title FROM follow_ups f
           LEFT JOIN tasks t ON t.id = f.task_id
           WHERE f.state='open' AND lower(trim(f.who))='ben' AND COALESCE(t.assigned_to, f.client) = ?
           ORDER BY f.due_at IS NULL, f.due_at, f.created_at""", (client,))]
    out["changed"] = {
        "done": [dict(r) for r in conn.execute(
            "SELECT id, kind, title, state, completed_at FROM tasks WHERE assigned_to=? AND completed_at >= ? "
            "ORDER BY completed_at DESC", (client, since))],
        "decisions": [dict(r) for r in conn.execute(
            "SELECT d.id, d.task_id, d.statement, d.created_at FROM decisions d JOIN tasks t ON t.id=d.task_id "
            "WHERE t.assigned_to=? AND d.created_at >= ? ORDER BY d.created_at DESC", (client, since))],
        "intents": [dict(r) for r in conn.execute(
            "SELECT id, summary, source, state, created_at FROM intents WHERE created_at >= ? "
            "AND (summary LIKE ? OR id IN (SELECT from_intent FROM tasks WHERE assigned_to=?)) "
            "ORDER BY created_at DESC", (since, f"{client}:%", client))],
    }
    return out


def _clock(ts: int) -> str:
    return datetime.fromtimestamp(ts, _tz()).strftime("%H:%M")


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, _tz()).strftime("%a %d %b %H:%M")


def board_card(v: dict[str, Any]) -> str:
    """Today's briefs for /board; the 'not imported' state when no pull today."""
    from ..report import esc
    if not v["imported"]:
        last = f' Last import {_day(v["last_pull"])}.' if v["last_pull"] else ""
        return ('<div class="card lane" id="briefs"><h2>Meeting briefs</h2>'
                f'<p class="empty">Calendar not imported for today.{esc(last)}</p></div>')
    items = "".join(
        f'<li><b>{_clock(m["start_ts"])}</b> <a class="drill" href="{href(m["id"])}">{esc(m["subject"])}</a> '
        f'<span class="muted">{esc(m["client"] or "unclassified")}</span></li>' for m in v["meetings"])
    body = f'<ul class="queue">{items}</ul>' if items else '<p class="empty">No meetings today.</p>'
    return (f'<div class="card lane" id="briefs"><h2>Meeting briefs, today<span class="count">'
            f'{len(v["meetings"])}</span></h2>{body}</div>')


def render(b: dict[str, Any]) -> str:
    from ..report import _age, _task_link, esc, page
    m, t = b["meeting"], b["as_of"]
    who = esc(b["client"] or "no client mapped")
    # Plain text: card() escapes its title (escaping here too showed "R&amp;D").
    prev = (f'since the last meeting with them, {b["previous"]["subject"]} ({_day(b["previous"]["start_ts"])})'
            if b["previous"] else "in the last 7 days (no earlier meeting with them on the calendar)")
    sub = (f'{_day(m["start_ts"])}–{_clock(m["end_ts"])} · {who} · organizer {esc(m["organizer"] or "—")} · '
           f'{m["attendees"] or 0} attendees. Built from the board when opened; read-only.')

    def card(title: str, items: list[str], empty: str, more: str = "") -> str:
        shown = items[:CAP]
        rest = (f'<p class="muted">and {len(items) - CAP} more{" on " + more if more else ""}</p>'
                if len(items) > CAP else "")
        inner = f'<ul class="queue">{"".join(shown)}</ul>{rest}' if items else f'<p class="empty">{esc(empty)}</p>'
        return f'<div class="card lane"><h2>{esc(title)}<span class="count">{len(items)}</span></h2>{inner}</div>'

    if not b["client"]:
        body = ('<p class="clear">This meeting is not mapped to a client, so there is no client board to brief '
                'from. Classify it on /hours to map it.</p>')
        return page("Board", f'Brief: {m["subject"][:50]}', esc(m["subject"]), sub, body)

    cases = [f'<li>{_task_link(c["root_task_id"], c["title"])} · {esc(c["workflow"] or "")} '
             f'{esc(c["workflow_name"])} · at <b>{esc(c["step_name"])}</b>, waits on <b>{esc(c["waits_on"] or "—")}</b>'
             f'</li>' for c in b["cases"]]
    coms = []
    for c in b["commitments"]:
        due = (('<b class="st-failed">overdue</b> ' if c["due_at"] < t else "") + f'due {_day(c["due_at"])}'
               if c["due_at"] else "no date")
        coms.append(f'<li>to {esc(c["to_who"])}: {esc(c["what"])} <span class="muted">{due}</span></li>')
    drafts = [f'<li>{_task_link(d["task_id"], d.get("subject") or d.get("title") or "")} '
              f'<span class="muted">{esc(d.get("state") or "")}{", late" if d.get("late") else ""}</span></li>'
              for d in b["drafts"]]
    qs = [f'<li>{esc(q["what"])} '
          + (_task_link(q["task_id"], q["title"] or "") if q["task_id"] else "")
          + f' <span class="muted">{_age(q["created_at"], t)}</span></li>' for q in b["questions"]]
    ch = b["changed"]
    changed = ([f'<li>{_task_link(x["id"], x["title"])} <span class="muted">{esc(x["kind"])} {esc(x["state"])}, '
                f'{_age(x["completed_at"], t)}</span></li>' for x in ch["done"]]
               + [f'<li>decision: {esc(x["statement"][:200])} <span class="muted">{_age(x["created_at"], t)}</span></li>'
                  for x in ch["decisions"][:15]]
               + [f'<li>arrived: {esc(x["summary"][:160])} <span class="muted">{esc(x["source"])}, '
                  f'{esc(x["state"])}</span></li>' for x in ch["intents"]])
    body = ('<div class="lanes"><div class="col">'
            + card("Questions Ben owes", qs, "Nothing waiting on Ben for this client.",
                   f'<a href="/queue?client={quote(b["client"])}">/queue</a>')
            + card("Commitments", coms, "No live commitments to this client.")
            + card("Drafts waiting", drafts, "No drafts waiting.")
            + '</div><div class="col">'
            + card("Open cases", cases, "No open cases for this client.", '<a href="/workflows">/workflows</a>')
            + card(f"What changed {prev}", changed, "Nothing recorded since then.")
            + '</div></div>')
    return page("Board", f'Brief: {m["subject"][:50]}', esc(m["subject"]), sub, body, refresh=300)
