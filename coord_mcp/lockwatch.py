"""Who holds the board's write lock? A watchdog for the dashboard process.

After 528e728 the dashboard still held the write lock for 89 s at a stretch
(probe, 2026-11-19 07:10), from an HTTP handler thread: every other writer
timed out on busy_timeout. A probe outside the process can see that the
lock is held but not by whom. This thread, inside the process, tries the
lock every POLL seconds without waiting; once it has been unavailable for
DUMP_AFTER seconds it writes every other thread's stack to
~/.coord/lock-holders.log, once per hold (again at each REDUMP), so the
holder names itself. If the holder is another process, the dump shows only
waiting threads, which says so too.
"""
from __future__ import annotations

import sqlite3
import sys
import threading
import time
import traceback
from pathlib import Path

POLL = 0.5
DUMP_AFTER = 10.0
REDUMP = 60.0


def log_path() -> Path:
    return Path.home() / ".coord" / "lock-holders.log"


def dump(held: float, out: Path) -> None:
    me = threading.get_ident()
    names = {t.ident: t.name for t in threading.enumerate()}
    lines = [f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} write lock unavailable {held:.1f}s"]
    for ident, frame in sys._current_frames().items():
        if ident == me:
            continue
        lines.append(f"thread {names.get(ident, ident)}:")
        lines.extend(x.rstrip("\n") for x in traceback.format_stack(frame))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n\n")


def watch(db: Path | str, out: Path | None = None, stop: threading.Event | None = None) -> None:
    out = out or log_path()
    stop = stop or threading.Event()
    conn = sqlite3.connect(str(db), timeout=0, isolation_level=None, check_same_thread=False)
    since: float | None = None
    next_dump = DUMP_AFTER
    try:
        while not stop.wait(POLL):
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("ROLLBACK")
                since, next_dump = None, DUMP_AFTER
            except sqlite3.OperationalError:
                now = time.monotonic()
                if since is None:
                    since = now
                elif now - since >= next_dump:
                    try:
                        dump(now - since, out)
                    except Exception:  # noqa: BLE001 - a diagnostic never takes the dashboard down
                        pass
                    next_dump += REDUMP
    finally:
        conn.close()


def start(db: Path | str) -> threading.Thread:
    t = threading.Thread(target=watch, args=(db,), name="lockwatch", daemon=True)
    t.start()
    return t
