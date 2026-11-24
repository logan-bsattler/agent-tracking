"""Step 2 of the dispatch automation: a request_changes dod_review opens the
next round (store.next_round, switch auto_revise); after the second
request_changes in the chain the call is Ben's ("Two rounds, then Ben")."""

from __future__ import annotations

import json

import pytest

from coord_mcp import queue, store
from coord_mcp.db import connect

SELF = [{"item": "a", "met": True, "note": "ok"}]


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DOCS_ROOT", tmp_path / "docs")
    monkeypatch.setattr(store, "AGENTS_ROOT", tmp_path / "agents")
    c = connect(tmp_path / "t.db")
    queue.set_switch(c, "auto_revise", True)
    yield c
    c.close()


def _finish(conn, tid):
    store.complete_task(conn, tid, {"done": True, "summary": "s", "path": "C:/x/report.html",
                                    "deliverable_type": "status-report", "dod_path": "dod/status-report.md",
                                    "self_check": SELF})


def _review_of(conn, tid):
    return conn.execute("SELECT id FROM tasks WHERE parent_id=? AND kind='dod_review'", (tid,)).fetchone()["id"]


def _request_changes(conn, review_id, items=("Fix the date",)):
    return store.complete_task(conn, review_id, {
        "done": True, "verdict": "request_changes", "summary": "s", "path": "p", "dod_path": "d",
        "criteria": [{"item": "a", "met": False}],
        "findings": [{"criterion": "a", "severity": "blocker", "note": "date wrong"}],
        "follow_ups": [{"who": "MAG", "what": w} for w in items]})


def _deliverable(conn):
    tid = store.create_task(conn, "deliverable", "Status report", {"format": "html", "deliverable_type": "status-report"},
                            assigned_to="MAG")["task_id"]
    _finish(conn, tid)
    return tid


def test_request_changes_opens_the_next_round(conn):
    d1 = _deliverable(conn)
    rv = _review_of(conn, d1)
    out = _request_changes(conn, rv, ("Fix the date", "Add the owner"))
    nxt = out["next_round"]
    t = conn.execute("SELECT * FROM tasks WHERE id=?", (nxt["task_id"],)).fetchone()
    assert t["kind"] == "deliverable" and t["parent_id"] == d1 and t["assigned_to"] == "MAG"
    assert t["title"] == "Revise: Status report"
    spec = json.loads(t["spec"])
    assert spec["findings"] == ["Fix the date", "Add the owner"] and spec["revises"] == d1
    assert spec["format"] == "html" and spec["deliverable_type"] == "status-report"
    fus = conn.execute("SELECT state, resolved_by FROM follow_ups WHERE task_id=?", (rv,)).fetchall()
    assert [(f["state"], f["resolved_by"]) for f in fus] == [("tasked", nxt["task_id"])] * 2


def test_second_request_changes_goes_to_ben(conn):
    d1 = _deliverable(conn)
    d2 = _request_changes(conn, _review_of(conn, d1))["next_round"]["task_id"]
    _finish(conn, d2)
    rv2 = _review_of(conn, d2)
    out = _request_changes(conn, rv2, ("Still wrong",))
    assert out["next_round"]["round_cap"] is True
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE kind='deliverable'").fetchone()[0] == 2
    ben = conn.execute("SELECT what, state FROM follow_ups WHERE task_id=? AND who='Ben'", (rv2,)).fetchone()
    assert ben["state"] == "open" and "Two rounds" in ben["what"] and "Still wrong" in ben["what"]
    client = conn.execute("SELECT state FROM follow_ups WHERE task_id=? AND who='MAG'", (rv2,)).fetchone()
    assert client["state"] == "dropped"


def test_an_open_revision_takes_the_findings(conn):
    d1 = _deliverable(conn)
    live = store.create_task(conn, "deliverable", "Revise by hand", {"revises": d1}, assigned_to="MAG",
                             parent_id=d1)["task_id"]
    out = _request_changes(conn, _review_of(conn, d1))
    assert out["next_round"] == {"task_id": live, "folded": True}
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE kind='deliverable'").fetchone()[0] == 2


def test_approve_and_switch_off_do_nothing(conn):
    d1 = _deliverable(conn)
    queue.set_switch(conn, "auto_revise", False)
    assert "next_round" not in _request_changes(conn, _review_of(conn, d1))
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE kind='deliverable'").fetchone()[0] == 1
