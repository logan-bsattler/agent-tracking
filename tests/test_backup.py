import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pytest

from coord_mcp import backup


@pytest.fixture
def board(tmp_path, monkeypatch):
    db = tmp_path / "coord.db"
    monkeypatch.setenv("COORD_DB", str(db))
    monkeypatch.delenv("COORD_BACKUP_DIR", raising=False)
    c = sqlite3.connect(db)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t(x)")
    c.execute("INSERT INTO t VALUES (1)")
    c.commit()
    yield db, c
    c.close()


def rows(p: Path) -> list:
    c = sqlite3.connect(p)
    try:
        return [r[0] for r in c.execute("SELECT x FROM t ORDER BY x")]
    finally:
        c.close()


def test_test_board_backs_up_beside_itself(board):
    db, _ = board
    assert backup.backup_dir() == db.parent / "backups"


def test_env_overrides_dir(board, tmp_path, monkeypatch):
    monkeypatch.setenv("COORD_BACKUP_DIR", str(tmp_path / "elsewhere"))
    assert backup.backup_dir() == tmp_path / "elsewhere"


def test_real_board_never_uses_onedrive_env(monkeypatch, tmp_path):
    monkeypatch.delenv("COORD_DB", raising=False)
    monkeypatch.delenv("COORD_BACKUP_DIR", raising=False)
    monkeypatch.setenv("OneDrive", str(tmp_path / "client tenant"))
    d = backup.backup_dir()
    assert "client tenant" not in str(d)
    assert d.name in ("coord-backups", "backups")


def test_snapshot_includes_uncheckpointed_wal(board):
    db, c = board
    c.execute("INSERT INTO t VALUES (2)")
    c.commit()  # still in the WAL: connection open, no checkpoint
    got = backup.snapshot(now=datetime(2026, 11, 6, 9))
    assert got.name == "coord-20261106.db"
    assert rows(got) == [1, 2]


def test_one_per_day_unless_forced(board):
    day = datetime(2026, 11, 6, 9)
    assert backup.snapshot(now=day)
    assert backup.snapshot(now=day.replace(hour=17)) is None
    forced = backup.snapshot(now=day.replace(hour=17), force=True)
    assert forced.name == "coord-20261106-170000.db"


def test_keeps_newest(board):
    for d in range(1, 6):
        backup.snapshot(now=datetime(2026, 11, d), keep=3)
    names = [p.name for p in backup.snapshots(backup.backup_dir())]
    assert names == ["coord-20261105.db", "coord-20261104.db", "coord-20261103.db"]


def test_live_lock_skips_stale_lock_is_taken(board):
    db, _ = board
    dest = backup.backup_dir()
    dest.mkdir(parents=True)
    lock = dest / ".taking"
    lock.write_text("")
    assert backup.snapshot(now=datetime(2026, 11, 6)) is None
    old = time.time() - backup.STALE - 5
    os.utime(lock, (old, old))
    assert backup.snapshot(now=datetime(2026, 11, 6))
    assert not lock.exists()


def test_stale_temp_files_are_cleaned(board):
    dest = backup.backup_dir()
    dest.mkdir(parents=True)
    tmp = dest / "coord-20261101.db.999.tmp"
    tmp.write_text("partial")
    old = time.time() - backup.STALE - 5
    os.utime(tmp, (old, old))
    backup.snapshot(now=datetime(2026, 11, 6))
    assert not tmp.exists()


def test_missing_board_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setenv("COORD_DB", str(tmp_path / "none.db"))
    assert backup.snapshot() is None
    assert not (tmp_path / "backups").exists()


def test_start_daily_swallows_errors(board, monkeypatch, capsys):
    def boom(**_):
        raise RuntimeError("disk full")
    monkeypatch.setattr(backup, "snapshot", boom)
    backup.start_daily().join(5)
    assert "coord backup: disk full" in capsys.readouterr().err
