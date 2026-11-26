"""Rule P3: a sizing or estimate task is created high (Ben, 2026-11-25)."""

from __future__ import annotations

import pytest

from coord_mcp import priority, queue, store
from coord_mcp.db import connect


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    queue.set_switch(c, "estimate_high", True)  # unmarked SWITCHES read off in tests
    yield c
    c.close()


def _spec(conn, tid):
    import json
    return json.loads(conn.execute("SELECT spec FROM tasks WHERE id=?", (tid,)).fetchone()["spec"])


@pytest.mark.parametrize("title", ["Size DEV-39 sales order audit sizing", "Estimate FDD-099 hours",
                                   "Ballpark the EDI rework", "Re-estimate wave 2"])
def test_estimate_titles_go_high(conn, title):
    out = store.create_task(conn, "investigation", title, {"q": "x", "priority": "normal"}, assigned_to="MAG")
    assert _spec(conn, out["task_id"])["priority"] == "high"
    assert any("rule P3" in w and "was normal" in w for w in out["warnings"])


def test_sizing_deliverable_type_goes_high(conn):
    out = store.create_task(conn, "investigation", "DEV-39 work-up",
                            {"q": "x", "deliverable_type": "sizing"}, assigned_to="MAG")
    assert priority.of(conn, out["task_id"]) == "high"


def test_other_work_and_code_are_left_alone(conn):
    a = store.create_task(conn, "investigation", "Why is the PO blank", {"q": "x", "priority": "low"},
                          assigned_to="MAG")["task_id"]
    b = store.create_task(conn, "code_change", "Fix the estimate calc", {"q": "x"}, assigned_to="MAG")["task_id"]
    assert priority.of(conn, a) == "low" and priority.of(conn, b) == "normal"


def test_switch_off(conn):
    queue.set_switch(conn, "estimate_high", False)
    t = store.create_task(conn, "investigation", "Sizing", {"q": "x", "priority": "low"}, assigned_to="MAG")
    assert priority.of(conn, t["task_id"]) == "low"


def test_backfill_lists_and_raises_open_estimates(conn):
    queue.set_switch(conn, "estimate_high", False)
    t = store.create_task(conn, "investigation", "Estimate wave 3", {"q": "x", "priority": "normal"},
                          assigned_to="MAG")["task_id"]
    store.create_task(conn, "investigation", "Unrelated", {"q": "x"}, assigned_to="MAG")
    assert [r["task_id"] for r in priority.open_estimates(conn)] == [t]
    priority.set_priority(conn, t, "high")
    assert priority.open_estimates(conn) == []
