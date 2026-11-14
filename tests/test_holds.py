"""Holds behind Ben as working defaults (coord_mcp/holds.py, 2026-11-14)."""

from __future__ import annotations

import json

import pytest

from coord_mcp import holds, queue, store
from coord_mcp.db import connect, now


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _task(conn, kind="investigation", client="Royal", spec=None, parent_id=None):
    return store.create_task(conn, kind, "t", spec or {"q": "x"}, assigned_to=client, parent_id=parent_id)["task_id"]


_n = [0]


def _fu(conn, task_id, who, what, state="open", client="Royal"):
    _n[0] += 1
    fid = f"{_n[0]:012x}"
    conn.execute("INSERT INTO follow_ups(id, task_id, idx, who, what, state, created_at, client) "
                 "VALUES (?,?,?,?,?,?,?,?)", (fid, task_id, _n[0], who, what, state, now(), client))
    return fid


def _question(conn, task_id, fid, options, recommended):
    conn.execute("INSERT INTO questions(task_id, follow_up_id, question, options, recommended, created_at) "
                 "VALUES (?,?,?,?,?,?)", (task_id, fid, "Which?", json.dumps(options), recommended, now()))


def _setup(conn, recommended=0, held_what="Revise the change list (round 3)"):
    t = _task(conn)
    ben = _fu(conn, t, "Ben", "Rule round 3 on the change list: revise, accept or rework")
    _question(conn, t, ben, ["Revise once more", "Accept as is", "Rework the spec"], recommended)
    work = _fu(conn, t, "Royal", held_what)
    return t, ben, work


def _hold(conn, t, ben, work, because="master default; Ben may override"):
    d = store.record_decision(conn, f"Defer {work} until Ben rules {ben}.", because, t)
    store.resolve_follow_up(conn, work, decision_id=d["decision_id"], defer=True)
    return d["decision_id"]


def test_internal_hold_with_a_recommended_option_would_proceed(conn):
    t, ben, work = _setup(conn)
    did = _hold(conn, t, ben, work)
    [h] = holds.find_holds(conn)
    assert h["decision_id"] == did and h["verdict"] == "proceed"
    assert h["default"] == "Revise once more" and h["default_source"].endswith("recommended")
    assert h["master_default_hold"] and h["work"][0]["ref"] == work


def test_outward_work_stays_held(conn):
    t, ben, work = _setup(conn, held_what="Send the change list to John")
    _hold(conn, t, ben, work)
    [h] = holds.find_holds(conn)
    assert h["verdict"] == "keep" and h["facing"] == "outward"


def test_no_default_stays_bens(conn):
    t, ben, work = _setup(conn, recommended=None)
    _hold(conn, t, ben, work)
    assert holds.find_holds(conn)[0]["reason"].startswith("no default")


def test_a_recommendation_to_wait_is_not_a_default(conn):
    t = _task(conn)
    ben = _fu(conn, t, "Ben", "Rule on X")
    _question(conn, t, ben, ["Defer until Monday", "Go"], 0)
    work = _fu(conn, t, "Royal", "Revise the note")
    _hold(conn, t, ben, work)
    assert holds.find_holds(conn)[0]["verdict"] == "keep"


def test_waiting_on_bens_action_stays_held(conn):
    t = _task(conn)
    work = _fu(conn, t, "Royal", "Re-read the API")
    d = store.record_decision(conn, f"Hold {work} until Ben refreshes the API credential.", None, t)
    store.resolve_follow_up(conn, work, decision_id=d["decision_id"], defer=True)
    [h] = holds.find_holds(conn)
    assert h["type"] == "action" and h["verdict"] == "keep"


def test_handoffs_and_lifts_are_not_holds(conn):
    t, ben, work = _setup(conn)
    store.record_decision(conn, f"Handoff: {work} waits on Ben {ben}.", None, t)
    store.record_decision(conn, f"Lift hold on {work}: Ben ruled.", None, t)
    assert holds.find_holds(conn) == []


def test_apply_dry_run_writes_nothing_and_write_needs_the_switch(conn, monkeypatch):
    t, ben, work = _setup(conn)
    did = _hold(conn, t, ben, work)
    out = holds.apply(conn)
    assert out["count"] == 1 and not out["applied"] and out["items"][0]["reopen"] == [work]
    assert conn.execute("SELECT state FROM follow_ups WHERE id=?", (work,)).fetchone()["state"] == "deferred"
    monkeypatch.setattr(queue, "SWITCHES", {**queue.SWITCHES, "hold_apply": False})
    with pytest.raises(ValueError, match="hold_apply is off"):
        holds.apply(conn, write=True)
    queue.set_switch(conn, "hold_apply", True)
    out = holds.apply(conn, write=True)
    new = out["items"][0]["decision"]
    assert conn.execute("SELECT supersedes FROM decisions WHERE id=?", (new,)).fetchone()["supersedes"] == did
    assert conn.execute("SELECT state FROM follow_ups WHERE id=?", (work,)).fetchone()["state"] == "open"
    assert conn.execute("SELECT state FROM follow_ups WHERE id=?", (ben,)).fetchone()["state"] == "open"
    assert holds.find_holds(conn) == []  # superseded


def test_outward_steps_wait_for_ben_after_apply(conn):
    from coord_mcp import outlook
    t, ben, work = _setup(conn)
    _hold(conn, t, ben, work)
    queue.set_switch(conn, "hold_apply", True)
    holds.apply(conn, write=True)
    reply = _task(conn, kind="draft_reply", spec={"q": "x"})
    store.resolve_follow_up(conn, work, task_id=reply)
    assert holds.pending_gate(conn, reply)["ben_follow_ups"] == [ben]
    conn.execute("UPDATE tasks SET state='done', result=? WHERE id=?",
                 (json.dumps({"done": True, "to": ["a@b.c"], "subject": "s", "body_path": "x", "summary": "s"}), reply))
    with pytest.raises(ValueError, match="master default"):
        outlook.draft(conn, reply)
    store.resolve_follow_up(conn, ben, decision_id=store.record_decision(conn, "Ben ruled: revise")["decision_id"])
    assert holds.pending_gate(conn, reply) is None


def test_new_internal_hold_with_a_default_is_refused_by_the_rule(conn, monkeypatch):
    monkeypatch.setattr(queue, "SWITCHES", {**queue.SWITCHES, "hold_rule": True})
    t, ben, work = _setup(conn)
    with pytest.raises(ValueError, match="hold refused"):
        holds.check_new_hold(conn, f"Defer {work} until Ben rules {ben}.", None, t)
    holds.check_new_hold(conn, f"Hold {work}: send to John waits until Ben rules {ben}.", None, t)  # outward: ok
    queue.set_switch(conn, "hold_rule", False)
    holds.check_new_hold(conn, f"Defer {work} until Ben rules {ben}.", None, t)


def test_a_master_default_decision_naming_bens_open_question_registers_the_gate(conn):
    t, ben, work = _setup(conn)
    stmt = f"Proceed on master default for {work}: revise once more; Ben's {ben} stays open"
    d = store.record_decision(conn, stmt, "master default; Ben may override", t)
    assert holds.after_decision(conn, d["decision_id"], stmt, "master default; Ben may override", t, None) == \
        {"gated_until_ben": [ben]}
    assert holds.pending_gate(conn, t)["decision_id"] == d["decision_id"]


def test_minder_lists_holds_recorded_after_the_rule(conn, monkeypatch):
    monkeypatch.setattr(queue, "SWITCHES", {**queue.SWITCHES, "hold_rule": True})
    assert holds.minder_owed(conn) == []  # first call stamps the start
    conn.execute("UPDATE meta SET value=? WHERE key=?", (str(now() - 5), holds.SINCE_KEY))
    t, ben, work = _setup(conn)
    did = _hold(conn, t, ben, work)
    assert holds.minder_owed(conn) == [did]
    assert did in store.minder_owed(conn, now())["holds"]


def test_case_hold_stops_on_outward_and_proceeds_on_internal_under_the_rule(conn, monkeypatch):
    t, ben, work = _setup(conn)
    _hold(conn, t, ben, work)
    monkeypatch.setattr(queue, "SWITCHES", {**queue.SWITCHES, "hold_rule": False})
    # Ben 2026-11-14 (f294e33095c1): a reply is still drafted past an internal
    # hold; any other kind stops without hold_rule.
    assert "draft" in holds.case_hold(conn, [t], "draft_reply")
    assert "stop" in holds.case_hold(conn, [t], "investigation")
    queue.set_switch(conn, "hold_rule", True)
    assert holds.case_hold(conn, [t], "draft_reply")["proceed"]["verdict"] == "proceed"
    t2, ben2, work2 = _setup(conn, held_what="Send the change list to John")
    _hold(conn, t2, ben2, work2)
    assert "stop" in holds.case_hold(conn, [t2], "draft_reply")
    assert holds.case_hold(conn, [_task(conn)], "draft_reply") is None


def test_case_hold_outward_hold_stops_even_after_an_internal_one(conn):
    # Both holds sit on one case; the internal one is recorded first. The
    # outward one must still stop the draft, not be skipped by a first match.
    queue.set_switch(conn, "hold_rule", False)
    t, ben, work = _setup(conn)
    _hold(conn, t, ben, work)
    out = _fu(conn, t, "Royal", "Send the change list to John")
    _hold(conn, t, ben, out)
    assert "stop" in holds.case_hold(conn, [t], "draft_reply")
