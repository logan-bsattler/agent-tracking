"""Rec B: a finished deliverable, or gated CVC code / client email, gets its
dod_review created by complete_task, in the same transaction; never a
second one, and never past the round cap (that is Ben's, through master)."""

from __future__ import annotations

import json

import pytest

from coord_mcp import gate, queue, store
from coord_mcp.db import connect, now

SELF = [{"item": "a", "met": True, "note": "ok"}]


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DOCS_ROOT", tmp_path / "docs")
    monkeypatch.setattr(store, "AGENTS_ROOT", tmp_path / "agents")
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _deliverable(conn, parent=None, spec=None, pipeline=None):
    tid = store.create_task(conn, "deliverable", "Status report", spec or {"format": "html"},
                            assigned_to="MAG", parent_id=parent, pipeline_task_id=pipeline)["task_id"]
    store.complete_task(conn, tid, {"done": True, "summary": "s", "path": "C:/x/report.html",
                                    "deliverable_type": "status-report", "dod_path": "dod/status-report.md",
                                    "self_check": SELF})
    return tid


def _reviews(conn, parent):
    return conn.execute("SELECT * FROM tasks WHERE parent_id=? AND kind='dod_review'", (parent,)).fetchall()


def _verdict(conn, review_id, verdict):
    store.complete_task(conn, review_id, {"done": True, "verdict": verdict, "summary": "s", "path": "p",
                                          "dod_path": "d", "criteria": [{"item": "a", "met": verdict == "approve"}]})


def test_a_finished_deliverable_gets_one_review(conn):
    tid = _deliverable(conn, pipeline="417")
    [r] = _reviews(conn, tid)
    assert r["state"] == "open" and r["assigned_to"] == "MAG" and r["picked_up_at"] is None
    spec = json.loads(r["spec"])
    assert spec["review_of"] == tid and spec["dod_path"] == "dod/status-report.md"
    assert spec["files"] == ["C:/x/report.html"]
    assert "BEFORE reading the producer's self_check" in spec["brief"] and "who = MAG" in spec["brief"]
    assert queue.pipeline_id(conn, r["id"]) == "417"  # inherited through the parent
    assert store.create_review(conn, tid) is None      # never a second


def test_spec_dod_path_wins_then_the_checklist_on_disk(conn, tmp_path):
    a = _deliverable(conn, spec={"format": "html", "dod_path": "C:/c/dod/x.md"})
    assert json.loads(_reviews(conn, a)[0]["spec"])["dod_path"] == "C:/c/dod/x.md"
    (tmp_path / "agents" / "dod").mkdir(parents=True)
    (tmp_path / "agents" / "dod" / "cvc-code.md").write_text("x")
    assert store._dod_checklist("Royal", "cvc-code") == str(tmp_path / "agents" / "dod" / "cvc-code.md")
    (tmp_path / "docs" / "Royal" / "dod").mkdir(parents=True)
    (tmp_path / "docs" / "Royal" / "dod" / "cvc-code.md").write_text("x")
    assert store._dod_checklist("Royal", "cvc-code") == str(tmp_path / "docs" / "Royal" / "dod" / "cvc-code.md")
    assert store._dod_checklist("Royal", "client-email") == "dod/client-email.md"


def test_failed_and_ungated_work_gets_none(conn):
    tid = store.create_task(conn, "deliverable", "x", {"format": "html"}, assigned_to="MAG")["task_id"]
    store.complete_task(conn, tid, {"done": False, "reason": "VPN down", "retryable": True})
    code = store.create_task(conn, "code_change", "x", {"cvc": True}, assigned_to="Royal")["task_id"]
    store.complete_task(conn, code, {"done": True, "summary": "s", "files_changed": [{"path": "a.p"}]})
    assert not _reviews(conn, tid) and not _reviews(conn, code)  # gate not started


def test_gated_code_and_email_get_their_checklists(conn, tmp_path):
    dec = store.record_decision(conn, "Ben approved both checklists", "ruling")["decision_id"]
    gate.start(conn, dec)
    code = store.create_task(conn, "code_change", "Fix", {"cvc": True}, assigned_to="Royal")["task_id"]
    store.complete_task(conn, code, {"done": True, "summary": "s", "files_changed": [{"path": "a.p"}]})
    plain = store.create_task(conn, "code_change", "Fix", {}, assigned_to="Royal")["task_id"]
    store.complete_task(conn, plain, {"done": True, "summary": "s", "files_changed": [{"path": "a.p"}]})
    mail = store.create_task(conn, "draft_reply", "Reply", {"q": "x"}, assigned_to="MAG")["task_id"]
    store.complete_task(conn, mail, {"done": True, "to": ["a@b.com"], "subject": "RE", "body_path": "C:/b.md",
                                     "summary": "s"})
    assert json.loads(_reviews(conn, code)[0]["spec"])["dod_path"] == "dod/cvc-code.md"
    assert json.loads(_reviews(conn, code)[0]["spec"])["files"] == ["a.p"]
    assert json.loads(_reviews(conn, mail)[0]["spec"])["dod_path"] == "dod/client-email.md"
    assert not _reviews(conn, plain)
    assert store.minder_owed(conn, now() + 3600)["unreviewed"] == []


def test_the_round_cap_stops_the_third_review(conn):
    r1 = _deliverable(conn)
    _verdict(conn, _reviews(conn, r1)[0]["id"], "request_changes")
    r2 = _deliverable(conn, parent=r1)
    assert store.review_rounds(conn, r2) == 1
    _verdict(conn, _reviews(conn, r2)[0]["id"], "request_changes")
    r3 = _deliverable(conn, parent=r2)  # Ben said revise once more
    assert store.review_rounds(conn, r3) == 2
    assert not _reviews(conn, r3)


def test_an_approval_does_not_count_as_a_round(conn):
    r1 = _deliverable(conn)
    _verdict(conn, _reviews(conn, r1)[0]["id"], "approve")
    assert store.review_rounds(conn, r1) == 0


def test_a_failed_review_creation_rolls_back_the_completion(conn, monkeypatch):
    tid = store.create_task(conn, "deliverable", "x", {"format": "html"}, assigned_to="MAG")["task_id"]

    def boom(*a, **k):
        raise RuntimeError("no")

    monkeypatch.setattr(store, "create_review", boom)
    with pytest.raises(RuntimeError):
        store.complete_task(conn, tid, {"done": True, "summary": "s", "path": "C:/x/r.html",
                                        "deliverable_type": "t", "dod_path": "d", "self_check": SELF,
                                        "follow_ups": [{"who": "MAG", "what": "w"}]})
    r = conn.execute("SELECT state, result FROM tasks WHERE id=?", (tid,)).fetchone()
    assert (r["state"], r["result"]) == ("open", None)
    assert conn.execute("SELECT COUNT(*) FROM follow_ups").fetchone()[0] == 0
