"""A bare outermost savepoint (immediate=False) whose ROLLBACK TO / RELEASE
fails must not leave the connection inside a transaction (2026-11-27: the
minder's direct.sweep hit "database is locked", the transaction stayed open,
and every later BEGIN IMMEDIATE in the loop pass failed, so autotask created
nothing for two days)."""
import sqlite3

import pytest

from coord_mcp import store


class FlakyConn(sqlite3.Connection):
    fail_release = False

    def execute(self, sql, *a):
        if self.fail_release and (sql.startswith("ROLLBACK TO") or sql.startswith("RELEASE")):
            raise sqlite3.OperationalError("database is locked")
        return super().execute(sql, *a)


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.db"), isolation_level=None, factory=FlakyConn)
    c.execute("CREATE TABLE t (x)")
    yield c
    c.close()


def test_failed_release_after_error_ends_transaction(conn):
    with pytest.raises(ValueError):
        with store.savepoint(conn, "sweep", immediate=False):
            conn.execute("SELECT 1")
            conn.fail_release = True
            raise ValueError("boom")
    conn.fail_release = False
    assert not conn.in_transaction
    conn.execute("BEGIN IMMEDIATE")  # what autotask does next
    conn.execute("ROLLBACK")


def test_failed_release_on_success_rolls_back_and_raises(conn):
    with pytest.raises(sqlite3.OperationalError):
        with store.savepoint(conn, "sweep", immediate=False):
            conn.execute("INSERT INTO t VALUES (1)")
            conn.fail_release = True
    conn.fail_release = False
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_nested_bare_savepoint_leaves_outer_alone(conn):
    conn.execute("BEGIN")
    with store.savepoint(conn, "inner", immediate=False):
        conn.execute("INSERT INTO t VALUES (1)")
    assert conn.in_transaction
    conn.execute("COMMIT")
