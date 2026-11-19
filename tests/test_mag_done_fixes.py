"""MAG noise audit 2026-11-17, section 3: six fixes that get more work to Done."""

from __future__ import annotations

import pytest

from coord_mcp import direct, queue, store
from coord_mcp.db import connect


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    queue.set_switch(c, "require_pipeline", False)
    queue.set_switch(c, "auto_promote", False)
    yield c
    c.close()


DSPEC = {"from": "ben-direct", "deliverable_type": "status-report", "dod_path": "dod/status-report.md",
         "format": "html"}
CRIT = [{"item": "Has a summary", "met": True}]


def _deliv(conn, parent=None, spec=None, title="Status report"):
    return store.create_task(conn, "deliverable", title, spec or dict(DSPEC), assigned_to="Moog",
                             parent_id=parent)["task_id"]


def _done_deliv(conn, tid, self_check=None):
    return store.complete_task(conn, tid, {"done": True, "summary": "s", "path": r"C:\w\r.html",
                                           "deliverable_type": "status-report", "dod_path": "dod/x.md",
                                           "self_check": self_check or CRIT})


def _review_of(conn, tid):
    return conn.execute("SELECT id FROM tasks WHERE parent_id=? AND kind='dod_review' AND state='open'",
                        (tid,)).fetchone()["id"]


def _review(conn, rid, verdict, fus):
    res = {"done": True, "verdict": verdict, "summary": "r", "path": r"C:\w\r.html", "dod_path": "dod/x.md",
           "criteria": CRIT, "follow_ups": [{"who": "Moog", "what": w} for w in fus]}
    if verdict == "approve":
        res["findings"] = [{"criterion": "Has a summary", "severity": "minor", "note": "wording"}]
    return store.complete_task(conn, rid, res)


def test_fix1_approve_with_notes_tasks_nothing_and_notes_reach_next_version(conn):
    d = _deliv(conn)
    _done_deliv(conn, d)
    out = _review(conn, _review_of(conn, d), "approve", ["Tighten the intro", "Rename the tab"])
    assert "hand_back_tasks" not in out
    assert conn.execute("SELECT COUNT(*) FROM follow_ups WHERE state='open'").fetchone()[0] == 0
    assert direct.version_notes(conn, d) == ["Tighten the intro; Rename the tab"]
    assert "non-blocker" in store.REVIEW_BRIEF


def test_fix2_unmet_self_check_warns_never_refuses(conn):
    d = _deliv(conn)
    out = _done_deliv(conn, d, [{"item": "Has a summary", "met": False}, {"item": "HTML", "met": True}])
    assert out["state"] == "done"
    assert any("self_check has 1 unmet" in w for w in out["warnings"])
    assert store.self_check_warnings("deliverable", {"done": True, "self_check": CRIT}) == []


def test_fix3_one_open_revision_per_deliverable(conn):
    d = _deliv(conn, spec={k: v for k, v in DSPEC.items() if k != "from"})
    _deliv(conn, parent=d, spec={k: v for k, v in DSPEC.items() if k != "from"})
    with pytest.raises(ValueError, match="already has an open revision"):
        _deliv(conn, parent=d, spec={k: v for k, v in DSPEC.items() if k != "from"})
    with pytest.raises(ValueError, match="already has an open revision"):
        _deliv(conn, spec={**{k: v for k, v in DSPEC.items() if k != "from"}, "revises": d})


def test_fix3_follow_ups_grouped_into_one_non_deliverable_child(conn):
    d = _deliv(conn)
    out = store.complete_task(conn, d, {"done": True, "summary": "s", "path": r"C:\w\r.html",
                                        "deliverable_type": "status-report", "dod_path": "dod/x.md",
                                        "self_check": CRIT, "follow_ups": [
                                            {"who": "Moog", "what": "Check the totals tab"},
                                            {"who": "Moog", "what": "Pull last week's numbers"}]})
    tasks = out["hand_back_tasks"]
    assert len(tasks) == 1 and tasks[0]["kind"] == "investigation" and len(tasks[0]["follow_ups"]) == 2


def test_fix3_request_changes_folds_into_open_revision(conn, monkeypatch):
    monkeypatch.setattr(store, "REVIEW_ROUNDS", 3)  # below the cap, so the fold path runs
    d = _deliv(conn)
    _done_deliv(conn, d)
    rid = _review_of(conn, d)
    first = _review(conn, rid, "request_changes", ["Fix the dates"])["hand_back_tasks"][0]
    assert first["kind"] == "deliverable"
    # A second review of the same version (rare) must not open a second revision.
    r2 = store.create_task(conn, "dod_review", "DoD review again", {"review_of": d}, assigned_to="Moog",
                           parent_id=d)["task_id"]
    again = _review(conn, r2, "request_changes", ["Fix the totals"])
    folded = again["hand_back_tasks"][0]
    assert folded["task_id"] == first["task_id"] and folded.get("folded")
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE kind='deliverable' AND state='open'").fetchone()[0] == 1


def test_fix4_conditional_items_and_status_notes_are_deferred(conn):
    root = store.create_task(conn, "investigation", "Check", {"from": "ben-direct"},
                             assigned_to="Moog")["task_id"]
    out = store.complete_task(conn, root, {"done": True, "verdict": "v", "confidence": "high", "evidence": ["e"],
                                           "follow_ups": [
                                               {"who": "Moog", "what": "When Jane replies, update the map"},
                                               {"who": "Moog", "what": "Sister task abc is now done"},
                                               {"who": "Moog", "what": "Next revision: add the legend"}]})
    assert "hand_back_tasks" not in out
    states = {r[0] for r in conn.execute("SELECT state FROM follow_ups")}
    assert states == {"deferred"}
    assert direct.chain_state(conn, root)["ready"]
    assert out["ready_for_ben"] == [root]


def test_fix5_round_cap_leaves_ben_one_item(conn):
    d = _deliv(conn)
    _done_deliv(conn, d)
    rev1 = _review(conn, _review_of(conn, d), "request_changes", ["Fix the dates"])["hand_back_tasks"][0]
    _done_deliv(conn, rev1["task_id"])
    rid2 = _review_of(conn, rev1["task_id"])
    out = _review(conn, rid2, "request_changes", ["Fix the totals", "Fix the title"])
    assert out["hand_back_tasks"][0]["kind"] == "round_cap"
    ben = conn.execute("SELECT what FROM follow_ups WHERE who='Ben' AND state='open'").fetchall()
    assert len(ben) == 1 and ben[0]["what"].startswith("Accept Status report as is, or revise once more")
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE kind='deliverable' AND state='open'").fetchone()[0] == 0


DRAFT = {"to": ["jane@mag.com"], "subject": "RE: Pricing load", "summary": "s", "body_path": "b.txt"}


def _draft_task(conn, parent=None, spec=None):
    return store.create_task(conn, "draft_reply", "Reply", spec or {"subject": "Pricing load",
                                                                    "to": ["Jane <jane@mag.com>"]},
                             assigned_to="MAG", parent_id=parent)["task_id"]


def test_fix6_same_email_refused_unless_revision(conn):
    a = _draft_task(conn)
    with pytest.raises(ValueError, match="One email, one live draft"):
        _draft_task(conn, spec={"subject": "Re: pricing load", "to": ["jane@mag.com"]})
    b = _draft_task(conn, parent=a)  # a revision is fine
    assert b
    _draft_task(conn, spec={"subject": "Pricing load", "to": ["bob@mag.com"]})  # other recipients
    _draft_task(conn, spec={"subject": "Pricing load", "to": ["jane@mag.com"], "duplicate_ok": True})


def test_fix6_revision_sets_aside_drafted_parent(conn):
    a = _draft_task(conn)
    store.complete_task(conn, a, {"done": True, **DRAFT})
    store.record_decision(conn, f"{a} drafted to Outlook Drafts", task_id=a)
    assert store.outlook_marks(conn)[a]["mark"] == "drafted"
    b = _draft_task(conn, parent=a)
    out = store.complete_task(conn, b, {"done": True, **DRAFT})
    assert out.get("set_aside")
    assert store.outlook_marks(conn)[a]["mark"] == store.SET_ASIDE
    st = conn.execute("SELECT statement FROM decisions WHERE id=?", (out["set_aside"],)).fetchone()[0]
    assert "delete it from Outlook Drafts" in st
