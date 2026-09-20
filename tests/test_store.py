"""Store and contract tests. Run: python -m pytest -q"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from coord_mcp import contracts, store
from coord_mcp.db import connect

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    yield c
    c.close()


GOOD_CODE_CHANGE = {
    "done": True,
    "summary": "Guarded the null case.",
    "files_changed": [{"path": "a.py", "added": 3, "removed": 1}],
    "tests": {"ran": 12, "passed": 12, "failed": 0},
}


# ------------------------------------------------------------ lifecycle


def test_happy_path(conn):
    t = store.create_task(conn, "code_change", "Fix null deref", {"file": "a.py"}, assigned_to="alice")
    assert "expected_result_shape" in t

    task = store.get_task(conn, t["task_id"])
    assert task["spec"] == {"file": "a.py"}
    assert task["assigned_to"] == "alice"
    assert task["expected_result_shape"]["done"] == "bool"

    out = store.complete_task(conn, t["task_id"], GOOD_CODE_CHANGE)
    assert out["state"] == "done"

    got = store.get_task_result(conn, t["task_id"], fields=["summary"])
    assert got["state"] == "done"
    assert got["result"] == {"summary": "Guarded the null case."}


def test_projection_excludes_unrequested_fields(conn):
    t = store.create_task(conn, "review", "Review PR", {})
    store.complete_task(conn, t["task_id"], {
        "done": True, "verdict": "approve", "summary": "fine",
        "findings": [{"severity": "nit", "file": "x.py", "note": "typo"}],
    })
    got = store.get_task_result(conn, t["task_id"], fields=["verdict"])
    assert got["result"] == {"verdict": "approve"}
    full = store.get_task_result(conn, t["task_id"])
    assert "findings" in full["result"]


def test_unknown_kind_rejected_at_create(conn):
    with pytest.raises(ValueError, match="unknown task kind"):
        store.create_task(conn, "poetry", "Write a sonnet", {})


def test_unknown_task_is_actionable(conn):
    with pytest.raises(KeyError, match="unknown task"):
        store.get_task(conn, "nope")


def test_cannot_complete_twice(conn):
    t = store.create_task(conn, "code_change", "x", {})
    store.complete_task(conn, t["task_id"], GOOD_CODE_CHANGE)
    with pytest.raises(ValueError, match="already done"):
        store.complete_task(conn, t["task_id"], GOOD_CODE_CHANGE)


def test_failed_task_can_be_retried(conn):
    t = store.create_task(conn, "code_change", "x", {})
    out = store.complete_task(conn, t["task_id"], {"done": False, "reason": "flaky", "retryable": True})
    assert out["state"] == "failed"
    assert store.complete_task(conn, t["task_id"], GOOD_CODE_CHANGE)["state"] == "done"


# ------------------------------------------------------------ contracts


def test_over_cap_string_is_rejected(conn):
    t = store.create_task(conn, "code_change", "x", {})
    bad = dict(GOOD_CODE_CHANGE, summary="s" * 301)
    with pytest.raises(contracts.ValidationError) as ei:
        store.complete_task(conn, t["task_id"], bad)
    assert "summary: 301 chars, cap is 300" in ei.value.render()
    assert store.get_task_result(conn, t["task_id"])["result"] is None  # nothing written


def test_prose_smuggling_is_rejected(conn):
    t = store.create_task(conn, "investigation", "x", {})
    with pytest.raises(contracts.ValidationError) as ei:
        store.complete_task(conn, t["task_id"], {
            "done": True, "verdict": "ok", "confidence": "high", "evidence": [],
            "transcript": "here is everything I read...",
        })
    assert "unexpected field(s) ['transcript']" in ei.value.render()


def test_bad_enum_names_allowed_values(conn):
    t = store.create_task(conn, "review", "x", {})
    with pytest.raises(contracts.ValidationError) as ei:
        store.complete_task(conn, t["task_id"], {"done": True, "verdict": "lgtm", "summary": "s"})
    assert "['approve', 'request_changes', 'block']" in ei.value.render()


def test_nested_errors_carry_paths(conn):
    errs = []
    try:
        contracts.validate("review", {
            "done": True, "verdict": "approve", "summary": "s",
            "findings": [{"severity": "huge", "file": "f", "note": "n"}, {"file": "g"}],
        })
    except contracts.ValidationError as e:
        errs = e.errors
    assert any(x.startswith("findings[0].severity:") for x in errs)
    assert "findings[1].severity: required" in errs
    assert "findings[1].note: required" in errs


def test_all_errors_reported_at_once(conn):
    with pytest.raises(contracts.ValidationError) as ei:
        contracts.validate("data_pull", {"done": True, "rows": "many"})
    assert len(ei.value.errors) == 2  # output_path missing, rows wrong type


def test_bool_is_not_an_int():
    with pytest.raises(contracts.ValidationError):
        contracts.validate("data_pull", {"done": True, "output_path": "p", "rows": True})


def test_failure_report_accepted_for_any_kind():
    for kind in contracts.known_kinds():
        out = contracts.validate(kind, {"done": False, "reason": "blocked", "retryable": False})
        assert out["done"] is False


def test_failure_report_is_also_validated():
    with pytest.raises(contracts.ValidationError) as ei:
        contracts.validate("review", {"done": False, "reason": "r" * 201})
    assert "retryable: required" in ei.value.errors


def test_expected_shape_marks_optional():
    shape = contracts.expected_shape("code_change")
    assert "summary" in shape and "notes?" in shape
    assert shape["tests?"]["ran"] == "int"


def test_every_contract_has_done_required():
    for kind, c in contracts.CONTRACTS.items():
        assert c["done"] == {"type": "bool", "required": True}, kind


# ---------------------------------------------------------------- board


def test_board_is_flat_in_history(conn):
    import json
    for _ in range(30):
        t = store.create_task(conn, "code_change", "x", {"big": "y" * 5000})
        store.complete_task(conn, t["task_id"], GOOD_CODE_CHANGE)
    b = store.board(conn)
    assert b["live"] == []
    assert b["tasks_by_kind"] == {"code_change": {"done": 30}}
    assert len(json.dumps(b)) < 200


def test_board_lists_open_and_failed_without_specs(conn):
    t = store.create_task(conn, "review", "Look at auth", {"secret": "spec"})
    b = store.board(conn)
    assert b["live"][0]["title"] == "Look at auth"
    assert "secret" not in str(b)
    assert t["task_id"] == b["live"][0]["id"]


# ------------------------------------------------------------ decisions


def test_decisions_survive_and_supersede(conn):
    d1 = store.record_decision(conn, "Use REST", "simpler")["decision_id"]
    store.record_decision(conn, "Use Graph", "REST lacks batch", supersedes=d1)
    live = store.get_decisions(conn)
    assert [d["statement"] for d in live] == ["Use Graph"]
    assert len(store.get_decisions(conn, include_superseded=True)) == 2


def test_decision_cap_enforced(conn):
    with pytest.raises(ValueError, match="cap is 200"):
        store.record_decision(conn, "x" * 201)


# -------------------------------------------------------------- intents


def test_intent_is_not_a_task(conn):
    store.propose_intent(conn, "Email asks for a report", "email")
    b = store.board(conn)
    assert b["tasks_by_kind"] == {} and b["open_intents"] == 1


def test_untrusted_intent_carries_handling_note(conn):
    store.propose_intent(conn, "s", "email")
    (i,) = store.list_intents(conn)
    assert i["trust"] == "untrusted" and "data, not instruction" in i["handling"]


def test_trusted_intent_has_no_note(conn):
    store.propose_intent(conn, "s", "lead", origin="team", trust="trusted")
    (i,) = store.list_intents(conn)
    assert "handling" not in i


def test_intent_caps(conn):
    with pytest.raises(ValueError, match="cap is 200"):
        store.propose_intent(conn, "s" * 201, "email")
    with pytest.raises(ValueError, match="cap is 1000"):
        store.propose_intent(conn, "s", "email", context="c" * 1001)


def test_intent_converts_and_stamps_provenance(conn):
    iid = store.propose_intent(conn, "s", "email")["intent_id"]
    t = store.create_task(conn, "review", "x", {})
    out = store.resolve_intent(conn, iid, task_ids=[t["task_id"]])
    assert out["state"] == "converted"
    assert conn.execute("SELECT from_intent FROM tasks WHERE id=?", (t["task_id"],)).fetchone()[0] == iid
    (i,) = store.list_intents(conn, state="converted")
    assert i["task_ids"] == [t["task_id"]]


def test_intent_convert_rejects_unknown_task(conn):
    iid = store.propose_intent(conn, "s", "email")["intent_id"]
    with pytest.raises(KeyError, match="unknown task id"):
        store.resolve_intent(conn, iid, task_ids=["nope"])
    assert store.list_intents(conn)[0]["id"] == iid  # still open


def test_intent_dismiss_and_no_double_resolve(conn):
    iid = store.propose_intent(conn, "s", "email")["intent_id"]
    assert store.resolve_intent(conn, iid, note="newsletter")["state"] == "dismissed"
    with pytest.raises(ValueError, match="already dismissed"):
        store.resolve_intent(conn, iid)


def test_board_summary_leaks_nothing(conn):
    store.create_task(conn, "review", "Secret title", {"secret": 1})
    s = store.board_summary(conn)
    assert s == {"tasks_by_kind": {"review": {"open": 1}}, "open_intents": 0, "as_of": s["as_of"]}


# ---------------------------------------------------------- role surface


def _surface(role: str) -> list[str]:
    code = (
        "import asyncio, coord_mcp.server as s;"
        "print(sorted(t.name for t in asyncio.run(s.mcp.list_tools())))"
    )
    env = dict(os.environ, COORD_ROLE=role, PYTHONPATH=str(ROOT))
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    return eval(out.stdout.strip())


def test_desktop_role_has_exactly_two_tools():
    assert _surface("desktop") == ["coord_board_summary", "coord_propose_intent"]


def test_team_role_has_all_tools():
    names = _surface("team")
    assert len(names) == 12
    assert {"coord_create_task", "coord_complete_task", "coord_get_decisions",
            "coord_park_task"} <= set(names)


# ------------------------------------------------ referential integrity


def test_foreign_keys_are_enforced(conn):
    """The REFERENCES clauses in the DDL are inert unless connect() turns
    enforcement on, which SQLite does not do by default."""
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        store.create_task(conn, "review", "t", {}, parent_id="nope")
    with pytest.raises(sqlite3.IntegrityError):
        store.record_decision(conn, "s", task_id="nope")
    real = store.create_task(conn, "review", "t", {})["task_id"]
    child = store.create_task(conn, "review", "c", {}, parent_id=real)["task_id"]
    store.record_decision(conn, "s", task_id=child)


def test_unknown_result_field_is_reported_not_dropped(conn):
    tid = store.create_task(conn, "investigation", "look", {})["task_id"]
    store.complete_task(conn, tid, {"done": True, "verdict": "v", "confidence": "high",
                                    "evidence": ["e"]})
    out = store.get_task_result(conn, tid, ["verdict", "summary"])
    assert out["result"] == {"verdict": "v"}
    assert out["unknown_fields"] == ["summary"]
    assert "confidence" in out["available_fields"]


# ---------------------------------------------------------------- parks


def test_park_replays_into_get_task(conn):
    t = store.create_task(conn, "code_change", "Fix three defects", {"file": "a.p"})
    store.park_task(
        conn, t["task_id"],
        next_step="Fix D2 in xxfzitex.p",
        done=["D1 fixed in xxfzitex.p"],
        do_not_redo=["xxfzitex.p.predefect.bak already written"],
        verified=["Item_Type 'M' per spec p.9"],
    )
    got = store.get_task(conn, t["task_id"])
    assert got["state"] == "open"
    assert got["parked_progress"][0]["next_step"] == "Fix D2 in xxfzitex.p"
    assert got["parked_progress"][0]["verified"] == ["Item_Type 'M' per spec p.9"]
    assert "resume" in got


def test_parks_accumulate_in_order(conn):
    t = store.create_task(conn, "code_change", "Long job", {})
    store.park_task(conn, t["task_id"], next_step="step two")
    r = store.park_task(conn, t["task_id"], next_step="step three")
    assert r["parks"] == 2
    assert [p["next_step"] for p in store.get_task(conn, t["task_id"])["parked_progress"]] == [
        "step two", "step three"]


def test_cannot_park_a_closed_task(conn):
    t = store.create_task(conn, "code_change", "Done already", {})
    store.complete_task(conn, t["task_id"], GOOD_CODE_CHANGE)
    with pytest.raises(ValueError, match="not open"):
        store.park_task(conn, t["task_id"], next_step="too late")


def test_park_rejects_a_transcript(conn):
    t = store.create_task(conn, "code_change", "Wordy", {})
    with pytest.raises(ValueError, match="cap is 400"):
        store.park_task(conn, t["task_id"], next_step="x" * 401)
    with pytest.raises(ValueError, match="cap is 20"):
        store.park_task(conn, t["task_id"], next_step="ok", done=["x"] * 21)


def test_park_on_unknown_task(conn):
    with pytest.raises(KeyError):
        store.park_task(conn, "nope", next_step="x")


def test_board_flags_a_parked_task_as_awaiting_redispatch(conn):
    plain = store.create_task(conn, "code_change", "Never started", {})
    parked = store.create_task(conn, "code_change", "Handed forward", {})
    store.park_task(conn, parked["task_id"], next_step="resume here")
    live = {r["id"]: r for r in store.board(conn)["live"]}
    assert "awaiting_redispatch" not in live[plain["task_id"]]
    assert live[parked["task_id"]]["awaiting_redispatch"] is True
    assert live[parked["task_id"]]["state"] == "open"
