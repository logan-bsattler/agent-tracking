"""Step 4 of the workflow build: the engine advances a case when a result lands."""

from __future__ import annotations

import json

import pytest

from coord_mcp import store
from coord_mcp.db import connect
from coord_mcp.workflows import cases as cs
from coord_mcp.workflows import definitions as defs
from coord_mcp.workflows import engine

INV = {"done": True, "verdict": "v", "confidence": "high", "evidence": ["e"]}


@pytest.fixture()
def c(tmp_path):
    conn = connect(tmp_path / "t.db")
    cs.ensure_workflows(conn)
    engine._since(conn, write=True)  # switched on before anything below lands
    conn.execute("UPDATE meta SET value=? WHERE key=?", (str(store.now() - 60), engine.SINCE_KEY))
    yield conn
    conn.close()


def _status_case(c, reply_from="john@client.example"):
    i = store.propose_intent(c, "LNK: John asks where RAID 695 stands", "email-status",
                             context=f"msg X; reply_from {reply_from}; reply_date 2026-11-13T10:00:00Z; "
                                     'subject "RAID 695"', origin="team", trust="trusted")["intent_id"]
    t = store.create_task(c, "investigation", "LNK: status of RAID 695 for John", {}, assigned_to="LNK",
                          from_intent=i)["task_id"]
    return t


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("COORD_WORKFLOW_ENGINE", raising=False)
    assert not engine.enabled()
    monkeypatch.setenv("COORD_WORKFLOW_ENGINE", "1")
    assert engine.enabled()


def test_status_read_landing_creates_the_draft_once(c):
    t = _status_case(c)
    store.complete_task(c, t, {**INV, "follow_ups": [{"who": "LNK", "what": "draft the reply to John"}]})
    cs.sync(c)
    assert engine.run(c, dry_run=True) and c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    did = engine.run(c)
    assert len(did) == 1 and did[0]["kind"] == "draft_reply"
    new = c.execute("SELECT * FROM tasks WHERE id=?", (did[0]["task_id"],)).fetchone()
    spec = json.loads(new["spec"])
    assert new["parent_id"] == t and new["assigned_to"] == "LNK" and new["case_step"] == "draft"
    assert spec["reply_from"] == "john@client.example" and spec["reply_date"].startswith("2026-11-13")
    assert t in spec["read_first"] and not engine._PH.search(json.dumps(spec))
    fu = c.execute("SELECT state, resolved_by FROM follow_ups WHERE task_id=?", (t,)).fetchone()
    assert (fu["state"], fu["resolved_by"]) == ("tasked", new["id"])
    assert c.execute("SELECT COUNT(*) FROM decisions WHERE task_id=?", (new["id"],)).fetchone()[0] == 1
    assert engine.run(c) == []  # the draft is open now; nothing more
    case = c.execute("SELECT * FROM workflow_cases").fetchone()
    assert (case["step"], case["waits_on"]) == ("draft", "agent")


def test_no_reply_without_a_message_to_answer(c):
    t = store.create_task(c, "investigation", "LNK: status of RAID 695 for John", {}, assigned_to="LNK")["task_id"]
    store.complete_task(c, t, INV)
    cs.sync(c)
    assert engine.run(c) == []


def test_a_ben_loose_end_holds_the_case(c):
    t = _status_case(c)
    store.complete_task(c, t, {**INV, "follow_ups": [{"who": "Ben", "what": "decide what to tell John"}]})
    cs.sync(c)
    assert engine.run(c) == []


def test_history_before_switch_on_is_left_alone(c):
    t = _status_case(c)
    store.complete_task(c, t, INV)
    c.execute("UPDATE tasks SET completed_at=? WHERE id=?", (store.now() - 3600, t))
    cs.sync(c)
    assert engine.run(c) == []


def test_defect_fix_takes_the_client_loose_end_and_is_cvc_bound(c):
    t = store.create_task(c, "investigation", "LNK: why TEST Production Orders lacks the Location field", {},
                          assigned_to="LNK")["task_id"]
    store.complete_task(c, t, {**INV, "follow_ups": [{"who": "LNK", "what": "add Location to xxpo01.p"}]})
    cs.sync(c)
    did = engine.run(c)
    assert [d["kind"] for d in did] == ["code_change"]
    spec = json.loads(c.execute("SELECT spec FROM tasks WHERE id=?", (did[0]["task_id"],)).fetchone()["spec"])
    assert spec["cvc"] is True and spec["goal"] == "add Location to xxpo01.p" and "checkin_of" not in spec


def test_a_ben_step_between_stops_the_engine(c, monkeypatch):
    d = json.loads(json.dumps(defs.get("A1")))
    d["steps"].insert(2, {"key": "ok", "name": "Ben approves", "who": "ben", "kinds": [], "gate": "g", "next": "n"})
    d["steps"][3]["spawn"]["after"] = "status_read"
    assert defs.validate(d) == []
    real = defs.get
    monkeypatch.setattr(defs, "get", lambda code: d if code == "A1" else real(code))
    t = _status_case(c)
    store.complete_task(c, t, INV)
    cs.sync(c)
    did = engine.run(c)
    assert [x.get("stop") for x in did] == ["draft"]
    assert c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert engine.run(c) == []  # recorded once
    assert c.execute("SELECT COUNT(*) FROM workflow_case_events WHERE event='stopped'").fetchone()[0] == 1
