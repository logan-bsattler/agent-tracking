"""coord_file_draft: a draft Ben asked a client session for in chat joins the
same road as master's draft_reply tasks (/drafts, the review gate, the
minder's "finished drafts not yet in Outlook", coord_mcp.outlook)."""

from __future__ import annotations

import json

import pytest

from coord_mcp import gate, outlook, queue, store
from coord_mcp.db import connect, now


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _body(tmp_path, name="reply-Corwin.html"):
    d = tmp_path / "Moog" / "work"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text("Hi Corwin,\n\nThe fix is in DEVL.\n\nThanks!\n", encoding="utf-8")
    return f


def _result(f, **extra):
    r = {"done": True, "to": ["corwin@moog.com"], "subject": "RE: QSM-1050438", "body_path": str(f),
         "summary": "Tell Corwin the fix is in DEVL.", "reply_from": "corwin@moog.com",
         "reply_date": "2026-11-13 09:12",
         "follow_ups": [{"who": "Ben", "what": "Review and draft to Corwin."}]}
    r.update(extra)
    return r


def _file(conn, tmp_path, f=None, **kw):
    f = f or _body(tmp_path)
    return store.file_draft(conn, "Moog", _result(f), asked="Reply to Corwin on the QSM", root=tmp_path, **kw)


def test_files_a_finished_draft_reply_in_one_step(conn, tmp_path):
    out = _file(conn, tmp_path)
    assert out["ok"] and out["state"] == "done" and out["from"] == store.BEN_DIRECT
    assert "Do not paste it into Outlook" in out["next"]
    t = conn.execute("SELECT * FROM tasks WHERE id=?", (out["task_id"],)).fetchone()
    assert t["kind"] == "draft_reply" and t["assigned_to"] == "Moog" and t["picked_up_at"]
    assert t["title"] == "RE: QSM-1050438"
    spec = json.loads(t["spec"])
    assert spec == {"from": "ben-direct", "asked": "Reply to Corwin on the QSM", "reply_date": "2026-11-13 09:12"}
    # Ben's "review and draft" loose end lands like any draft's.
    fus = conn.execute("SELECT who, what FROM follow_ups WHERE task_id=?", (out["task_id"],)).fetchall()
    assert [(f["who"], f["what"]) for f in fus] == [("Ben", "Review and draft to Corwin.")]


def test_never_sits_open_for_the_minder(conn, tmp_path):
    out = _file(conn, tmp_path)
    owed = store.minder_owed(conn, now() + 3600)
    assert out["task_id"] not in owed["undelivered"] and out["task_id"] not in owed["stalled"]
    assert out["task_id"] in owed["unread"]  # master reads it like any result


def test_shows_on_drafts_and_goes_to_outlook(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DOCS_ROOT", tmp_path)
    out = _file(conn, tmp_path)
    rows = store.draft_replies(conn)["drafts"]
    d = next(r for r in rows if r["task_id"] == out["task_id"])
    assert d["state"] == "waiting" and d["from"] == "ben-direct" and d["exists"] and d["reply_all"]
    assert out["task_id"] in queue.undrafted(conn, now() + 15 * 60, 10 * 60)
    o = outlook.draft(conn, out["task_id"])
    assert o["body"].startswith("Hi Corwin,") and o["search"]


def test_review_gate_holds_it_like_any_draft(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DOCS_ROOT", tmp_path)
    dec = store.record_decision(conn, "Ben approved both checklists", None)["decision_id"]
    gate.start(conn, dec)
    conn.execute("UPDATE meta SET value=? WHERE key=?", (str(now() - 5), gate.SINCE_KEY))
    out = _file(conn, tmp_path)
    assert "once its dod_review approves it" in out["next"]
    rev = conn.execute("SELECT * FROM tasks WHERE parent_id=? AND kind='dod_review'", (out["task_id"],)).fetchone()
    assert rev is not None and rev["assigned_to"] == "Moog"
    assert json.loads(rev["spec"])["deliverable_type"] == "client-email"
    with pytest.raises(ValueError, match="dod_review"):
        outlook.draft(conn, out["task_id"])
    assert out["task_id"] not in queue.undrafted(conn, now() + 15 * 60, 10 * 60)


def test_refuses_a_session_that_is_not_a_client(conn, tmp_path):
    f = _body(tmp_path)
    for who in (None, "agents", "desk"):
        with pytest.raises(ValueError, match="client sessions"):
            store.file_draft(conn, who, _result(f), root=tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_refuses_unfinished_off_contract_or_missing_body(conn, tmp_path):
    f = _body(tmp_path)
    with pytest.raises(ValueError, match="finished draft"):
        store.file_draft(conn, "Moog", {"done": False, "reason": "x", "retryable": True}, root=tmp_path)
    with pytest.raises(ValueError, match="subject"):
        store.file_draft(conn, "Moog", {k: v for k, v in _result(f).items() if k != "subject"}, root=tmp_path)
    with pytest.raises(ValueError, match="not on disk"):
        store.file_draft(conn, "Moog", _result(tmp_path / "nope.html"), root=tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_relative_body_path_resolves_in_the_client_folder(conn, tmp_path):
    _body(tmp_path)
    out = store.file_draft(conn, "Moog", _result("work/reply-Corwin.html"), root=tmp_path)
    assert out["state"] == "done"


def test_same_body_twice_needs_parent_id(conn, tmp_path):
    f = _body(tmp_path)
    first = _file(conn, tmp_path, f)
    with pytest.raises(ValueError, match=f"parent_id={first['task_id']}"):
        _file(conn, tmp_path, f)
    again = _file(conn, tmp_path, f, parent_id=first["task_id"])
    assert conn.execute("SELECT parent_id FROM tasks WHERE id=?", (again["task_id"],)).fetchone()[0] == first["task_id"]


def test_parent_must_be_this_clients_draft(conn, tmp_path):
    other = store.create_task(conn, "draft_reply", "LNK reply", {}, assigned_to="LNK")["task_id"]
    with pytest.raises(ValueError, match="draft_reply tasks"):
        _file(conn, tmp_path, parent_id=other)
