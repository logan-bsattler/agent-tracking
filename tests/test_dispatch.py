"""Dispatch in code: the session registry, next_dispatch's choice, the claim
lease, the sent report, the health problem and minder's quiet window."""

from __future__ import annotations

import pytest

from coord_mcp import sessions, store
from coord_mcp.db import connect, now

INV = {"done": True, "verdict": "v", "confidence": "high", "evidence": ["e"]}
OLD = 10 * 60  # past DISPATCH_AFTER


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    sessions.set_session(c, "MAG", "primary", "local_mag")
    yield c
    c.close()


def _task(conn, kind="investigation", client="MAG", age=OLD, spec=None, parent=None):
    tid = store.create_task(conn, kind, "t", spec or {"q": "x"}, assigned_to=client, parent_id=parent)["task_id"]
    conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (now() - age, tid))
    return tid


def test_seed_loads_the_thirteen_primaries(tmp_path):
    c = connect(tmp_path / "s.db")
    sessions.seed(c)
    rows = sessions.list_sessions(c)
    assert len(rows) == 13 and {r["role"] for r in rows} == {"primary"}
    pc = next(r for r in rows if r["client"] == "P&C")
    assert pc["session_id"] == "local_12ca855c-2ea1-4d76-93e9-9a3ce626255d" and pc["title"] == "P&C Local session"
    assert next(r for r in rows if r["client"] == "Moog")["title"] == "Moog - Local"
    sessions.seed(c)  # idempotent
    assert len(sessions.list_sessions(c)) == 13
    with pytest.raises(ValueError):
        sessions.set_session(c, "MAG", "tertiary", "x")
    c.close()


def test_cli_set_and_list(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COORD_DB", str(tmp_path / "cli.db"))
    assert sessions.main(["set", "LNK", "side", "local_lnk2", "--title", "LNK - Local 2"]) == 0
    assert sessions.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "local_lnk2" in out and "1 registered" in out


def test_oldest_undelivered_task_goes_to_the_primary(conn):
    a = _task(conn, age=OLD + 60)
    _task(conn)
    d = store.next_dispatch(conn)
    assert d == {"task_id": a, "client": "MAG", "role": "primary", "session_id": "local_mag",
                 "kind": "investigation", "message": f"Do task {a}. Start with coord_get_task."}


def test_a_new_task_waits_for_masters_own_send(conn):
    _task(conn, age=10)
    assert store.next_dispatch(conn) is None


def test_a_busy_session_gets_nothing(conn):
    running = _task(conn, age=OLD + 60)
    store.get_task(conn, running, reader="MAG")
    _task(conn)
    assert store.next_dispatch(conn) is None


def test_a_parked_task_holds_its_session_and_is_never_sent(conn):
    p = _task(conn, age=OLD + 60)
    store.get_task(conn, p, reader="MAG")
    store.park_task(conn, p, next_step="resume")
    _task(conn)
    assert store.next_dispatch(conn) is None  # the clear must not be cancelled
    conn.execute("UPDATE tasks SET state='done' WHERE id=?", (p,))
    assert store.next_dispatch(conn)["task_id"] != p


def test_side_kinds_go_to_the_side_session_when_one_exists(conn):
    running = _task(conn, kind="code_change", age=OLD + 60)
    store.get_task(conn, running, reader="MAG")
    draft = _task(conn, kind="draft_reply")
    assert store.next_dispatch(conn) is None  # no side session: the primary is busy
    sessions.set_session(conn, "MAG", "side", "local_mag2")
    d = store.next_dispatch(conn)
    assert (d["task_id"], d["role"], d["session_id"]) == (draft, "side", "local_mag2")


def test_a_review_with_no_side_session_is_left_for_master(conn):
    _task(conn, kind="dod_review")
    assert store.next_dispatch(conn) is None
    sessions.set_session(conn, "MAG", "side", "local_mag2")
    assert store.next_dispatch(conn)["session_id"] == "local_mag2"


def test_unregistered_held_and_waiting_tasks_are_skipped(conn):
    _task(conn, client="Royal")
    held = _task(conn)
    store.record_decision(conn, "hold until Monday", "waiting on access", task_id=held)
    assert store.next_dispatch(conn) is None
    blocker = _task(conn, client="Royal")
    waiter = _task(conn)
    store.add_dep(conn, waiter, blocker)
    assert store.next_dispatch(conn) is None


def test_a_checkin_is_dispatched_like_any_code_change(conn):
    code = store.create_task(conn, "code_change", "c", {"q": "x"}, assigned_to="MAG")["task_id"]
    store.complete_task(conn, code, {"done": True, "summary": "s", "files_changed": [{"path": "a.p"}]})
    ci = _task(conn, kind="code_change", spec={"checkin_of": code})
    assert store.next_dispatch(conn)["task_id"] == ci


def test_claim_leases_and_sent_stamps_the_task(conn):
    a = _task(conn)
    sessions.set_session(conn, "LNK", "primary", "local_lnk")
    b = _task(conn, client="LNK")
    first = sessions.claim(conn, "master")
    assert first["task_id"] == a and first["attempt_id"]
    second = sessions.claim(conn, "desk")  # the other process: the next session's task, never the same
    assert second["task_id"] == b
    assert sessions.claim(conn, "desk") is None
    sessions.dispatch_sent(conn, a, "delivered", "delivered")
    r = conn.execute("SELECT dispatched_at, dispatch_status, dispatch_session FROM tasks WHERE id=?", (a,)).fetchone()
    assert r["dispatched_at"] and r["dispatch_status"] == "delivered" and r["dispatch_session"] == "local_mag"
    with pytest.raises(ValueError):
        sessions.dispatch_sent(conn, a, "delivered")  # no open claim left
    with pytest.raises(ValueError):
        sessions.dispatch_sent(conn, b, "sent")


def test_a_dispatched_task_occupies_its_session_and_is_resent_late(conn):
    a = _task(conn, age=OLD + 60)
    b = _task(conn)
    t = now()
    sessions.claim(conn, now_ts=t)
    sessions.dispatch_sent(conn, a, "queued", now_ts=t)
    assert sessions.next_dispatch(conn, t + 60) is None  # b waits: a is in flight
    later = t + sessions.RESEND_AFTER + 1
    d = sessions.next_dispatch(conn, later)
    assert d["task_id"] == a  # never picked up: sent again, b still behind it
    store.get_task(conn, a, reader="MAG")
    assert sessions.next_dispatch(conn, later) is None
    store.complete_task(conn, a, INV)
    assert sessions.next_dispatch(conn, later)["task_id"] == b


def test_an_expired_lease_frees_the_task_after_the_retry_wait(conn):
    a = _task(conn)
    t = now()
    sessions.claim(conn, now_ts=t)
    assert sessions.next_dispatch(conn, t + sessions.LEASE + 1) is None
    assert sessions.next_dispatch(conn, t + sessions.RETRY_AFTER + 1)["task_id"] == a


def test_two_errors_in_a_row_are_a_health_problem(conn):
    a = _task(conn)
    t = now()
    sessions.claim(conn, now_ts=t)
    sessions.dispatch_sent(conn, a, "error", "session not found", now_ts=t)
    assert sessions.trouble(conn) == []
    assert conn.execute("SELECT dispatch_status FROM tasks WHERE id=?", (a,)).fetchone()[0] == "error"
    assert sessions.next_dispatch(conn, t + 10) is None  # retry waits
    t2 = t + sessions.RETRY_AFTER + 1
    assert sessions.claim(conn, now_ts=t2)["task_id"] == a
    sessions.dispatch_sent(conn, a, "error", "session not found", now_ts=t2)
    [p] = [h for h in store.health(conn) if h["key"] == "dispatch"]
    assert "local_mag" in p["text"] and p["detail"] == "session not found"
    t3 = t2 + sessions.RETRY_AFTER + 1
    sessions.claim(conn, now_ts=t3)
    sessions.dispatch_sent(conn, a, "delivered", now_ts=t3)
    assert sessions.trouble(conn) == []


def test_minder_is_quiet_for_what_code_sent(conn):
    a = _task(conn, age=store.MINDER_UNDELIVERED * 60 + 120)
    t = now()
    assert a in store.minder_owed(conn, t)["undelivered"]
    sessions.claim(conn, now_ts=t)
    sessions.dispatch_sent(conn, a, "delivered", now_ts=t)
    assert a not in store.minder_owed(conn, t)["undelivered"]
    assert a in store.minder_owed(conn, t + sessions.MINDER_QUIET + 1)["undelivered"]


def test_dispatch_endpoints(tmp_path, monkeypatch):
    import functools
    import json
    import socket
    import threading
    import time
    import urllib.request

    from coord_mcp import report, usage

    monkeypatch.setattr(usage, "ingest", lambda conn: None)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    make = functools.partial(connect, tmp_path / "srv.db")
    c = make()
    sessions.set_session(c, "MAG", "primary", "local_mag")
    tid = _task(c)
    threading.Thread(target=report.serve, args=(make, port), daemon=True).start()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)

    def post(path, body=b""):
        try:
            with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body),
                                        timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    code, body = post("/api/dispatch/claim", b"claimer=desk")
    assert code == 200 and body["dispatch"]["task_id"] == tid
    assert body["dispatch"]["message"] == f"Do task {tid}. Start with coord_get_task."
    assert post("/api/dispatch/claim", b"claimer=master")[1] == {"dispatch": None}
    assert post("/api/dispatch/sent", f"task_id={tid}&status=bogus".encode())[0] == 400
    code, body = post("/api/dispatch/sent", f"task_id={tid}&status=delivered&detail=ok".encode())
    assert code == 200 and body["status"] == "delivered"
    c.close()
