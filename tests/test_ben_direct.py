"""Ben's direct asks (direct.py): flagged, dispatched first, owed at once,
follow-ups tasked, and handed back as a ready item when the chain is finished."""

from __future__ import annotations

import pytest

from coord_mcp import direct, queue, sessions, store
from coord_mcp.db import connect, now


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    queue.set_switch(c, "require_pipeline", False)
    yield c
    c.close()


INV = {"done": True, "verdict": "The allocation is wrong for site 20", "confidence": "high",
       "evidence": ["so_mstr 1234 shows site 20"]}


def _task(conn, spec=None, parent=None, kind="investigation", client="Moog"):
    return store.create_task(conn, kind, "Check allocations", spec or {"q": "why"}, assigned_to=client,
                             parent_id=parent)["task_id"]


def test_flag_is_inherited_down_the_chain(conn):
    root = _task(conn, {"from": "ben-direct", "asked": "Why is SO 1234 short?"})
    child = _task(conn, parent=root)
    other = _task(conn, {"note": "Ben said ben-direct in passing"})
    assert direct.is_direct(conn, root) and direct.is_direct(conn, child)
    assert not direct.is_direct(conn, other)
    assert direct.direct_ids(conn) == {root, child}


def test_dispatch_sends_direct_first_with_no_wait(conn):
    sessions.set_session(conn, "Moog", "primary", "local_moog")
    t = now()
    old = _task(conn)
    conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (t - 3600, old))
    new = _task(conn, {"from": "ben-direct"})  # created this second: no DISPATCH_AFTER wait
    d = sessions.next_dispatch(conn, t)
    assert d and d["task_id"] == new


def test_minder_lists_direct_work_at_once(conn):
    root = _task(conn, {"from": "ben-direct"})
    owed = store.minder_owed(conn, now())
    assert owed["ben_direct"] == [f"{root} undelivered"]
    assert root not in owed["undelivered"]


def test_follow_ups_become_tasks_and_ready_item_lands(conn):
    root = _task(conn, {"from": "ben-direct"})
    out = store.complete_task(conn, root, {**INV, "follow_ups": [
        {"who": "Moog", "what": "Check the site 30 orders the same way"}]})
    assert out["ben_direct"] and len(out["hand_back_tasks"]) == 1
    child = out["hand_back_tasks"][0]["task_id"]
    row = conn.execute("SELECT kind, parent_id, state FROM tasks WHERE id=?", (child,)).fetchone()
    assert (row["kind"], row["parent_id"], row["state"]) == ("investigation", root, "open")
    assert not direct.chain_state(conn, root)["ready"]
    assert direct.ready_items(conn) == []
    out2 = store.complete_task(conn, child, INV)
    assert out2["ready_for_ben"] == [root]
    items = direct.ready_items(conn)
    assert [i["root_id"] for i in items] == [root] and items[0]["task_id"] == child
    # toasted once, by the desk when it is live, else the worker
    first = direct.claim_announce(conn, "desk")
    assert first and first["root_id"] == root
    assert direct.claim_announce(conn, "desk") is None
    assert direct.mark_seen(conn, root) and direct.ready_items(conn) == []


def test_ordinary_tasks_are_untouched(conn):
    tid = _task(conn)
    out = store.complete_task(conn, tid, {**INV, "follow_ups": [{"who": "Moog", "what": "Check site 30"}]})
    assert "ben_direct" not in out and direct.ready_items(conn) == []


def test_deliverable_is_ready_only_once_promoted(conn, tmp_path):
    root = store.create_task(conn, "deliverable", "Status report", {
        "from": "ben-direct", "deliverable_type": "status-report", "dod_path": "dod/status-report.md",
        "format": "html"}, assigned_to="Moog")["task_id"]
    st = direct.chain_state(conn, root)
    assert not st["ready"]
    conn.execute("UPDATE tasks SET state='done', completed_at=?, result='{\"done\": true}' WHERE id=?",
                 (now(), root))
    st = direct.chain_state(conn, root)
    assert not st["ready"] and any("not promoted" in w for w in st["waits"])


def test_file_work_lands_flagged_and_done(conn):
    out = store.file_work(conn, "Moog", "investigation", INV, "Why is SO 1234 short", asked="Why short?")
    assert out["state"] == "done" and out["from"] == "ben-direct" and out["ben_direct"]
    assert out["ready_for_ben"] == [out["task_id"]]
    with pytest.raises(ValueError):
        store.file_work(conn, "Moog", "dod_review", INV, "x")
    with pytest.raises(ValueError):
        store.file_work(conn, "desk", "investigation", INV, "x")
