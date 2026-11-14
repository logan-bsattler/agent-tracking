"""Workflow enforcement 3: case-step warnings on create and complete (advice, never refusal)."""

from __future__ import annotations

import pytest

from coord_mcp import queue, store
from coord_mcp.db import connect
from coord_mcp.workflows import cases as cs
from coord_mcp.workflows import checks

INV = {"done": True, "verdict": "v", "confidence": "high", "evidence": ["e"]}


@pytest.fixture()
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(queue, "SWITCHES", {**{k: False for k in queue.SWITCHES}, checks.SWITCH: True})
    conn = connect(tmp_path / "t.db")
    cs.ensure_workflows(conn)
    yield conn
    conn.close()


def _intent(c, text="LNK: label defect on RAID 695"):
    return store.propose_intent(c, text, "email", context="msg X", origin="team", trust="trusted")["intent_id"]


def _case_warnings(out):
    return [w for w in out.get("warnings", []) if "case" in w]


def test_same_intent_root_task_warns_no_case(c):
    i = _intent(c)
    store.create_task(c, "investigation", "LNK: investigate label defect RAID 695", {}, assigned_to="LNK",
                      from_intent=i)
    cs.sync(c)
    out = store.create_task(c, "code_change", "LNK: fix the label", {}, assigned_to="LNK", from_intent=i)
    w = _case_warnings(out)
    assert len(w) == 1 and w[0].startswith("no case") and "same intent" in w[0]
    assert f"attach {out['task_id']}" in w[0]


def test_similar_title_same_workflow_warns(c):
    store.create_task(c, "investigation", "LNK: shipping label defect on pack station", {}, assigned_to="LNK")
    cs.sync(c)
    out = store.create_task(c, "investigation", "LNK: shipping label defect pack station", {}, assigned_to="LNK")
    assert any("near-identical title" in w for w in _case_warnings(out))
    other = store.create_task(c, "investigation", "LNK: invoice totals wrong on credit memos", {}, assigned_to="LNK")
    assert _case_warnings(other) == []
    elsewhere = store.create_task(c, "investigation", "MAG: shipping label defect pack station", {},
                                  assigned_to="MAG")
    assert _case_warnings(elsewhere) == []


def test_recurring_task_with_a_new_date_is_not_a_step(c):
    store.create_task(c, "investigation", "LNK: board cleanup evidence check 10/29", {}, assigned_to="LNK")
    cs.sync(c)
    out = store.create_task(c, "investigation", "LNK: board cleanup evidence check 11/08", {}, assigned_to="LNK")
    assert _case_warnings(out) == []


def test_child_or_named_case_does_not_warn_no_case(c):
    i = _intent(c)
    root = store.create_task(c, "investigation", "LNK: investigate label defect RAID 695", {}, assigned_to="LNK",
                             from_intent=i)["task_id"]
    cs.sync(c)
    case = c.execute("SELECT id FROM workflow_cases").fetchone()["id"]
    named = store.create_task(c, "investigation", "LNK: more on the label", {"case_id": case}, assigned_to="LNK",
                              from_intent=i)
    assert _case_warnings(named) == []
    assert c.execute("SELECT case_id FROM tasks WHERE id=?", (named["task_id"],)).fetchone()[0] == case
    bad = store.create_task(c, "investigation", "LNK: x", {"case_id": "nope"}, assigned_to="LNK")
    assert any("not a case" in w for w in bad.get("warnings", []))
    store.complete_task(c, root, INV)
    child = store.create_task(c, "code_change", "LNK: fix label", {}, assigned_to="LNK", parent_id=root)
    assert not any(w.startswith("no case") for w in child.get("warnings", []))


def test_step_that_skips_an_agent_step_warns(c):
    i = _intent(c)
    root = store.create_task(c, "investigation", "LNK: investigate label defect RAID 695", {}, assigned_to="LNK",
                             from_intent=i)["task_id"]
    cs.sync(c)
    store.complete_task(c, root, INV)
    cs.sync(c)
    case = c.execute("SELECT * FROM workflow_cases").fetchone()
    assert case["workflow"] == "A3"
    reply = store.create_task(c, "draft_reply", "LNK: reply on the label", {"reply_date": "2026-11-13"},
                              assigned_to="LNK", parent_id=root)
    w = _case_warnings(reply)
    assert len(w) == 1 and "step reply" in w[0] and "(fix)" in w[0] and "(checkin)" in w[0]
    assert "(review)" not in w[0]  # the review gate is off on this board
    fix = store.create_task(c, "code_change", "LNK: fix label", {}, assigned_to="LNK", parent_id=root)
    assert _case_warnings(fix) == []  # the next step: nothing skipped


def test_complete_warns_on_a_skipped_step(c):
    i = _intent(c)
    root = store.create_task(c, "investigation", "LNK: investigate label defect RAID 695", {}, assigned_to="LNK",
                             from_intent=i)["task_id"]
    store.complete_task(c, root, INV)
    cs.sync(c)
    reply = store.create_task(c, "draft_reply", "LNK: reply", {"reply_date": "2026-11-13"}, assigned_to="LNK",
                              parent_id=root)["task_id"]
    cs.sync(c)
    out = store.complete_task(c, reply, {"done": True, "to": ["a@b.example"], "subject": "s",
                                         "body_path": "x.txt", "summary": "s"})
    assert any("carried step reply" in w for w in out.get("warnings", []))


def test_switch_off_and_faults_never_block(c, monkeypatch):
    i = _intent(c)
    store.create_task(c, "investigation", "LNK: investigate label defect RAID 695", {}, assigned_to="LNK",
                      from_intent=i)
    cs.sync(c)
    monkeypatch.setattr(queue, "SWITCHES", {**queue.SWITCHES, checks.SWITCH: False})
    out = store.create_task(c, "code_change", "LNK: fix the label", {}, assigned_to="LNK", from_intent=i)
    assert _case_warnings(out) == []
    monkeypatch.setattr(queue, "SWITCHES", {**queue.SWITCHES, checks.SWITCH: True})
    monkeypatch.setattr(checks, "matching_cases", lambda *a, **k: 1 / 0)
    out = store.create_task(c, "code_change", "LNK: fix the label again", {}, assigned_to="LNK", from_intent=i)
    assert out["task_id"] and any("case check skipped" in w for w in out["warnings"])


def test_attach_moves_a_task_and_drops_an_empty_case(c):
    i = _intent(c)
    store.create_task(c, "investigation", "LNK: investigate label defect RAID 695", {}, assigned_to="LNK",
                      from_intent=i)
    stray = store.create_task(c, "code_change", "LNK: fix the label", {}, assigned_to="LNK")["task_id"]
    cs.sync(c)
    target = c.execute("SELECT case_id FROM tasks WHERE id<>? LIMIT 1", (stray,)).fetchone()[0]
    old = c.execute("SELECT case_id FROM tasks WHERE id=?", (stray,)).fetchone()[0]
    out = checks.attach(c, stray, target)
    assert out["was"] == old and c.execute("SELECT case_id FROM tasks WHERE id=?", (stray,)).fetchone()[0] == target
    assert c.execute("SELECT state FROM workflow_cases WHERE id=?", (old,)).fetchone()[0] == "dropped"
    with pytest.raises(KeyError):
        checks.attach(c, stray, "nope")
