"""Session upkeep (upkeep.py): VPN watch, parked re-dispatch, the clear
check, and restarts after a coord update."""

from __future__ import annotations

import json

import pytest

from coord_mcp import queue, reaper, sessions, store, upkeep
from coord_mcp.db import connect, now

OLD = 10 * 60
VPN_FAIL = {"done": False, "reason": "VPN down", "retryable": True}


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    for s in ("vpn_watch", "auto_redispatch", "clear_check", "code_restart", "idle_reaper_keep_rc"):
        queue.set_switch(c, s, True)  # conftest turns every switch off
    sessions.set_session(c, "Royal", "primary", "local_royal")
    sessions.set_session(c, "Furlani", "primary", "local_furl")
    upkeep.ensure_upkeep(c)
    yield c
    c.close()


def _task(conn, client="Royal", kind="code_change", age=OLD):
    tid = store.create_task(conn, kind, "t", {"q": "x"}, assigned_to=client)["task_id"]
    conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (now() - age, tid))
    conn.commit()
    return tid


def _fail_vpn(conn, tid, at):
    store.complete_task(conn, tid, VPN_FAIL)
    conn.execute("UPDATE tasks SET completed_at=?, picked_up_at=? WHERE id=?", (at, at - 5, tid))
    conn.commit()


def _probe(up):
    return lambda host: (up, "fake")


# ------------------------------------------------------------ 1. VPN

def test_vpn_failure_waits_then_reopens_when_server_answers(conn):
    t = now()
    tid = _task(conn)
    _fail_vpn(conn, tid, t - 300)
    # master acknowledged it: that decision must not hold the reopened task
    store.record_decision(conn, "Royal VPN down; wait", task_id=tid)
    conn.execute("UPDATE decisions SET created_at=? WHERE task_id=?", (t - 200, tid))
    assert [w["task_id"] for w in upkeep.vpn_waits(conn)] == [tid]
    assert tid in upkeep.vpn_waiting_failed(conn)
    upkeep.vpn_tick(conn, t - 100, prober=_probe(False), force=True)
    assert conn.execute("SELECT state FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "failed"
    r = upkeep.vpn_tick(conn, t, prober=_probe(True), force=True)
    assert r["reopened"] == [tid]
    row = conn.execute("SELECT state, picked_up_at, reopened_at FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["state"] == "open" and row["picked_up_at"] is None and row["reopened_at"] == t
    assert tid not in store.held_tasks(conn, t + 1)
    assert sessions.next_dispatch(conn, t + 1)["task_id"] == tid


def test_vpn_up_across_the_failure_does_not_reopen(conn):
    t = now()
    upkeep.vpn_tick(conn, t - 1000, prober=_probe(True), force=True)
    tid = _task(conn)
    _fail_vpn(conn, tid, t - 300)
    assert upkeep.vpn_tick(conn, t, prober=_probe(True), force=True)["reopened"] == []


def test_furlani_handoff_not_configured_is_not_a_vpn_wait(conn):
    tid = _task(conn, client="Furlani")
    store.complete_task(conn, tid, {"done": False, "reason": "VPN up but handoff not configured", "retryable": True})
    assert upkeep.vpn_waits(conn) == []


def test_vpn_switch_off_probes_nothing(conn):
    queue.set_switch(conn, "vpn_watch", False)
    assert upkeep.vpn_tick(conn, force=True, prober=_probe(True)) is None


def test_release_cap(conn):
    t = now()
    tid = _task(conn)
    _fail_vpn(conn, tid, t - 300)
    for i in range(upkeep.VPN_RELEASES_MAX):
        conn.execute("INSERT INTO vpn_releases(task_id, client, released_at) VALUES (?,?,?)", (tid, "Royal", t - 900 + i))
    upkeep.vpn_tick(conn, t, prober=_probe(True), force=True)
    assert conn.execute("SELECT state FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "failed"
    assert any(tid in f for f in upkeep.flagged(conn))


# ------------------------------------------------------------ 2 and 3. parked re-dispatch, clear check

def _park(conn, tid, at, text="out of context", n=1):
    conn.execute("UPDATE tasks SET picked_up_at=? WHERE id=?", (at - 50, tid))
    for i in range(n):
        store.park_task(conn, tid, next_step=text)
    conn.execute("UPDATE task_parks SET created_at=? WHERE task_id=?", (at, tid))
    conn.commit()


def _usage(messages_k):
    return {"context": {"status": "ok", "categories": [{"name": "Messages", "tokens": messages_k * 1000}]}}


def test_parked_task_full_cycle_with_one_failed_clear(conn):
    t = now()
    tid = _task(conn)
    _park(conn, tid, t - 120)
    assert tid in upkeep.auto_parked(conn, t)
    assert tid not in store.minder_owed(conn, t)["parked"]
    a = upkeep.claim(conn, "master", t)
    assert a["action"] == "get_session" and a["session_id"] == "local_royal" and a["task_id"] == tid
    assert upkeep.claim(conn, "desk", t) is None  # leased
    upkeep.report(conn, tid, running="true", t=t)
    assert upkeep.claim(conn, "master", t + 1) is None  # polls again after POLL
    a = upkeep.claim(conn, "master", t + upkeep.POLL)
    upkeep.report(conn, tid, running="false", t=t + 21)
    a = upkeep.claim(conn, "master", t + 21)
    assert a["action"] == "send" and a["message"] == f"Do task {tid}. Start with coord_get_task."
    upkeep.report(conn, tid, status="delivered", t=t + 22)
    a = upkeep.claim(conn, "master", t + 22 + upkeep.VERIFY_AFTER)
    assert a["action"] == "get_usage"
    upkeep.report(conn, tid, usage=_usage(150), t=t + 40)  # clear did not fire
    a = upkeep.claim(conn, "master", t + 40)
    assert a["action"] == "send" and "clear_session" in a["message"]
    upkeep.report(conn, tid, status="queued", t=t + 41)
    a = upkeep.claim(conn, "master", t + 41 + upkeep.POLL)
    assert a["action"] == "get_session"
    upkeep.report(conn, tid, running="false", t=t + 70)
    assert upkeep.claim(conn, "master", t + 70)["action"] == "send"
    upkeep.report(conn, tid, status="delivered", t=t + 71)
    upkeep.claim(conn, "master", t + 71 + upkeep.VERIFY_AFTER)
    upkeep.report(conn, tid, usage=_usage(9), t=t + 90)
    row = conn.execute("SELECT phase, clears, sends FROM upkeep WHERE key=?", (tid,)).fetchone()
    assert (row["phase"], row["clears"], row["sends"]) == ("done", 1, 2)


def test_clear_failing_twice_flags_master(conn):
    t = now()
    tid = _task(conn)
    conn.execute("UPDATE tasks SET picked_up_at=? WHERE id=?", (t, tid))
    upkeep._put(conn, tid, "dispatch", "local_royal", "verify", t, task_id=tid, client="Royal")
    conn.execute("UPDATE upkeep SET clears=? WHERE key=?", (upkeep.CLEARS_MAX, tid))
    upkeep.claim(conn, "master", t)
    upkeep.report(conn, tid, usage=_usage(200), t=t + 1)
    assert conn.execute("SELECT phase FROM upkeep WHERE key=?", (tid,)).fetchone()[0] == "flagged"
    assert any(f.startswith(tid) for f in store.minder_owed(conn, t + 2)["upkeep"])


def test_three_parks_left_to_master(conn):
    t = now()
    tid = _task(conn)
    _park(conn, tid, t - 120, n=3)
    assert upkeep.redispatchable(conn, t) == []
    assert tid in store.minder_owed(conn, t + 3600)["parked"]


def test_vpn_park_waits_for_the_server(conn):
    t = now()
    tid = _task(conn)
    _park(conn, tid, t - 120, text="VPN is down, cannot reach the server")
    upkeep.vpn_tick(conn, t - 60, prober=_probe(False), force=True)
    assert upkeep.redispatchable(conn, t) == []
    assert tid in upkeep.auto_parked(conn, t)
    upkeep.vpn_tick(conn, t, prober=_probe(True), force=True)
    assert [d["task_id"] for d in upkeep.redispatchable(conn, t + 1)] == [tid]


def test_auto_dispatch_delivered_starts_a_verify(conn):
    t = now()
    tid = _task(conn)
    d = sessions.claim(conn, "master", t)
    assert d["task_id"] == tid
    sessions.dispatch_sent(conn, tid, "delivered", now_ts=t)
    assert conn.execute("SELECT phase FROM upkeep WHERE key=?", (tid,)).fetchone()[0] == "verify"
    queue.set_switch(conn, "clear_check", False)
    t2 = _task(conn, client="Furlani")
    sessions.claim(conn, "master", t)
    sessions.dispatch_sent(conn, t2, "delivered", now_ts=t)
    assert conn.execute("SELECT 1 FROM upkeep WHERE key=?", (t2,)).fetchone() is None


def test_closed_task_finishes_its_row(conn):
    t = now()
    tid = _task(conn)
    upkeep._put(conn, tid, "redispatch", "local_royal", "idle_check", t, task_id=tid, client="Royal")
    store.complete_task(conn, tid, VPN_FAIL)
    assert upkeep.claim(conn, "master", t) is None
    assert conn.execute("SELECT phase FROM upkeep WHERE key=?", (tid,)).fetchone()[0] == "done"


# ------------------------------------------------------------ 4. restart after a coord update

def test_code_version_changes_with_the_code(tmp_path):
    (tmp_path / "a.py").write_text("x = 1")
    v1 = upkeep.code_version(tmp_path)
    (tmp_path / "a.py").write_text("x = 2")
    assert upkeep.code_version(tmp_path) != v1


def test_record_self_stores_the_version(conn):
    row = reaper.record_self(conn, env={"CLAUDE_CODE_HOST_SESSION_ID": "local_royal"})
    assert row["code_version"] == upkeep.code_version()
    s = upkeep.stale_sessions(conn)
    assert [(x["session_id"], x["stale"]) for x in s] == [("local_royal", False)]
    conn.execute("UPDATE session_procs SET code_version='old'")
    assert upkeep.stale_sessions(conn)[0]["stale"] is True


def test_stale_idle_session_restarts_only_when_not_running(conn, monkeypatch):
    t = now()
    reaper.record_self(conn, env={"CLAUDE_CODE_HOST_SESSION_ID": "local_royal"})
    conn.execute("UPDATE session_procs SET code_version='old'")
    pid = conn.execute("SELECT server_pid FROM session_procs").fetchone()[0]
    procs = {pid: {"pid": pid, "ppid": 0, "name": "x", "cmd": "", "mb": 1}}
    plan = [{"client": "Royal", "role": "primary", "session_id": "local_royal", "pid": 42, "mb": 1,
             "end": True, "why": "idle"}]
    monkeypatch.setattr(reaper, "plan", lambda *a, **k: plan)
    assert upkeep.plan_restarts(conn, procs, t) == ["local_royal"]
    assert "local_royal" in upkeep.restarting_sessions(conn)
    _task(conn)
    assert sessions.next_dispatch(conn, t) is None  # held for the restart
    a = upkeep.claim(conn, "master", t)
    assert a["action"] == "get_session" and a["key"] == "restart:local_royal"
    upkeep.report(conn, a["key"], running="true", t=t)
    assert conn.execute("SELECT phase FROM upkeep WHERE key=?", (a["key"],)).fetchone()[0] == "idle_check"
    killed = []
    upkeep.report(conn, a["key"], running="false", t=t + 60,
                  restarter=lambda c, sid: (killed.append(sid) or True, "ended"))
    assert killed == ["local_royal"]
    assert conn.execute("SELECT phase FROM upkeep WHERE key=?", (a["key"],)).fetchone()[0] == "done"


def test_restart_switch_off_plans_nothing(conn, monkeypatch):
    queue.set_switch(conn, "code_restart", False)
    reaper.record_self(conn, env={"CLAUDE_CODE_HOST_SESSION_ID": "local_royal"})
    conn.execute("UPDATE session_procs SET code_version='old'")
    monkeypatch.setattr(reaper, "plan", lambda *a, **k: [{"session_id": "local_royal", "end": True}])
    assert upkeep.plan_restarts(conn, {}, now()) == []
    assert json.loads(conn.execute("SELECT value FROM meta WHERE key='upkeep:stale'").fetchone()[0])["sessions"]


def test_page_renders(conn):
    upkeep.vpn_tick(conn, prober=_probe(False), force=True)
    html = upkeep.page_html(conn)
    assert "Royal" in html and "down" in html


# ------------------------------------------------------------ 5. reviews through a cleared primary

def _review(conn, client="Royal", producer_session=None):
    parent = _task(conn, client, kind="deliverable")
    conn.execute("UPDATE tasks SET state='done', dispatch_session=? WHERE id=?", (producer_session, parent))
    rid = store.create_task(conn, "dod_review", "r", {"review_of": parent}, assigned_to=client,
                            parent_id=parent)["task_id"]
    conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (now() - OLD, rid))
    conn.commit()
    return rid


def test_review_without_side_session_clears_primary_then_sends(conn):
    queue.set_switch(conn, "auto_review_dispatch", True)
    rid = _review(conn)
    assert sessions.next_dispatch(conn) is None  # never plain auto-dispatch
    a = upkeep.claim(conn, "t")
    assert (a["key"], a["action"], a["session_id"]) == (rid, "get_session", "local_royal")
    upkeep.report(conn, rid, running="false")
    a = upkeep.claim(conn, "t")
    assert a["action"] == "send" and "clear_session" in a["message"] and "review" in a["message"]
    upkeep.report(conn, rid, status="delivered")
    t = now() + upkeep.POLL + 1
    a = upkeep.claim(conn, "t", t)
    assert a["action"] == "get_session"
    upkeep.report(conn, rid, running="false", t=t)
    a = upkeep.claim(conn, "t", t)
    assert a["action"] == "send" and a["message"].startswith("Do task " + rid)
    upkeep.report(conn, rid, status="delivered", t=t)
    assert upkeep._row(conn, rid)["phase"] == "verify"
    assert conn.execute("SELECT dispatch_session FROM tasks WHERE id=?", (rid,)).fetchone()[0] == "local_royal"
    assert rid not in store.minder_owed(conn, now() + 3600).get("undelivered", [])


def test_review_goes_to_side_session_unless_it_wrote_the_work(conn):
    queue.set_switch(conn, "auto_review_dispatch", True)
    sessions.set_session(conn, "Royal", "side", "local_royal2")
    rid = _review(conn, producer_session="local_royal")
    d = sessions.next_dispatch(conn)
    assert (d["task_id"], d["session_id"]) == (rid, "local_royal2")
    assert upkeep.reviewable(conn) == []
    conn.execute("UPDATE tasks SET dispatch_session='local_royal2' WHERE id=(SELECT parent_id FROM tasks WHERE id=?)",
                 (rid,))
    conn.commit()
    assert sessions.next_dispatch(conn) is None
    assert [r["session_id"] for r in upkeep.reviewable(conn)] == ["local_royal"]


def test_review_waits_for_busy_primary_and_switch(conn):
    rid = _review(conn)
    assert upkeep.reviewable(conn) == []  # switch off (conftest)
    queue.set_switch(conn, "auto_review_dispatch", True)
    other = _task(conn)
    conn.execute("UPDATE tasks SET picked_up_at=? WHERE id=?", (now(), other))
    conn.commit()
    assert upkeep.reviewable(conn) == []
    conn.execute("UPDATE tasks SET state='done' WHERE id=?", (other,))
    conn.commit()
    assert [r["task_id"] for r in upkeep.reviewable(conn)] == [rid]


# ------------------------------------------------------------ 6. short tasks queued behind a busy session

def _busy(conn, client="Royal"):
    tid = _task(conn, client)
    conn.execute("UPDATE tasks SET picked_up_at=?, dispatch_session='local_royal' WHERE id=?", (now(), tid))
    conn.commit()
    return tid


def _ctx(pct):
    return {"context": {"status": "ok", "tokensUsed": pct * 1000, "contextWindow": 100000, "percentUsed": pct}}


def test_short_task_queued_to_busy_session_under_half_context(conn):
    queue.set_switch(conn, "auto_queue", True)
    _busy(conn)
    draft = _task(conn, kind="draft_reply")
    _task(conn, kind="investigation")  # spec {"q": "x"}: one question, but only one queues per session
    a = upkeep.claim(conn, "t")
    assert (a["key"], a["action"], a["session_id"]) == (draft, "get_usage", "local_royal")
    upkeep.report(conn, draft, usage=_ctx(30))
    a = upkeep.claim(conn, "t")
    assert a["action"] == "send" and a["message"].startswith("Do task " + draft)
    upkeep.report(conn, draft, status="queued")
    assert upkeep._row(conn, draft)["phase"] == "done"
    assert conn.execute("SELECT dispatch_session FROM tasks WHERE id=?", (draft,)).fetchone()[0] == "local_royal"


def test_queue_waits_when_context_full_and_never_code_or_behind_a_park(conn):
    queue.set_switch(conn, "auto_queue", True)
    busy = _busy(conn)
    _task(conn, kind="code_change")
    _task(conn, kind="deliverable")
    assert upkeep.queueable(conn) == []
    draft = _task(conn, kind="draft_reply")
    a = upkeep.claim(conn, "t")
    upkeep.report(conn, draft, usage=_ctx(70))
    assert upkeep._row(conn, draft)["phase"] == "skipped"
    assert upkeep.queueable(conn) == []  # asked again only after QUEUE_RECHECK
    assert [d["task_id"] for d in upkeep.queueable(conn, now() + upkeep.QUEUE_RECHECK + 1)] == [draft]
    store.park_task(conn, busy, "context", "next")
    assert upkeep.queueable(conn, now() + upkeep.QUEUE_RECHECK + 1) == []
    queue.set_switch(conn, "auto_queue", False)
    assert upkeep.queueable(conn, now() + 9999) == []


def test_multi_question_investigation_is_not_queued(conn):
    queue.set_switch(conn, "auto_queue", True)
    _busy(conn)
    tid = store.create_task(conn, "investigation", "t", {"questions": ["a", "b"]}, assigned_to="Royal")["task_id"]
    conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (now() - OLD, tid))
    assert upkeep.queueable(conn) == []
