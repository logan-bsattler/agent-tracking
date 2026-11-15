"""Regressions for the 2026-11-14 review of the workflow engine, cases, checks and briefs."""

from __future__ import annotations

import json
import sqlite3

import pytest

from coord_mcp import holds, store
from coord_mcp.db import connect
from coord_mcp.workflows import __main__ as wf_cli
from coord_mcp.workflows import briefs, checks, view
from coord_mcp.workflows import cases as cs
from coord_mcp.workflows import engine

INV = {"done": True, "verdict": "v", "confidence": "high", "evidence": ["e"]}


@pytest.fixture()
def c(tmp_path):
    conn = connect(tmp_path / "t.db")
    cs.ensure_workflows(conn)
    engine._since(conn, write=True)
    conn.execute("UPDATE meta SET value=? WHERE key=?", (str(store.now() - 60), engine.SINCE_KEY))
    yield conn
    conn.close()


def _status_case(c, follow_ups=None, reply_from="john@client.example"):
    i = store.propose_intent(c, "LNK: John asks where RAID 695 stands", "email-status",
                             context=f"msg X; reply_from {reply_from}; reply_date 2026-11-13T10:00:00Z; "
                                     'subject "RAID 695"', origin="team", trust="trusted")["intent_id"]
    t = store.create_task(c, "investigation", "LNK: status of RAID 695 for John", {}, assigned_to="LNK",
                          from_intent=i)["task_id"]
    store.complete_task(c, t, {**INV, "follow_ups": follow_ups or [{"who": "LNK", "what": "draft the reply"}]})
    return t


def _defect(c, follow_ups):
    t = store.create_task(c, "investigation", "LNK: why TEST Production Orders lacks the Location field", {},
                          assigned_to="LNK")["task_id"]
    store.complete_task(c, t, {**INV, "follow_ups": follow_ups})
    cs.sync(c)
    return t


def _ben(c, what="rule what John may hear"):
    t = store.create_task(c, "investigation", "LNK: read", {}, assigned_to="LNK")["task_id"]
    store.complete_task(c, t, {**INV, "follow_ups": [{"who": "Ben", "what": what}]})
    return c.execute("SELECT id FROM follow_ups WHERE task_id=?", (t,)).fetchone()["id"]


# 1 ------------------------------------------------------------------------

def test_engine_task_is_not_held_from_dispatch(c):
    _status_case(c)
    cs.sync(c)
    tid = engine.run(c)[0]["task_id"]
    assert tid not in store.held_tasks(c, store.now())
    # A real hold on it still holds.
    store.record_decision(c, f"Hold {tid} until the refresh lands", "waiting on DBA", tid)
    assert tid in store.held_tasks(c, store.now())


# 2 ------------------------------------------------------------------------

def test_fix_takes_the_fix_loose_end_not_the_reply(c):
    t = _defect(c, [{"who": "LNK", "what": "draft the reply to the client"},
                    {"who": "LNK", "what": "fix xxpo01.p to add Location"}])
    did = engine.run(c)
    assert [d["kind"] for d in did] == ["code_change"]
    spec = json.loads(c.execute("SELECT spec FROM tasks WHERE id=?", (did[0]["task_id"],)).fetchone()["spec"])
    assert spec["goal"] == "fix xxpo01.p to add Location"
    states = dict(c.execute("SELECT what, state FROM follow_ups WHERE task_id=?", (t,)).fetchall())
    assert states == {"draft the reply to the client": "open", "fix xxpo01.p to add Location": "tasked"}


def test_first_listed_loose_end_wins(c):
    _defect(c, [{"who": "LNK", "what": f"fix part {n} of xxpo01.p"} for n in range(6)])
    spec = json.loads(c.execute("SELECT spec FROM tasks WHERE id=?",
                                (engine.run(c)[0]["task_id"],)).fetchone()["spec"])
    assert spec["goal"] == "fix part 0 of xxpo01.p"


def test_ambiguous_loose_ends_are_left_for_master(c):
    _defect(c, [{"who": "LNK", "what": "look at the vendor note"}, {"who": "LNK", "what": "ask about the old layout"}])
    assert engine.run(c) == []


# 3 ------------------------------------------------------------------------

def test_dry_run_writes_nothing_and_uses_the_stored_since(c, tmp_path, capsys):
    t = _status_case(c)
    cs.sync(c)
    c.execute("UPDATE tasks SET case_step=NULL")  # a step the engine would assign
    before = c.total_changes
    assert engine.run(c, dry_run=True)
    assert c.total_changes == before
    assert c.execute("SELECT case_step FROM tasks WHERE id=?", (t,)).fetchone()[0] is None
    # A result older than the stored switch-on time is left alone in a dry run too.
    c.execute("UPDATE tasks SET completed_at=? WHERE id=?", (store.now() - 3600, t))
    assert engine.run(c, dry_run=True) == []


def test_cli_dry_run_does_not_sync(tmp_path, capsys):
    db = tmp_path / "cli.db"
    conn = connect(db)
    store.create_task(conn, "investigation", "LNK: read", {}, assigned_to="LNK")
    conn.close()
    assert wf_cli.main(["engine", "--db", str(db), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    conn = connect(db)
    assert not cs.have_workflows(conn) or conn.execute("SELECT COUNT(*) FROM workflow_cases").fetchone()[0] == 0
    conn.close()


# 4 ------------------------------------------------------------------------

def test_read_views_do_not_migrate(tmp_path):
    conn = connect(tmp_path / "ro.db")
    if cs.have_workflows(conn):
        pytest.skip("connect() already creates the case tables")
    assert cs.list_cases(conn) == [] and cs.case_detail(conn, "x") is None
    assert view.overview(conn)["totals"]["cases"] == 0
    conn.execute("""INSERT INTO meetings(id, start_ts, end_ts, subject, organizer, domains, attendees, show_as,
                    cancelled, all_day, status, client, why, pulled_at) VALUES ('m', 1, 2, 's', 'o', '[]', 1,
                    'busy', 0, 0, 'counted', 'LNK', 'test', 1)""")
    assert briefs.brief(conn, "m", store.now())["cases"] == []
    assert not cs.have_workflows(conn)
    conn.close()


# 5 ------------------------------------------------------------------------

def test_quiet_sync_writes_nothing(c):
    _status_case(c)
    cs.sync(c)
    stamp = c.execute("SELECT updated_at FROM workflow_cases").fetchone()[0]
    c.execute("UPDATE workflow_cases SET updated_at=1")
    before = c.total_changes
    out = cs.sync(c)
    assert c.total_changes == before and out["changed"] == 0 and stamp
    assert c.execute("SELECT updated_at FROM workflow_cases").fetchone()[0] == 1


# 6 ------------------------------------------------------------------------

def test_a_locked_board_fails_each_case_without_aborting(c, tmp_path):
    _status_case(c)
    _status_case(c, reply_from="mary@client.example")
    cs.sync(c)
    c.execute("PRAGMA busy_timeout=0")
    other = sqlite3.connect(str(tmp_path / "t.db"), isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(engine.EngineCaseErrors) as err:
            engine.run(c)
        assert len(err.value.failed) == 2 and not c.in_transaction
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert len(engine.run(c)) == 2


# 7 ------------------------------------------------------------------------

def test_attached_parentless_task_gets_no_no_case_warning(c, monkeypatch):
    from coord_mcp import queue
    monkeypatch.setattr(queue, "SWITCHES", {**queue.SWITCHES, checks.SWITCH: True})
    i = store.propose_intent(c, "LNK: label defect on RAID 695", "email", context="msg X", origin="team",
                             trust="trusted")["intent_id"]
    store.create_task(c, "investigation", "LNK: investigate label defect RAID 695", {}, assigned_to="LNK",
                      from_intent=i)
    stray = store.create_task(c, "code_change", "LNK: fix the label", {}, assigned_to="LNK", from_intent=i)["task_id"]
    cs.sync(c)
    target = c.execute("SELECT case_id FROM tasks WHERE id<>? LIMIT 1", (stray,)).fetchone()[0]
    checks.attach(c, stray, target)
    c.execute("UPDATE tasks SET state='done', completed_at=? WHERE id=?", (store.now(), stray))
    assert not any(w.startswith("no case") for w in checks.complete_warnings(c, stray))


# 8, 9 ---------------------------------------------------------------------

def _meeting(c, mid, start, client="LNK", organizer="ben@logan.example", subject="LNK weekly", all_day=0):
    c.execute("""INSERT INTO meetings(id, start_ts, end_ts, subject, organizer, domains, attendees, show_as,
                 cancelled, all_day, status, client, why, pulled_at) VALUES (?,?,?,?,?,'[]',4,'busy',0,?,
                 'counted',?,'test',?)""", (mid, start, start + 1800, subject, organizer, all_day, client, start))


def test_previous_meeting_is_the_same_clients_and_title_is_escaped_once(c):
    t = store.now()
    _meeting(c, "lnk", t - 5 * 86400, subject="R&D review")
    _meeting(c, "mag", t - 86400, client="MAG")          # same organizer, other client
    _meeting(c, "allday", t - 3600, all_day=1)           # same client, all day
    _meeting(c, "next", t + 3600)
    b = briefs.brief(c, "next", t)
    assert b["previous"]["id"] == "lnk"
    html = briefs.render(b)
    assert "R&amp;D review" in html and "R&amp;amp;D" not in html


# 10 -----------------------------------------------------------------------

def _reply_fu(c):
    t = _status_case(c, [{"who": "LNK", "what": "draft the reply to John"}])
    return t, c.execute("SELECT id FROM follow_ups WHERE task_id=?", (t,)).fetchone()["id"]


def test_two_internal_holds_both_gate_outlook(c):
    t, f = _reply_fu(c)
    b1, b2 = _ben(c), _ben(c, "rule the second thing")
    d1 = store.record_decision(c, f"Hold the reply {f} until Ben rules {b1}", "x", t)["decision_id"]
    d2 = store.record_decision(c, f"Also hold the reply {f} until Ben rules {b2}", "x", t)["decision_id"]
    store.resolve_follow_up(c, f, decision_id=d1, defer=True)
    cs.sync(c)
    did = engine.run(c)
    assert len(did) == 1 and set(did[0]["drafts_past"]) == {d1, d2}
    tid = did[0]["task_id"]
    assert b1 in holds.pending_gate(c, tid)["ben_follow_ups"] or b2 in holds.pending_gate(c, tid)["ben_follow_ups"]
    rule = store.record_decision(c, f"Ben ruled {b1}", "his answer")["decision_id"]
    store.resolve_follow_up(c, b1, decision_id=rule)
    assert holds.pending_gate(c, tid)["ben_follow_ups"] == [b2]  # the second hold still gates


def test_hold_without_ben_items_gates_until_lifted(c):
    t, f = _reply_fu(c)
    d = store.record_decision(c, f"Hold the reply {f} until Ben decides", "x", t)["decision_id"]
    store.resolve_follow_up(c, f, decision_id=d, defer=True)
    cs.sync(c)
    did = engine.run(c)
    assert len(did) == 1 and did[0]["draft_past"] == d
    tid = did[0]["task_id"]
    assert holds.pending_gate(c, tid)["ben_follow_ups"] == [holds.HOLD_REF + d]
    with pytest.raises(ValueError):
        holds.require_ruled(c, tid, "Draft in Outlook")
    store.record_decision(c, f"Ben ruled: send it; lifts {d}", "his answer", t, supersedes=d)
    assert holds.pending_gate(c, tid) is None
