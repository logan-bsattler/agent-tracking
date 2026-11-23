"""Board auto-tasking (autotask.py): client loose ends become child tasks
without master, one undelivered task per client at a time, behind switch
auto_task_loose; the minder skips its rate limit for a new intent or a
handoff, behind switch minder_fast_wake. Both off by default."""

from __future__ import annotations

import pytest

from coord_mcp import autotask, queue, sessions, store
from coord_mcp.db import connect, now

INV = {"done": True, "verdict": "v", "confidence": "high", "evidence": ["e"]}
LATER = now() + (store.MINDER_LOOSE + 5) * 60


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    sessions.set_session(c, "MAG", "primary", "local_mag")
    sessions.set_session(c, "LNK", "primary", "local_lnk")
    queue.set_switch(c, "auto_task_loose", True)
    yield c
    c.close()


def _done(conn, follow_ups, client="MAG", kind="investigation"):
    tid = store.create_task(conn, kind, "src", {"q": "x"}, assigned_to=client)["task_id"]
    store.get_task(conn, tid, reader=client)
    store.complete_task(conn, tid, {**INV, "follow_ups": follow_ups})
    return tid


def _open_fus(conn, tid):
    return [r["id"] for r in conn.execute("SELECT id FROM follow_ups WHERE task_id=? AND state='open'", (tid,))]


def test_off_by_default():
    # conftest zeroes SWITCHES for every test, so read the shipped defaults.
    from pathlib import Path
    src = Path(queue.__file__).read_text(encoding="utf-8")
    assert '"auto_task_loose": False, "minder_fast_wake": False' in src


def test_a_client_loose_end_becomes_one_child_task(conn):
    src = _done(conn, [{"who": "MAG", "what": "Check the ld_det totals for site 10"},
                       {"who": "MAG", "what": "Verify the CIM load file for site 20"}])
    out = autotask.run_loose(conn, LATER)
    assert len(out) == 1 and "error" not in out[0]
    t = conn.execute("SELECT * FROM tasks WHERE id=?", (out[0]["task_id"],)).fetchone()
    assert t["kind"] == "investigation" and t["parent_id"] == src and t["assigned_to"] == "MAG"
    import json
    spec = json.loads(t["spec"])
    assert "auto-task" in spec["created_by"] and spec["source_task"] == src
    assert all(f in spec["created_by"] for f in out[0]["follow_ups"])
    assert _open_fus(conn, src) == []  # both resolved with the one task
    dec = conn.execute("SELECT statement FROM decisions WHERE task_id=?", (t["id"],)).fetchone()
    assert dec["statement"].startswith(autotask.DECISION_PREFIX)
    # Bookkeeping, not a hold: auto-dispatch still sends it.
    assert t["id"] not in store.held_tasks(conn, LATER)


def test_switch_off_does_nothing(conn):
    queue.set_switch(conn, "auto_task_loose", False)
    _done(conn, [{"who": "MAG", "what": "Check the totals"}])
    assert autotask.run_loose(conn, LATER) == []


def test_too_young_is_left_for_master(conn):
    _done(conn, [{"who": "MAG", "what": "Check the totals"}])
    assert autotask.run_loose(conn, now()) == []


def test_tops_up_one_undelivered_per_client(conn):
    a = _done(conn, [{"who": "MAG", "what": "Check A"}])
    b = _done(conn, [{"who": "MAG", "what": "Check B"}])
    c = _done(conn, [{"who": "LNK", "what": "Check C"}], client="LNK")
    out = autotask.run_loose(conn, LATER)
    assert sorted(o["source"] for o in out) == sorted([a, c])  # MAG's oldest, and LNK's
    assert autotask.run_loose(conn, LATER) == []  # MAG's buffer is full
    assert _open_fus(conn, b)
    p = autotask.plan_loose(conn, LATER)
    assert [g["source"] for g in p["queued"]["MAG"]] == [b]
    # MAG picks up its task: room again.
    store.get_task(conn, out[0]["task_id"] if out[0]["source"] == a else out[1]["task_id"], reader="MAG")
    assert [o["source"] for o in autotask.run_loose(conn, LATER)] == [b]


def test_replies_get_their_own_draft_reply(conn):
    src = _done(conn, [{"who": "MAG", "what": "Check the totals"},
                       {"who": "MAG", "what": "Draft a reply to Shannon with the totals"}])
    queue.set_setting(conn, "auto_task_buffer", 5)
    kinds = sorted(o["kind"] for o in autotask.run_loose(conn, LATER))
    assert kinds == ["draft_reply", "investigation"]
    assert _open_fus(conn, src) == []


def test_what_master_keeps(conn):
    queue.set_setting(conn, "auto_task_buffer", 9)
    src = _done(conn, [{"who": "Ben", "what": "Approve the estimate"},
                       {"who": "Master", "what": "Update the pipeline note"},
                       {"who": "Shannon Nelson", "what": "Send the file"},
                       {"who": "LNK", "what": "Check the LNK side"},
                       {"who": "MAG", "what": "When DEVL is back: rerun the harness"},
                       {"who": "MAG", "what": "Draft the note to Alex once Ben decides"},
                       {"who": "MAG", "what": "Approve or drop RAIDs 615/505"}])
    p = autotask.plan_loose(conn, LATER)
    assert p["create"] == [] and p["queued"] == {}
    assert p["left"]["Ben's"] == 1
    assert p["left"]["not a client's (master or a person)"] == 2
    assert p["left"]["names another client than its task's"] == 1
    assert p["left"]["conditional or a status note"] == 2
    assert p["left"]["reads as Ben's (approve, send, scope, estimate, access)"] == 1
    assert len(_open_fus(conn, src)) == 7


def test_no_session_is_left_for_master(conn):
    _done(conn, [{"who": "Moog", "what": "Check the depot path"}], client="Moog")
    p = autotask.plan_loose(conn, LATER)
    assert p["create"] == [] and p["no_session"] == {"Moog": 1}


def test_a_child_already_working_holds_it(conn):
    src = _done(conn, [{"who": "MAG", "what": "Check the totals"}])
    store.create_task(conn, "investigation", "master's own", {"q": "x"}, assigned_to="MAG", parent_id=src)
    assert autotask.plan_loose(conn, LATER)["create"] == []


def test_minder_stops_listing_what_the_board_will_task(conn):
    _done(conn, [{"who": "MAG", "what": "Check the totals"}])
    _done(conn, [{"who": "Moog", "what": "Check the depot"}], client="Moog")
    owed = store.minder_owed(conn, LATER)
    assert len(owed["loose"]) == 1  # Moog's: no session, still master's
    queue.set_switch(conn, "auto_task_loose", False)
    assert len(store.minder_owed(conn, LATER)["loose"]) == 2


# ------------------------------------------------------------ fast wake


@pytest.fixture()
def quick_intents(monkeypatch):
    # A new intent is owed after a minute, so it lands inside MINDER_SOON of the take.
    monkeypatch.setattr(store, "MINDER_INTENTS", 1)


def _first_prompt(conn, t):
    store.propose_intent(conn, "first", "email", context="x", origin="test", trust="untrusted")
    r = store.minder_check(conn, t)
    assert r["fired"]
    store.take_operator_prompt(conn)
    conn.execute("UPDATE operator_prompts SET taken_at=? WHERE id=?", (t, r["prompt_id"]))


def test_a_new_intent_skips_the_rate_limit(conn, quick_intents):
    queue.set_switch(conn, "minder_fast_wake", True)
    t0 = now() + store.MINDER_INTENTS * 60 + 60
    _first_prompt(conn, t0)
    # Same list a minute later: rate limited.
    assert store.minder_check(conn, t0 + 60)["reason"] == "rate limit"
    iid = store.propose_intent(conn, "second", "email", context="y", origin="test", trust="untrusted")["intent_id"]
    conn.execute("UPDATE intents SET created_at=? WHERE id=?", (t0 + 10, iid))
    t1 = t0 + 10 + store.MINDER_INTENTS * 60 + 1
    assert autotask.fast_wake(conn, store.minder_owed(conn, t1), t1)
    assert store.minder_check(conn, t1)["fired"]


def test_fast_wake_is_off_by_switch(conn, quick_intents):
    t0 = now() + store.MINDER_INTENTS * 60 + 60
    _first_prompt(conn, t0)
    iid = store.propose_intent(conn, "second", "email", context="y", origin="test", trust="untrusted")["intent_id"]
    conn.execute("UPDATE intents SET created_at=? WHERE id=?", (t0 + 10, iid))
    t1 = t0 + 10 + store.MINDER_INTENTS * 60 + 1
    assert store.minder_check(conn, t1)["reason"] == "rate limit"
    assert autotask.fast_wake(conn, store.minder_owed(conn, t1), t1, force=True)


def test_a_handoff_wakes_master_after_it_settles(conn, quick_intents):
    queue.set_switch(conn, "minder_fast_wake", True)
    t0 = now() + store.MINDER_INTENTS * 60 + 60
    _first_prompt(conn, t0)
    d = store.record_decision(conn, "Handoff 11-23 10:43: MAG Local on x", because="clear")
    conn.execute("UPDATE decisions SET created_at=? WHERE id=?", (t0 + 30, d["decision_id"]))
    owed = store.minder_owed(conn, t0 + 60)
    assert autotask.fast_wake(conn, owed, t0 + 60) is None  # not settled yet
    assert "handoff" in autotask.fast_wake(conn, owed, t0 + 30 + autotask.HANDOFF_SETTLE)
    # An ordinary decision does not.
    conn.execute("UPDATE decisions SET statement='Proceed on default' WHERE id=?", (d["decision_id"],))
    assert autotask.fast_wake(conn, owed, t0 + 30 + autotask.HANDOFF_SETTLE) is None


def test_dry_run_cli_writes_nothing(tmp_path, monkeypatch, capsys):
    db = tmp_path / "cli.db"
    c = connect(db)
    sessions.set_session(c, "MAG", "primary", "local_mag")
    src = _done(c, [{"who": "MAG", "what": "Check the totals"}])
    c.execute("UPDATE follow_ups SET created_at=created_at-? WHERE task_id=?", (store.MINDER_LOOSE * 60 + 60, src))
    c.commit()
    c.close()
    monkeypatch.setenv("COORD_DB", str(db))
    import coord_mcp.autotask as at
    monkeypatch.setattr("coord_mcp.db.connect", lambda *a, **k: connect(db))
    assert at.main(["dry-run"]) == 0
    out = capsys.readouterr().out
    assert "this pass would create 1 task(s)" in out and "MAG" in out
    c = connect(db)
    assert c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    c.close()
