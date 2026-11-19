import sqlite3
import threading

from coord_mcp import lockwatch


def test_dumps_the_holding_thread(tmp_path, monkeypatch):
    db = tmp_path / "b.db"
    sqlite3.connect(db).execute("PRAGMA journal_mode=WAL").close()
    out = tmp_path / "holders.log"
    monkeypatch.setattr(lockwatch, "POLL", 0.02)
    monkeypatch.setattr(lockwatch, "DUMP_AFTER", 0.2)
    held, release = threading.Event(), threading.Event()

    def slow_page_holding_lock():
        c = sqlite3.connect(db, isolation_level=None)
        c.execute("BEGIN IMMEDIATE")
        held.set()
        release.wait(5)
        c.execute("ROLLBACK")
        c.close()

    h = threading.Thread(target=slow_page_holding_lock, name="holder")
    h.start()
    held.wait(5)
    stop = threading.Event()
    w = threading.Thread(target=lockwatch.watch, args=(db, out, stop))
    w.start()
    for _ in range(100):
        if out.exists():
            break
        stop.wait(0.05)
    release.set()
    stop.set()
    h.join()
    w.join()
    text = out.read_text(encoding="utf-8")
    assert "write lock unavailable" in text
    assert "slow_page_holding_lock" in text and "thread holder:" in text
