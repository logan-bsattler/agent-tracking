"""/board load time and the Promote redirect (2026-11-04)."""
import sqlite3

from coord_mcp import db
from coord_mcp.report import promote_back


def test_parent_index_added_to_existing_board(tmp_path):
    p = tmp_path / "coord.db"
    db.connect(p).close()
    raw = sqlite3.connect(p)
    raw.execute("DROP INDEX tasks_parent")  # a board made before the index existed
    raw.commit(); raw.close()
    conn = db.connect(p)
    plan = " ".join(r["detail"] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT 1 FROM tasks WHERE parent_id = ?", ("x",)))
    assert "tasks_parent" in plan


def test_promote_keeps_filters():
    assert promote_back("?days=30&client=Moog&q=fdd&k=ready", 3) == \
        "/deliverables?days=30&client=Moog&q=fdd&k=ready&promoted=3"


def test_promote_drops_foreign_keys_and_blank_back():
    assert promote_back("", 1) == "/deliverables?promoted=1"
    assert promote_back("?next=https://evil.example&k=final", 2) == "/deliverables?k=final&promoted=2"
