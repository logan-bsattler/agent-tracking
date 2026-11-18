"""Write-lock hygiene on the shared WAL board ("database is locked", 2026-11-18).

A deferred transaction that reads, then writes after another connection has
committed, fails at once with SQLITE_BUSY_SNAPSHOT: busy_timeout cannot help,
since waiting never makes a stale snapshot current. BEGIN IMMEDIATE takes the
write lock before the read, so it waits (busy_timeout) instead of failing.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from coord_mcp import db, store, usage


@pytest.fixture
def path(tmp_path, monkeypatch):
    p = tmp_path / "coord.db"
    monkeypatch.setenv("COORD_DB", str(p))
    db.connect(p).close()
    return p


def _bg(path, *sql, delay=0.0):
    """Run statements on a connection of the thread's own (sqlite3 objects
    are per-thread). Returns the thread and a list that gets any error."""
    errs: list[BaseException] = []

    def run():
        try:
            time.sleep(delay)
            c = db.connect(path)
            for q in sql:
                c.execute(q)
            c.close()
        except BaseException as e:  # noqa: BLE001
            errs.append(e)
    t = threading.Thread(target=run)
    t.start()
    return t, errs


def test_deferred_upgrade_on_stale_snapshot_fails_at_once(path):
    """The failure itself: no wait, whatever busy_timeout says."""
    a, b = db.connect(path), db.connect(path)
    a.execute("BEGIN")
    a.execute("SELECT COUNT(*) FROM meta").fetchone()  # snapshot taken
    b.execute("INSERT INTO meta(key, value) VALUES ('x', '1')")  # someone commits
    t = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        a.execute("INSERT INTO meta(key, value) VALUES ('y', '1')")
    assert time.monotonic() - t < 1  # busy_timeout (5 s) never applied
    a.execute("ROLLBACK")


def _holder(path, hold=0.3, key="x"):
    """A writer in another thread: takes the lock, holds it `hold` s, commits."""
    ready = threading.Event()
    errs: list[BaseException] = []

    def run():
        try:
            c = db.connect(path)
            c.execute("BEGIN IMMEDIATE")
            c.execute("INSERT INTO meta(key, value) VALUES (?, '1')", (key,))
            ready.set()
            time.sleep(hold)
            c.execute("COMMIT")
            c.close()
        except BaseException as e:  # noqa: BLE001
            errs.append(e)
            ready.set()
    t = threading.Thread(target=run)
    t.start()
    ready.wait(5)
    return t, errs


def test_immediate_waits_for_the_writer_instead(path):
    a = db.connect(path)
    t, errs = _holder(path)
    a.execute("BEGIN IMMEDIATE")  # waits out the holder, then holds a current snapshot
    a.execute("SELECT COUNT(*) FROM meta").fetchone()
    a.execute("INSERT INTO meta(key, value) VALUES ('y', '1')")
    a.execute("COMMIT")
    t.join()
    assert not errs
    assert a.execute("SELECT COUNT(*) FROM meta WHERE key IN ('x','y')").fetchone()[0] == 2


def test_savepoint_takes_the_write_lock_up_front(path):
    """store.savepoint, outermost: read then write survives a writer in between,
    which waits for the savepoint instead of committing under it."""
    a = db.connect(path)
    with store.savepoint(a, "t"):
        a.execute("SELECT COUNT(*) FROM meta").fetchone()
        t, errs = _bg(path, "INSERT INTO meta(key, value) VALUES ('x', '1')")
        time.sleep(0.2)
        a.execute("INSERT INTO meta(key, value) VALUES ('y', '1')")
    assert not a.in_transaction
    t.join()
    assert not errs
    assert a.execute("SELECT COUNT(*) FROM meta WHERE key IN ('x','y')").fetchone()[0] == 2


def test_savepoint_rolls_back_and_releases_on_error(path):
    a = db.connect(path)
    with pytest.raises(ValueError):
        with store.savepoint(a, "t"):
            a.execute("INSERT INTO meta(key, value) VALUES ('y', '1')")
            raise ValueError
    assert not a.in_transaction
    assert a.execute("SELECT COUNT(*) FROM meta WHERE key='y'").fetchone()[0] == 0


def test_nested_savepoint_leaves_the_outer_transaction_alone(path):
    a = db.connect(path)
    a.execute("BEGIN IMMEDIATE")
    with store.savepoint(a, "t"):
        a.execute("INSERT INTO meta(key, value) VALUES ('y', '1')")
    assert a.in_transaction
    a.execute("ROLLBACK")


def _transcript(tmp_path, monkeypatch, n):
    proj = tmp_path / "projects" / "C--x"
    proj.mkdir(parents=True)
    f = proj / "s1.jsonl"
    lines = [json.dumps({"type": "assistant", "sessionId": "s1", "requestId": f"r{i}",
                         "timestamp": "2026-11-18T09:00:00Z", "cwd": "C:\\x",
                         "message": {"model": "claude-opus-5-5", "usage": {"input_tokens": 1, "output_tokens": 1}}})
             for i in range(n)]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(usage, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(usage, "ingest_desktop_quota", lambda conn: 0)
    monkeypatch.setattr(usage, "ingest_live_quota", lambda conn: 0)
    return f


def test_ingest_commits_in_chunks_and_keeps_its_offset(path, tmp_path, monkeypatch):
    f = _transcript(tmp_path, monkeypatch, 25)
    monkeypatch.setattr(usage, "INGEST_CHUNK_LINES", 10)
    conn = db.connect(path)
    commits = []
    real = usage._ingest_mark
    monkeypatch.setattr(usage, "_ingest_mark", lambda c, k, o, s: (commits.append(o), real(c, k, o, s)))
    assert usage.ingest(conn)["requests_added"] == 25
    assert len(commits) == 3 and commits[-1] == f.stat().st_size  # 10, 20, then the tail
    assert not conn.in_transaction
    assert usage.ingest(conn)["files_read"] == 0  # offset stored: nothing re-read


def test_ingest_failure_keeps_committed_chunks(path, tmp_path, monkeypatch):
    f = _transcript(tmp_path, monkeypatch, 25)
    monkeypatch.setattr(usage, "INGEST_CHUNK_LINES", 10)
    conn = db.connect(path)
    real, n = usage._ingest_line, [0]

    def boom(*a):
        n[0] += 1
        if n[0] == 15:
            raise sqlite3.OperationalError("database is locked")
        return real(*a)
    monkeypatch.setattr(usage, "_ingest_line", boom)
    with pytest.raises(sqlite3.OperationalError):
        usage.ingest(conn)
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 10
    monkeypatch.setattr(usage, "_ingest_line", real)
    assert usage.ingest(conn)["requests_added"] == 15  # resumes at the stored offset
    assert conn.execute("SELECT offset FROM ingest_files").fetchone()[0] == f.stat().st_size


def test_ingest_survives_a_commit_between_its_reads_and_writes(path, tmp_path, monkeypatch):
    """The live failure (usage.ingest -> INSERT INTO cc_sessions): another
    process commits while ingest has a snapshot open. Deferred BEGIN failed at
    once; IMMEDIATE holds the lock, so the other writer waits instead."""
    _transcript(tmp_path, monkeypatch, 3)
    a = db.connect(path)
    real, threads = usage._ingest_line, []

    def racing(conn, *rest):
        conn.execute("SELECT COUNT(*) FROM requests").fetchone()  # a snapshot, as a read would
        threads.append(_bg(path, "INSERT OR REPLACE INTO meta(key, value) VALUES ('z', '1')"))
        time.sleep(0.1)
        return real(conn, *rest)
    monkeypatch.setattr(usage, "_ingest_line", racing)
    assert usage.ingest(a)["requests_added"] == 3
    for t, errs in threads:
        t.join()
        assert not errs
