"""Workflow-enforcement piece 1: step time limits (sla) and the minder condition."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from coord_mcp import store
from coord_mcp.db import connect
from coord_mcp.workflows import cases as cs
from coord_mcp.workflows import definitions as defs
from coord_mcp.workflows import sla, view

UTC = timezone.utc
H = 3600
INV = {"done": True, "verdict": "v", "confidence": "high", "evidence": ["e"]}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("COORD_TZ", "UTC")
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def ts(y, m, d, h=0):
    return int(datetime(y, m, d, h, tzinfo=UTC).timestamp())


def test_every_shipped_step_has_a_sound_sla():
    for code, d in defs.load().items():
        for st in d["steps"]:
            assert "sla" in st, f"{code}.{st['key']} has no sla"
            assert defs.sla_errors(st["sla"]) == [], f"{code}.{st['key']}"
    # The asks the brief named: status draft, review and target date in a business day.
    a1 = {s["key"]: s["sla"] for s in defs.get("A1")["steps"]}
    assert sla.limit_seconds(a1["draft"]) <= 24 * H and sla.limit_seconds(a1["review"]) <= 24 * H
    a2 = defs.get("A2")["steps"]
    assert sum(sla.limit_seconds(s["sla"]) for s in a2) <= 24 * H
    assert all("d1c3d5216d4f" in s["sla"]["why"] for s in a2)


@pytest.mark.parametrize("bad,needle", [
    ("1bd", "object"),
    ({}, "exactly one"),
    ({"business_days": 1, "business_hours": 2}, "exactly one"),
    ({"business_days": 0}, "positive"),
    ({"business_days": True}, "positive"),
    ({"business_days": 1, "hours": 2}, "unknown"),
    ({"business_days": 1, "why": ""}, "why"),
])
def test_validate_refuses_bad_slas(bad, needle):
    d = json.loads(json.dumps(defs.get("A1")))
    d["steps"][1]["sla"] = bad
    assert any(needle in e for e in defs.validate(d)), defs.validate(d)


def test_sla_is_optional():
    d = json.loads(json.dumps(defs.get("A1")))
    del d["steps"][1]["sla"]
    assert defs.validate(d) == []


def test_business_time_skips_weekends():
    fri = ts(2026, 11, 13, 12)  # a Friday
    mon = ts(2026, 11, 16, 12)
    assert sla.business_seconds(fri, mon, UTC) == 24 * H
    assert sla.deadline(fri, 24 * H, UTC) == mon
    assert sla.deadline(fri, 4 * H, UTC) == ts(2026, 11, 13, 16)
    assert sla.business_seconds(ts(2026, 11, 14), ts(2026, 11, 15, 23), UTC) == 0


def _overdue_case(c, who_follow_up=None):
    root = store.create_task(c, "investigation", "LNK: why the AMEX report fails", {}, assigned_to="LNK")["task_id"]
    if who_follow_up:
        store.complete_task(c, root, {**INV, "follow_ups": [{"who": who_follow_up, "what": "approve the fix"}]})
    cs.sync(c)
    case = c.execute("SELECT * FROM workflow_cases").fetchone()
    return root, case


def test_overdue_lists_case_step_time_over_and_who(db):
    c = db
    root, case = _overdue_case(c)
    assert (case["workflow"], case["step"], case["waits_on"]) == ("A3", "investigate", "agent")
    start = case["step_at"]
    assert sla.overdue(c, start + H) == []
    later = sla.deadline(start, 2 * 24 * H) + 5 * H
    o = sla.overdue(c, later)
    assert [(x["case"], x["step"], x["waits_on"]) for x in o] == [(case["id"], "investigate", "agent")]
    assert o[0]["over"] == 5 * H
    assert sla.label(o[0]) == f'{case["id"]} A3/investigate +5h agent'


def test_minder_lists_master_and_ben_cases_apart_and_quiets_on_a_decision(db):
    c = db
    root, case = _overdue_case(c, who_follow_up="Ben")
    assert case["waits_on"] == "ben"
    # The minder starts counting from its first pass: the backlog stays on /workflows.
    sla.since(c, case["step_at"] - 1)
    later = case["step_at"] + 30 * 24 * H
    owed = store.minder_owed(c, later)
    assert owed["case_sla"] == [] and len(owed["case_sla_ben"]) == 1
    assert case["id"] in owed["case_sla_ben"][0]
    text = store.minder_text(owed, later)
    assert "cases past step limit for Ben" in text
    store.record_decision(c, "Ben has it on his desk", "waiting on the QAD call", task_id=root)
    c.execute("UPDATE decisions SET created_at=?", (later - 60,))
    assert store.minder_owed(c, later)["case_sla_ben"] == []


def test_minder_skips_cases_over_before_it_first_ran(db, monkeypatch):
    c = db
    _, case = _overdue_case(c)
    later = case["step_at"] + 30 * 24 * H
    assert sla.minder_owed(c, later)["master"] == []  # first pass sets the start mark to now
    assert sla.overdue(c, later)  # still shown on /workflows
    monkeypatch.setenv("COORD_WORKFLOW_SLA", "0")
    c.execute("UPDATE meta SET value='0' WHERE key=?", (sla.SINCE_KEY,))
    assert sla.minder_owed(c, later) == {"master": [], "ben": []}
    monkeypatch.delenv("COORD_WORKFLOW_SLA")
    assert len(sla.minder_owed(c, later)["master"]) == 1


def test_workflows_page_shows_over_limit(db):
    c = db
    _, case = _overdue_case(c)
    v = view.overview(c, t=case["step_at"] + 30 * 24 * H)
    assert v["totals"]["over"] == 1
    assert next(r for r in v["workflows"] if r["workflow"] == "A3")["over"] == 1
    html = view.render(v)
    assert "over limit by" in html and "past their step limit" in html
    assert "AMEX" in view.render(v, waits="over limit")
    assert "AMEX" not in view.render(view.overview(c, t=case["step_at"] + H), waits="over limit")
