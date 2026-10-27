"""The review gate: CVC code and client emails need an approving dod_review."""

from __future__ import annotations

import os

import pytest

from coord_mcp import gate, outlook, report, store
from coord_mcp.db import connect, now

CHECK = [{"item": "Ends with Thanks!", "met": True}]


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _start(conn):
    dec = store.record_decision(conn, "Ben approved cvc-code and client-email checklists", "his ruling")
    return gate.start(conn, dec["decision_id"])


def _draft(conn, tmp_path, name="reply.md"):
    body = tmp_path / name
    body.write_text("Hi Harald,\n\nDone.\n\nThanks!\n", encoding="utf-8")
    tid = store.create_task(conn, "draft_reply", "Reply to Harald", {"q": "x"}, assigned_to="MAG")["task_id"]
    store.complete_task(conn, tid, {"done": True, "to": ["h@client.com"], "subject": "RE: timeline",
                                    "body_path": str(body), "summary": "answers the timeline"})
    return tid, body


def _code(conn, tmp_path, cvc=True):
    src = tmp_path / "xxshp.p"
    src.write_text("/* v1 */", encoding="utf-8")
    spec = {"q": "x", "cvc": True} if cvc else {"q": "x"}
    tid = store.create_task(conn, "code_change", "Fix shipper", spec, assigned_to="Royal")["task_id"]
    store.complete_task(conn, tid, {"done": True, "summary": "fixed", "files_changed": [{"path": str(src)}]})
    return tid, src


def _review(conn, parent, verdict="approve", path="reply.md"):
    rid = store.create_task(conn, "dod_review", "Review", {"q": "x"}, assigned_to="MAG",
                            parent_id=parent)["task_id"]
    store.complete_task(conn, rid, {"done": True, "verdict": verdict, "summary": "checked", "path": path,
                                    "dod_path": "dod/client-email.md", "criteria": CHECK})
    return rid


def _age(path, secs=-60):
    t = now() + secs
    os.utime(path, (t, t))


def test_before_the_start_nothing_is_gated(conn, tmp_path):
    tid, _ = _draft(conn, tmp_path)
    assert gate.since(conn) is None
    assert outlook.draft(conn, tid)["task_id"] == tid
    code, _ = _code(conn, tmp_path)
    store.create_task(conn, "code_change", "Check in", {"checkin_of": code})


def test_start_needs_a_decision_and_keeps_its_first_time(conn):
    with pytest.raises(ValueError, match="no decision"):
        gate.start(conn, "nope")
    first = _start(conn)
    assert not first["already"]
    again = _start(conn)
    assert again["already"] and again["since"] == first["since"]


def test_tasks_from_before_the_start_keep_the_old_route(conn, tmp_path):
    old, _ = _draft(conn, tmp_path)
    conn.execute("UPDATE tasks SET created_at = created_at - 100 WHERE id=?", (old,))
    _start(conn)
    assert gate.stage(conn, old) is None
    assert outlook.draft(conn, old)["task_id"] == old


def test_draft_in_outlook_waits_for_an_approving_review(conn, tmp_path):
    _start(conn)
    tid, body = _draft(conn, tmp_path)
    _age(body)
    with pytest.raises(ValueError, match="no dod_review"):
        outlook.draft(conn, tid)
    with pytest.raises(ValueError, match="no dod_review"):
        store.queue_operator_prompt(conn, "draft", tid)
    _review(conn, tid, "request_changes")
    with pytest.raises(ValueError, match="requests changes"):
        outlook.draft(conn, tid)
    _review(conn, tid, "approve")
    assert outlook.draft(conn, tid)["task_id"] == tid
    assert store.queue_operator_prompt(conn, "draft", tid)["ok"]


def test_a_body_edited_after_approval_needs_review_again(conn, tmp_path):
    _start(conn)
    tid, body = _draft(conn, tmp_path)
    _age(body)
    _review(conn, tid)
    _age(body, +60)
    assert gate.stage(conn, tid)["stage"] == "stale"
    with pytest.raises(ValueError, match="changed after"):
        outlook.draft(conn, tid)


def test_drafts_page_holds_an_unreviewed_draft_without_the_button(conn, tmp_path):
    _start(conn)
    tid, body = _draft(conn, tmp_path)
    v = store.draft_replies(conn)
    d = next(x for x in v["drafts"] if x["task_id"] == tid)
    assert d["state"] == "review" and d["review"]["stage"] == "review"
    html = report.drafts_page(v)
    assert "Awaiting review" in html and "Draft in Outlook</button>" not in html
    _age(body)
    _review(conn, tid)
    d = next(x for x in store.draft_replies(conn)["drafts"] if x["task_id"] == tid)
    assert d["state"] == "waiting"


def test_checkin_is_refused_until_the_code_is_approved(conn, tmp_path):
    _start(conn)
    code, src = _code(conn, tmp_path)
    _age(src)
    with pytest.raises(ValueError, match="no dod_review"):
        store.create_task(conn, "code_change", "Check in", {"checkin_of": code})
    with pytest.raises(ValueError, match="belongs on a code_change"):
        store.create_task(conn, "investigation", "Check in", {"checkin_of": code})
    with pytest.raises(ValueError, match="no task"):
        store.create_task(conn, "code_change", "Check in", {"checkin_of": "missing"})
    _review(conn, code, path=str(src))
    assert store.create_task(conn, "code_change", "Check in", {"checkin_of": code})["task_id"]
    _age(src, +60)
    with pytest.raises(ValueError, match="changed after"):
        store.create_task(conn, "code_change", "Check in", {"checkin_of": code})


def test_code_not_bound_for_cvc_is_not_gated(conn, tmp_path):
    _start(conn)
    code, _ = _code(conn, tmp_path, cvc=False)
    assert gate.stage(conn, code) is None
    store.create_task(conn, "code_change", "Check in", {"checkin_of": code})


def test_minder_owes_a_review_for_each_gated_result(conn, tmp_path):
    _start(conn)
    tid, _ = _draft(conn, tmp_path)
    code, _ = _code(conn, tmp_path)
    plain, _ = _code(conn, tmp_path, cvc=False)
    later = now() + (store.MINDER_UNREAD + 1) * 60
    owed = store.minder_owed(conn, later)["unreviewed"]
    assert set(owed) == {tid, code}
    _review(conn, tid)
    assert store.minder_owed(conn, later)["unreviewed"] == [code]
    assert "no dod_review" in store.minder_text(store.minder_owed(conn, later), later)
