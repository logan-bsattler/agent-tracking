"""Task priority orders undated work (coord_mcp/priority.py)."""

from __future__ import annotations

import pytest

from coord_mcp import priority, sessions, store
from coord_mcp.db import connect, now

OLD = 10 * 60  # past DISPATCH_AFTER


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    sessions.set_session(c, "MAG", "primary", "local_mag")
    yield c
    c.close()


def _task(conn, age=OLD, spec=None, parent=None, client="MAG"):
    tid = store.create_task(conn, "investigation", "t", spec or {"q": "x"}, assigned_to=client,
                            parent_id=parent)["task_id"]
    conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (now() - age, tid))
    return tid


def test_high_goes_before_older_normal_and_low(conn):
    low = _task(conn, age=OLD + 900, spec={"q": "x", "priority": "low"})
    normal = _task(conn, age=OLD + 600)
    high = _task(conn, spec={"q": "x", "priority": "high"})
    order = [r["id"] for r in sessions._candidates(conn, now())]
    assert order == [high, normal, low]


def test_a_due_date_beats_priority(conn):
    from coord_mcp import commitments
    high = _task(conn, age=OLD + 600, spec={"q": "x", "priority": "high"})
    dated = _task(conn, spec={"q": "x", "priority": "low"})
    commitments.add(conn, "MAG", "Pat", "send the fix", due="2026-11-16", task_id=dated)
    assert [r["id"] for r in sessions._candidates(conn, now())] == [dated, high]


def test_a_child_inherits_its_chain_and_can_override(conn):
    root = _task(conn, spec={"q": "x", "priority": "high"})
    child = _task(conn, parent=root)
    own = _task(conn, parent=root, spec={"q": "x", "priority": "low"})
    assert priority.of(conn, child) == "high"
    assert priority.of(conn, own) == "low"
    assert priority.of(conn, _task(conn)) == "normal"


def test_a_bad_level_is_refused(conn):
    with pytest.raises(ValueError, match="spec.priority"):
        _task(conn, spec={"q": "x", "priority": "urgent"})


def test_set_records_a_decision_and_reorders(conn):
    old = _task(conn, age=OLD + 600)
    new = _task(conn)
    out = priority.set_priority(conn, new, "high", "Ben wants it first")
    assert out["priority"] == "high"
    assert conn.execute("SELECT statement FROM decisions WHERE id=?",
                        (out["decision_id"],)).fetchone()[0] == f"Priority of {new} set to high"
    assert [r["id"] for r in sessions._candidates(conn, now())] == [new, old]
    with pytest.raises(ValueError, match="no task"):
        priority.set_priority(conn, "nope", "low")


def _ben_item(conn, task_id, what, age):
    from coord_mcp.direct import _ben_item
    fid = _ben_item(conn, task_id, what)
    conn.execute("UPDATE follow_ups SET created_at=? WHERE id=?", (now() - age, fid))
    return fid


def test_bens_queue_orders_undated_items_by_priority(conn):
    from coord_mcp import commitments, queue
    normal_t, high_t, low_t, dated_t = (_task(conn) for _ in range(4))
    priority.set_priority(conn, high_t, "high")  # a finished task's items count too
    conn.execute("UPDATE tasks SET state='done' WHERE id=?", (high_t,))
    priority.set_priority(conn, low_t, "low")
    commitments.add(conn, "MAG", "Pat", "send it", due="2099-01-01", task_id=dated_t)
    old = _ben_item(conn, normal_t, "normal, oldest", 900)
    low = _ben_item(conn, low_t, "low, older", 600)
    high = _ben_item(conn, high_t, "high, newest", 10)
    dated = _ben_item(conn, dated_t, "dated, low priority", 5)
    priority.set_priority(conn, dated_t, "low")
    items = queue.ben_queue(conn)["groups"][0]["items"]
    assert [i["id"] for i in items] == [dated, high, old, low]
    assert [i["priority"] for i in items] == ["low", "high", "normal", "low"]
    assert all("rank" not in i for i in items)


def test_undated_client_groups_order_by_best_priority(conn):
    from coord_mcp import queue
    mag = _ben_item(conn, _task(conn), "MAG, old", 900)
    lnk_t = _task(conn, client="LNK", spec={"q": "x", "priority": "high"})
    lnk = _ben_item(conn, lnk_t, "LNK, new, high", 10)
    groups = queue.ben_queue(conn)["groups"]
    assert [g["client"] for g in groups] == ["LNK", "MAG"]
    assert (groups[0]["items"][0]["id"], groups[1]["items"][0]["id"]) == (lnk, mag)


def test_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COORD_DB", str(tmp_path / "cli.db"))
    c = connect(tmp_path / "cli.db")
    tid = store.create_task(c, "investigation", "t", {"q": "x"}, assigned_to="MAG")["task_id"]
    c.commit()
    c.close()
    assert priority.main(["set", tid, "low"]) == 0
    assert priority.main(["get", tid]) == 0
    assert capsys.readouterr().out.strip().endswith("low")
