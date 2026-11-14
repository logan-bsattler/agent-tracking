"""Step 2 of the workflow build: definitions and cases."""

from __future__ import annotations

import json

import pytest

from coord_mcp import store
from coord_mcp.db import connect
from coord_mcp.workflows import CATALOG
from coord_mcp.workflows import __main__ as cli
from coord_mcp.workflows import cases as cs
from coord_mcp.workflows import definitions as defs


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "t.db"
    c = connect(p)
    yield p, c
    c.close()


INV = {"done": True, "verdict": "v", "confidence": "high", "evidence": ["e"]}


def test_every_definition_validates_and_covers_the_catalog():
    d = defs.load()
    assert set(d) == set(CATALOG)
    for code, x in d.items():
        assert defs.validate(x) == [], code
    assert {c for c, x in d.items() if x.get("engine")} >= {"A1", "A3", "B1"}


@pytest.mark.parametrize("bad,needle", [
    ({"who": "ben", "spawn": {"on": "result", "kind": "draft_reply", "title": "t", "spec": {}}}, "only an agent"),
    ({"kinds": ["code_change"], "spawn": {"on": "result", "kind": "code_change", "title": "t", "spec": {}}}, "cvc"),
    ({"kinds": ["dod_review"], "spawn": {"on": "result", "kind": "dod_review", "title": "t", "spec": {}}}, "spawn.kind"),
    ({"kinds": ["code_change"], "match": {"spec_has": "checkin_of"},
      "spawn": {"on": "result", "kind": "code_change", "cvc": True, "title": "t", "spec": {}}}, "check-in"),
])
def test_validate_refuses_unsafe_spawns(bad, needle):
    d = json.loads(json.dumps(defs.get("A1")))
    d["steps"][2] = {**d["steps"][2], **bad}
    if "kinds" not in bad:
        d["steps"][2]["kinds"] = d["steps"][2]["kinds"]
    assert any(needle in e for e in defs.validate(d)), defs.validate(d)


def test_step_for_follows_the_parent_and_isolates_checkins():
    b1 = defs.get("B1")
    assert defs.step_for(b1, "investigation", {}) == "sizing"
    assert defs.step_for(b1, "code_change", {}, after=defs.step_index(b1, "handoff")) == "build"
    assert defs.step_for(b1, "code_change", {"checkin_of": "abc"}) == "checkin"
    assert defs.step_for(b1, "code_change", {}, after=defs.step_index(b1, "build")) == "build"
    assert defs.step_for(b1, "dod_review", {}) == "review"
    assert defs.step_for(b1, "draft_reply", {}) is None
    assert defs.step_for(None, "investigation", {}) is None


def test_migration_adds_the_columns_and_tables_once(db):
    _, c = db
    cs.ensure_workflows(c)
    cs.ensure_workflows(c)
    cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
    assert {"case_id", "case_step"} <= cols
    assert c.execute("SELECT COUNT(*) FROM workflow_cases").fetchone()[0] == 0


def test_sync_walks_a_case_through_agent_ben_master_and_done(db):
    _, c = db
    root = store.create_task(c, "investigation", "LNK: why the AMEX report fails", {}, assigned_to="LNK")["task_id"]
    cs.sync(c)
    case = c.execute("SELECT * FROM workflow_cases").fetchone()
    assert (case["workflow"], case["state"], case["step"], case["waits_on"]) == ("A3", "open", "investigate", "agent")

    store.complete_task(c, root, {**INV, "follow_ups": [{"who": "Ben", "what": "approve the fix"}]})
    cs.sync(c)
    case = c.execute("SELECT * FROM workflow_cases").fetchone()
    assert (case["state"], case["waits_on"]) == ("open", "ben")

    fu = c.execute("SELECT id FROM follow_ups WHERE task_id=?", (root,)).fetchone()["id"]
    c.execute("UPDATE follow_ups SET state='dropped' WHERE id=?", (fu,))
    cs.sync(c)
    case = c.execute("SELECT * FROM workflow_cases").fetchone()
    assert case["state"] == "done" and case["closed_at"]

    kid = store.create_task(c, "code_change", "LNK: fix it", {}, assigned_to="LNK", parent_id=root)["task_id"]
    cs.sync(c)
    case = c.execute("SELECT * FROM workflow_cases").fetchone()
    assert (case["state"], case["waits_on"]) == ("open", "agent")
    assert c.execute("SELECT case_id, case_step FROM tasks WHERE id=?", (kid,)).fetchone()["case_id"] == case["id"]
    evs = [r["event"] for r in c.execute("SELECT event FROM workflow_case_events ORDER BY ts, rowid")]
    assert evs[0] == "opened" and "closed" in evs and "reopened" in evs


def test_client_loose_end_waits_on_master(db):
    _, c = db
    root = store.create_task(c, "investigation", "LNK: why the AMEX report fails", {}, assigned_to="LNK")["task_id"]
    store.complete_task(c, root, {**INV, "follow_ups": [{"who": "LNK", "what": "fix the report"}]})
    cs.sync(c)
    case = c.execute("SELECT * FROM workflow_cases").fetchone()
    assert (case["state"], case["waits_on"]) == ("open", "master")


def test_backfill_is_idempotent(db, capsys):
    path, c = db
    root = store.create_task(c, "investigation", "MAG: reconcile the keeper list", {}, assigned_to="MAG")["task_id"]
    store.create_task(c, "code_change", "MAG: regen", {}, assigned_to="MAG", parent_id=root)
    first = cs.backfill(c)
    n_events = c.execute("SELECT COUNT(*) FROM workflow_case_events").fetchone()[0]
    second = cs.backfill(c)
    assert first["cases"] == second["cases"] == 1
    assert c.execute("SELECT COUNT(*) FROM workflow_case_events").fetchone()[0] == n_events
    assert cli.main(["cases", "--db", str(path), "--open"]) == 0
    assert "1 case(s)" in capsys.readouterr().out
    cid = c.execute("SELECT id FROM workflow_cases").fetchone()["id"]
    assert cli.main(["case", cid, "--db", str(path)]) == 0
    assert len(json.loads(capsys.readouterr().out)["tasks"]) == 2


def test_sync_switch(monkeypatch):
    monkeypatch.setenv("COORD_WORKFLOW_SYNC", "0")
    assert not cs.enabled()
    monkeypatch.delenv("COORD_WORKFLOW_SYNC")
    assert cs.enabled()


def test_workflows_page_shows_open_cases_and_per_workflow_rows(db):
    from coord_mcp import report
    from coord_mcp.workflows import view
    _, c = db
    root = store.create_task(c, "investigation", "LNK: why the AMEX report fails", {}, assigned_to="LNK")["task_id"]
    store.complete_task(c, root, {**INV, "follow_ups": [{"who": "Ben", "what": "approve the fix"}]})
    done = store.create_task(c, "investigation", "MAG: reconcile the keeper list", {}, assigned_to="MAG")["task_id"]
    store.complete_task(c, done, INV)
    cs.sync(c)
    v = view.overview(c)
    assert v["totals"] == {"cases": 2, "open": 1, "waits": {"ben": 1, "master": 0, "agent": 0}}
    a3 = next(r for r in v["workflows"] if r["workflow"] == "A3")
    assert (a3["open"], a3["ben_touches"], a3["engine"]) == (1, 1.0, True)
    c3 = next(r for r in v["workflows"] if r["workflow"] == "C3")
    assert c3["median_cycle_h"] is not None
    html = view.render(v)
    assert "AMEX" in html and "waits on <b>ben</b>" in html and "keeper" not in html
    assert '<a href="/workflows">Workflows</a>' in report.board_page(store.operator_view(c))
    assert "AMEX" not in view.render(v, waits="master")
