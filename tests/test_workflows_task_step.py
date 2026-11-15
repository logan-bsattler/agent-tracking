"""A task's workflow step on coord_board and on its /board/task page."""

from __future__ import annotations

import pytest

from coord_mcp import report, store
from coord_mcp.db import connect
from coord_mcp.workflows import cases as cs


@pytest.fixture()
def c(tmp_path):
    conn = connect(tmp_path / "t.db")
    yield conn
    conn.close()


INV = {"done": True, "verdict": "v", "confidence": "high", "evidence": ["e"]}


def test_board_without_workflow_tables_reads_as_before(c):
    tid = store.create_task(c, "investigation", "LNK: why the AMEX report fails", {}, assigned_to="LNK")["task_id"]
    row = next(r for r in store.board(c)["live"] if r["id"] == tid)
    assert "workflow" not in row and "step" not in row
    assert cs.task_steps(c, [tid]) == {}
    assert report._workflow_kv(store.task_detail(c, tid)["workflow"], tid) == ""


def test_board_and_task_page_show_the_step(c):
    root = store.create_task(c, "investigation", "LNK: why the AMEX report fails", {}, assigned_to="LNK")["task_id"]
    cs.sync(c)
    row = next(r for r in store.board(c)["live"] if r["id"] == root)
    assert row["workflow"] == "A3" and row["step"].startswith("investigate ")
    n, of = row["step"].split()[1].split("/")
    assert 1 <= int(n) <= int(of)

    html = report.task_page(store.task_detail(c, root), root)
    assert "<dt>workflow</dt>" in html and "/workflows?wf=A3" in html
    assert "<b>Investigate</b>" in html and f"(step {n} of {of})" in html


def test_task_page_says_where_the_case_moved_to(c):
    root = store.create_task(c, "investigation", "LNK: why the AMEX report fails", {}, assigned_to="LNK")["task_id"]
    store.complete_task(c, root, INV)
    kid = store.create_task(c, "code_change", "LNK: fix it", {}, assigned_to="LNK", parent_id=root)["task_id"]
    cs.sync(c)
    w = cs.task_steps(c, [root, kid])
    assert w[root]["case_id"] == w[kid]["case_id"] and w[kid]["case_at"] == w[kid]["step"] != w[root]["step"]
    html = report.task_page(store.task_detail(c, root), root)
    assert f'case now at {w[kid]["step_name"]}' in html
    kid_html = report.task_page(store.task_detail(c, kid), kid)
    assert f"/board/task/{root}" in kid_html  # links the case root
