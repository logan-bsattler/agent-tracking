"""Step 5 of the workflow build: per-meeting briefs."""

from __future__ import annotations

import pytest

from coord_mcp import report, store
from coord_mcp.db import connect
from coord_mcp.workflows import briefs
from coord_mcp.workflows import cases as cs

INV = {"done": True, "verdict": "v", "confidence": "high", "evidence": ["e"]}


@pytest.fixture()
def c(tmp_path):
    conn = connect(tmp_path / "t.db")
    yield conn
    conn.close()


def _meeting(c, mid, start, client="LNK", organizer="john@lnk.example", pulled=None, subject="LNK weekly"):
    c.execute("""INSERT INTO meetings(id, start_ts, end_ts, subject, organizer, domains, attendees, show_as,
                 cancelled, all_day, status, client, why, pulled_at) VALUES (?,?,?,?,?,'[]',4,'busy',0,0,
                 'counted',?,'test',?)""", (mid, start, start + 1800, subject, organizer, client, pulled or start))


def test_board_card_says_when_the_calendar_was_not_imported_today(c):
    t = store.now()
    lo, _ = briefs.day_bounds(t)
    _meeting(c, "old", lo - 86400, pulled=lo - 3600)
    v = briefs.today(c, t)
    assert not v["imported"]
    assert "Calendar not imported for today" in briefs.board_card(v)
    _meeting(c, "now=/x", lo + 3600, pulled=lo + 60)
    v = briefs.today(c, t)
    assert v["imported"] and [m["id"] for m in v["meetings"]] == ["now=/x"]
    assert briefs.href("now=/x") in briefs.board_card(v)


def test_brief_gathers_the_clients_board_since_the_last_meeting(c):
    t = store.now()
    _meeting(c, "prev", t - 5 * 86400)
    _meeting(c, "next", t + 3600)
    old = store.create_task(c, "investigation", "LNK: old thing", {}, assigned_to="LNK")["task_id"]
    store.complete_task(c, old, INV)
    c.execute("UPDATE tasks SET completed_at=? WHERE id=?", (t - 6 * 86400, old))
    root = store.create_task(c, "investigation", "LNK: why the AMEX report fails", {}, assigned_to="LNK")["task_id"]
    store.complete_task(c, root, {**INV, "follow_ups": [{"who": "Ben", "what": "approve the AMEX fix"}]})
    store.create_task(c, "investigation", "MAG: not this client", {}, assigned_to="MAG")
    cs.sync(c)
    b = briefs.brief(c, "next", t)
    assert b["previous"]["id"] == "prev"
    assert [q["what"] for q in b["questions"]] == ["approve the AMEX fix"]
    assert [x["id"] for x in b["changed"]["done"]] == [root]
    assert [x["client"] for x in b["cases"]] == ["LNK"]
    html = briefs.render(b)
    assert "approve the AMEX fix" in html and "MAG: not this client" not in html and "LNK weekly" in html
    assert briefs.brief(c, "nope", t) is None


def test_unmapped_meeting_says_so(c):
    t = store.now()
    _meeting(c, "int", t + 60, client="Internal", subject="Strategic AI Committee")
    b = briefs.brief(c, "int", t)
    assert b["client"] is None and "not mapped to a client" in briefs.render(b)


def test_board_page_shows_the_briefs_card(c):
    v = store.operator_view(c)
    v["briefs_card"] = briefs.board_card(briefs.today(c, store.now()))
    assert 'id="briefs"' in report.board_page(v)
