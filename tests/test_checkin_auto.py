"""Check-in auto-approval (checkin_auto.py): the checkin_ok step of A3/B1."""

from __future__ import annotations

import os

import pytest

from coord_mcp import checkin_auto, gate, queue, report, store
from coord_mcp.db import connect, now

CHECK = [{"item": "Compiles", "met": True}]
DAY = 86400


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    queue.set_switch(c, "auto_checkin", True)  # conftest turns every switch off; this one is on by default
    yield c
    c.close()


def _start(conn):
    dec = store.record_decision(conn, "Ben approved cvc-code and client-email checklists", "his ruling")
    gate.start(conn, dec["decision_id"])


def _age(path, secs=-60):
    t = now() + secs
    os.utime(path, (t, t))


def _code(conn, tmp_path, compile_="clean", tests=None):
    src = tmp_path / "xxshp.p"
    src.write_text("/* v1 */", encoding="utf-8")
    tid = store.create_task(conn, "code_change", "Fix shipper", {"q": "x", "cvc": True}, assigned_to="Royal")["task_id"]
    res = {"done": True, "summary": "fixed", "files_changed": [{"path": str(src)}]}
    if compile_ is not None:
        res["compile"] = compile_
    if tests is not None:
        res["tests"] = tests
    store.complete_task(conn, tid, res)
    _age(src)
    return tid, src


def _review(conn, parent, verdict="approve", ben_item=True):
    rid = store.create_task(conn, "dod_review", "Review", {"q": "x"}, assigned_to="Royal",
                            parent_id=parent)["task_id"]
    res = {"done": True, "verdict": verdict, "summary": "checked", "path": "xxshp.p",
           "dod_path": "dod/cvc-code.md", "criteria": CHECK}
    if ben_item:
        res["follow_ups"] = [{"who": "Ben", "what": "Approve the check-in of the shipper fix", "reason": "approve"}]
    store.complete_task(conn, rid, res)
    return rid


def _ben_open(conn, tid):
    return [r["id"] for r in conn.execute(
        """SELECT f.id FROM follow_ups f JOIN tasks t ON t.id=f.task_id WHERE f.state='open'
           AND lower(f.who)='ben' AND (t.id=? OR t.parent_id=?)""", (tid, tid))]


def _checkin(conn, code):
    return store.create_task(conn, "code_change", "Check in", {"checkin_of": code}, assigned_to="Royal")


def _ready(conn, tmp_path, **kw):
    _start(conn)
    code, src = _code(conn, tmp_path, **kw)
    rid = _review(conn, code)
    return code, src, rid


def test_inactive_before_the_gate_starts(conn, tmp_path):
    code, _ = _code(conn, tmp_path)
    _review(conn, code)
    assert not checkin_auto.active(conn)
    assert checkin_auto.run(conn, t=now() + 30 * DAY) == []
    assert checkin_auto.row(conn, code) is None
    assert _ben_open(conn, code)  # Ben's item untouched
    _checkin(conn, code)  # the old route: nothing gated, nothing held


def test_all_three_met_is_approved_then_released_after_the_window(conn, tmp_path):
    code, _, rid = _ready(conn, tmp_path)
    assert checkin_auto.active(conn)
    t = now()
    out = checkin_auto.run(conn, t=t)
    assert [o["action"] for o in out] == ["approved"]
    a = checkin_auto.row(conn, code)
    assert a["state"] == "window" and a["review_id"] == rid and a["veto_by"] > t
    dec = conn.execute("SELECT statement, because FROM decisions WHERE id=?", (a["decision_id"],)).fetchone()
    assert dec["statement"].startswith(f"Check-in auto-approved for {code} (review {rid}); veto by ")
    assert dec["because"].endswith("auto rule; Ben may override")
    assert _ben_open(conn, code) == []  # Ben's check-in approval item closed by it
    with pytest.raises(ValueError, match="veto window"):
        _checkin(conn, code)
    assert checkin_auto.run(conn, t=t + 60) == []  # still inside the window
    out = checkin_auto.run(conn, t=a["veto_by"] + 1)
    assert [o["action"] for o in out] == ["released"]
    fu = conn.execute("SELECT who, what FROM follow_ups WHERE id=?", (out[0]["follow_up"],)).fetchone()
    assert fu["who"] == "Royal" and f"checkin_of {code}" in fu["what"]
    assert _checkin(conn, code)["task_id"]
    assert checkin_auto.run(conn, t=a["veto_by"] + 2) == []  # once only


@pytest.mark.ben_checks
def test_switch_is_on_by_default(tmp_path):
    c = connect(tmp_path / "d.db")
    assert queue.switch(c, "auto_checkin") is True
    c.close()


def test_window_is_four_business_hours_by_default_and_a_setting(conn, tmp_path):
    assert queue.setting(conn, "auto_checkin_hours") == 4.0
    queue.set_setting(conn, "auto_checkin_hours", 1)
    code, _, _ = _ready(conn, tmp_path)
    from coord_mcp import defer
    from coord_mcp.workflows import sla
    t = now()
    checkin_auto.run(conn, t=t)
    assert checkin_auto.row(conn, code)["veto_by"] == sla.deadline(t, 3600, off=defer.holidays(conn))


@pytest.mark.parametrize("why", ["files_changed", "review_not_approving", "compile_missing", "compile_errors",
                                 "tests_failed"])
def test_any_condition_unmet_leaves_bens_item(conn, tmp_path, why):
    _start(conn)
    kw = {"compile_missing": {"compile_": None}, "compile_errors": {"compile_": "errors"},
          "tests_failed": {"tests": {"ran": 3, "passed": 2, "failed": 1}}}.get(why, {})
    code, src = _code(conn, tmp_path, **kw)
    _review(conn, code, verdict="request_changes" if why == "review_not_approving" else "approve")
    if why == "files_changed":
        _age(src, +60)
    assert checkin_auto.run(conn, t=now()) == []
    assert checkin_auto.row(conn, code) is None
    assert _ben_open(conn, code)


def test_files_changed_inside_the_window_lapses_back_to_ben(conn, tmp_path):
    code, src, _ = _ready(conn, tmp_path)
    t = now()
    checkin_auto.run(conn, t=t)
    _age(src, +60)
    a = checkin_auto.row(conn, code)
    out = checkin_auto.run(conn, t=a["veto_by"] + 1)
    assert [o["action"] for o in out] == ["lapsed"]
    assert _ben_open(conn, code)
    assert checkin_auto.row(conn, code)["state"] == "lapsed"


def test_veto_button_inside_the_window_cancels(conn, tmp_path):
    code, _, _ = _ready(conn, tmp_path)
    t = now()
    checkin_auto.run(conn, t=t)
    assert "/api/checkin-veto" in report.task_page(store.task_detail(conn, code), code)
    r = checkin_auto.veto(conn, code)
    assert r["action"] == "vetoed"
    assert _ben_open(conn, code)  # his item is back
    assert checkin_auto.run(conn, t=t + 30 * DAY) == []  # never released, never re-approved
    assert "/api/checkin-veto" not in report.task_page(store.task_detail(conn, code), code)
    with pytest.raises(ValueError, match="vetoed"):
        _checkin(conn, code)
    for fid in _ben_open(conn, code):  # Ben approves it himself
        d = store.record_decision(conn, "Ben approved the check-in", "his call", code)
        store.resolve_follow_up(conn, fid, decision_id=d["decision_id"])
    assert _checkin(conn, code)["task_id"]


def test_veto_decision_inside_the_window_cancels(conn, tmp_path):
    code, _, _ = _ready(conn, tmp_path)
    t = now()
    checkin_auto.run(conn, t=t)
    store.record_decision(conn, "Veto: hold this check-in until the client signs off", "Ben", code)
    out = checkin_auto.run(conn, t=t + 30 * DAY)
    assert [o["action"] for o in out] == ["vetoed"]
    assert _ben_open(conn, code)


def test_veto_without_a_ben_item_opens_one(conn, tmp_path):
    _start(conn)
    code, _ = _code(conn, tmp_path)
    _review(conn, code, ben_item=False)
    checkin_auto.run(conn, t=now())
    assert checkin_auto.row(conn, code)["closed"] == "[]"
    checkin_auto.veto(conn, code)
    assert len(_ben_open(conn, code)) == 1


def test_switch_off_restores_the_old_behaviour(conn, tmp_path):
    code, _, _ = _ready(conn, tmp_path)
    queue.set_switch(conn, "auto_checkin", False)
    assert not checkin_auto.active(conn)
    assert checkin_auto.run(conn, t=now()) == []
    assert checkin_auto.row(conn, code) is None
    assert _ben_open(conn, code)
    assert _checkin(conn, code)["task_id"]  # check-in gated only by the review, as before


def test_switch_off_withdraws_an_open_window(conn, tmp_path):
    code, _, _ = _ready(conn, tmp_path)
    t = now()
    checkin_auto.run(conn, t=t)
    queue.set_switch(conn, "auto_checkin", False)
    out = checkin_auto.run(conn, t=t + 60)
    assert [o["action"] for o in out] == ["withdrawn"]
    assert _ben_open(conn, code)


def test_a_check_in_is_never_a_candidate(conn, tmp_path):
    code, _, _ = _ready(conn, tmp_path)
    a = checkin_auto.run(conn, t=now())[0]
    checkin_auto.run(conn, t=a["veto_by"] + 1)
    ci = _checkin(conn, code)["task_id"]
    store.complete_task(conn, ci, {"done": True, "summary": "checked in", "files_changed": [], "compile": "clean"})
    assert checkin_auto.candidates(conn) == []


def test_definitions_put_checkin_ok_on_code_with_ben_fallback():
    from coord_mcp.workflows import definitions as defs
    for code in ("A3", "B1"):
        d = defs.get(code)
        st = next(s for s in d["steps"] if s["key"] == "checkin_ok")
        assert st["who"] == "code" and st["fallback"] == "ben"
        assert defs.validate(d) == []
    bad = {**defs.get("A3")}
    bad["steps"] = [dict(s, fallback="ben") if s["key"] == "fix" else s for s in bad["steps"]]
    assert any("fallback" in e for e in defs.validate(bad))
