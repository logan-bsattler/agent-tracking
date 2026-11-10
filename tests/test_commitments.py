"""Commitments (docs/commitments.md): the table on first use, due-date
resolution, sweep, re-date, effective due, Ben's queue order, the minder,
email-commitment validation and the /commitments page."""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from coord_mcp import commitments as cm
from coord_mcp import db, projects, queue, report, store
from coord_mcp.db import connect, now

TZ = "America/Detroit"
INV = {"done": True, "verdict": "v", "confidence": "high", "evidence": ["e"]}
MIN, DAY = 60, 86400


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("COORD_TZ", TZ)
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _local(ts):
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(ts, ZoneInfo(TZ))


def _iso(s):
    """A local wall-clock time in Ben's zone, as epoch."""
    from zoneinfo import ZoneInfo
    return int(datetime.fromisoformat(s).replace(tzinfo=ZoneInfo(TZ)).timestamp())


def _task(conn, client="MAG", parent=None):
    return store.create_task(conn, "investigation", "t", {"q": "x"}, assigned_to=client, parent_id=parent)["task_id"]


# ------------------------------------------------------------ table


def test_ddl_on_first_use_is_idempotent(tmp_path):
    path = tmp_path / "old.db"
    c = connect(path)
    assert c.execute("SELECT 1 FROM sqlite_master WHERE name='commitments'").fetchone() is None
    db.ensure_commitments(c)
    db.ensure_commitments(c)
    assert c.execute("SELECT 1 FROM sqlite_master WHERE name='commitments'").fetchone()
    assert c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)
    c.close()


def test_minder_works_on_a_board_without_the_table(tmp_path):
    c = connect(tmp_path / "bare.db")
    assert store.minder_owed(c, now())["commitments"] == []
    c.close()


# ------------------------------------------------------------ due dates


@pytest.mark.parametrize("said, text, want", [
    ("2026-11-13T15:00", "Friday", "2026-11-20 17:00"),        # a Friday said on a Friday is next Friday
    ("2026-11-12T09:00", "by Friday", "2026-11-13 17:00"),
    ("2026-11-13T09:00", "Monday", "2026-11-16 17:00"),        # across the week boundary
    ("2026-11-12T09:00", "tomorrow", "2026-11-13 17:00"),
    ("2026-11-12T09:00", "today", "2026-11-12 17:00"),
    ("2026-11-10T09:00", "end of week", "2026-11-13 17:00"),
    ("2026-11-13T09:00", "end of the week", "2026-11-13 17:00"),
    ("2026-11-10T09:00", "next week", "2026-11-20 17:00"),
    ("2026-11-14T09:00", "next week", "2026-11-20 17:00"),     # said on a Saturday
    ("2026-11-10T09:00", "2026-12-01", "2026-12-01 17:00"),
    ("2026-10-30T09:00", "Monday", "2026-11-02 17:00"),        # across the DST change (Nov 1)
])
def test_stated_due_dates(said, text, want):
    due_at, basis = cm.resolve_due(text, _iso(said), TZ)
    assert basis == "stated" and _local(due_at).strftime("%Y-%m-%d %H:%M") == want


def test_inferred_due_skips_weekends():
    due_at, basis = cm.resolve_due("I'll get back to you", _iso("2026-11-12T10:00"), TZ)  # Thursday
    assert basis == "inferred" and _local(due_at).strftime("%Y-%m-%d %H:%M") == "2026-11-16 17:00"
    due_at, _ = cm.resolve_due(None, _iso("2026-11-10T10:00"), TZ)  # Tuesday
    assert _local(due_at).strftime("%Y-%m-%d") == "2026-11-12"


def test_add_refuses_a_duplicate_and_no_date_never_slips(conn):
    cm.add(conn, "MAG", "David Roe", "send the browse specs", "Friday")
    with pytest.raises(ValueError, match="already holds"):
        cm.add(conn, "MAG", "david roe", "Send the browse specs", "Monday")
    c = cm.add(conn, "MAG", "David Roe", "keep you posted", no_date=True)
    assert c["due_basis"] == "none" and c["due_at"] is None and c["by_who"] == "Ben"
    assert cm.sweep(conn, now() + 30 * DAY) == [x["id"] for x in cm.list_commitments(conn) if x["due_at"]]
    assert cm.get(conn, c["id"])["state"] == "open"


# ------------------------------------------------------------ sweep, re-date, close


def test_sweep_once_and_slipped_at_survives_done(conn):
    c = cm.add(conn, "MAG", "David", "specs", "tomorrow")
    t1 = c["due_at"] + 60
    assert cm.sweep(conn, c["due_at"] - 60) == []
    assert cm.sweep(conn, t1) == [c["id"]]
    assert cm.sweep(conn, t1 + 3600) == []
    got = cm.get(conn, c["id"])
    assert got["state"] == "slipped" and got["slipped_at"] == t1
    d = store.record_decision(conn, f"Commitment {c['id']} kept: sent late")["decision_id"]
    done = cm.close(conn, c["id"], "done", d)
    assert done["state"] == "done" and done["slipped_at"] == t1 and done["close_decision_id"] == d
    with pytest.raises(ValueError, match="only by a decision"):
        cm.close(conn, cm.add(conn, "MAG", "x", "y")["id"], "done", "")


def test_redate_writes_history_and_reopens(conn):
    c = cm.add(conn, "MAG", "David", "specs", "tomorrow")
    cm.sweep(conn, c["due_at"] + 60)
    d = store.record_decision(conn, "Ben moved it")["decision_id"]
    r = cm.set_(conn, c["id"], due="2099-01-02", decision_id=d, to_who="David Roe")
    assert r["state"] == "open" and r["to_who"] == "David Roe" and r["slipped_at"]
    assert r["due_history"][0]["due_at"] == c["due_at"] and r["due_history"][0]["decision_id"] == d
    assert cm.set is cm.set_


def test_task_finishing_does_not_close_its_commitment(conn):
    tid = _task(conn)
    c = cm.add(conn, "MAG", "David", "specs", "Friday", task_id=tid)
    store.get_task(conn, tid, reader="MAG")
    store.complete_task(conn, tid, INV)
    assert cm.get(conn, c["id"])["state"] == "open"


# ------------------------------------------------------------ effective due


def test_effective_due_through_chain_and_project(conn):
    root = _task(conn)
    child = _task(conn, parent=root)
    grand = _task(conn, parent=child)
    sibling = _task(conn)
    other = _task(conn)
    p = projects.add(conn, "MAG", "Browse specs")
    projects.attach(conn, p["id"], [root, sibling])
    late = cm.add(conn, "MAG", "A", "late one", "2099-03-01", task_id=root)
    soon = cm.add(conn, "MAG", "B", "soon one", "2099-02-01", task_id=child)
    closed = cm.add(conn, "MAG", "C", "closed one", "2099-01-01", task_id=grand)
    cm.close(conn, closed["id"], "dropped", store.record_decision(conn, "moot")["decision_id"])
    assert cm.effective_due(conn, grand) == soon["due_at"]       # parent chain, earliest wins, closed ignored
    assert cm.effective_due(conn, root) == soon["due_at"]        # same project as child
    assert cm.effective_due(conn, sibling) == soon["due_at"]     # project via a task under it
    assert cm.effective_due(conn, other) is None
    assert late["project_id"] == p["id"]


# ------------------------------------------------------------ Ben's queue


def _ben_loose(conn, client, what="decide"):
    tid = _task(conn, client)
    store.get_task(conn, tid, reader=client)
    store.complete_task(conn, tid, {**INV, "follow_ups": [{"who": "Ben", "what": what}]})
    return tid


def test_ben_queue_orders_overdue_then_soonest_then_age(conn):
    plain = _ben_loose(conn, "LNK", "oldest, no due")
    soon = _ben_loose(conn, "MAG", "due soon")
    over = _ben_loose(conn, "Moog", "overdue")
    cm.add(conn, "MAG", "A", "soon", "2099-01-01", task_id=soon)
    o = cm.add(conn, "Moog", "B", "over", "tomorrow", task_id=over)
    conn.execute("UPDATE commitments SET due_at=? WHERE id=?", (now() - 3600, o["id"]))
    q = queue.ben_queue(conn)
    assert [g["client"] for g in q["groups"]] == ["Moog", "MAG", "LNK"]
    assert q["groups"][0]["items"][0]["due"] < now() and q["groups"][2]["items"][0]["due"] is None
    assert plain == q["groups"][2]["items"][0]["task_id"]
    assert "overdue " in report.queue_page(q)


# ------------------------------------------------------------ minder


def test_minder_coming_due_quiets_after_a_decision(conn):
    tid = _task(conn)
    t = now()
    c = cm.add(conn, "MAG", "David", "specs", "x", task_id=tid)
    conn.execute("UPDATE commitments SET due_at=? WHERE id=?", (t + 3 * 3600, c["id"]))
    assert cm.minder_owed(conn, t) == [c["id"]]
    assert c["id"] in store.minder_owed(conn, t)["commitments"]
    store.record_decision(conn, "dispatched it", task_id=tid)
    assert cm.minder_owed(conn, t + 60) == []


def test_minder_coming_due_ignores_done_work_and_far_dates(conn):
    tid = _task(conn)
    t = now()
    far = cm.add(conn, "MAG", "A", "far", "2099-01-01", task_id=tid)
    assert far["id"] not in cm.minder_owed(conn, t)
    near = cm.add(conn, "MAG", "B", "near", "x", task_id=tid)
    conn.execute("UPDATE commitments SET due_at=? WHERE id=?", (t + 3600, near["id"]))
    store.get_task(conn, tid, reader="MAG")
    store.complete_task(conn, tid, INV)
    assert cm.minder_owed(conn, t) == []


def test_minder_slipped_fires_until_a_decision_names_it(conn):
    c = cm.add(conn, "MAG", "David", "specs", "x", follow_up_id=None)
    cm.link(conn, c["id"], task_id=_task(conn))
    t = now()
    conn.execute("UPDATE commitments SET due_at=? WHERE id=?", (t - 60, c["id"]))
    store.minder_check(conn, t)
    assert cm.get(conn, c["id"])["state"] == "slipped"
    assert cm.minder_owed(conn, t + 60) == [c["id"]]
    store.record_decision(conn, f"Commitment {c['id']}: asked Ben")
    assert cm.minder_owed(conn, t + 120) == []


def test_minder_untracked_after_thirty_minutes(conn):
    c = cm.add(conn, "MAG", "David", "call him", "2099-01-01")
    t = c["created_at"]
    assert cm.minder_owed(conn, t + (cm.UNTRACKED_MINUTES - 1) * MIN) == []
    assert cm.minder_owed(conn, t + (cm.UNTRACKED_MINUTES + 1) * MIN) == [c["id"]]
    cm.link(conn, c["id"], task_id=_task(conn))
    assert cm.minder_owed(conn, t + (cm.UNTRACKED_MINUTES + 1) * MIN) == []


def test_minder_text_names_commitments(conn):
    c = cm.add(conn, "MAG", "David", "call him", "2099-01-01")
    r = store.minder_check(conn, c["created_at"] + 31 * MIN)
    assert r["owed"]["commitments"] == [c["id"]]
    p = store.take_operator_prompt(conn)
    assert f"1 commitments coming due, slipped or untracked (commitment ids) ({c['id']})" in p["text"]


# ------------------------------------------------------------ intents

EMAIL_COMMITMENT = ('msg AAMkAGI2THVSAAA=; conv "MAG browse specs"; said 2026-11-10T18:45:16Z; to David Roe; '
                    'to_email droe@loganconsulting.com; due 2026-11-13; due_text "Friday"; '
                    'what send the browse spec markdowns; commitment abc123def456')


def test_email_commitment_validation_mirrors_teams(conn):
    store.propose_intent(conn, "MAG: promise", "email-commitment", EMAIL_COMMITMENT, trust="trusted")
    (i,) = store.list_intents(conn)
    e = i["email_message"]
    assert e["msg"] == "AAMkAGI2THVSAAA=" and e["to"] == "David Roe" and e["commitment"] == "abc123def456"
    assert i["trust"] == "untrusted" and "coord_mcp.commitments add" in i["handling"]
    with pytest.raises(ValueError, match="needs to, due, what"):
        store.propose_intent(conn, "x", "email-commitment", 'msg A=; conv "c"; said 2026-11-10T00:00Z')
    with pytest.raises(ValueError, match="Outlook message id"):
        store.propose_intent(conn, "x", "email-commitment", "msg teams:///chats/x; " + EMAIL_COMMITMENT[20:])
    assert store.TEAMS_SOURCES.keys() == {"teams-status", "teams-commitment", "teams-ask"}


def test_email_closes_is_parsed_not_validated(conn):
    store.propose_intent(conn, "closes", "email-closes", "anything at all", trust="trusted")
    store.propose_intent(conn, "closes", "email-closes", "msg A=; follow_ups f1; commitments c1, c2")
    a, b = store.list_intents(conn)
    assert a["trust"] == "trusted" and a["email_message"] == {}
    assert b["email_message"]["commitments"] == "c1, c2"
    store.propose_intent(conn, "x", "teams-status", 'msg teams:///m; conv "c"; from_email a@b.com; '
                         'said 2026-11-10T00:00Z; effect progress; strength none; commitments c9')
    assert store.list_intents(conn)[2]["teams_message"]["commitments"] == "c9"


# ------------------------------------------------------------ dashboard


def test_commitments_page_and_buttons(conn):
    tid = _task(conn)
    c = cm.add(conn, "MAG", "David Roe", "send the specs", "Friday", task_id=tid)
    d = cm.add(conn, "LNK", "Ann", "call back", None)
    html = report.commitments_page(cm.list_commitments(conn, state="live"), ["MAG", "LNK"])
    assert "David Roe" in html and "inferred" in html and 'action="/api/commitment"' in html
    assert 'href="/commitments"' in html or "<b>Commitments</b>" in html
    r = cm.act(conn, "redate", c["id"], "2099-01-05")
    assert r["commitment"]["due_history"][0]["decision_id"] == r["decision_id"]
    assert store.get_decisions(conn, task_id=tid)[-1]["statement"].startswith(f"Commitment {c['id']} re-dated")
    assert cm.act(conn, "done", c["id"])["commitment"]["state"] == "done"
    assert cm.act(conn, "drop", d["id"])["commitment"]["state"] == "dropped"
    with pytest.raises(ValueError):
        cm.act(conn, "done", c["id"])
    assert "No commitments here." in report.commitments_page(cm.list_commitments(conn, state="live"), [])


def test_due_chip_on_board_and_client_rows(conn):
    from coord_mcp import client_pages
    tid = _task(conn)
    p = projects.add(conn, "MAG", "Specs")
    projects.attach(conn, p["id"], [tid])
    cm.add(conn, "MAG", "David", "specs", None, task_id=tid)
    dues = cm.due_map(conn)
    view = cm.annotate(projects.client_view(conn, "MAG"), dues)
    assert "due-chip" in client_pages.client_page(view) and "inferred" in client_pages.client_page(view)
    v = store.operator_view(conn)
    cm.annotate(v, dues)
    assert "due-chip" in report.board_page(v)


def test_cli_round_trip(conn, tmp_path, monkeypatch, capsys):
    import json
    monkeypatch.setenv("COORD_DB", str(tmp_path / "cli.db"))
    assert cm.main(["add", "MAG", "--to", "David", "--what", "send specs", "--due", "Friday"]) == 0
    cid = json.loads(capsys.readouterr().out)["id"]
    assert cm.main(["list", "MAG", "--state", "live"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == cid
    assert cm.main(["sweep"]) == 0 and "slipped" in capsys.readouterr().out
    assert cm.main(["close", cid, "done", "--decision", "nope"]) == 1
