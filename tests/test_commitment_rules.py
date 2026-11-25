"""Rules C1, C2 and P2 (Ben, 2026-11-25): commitments linked at birth, Ben's
dated item a working day before due, and a priority on every root task."""

from __future__ import annotations

import pytest

from coord_mcp import commitments as cm
from coord_mcp import queue, store
from coord_mcp.db import connect, now

TZ = "America/Detroit"
DAY = 86400


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("COORD_TZ", TZ)
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _task(conn, client="MAG", parent=None):
    return store.create_task(conn, "investigation", "t", {"q": "x"}, assigned_to=client, parent_id=parent)["task_id"]


def _intent(conn):
    return store.propose_intent(conn, "Ben promised David the specs", "email", "ctx")["intent_id"]


def _fu(conn, fid):
    return conn.execute("SELECT * FROM follow_ups WHERE id=?", (fid,)).fetchone()


def _ben_open(conn, fid):
    return _fu(conn, fid)["state"] == "open"


# ------------------------------------------------------------ C1


@pytest.mark.commitment_link
def test_c1_add_refuses_an_unlinked_commitment(conn):
    with pytest.raises(ValueError, match="linked when it is added"):
        cm.add(conn, "MAG", "David", "specs", "Friday", intent_id=_intent(conn))
    fu = store.ben_item(conn, _intent(conn), "something", "MAG")["follow_up_id"]
    with pytest.raises(ValueError, match="loose end alone"):
        cm.add(conn, "MAG", "David", "specs", "Friday", follow_up_id=fu)
    c = cm.add(conn, "MAG", "David", "specs", "Friday", task_id=_task(conn))
    assert c["task_id"] and not c["ben_item_id"]  # linked work gets its item a day out (C2)


@pytest.mark.commitment_link
def test_c1_ben_opens_a_dated_item_at_once(conn):
    iid = _intent(conn)
    c = cm.add(conn, "MAG", "David", "specs", "2099-01-02", intent_id=iid, ben=True)
    f = _fu(conn, c["ben_item_id"])
    assert f["who"] == "Ben" and f["intent_id"] == iid and f["task_id"] is None and f["client"] == "MAG"
    assert f["due_at"] == c["due_at"] and "Done, Re-date or Drop" in f["what"] and "David" in f["what"]
    assert c["follow_up_id"] == c["ben_item_id"]
    u = cm.add(conn, "MAG", "Sam", "a call", no_date=True, intent_id=_intent(conn), ben=True)
    assert "undated" in _fu(conn, u["ben_item_id"])["what"]


@pytest.mark.commitment_link
def test_c1_ben_needs_somewhere_to_hang_the_item(conn):
    with pytest.raises(ValueError, match="needs --intent"):
        cm.add(conn, "MAG", "David", "specs", "Friday", ben=True)


@pytest.mark.commitment_link
def test_c1_cli_takes_ben_flag(conn, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COORD_DB", str(tmp_path / "t.db"))
    iid = _intent(conn)
    assert cm.main(["add", "MAG", "--to", "David", "--what", "specs"]) == 1
    assert "linked when it is added" in capsys.readouterr().err
    assert cm.main(["add", "MAG", "--to", "David", "--what", "specs", "--intent", iid, "--ben"]) == 0
    assert conn.execute("SELECT ben_item_id FROM commitments").fetchone()[0]


# ------------------------------------------------------------ C2


def test_c2_sweep_opens_one_item_a_working_day_out(conn):
    tid = _task(conn)
    t = now()
    c = cm.add(conn, "MAG", "David", "specs", "2099-01-02", task_id=tid, intent_id=_intent(conn))
    conn.execute("UPDATE commitments SET due_at=? WHERE id=?", (t + 10 * DAY, c["id"]))
    cm.sweep(conn, t)
    assert cm.get(conn, c["id"])["ben_item_id"] is None  # not yet
    conn.execute("UPDATE commitments SET due_at=? WHERE id=?", (t + 3600, c["id"]))
    cm.sweep(conn, t)
    fid = cm.get(conn, c["id"])["ben_item_id"]
    assert fid and _fu(conn, fid)["due_at"] == t + 3600
    cm.sweep(conn, t + 60)
    assert conn.execute("SELECT COUNT(*) FROM follow_ups WHERE who='Ben'").fetchone()[0] == 1  # one, not two


def test_c2_item_hangs_on_the_task_when_there_is_no_intent(conn):
    tid = _task(conn)
    c = cm.add(conn, "MAG", "David", "specs", "x", task_id=tid)
    conn.execute("UPDATE commitments SET due_at=? WHERE id=?", (now() - 60, c["id"]))
    cm.sweep(conn)
    f = _fu(conn, cm.get(conn, c["id"])["ben_item_id"])
    assert f["task_id"] == tid and f["who"] == "Ben"


def _slipped_with_item(conn):
    c = cm.add(conn, "MAG", "David", "specs", "x", task_id=_task(conn), intent_id=_intent(conn))
    conn.execute("UPDATE commitments SET due_at=? WHERE id=?", (now() - 60, c["id"]))
    cm.sweep(conn)
    return cm.get(conn, c["id"])


def test_c2_only_the_commitment_closes_the_item(conn):
    c = _slipped_with_item(conn)
    fid = c["ben_item_id"]
    d = store.record_decision(conn, "handoff: still owed")["decision_id"]
    with pytest.raises(ValueError, match="only Done, Re-date or Drop"):
        store.resolve_follow_up(conn, fid, decision_id=d)
    with pytest.raises(ValueError, match="only Done, Re-date or Drop"):
        store.resolve_follow_up(conn, fid, decision_id=d, defer=True)
    with pytest.raises(ValueError, match="close only on the commitment"):
        queue.apply(conn, "drop", [fid])
    assert _ben_open(conn, fid)
    cm.close(conn, c["id"], "done", d)
    assert _fu(conn, fid)["state"] == "dropped" and _fu(conn, fid)["resolved_by"] == d


def test_c2_redate_needs_a_decision_and_moves_the_item(conn):
    c = _slipped_with_item(conn)
    old = c["ben_item_id"]
    with pytest.raises(ValueError, match="--decision"):
        cm.set_(conn, c["id"], due="2099-01-02")
    d = store.record_decision(conn, f"Commitment {c['id']} re-dated")["decision_id"]
    new = cm.set_(conn, c["id"], due="2099-01-02", decision_id=d)
    assert _fu(conn, old)["resolved_by"] == d
    assert new["state"] == "open" and new["ben_item_id"] is None  # linked work: next item a day before the new date
    cm.sweep(conn, new["due_at"] - 3600)
    assert cm.get(conn, c["id"])["ben_item_id"]


@pytest.mark.commitment_link
def test_c2_redate_of_bens_own_work_keeps_an_item(conn):
    c = cm.add(conn, "MAG", "David", "specs", "x", intent_id=_intent(conn), ben=True)
    d = store.record_decision(conn, "re-dated")["decision_id"]
    new = cm.set_(conn, c["id"], due="2099-01-02", decision_id=d)
    assert new["ben_item_id"] and new["ben_item_id"] != c["ben_item_id"]
    assert _fu(conn, new["ben_item_id"])["due_at"] == new["due_at"]


def test_c2_buttons_resolve_the_item(conn):
    c = _slipped_with_item(conn)
    cm.act(conn, "drop", c["id"])
    assert _fu(conn, c["ben_item_id"])["state"] == "dropped"


def test_c2_ben_cover_leaves_the_item_alone(conn):
    c = _slipped_with_item(conn)
    assert c["ben_item_id"] in cm.locked_items(conn)
    assert cm.locked_items(conn, ["nope"]) == {}


# ------------------------------------------------------------ P2


@pytest.mark.priority_rule
def test_p2_root_client_task_needs_a_priority(conn):
    queue.set_switch(conn, "require_priority", True)  # unmarked SWITCHES read off in tests
    with pytest.raises(ValueError, match="no priority"):
        _task(conn)
    root = store.create_task(conn, "investigation", "t", {"q": "x", "priority": "low"}, assigned_to="MAG")["task_id"]
    _task(conn, parent=root)  # a child inherits
    store.create_task(conn, "investigation", "t", {"q": "x"})  # unassigned: not a client task
    store.create_task(conn, "investigation", "t", {"q": "x"}, assigned_to="Master")
    store.create_task(conn, "investigation", "t", {"q": "x", "from": store.BEN_DIRECT}, assigned_to="MAG")


@pytest.mark.priority_rule
def test_p2_switch_turns_it_off(conn):
    queue.set_switch(conn, "require_priority", False)
    _task(conn)
