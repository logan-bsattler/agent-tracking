"""Daily snapshots of the board.

The board is one SQLite file (~/.coord/coord.db) and nothing else holds its
history. `snapshot()` copies it with VACUUM INTO, which reads one consistent
view through SQLite (the WAL included) while other sessions keep writing,
checks the copy, and keeps the newest KEEP.

Not GitHub: the board holds client task results, email text and meeting
intents, and the repo is pushed from a personal account.

Where, first match wins:
  1. COORD_BACKUP_DIR
  2. for the real board: `~/OneDrive - Logan Consulting/coord-backups` when
     that OneDrive exists, else ~/.coord/backups. Never $OneDrive: on this
     machine it points at a client's tenant (Mark Anthony Group).
  3. for any other board (COORD_DB, as tests set it): `backups/` beside it,
     so a test board never lands among the real snapshots.

When: `start_daily()` runs on a background thread as each coord server
starts, and takes one only when today's file is missing. Every session
starts a server, so no scheduled task is needed (the endpoint rules here
block .cmd and .ps1, and a logon task is the persistence the auto-mode
check refuses). A day with no session has nothing new to back up.

    python -m coord_mcp.backup            # today's, if missing
    python -m coord_mcp.backup --force    # one now, regardless
    python -m coord_mcp.backup list
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .db import default_db_path

KEEP = 14
PREFIX = "coord-"
STALE = 15 * 60  # a lock or temp file this old belongs to a server that died mid-copy


def _real_board() -> Path:
    return Path.home() / ".coord" / "coord.db"


def backup_dir(db: Path | None = None) -> Path:
    env = os.environ.get("COORD_BACKUP_DIR")
    if env:
        return Path(env).expanduser()
    db = Path(db) if db else default_db_path()
    if db.resolve() != _real_board().resolve():
        return db.parent / "backups"
    onedrive = Path.home() / "OneDrive - Logan Consulting"
    return onedrive / "coord-backups" if onedrive.is_dir() else Path.home() / ".coord" / "backups"


def snapshots(dest: Path) -> list[Path]:
    """Newest first. Names sort by date, so a forced one sorts beside its day."""
    if not dest.is_dir():
        return []
    return sorted(dest.glob(f"{PREFIX}*.db"), key=lambda p: p.name, reverse=True)


def _today_name(now: datetime) -> str:
    return f"{PREFIX}{now:%Y%m%d}.db"


def _clean(dest: Path, keep: int, now: float) -> list[Path]:
    gone = snapshots(dest)[keep:]
    for p in dest.glob("*.tmp"):
        if now - p.stat().st_mtime > STALE:
            gone.append(p)
    for p in gone:
        try:
            p.unlink()
        except OSError:
            pass
    return gone


def snapshot(db: Path | None = None, dest: Path | None = None, keep: int = KEEP,
             force: bool = False, now: datetime | None = None) -> Path | None:
    """Copy the board into `dest`. Returns the new file, or None when today's
    already exists (unless `force`) or another process is taking it."""
    db = Path(db) if db else default_db_path()
    if not db.exists():
        return None
    dest = Path(dest) if dest else backup_dir(db)
    dest.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now()
    name = f"{PREFIX}{now:%Y%m%d-%H%M%S}.db" if force else _today_name(now)
    final = dest / name
    if final.exists():
        return None

    lock = dest / ".taking"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if time.time() - lock.stat().st_mtime < STALE:
            return None
        lock.unlink(missing_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(fd)
    tmp = dest / f"{name}.{os.getpid()}.tmp"
    try:
        if final.exists():  # another process finished it while we waited
            return None
        src = sqlite3.connect(str(db), timeout=30.0)
        try:
            src.execute("PRAGMA busy_timeout=30000")
            src.execute("VACUUM INTO ?", (str(tmp),))
        finally:
            src.close()
        chk = sqlite3.connect(str(tmp))
        try:
            ok = chk.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            chk.close()
        if ok != "ok":
            raise RuntimeError(f"snapshot failed quick_check: {ok}")
        os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
    _clean(dest, keep, time.time())
    return final


def start_daily() -> threading.Thread:
    """Today's snapshot, off the request path: a 40 MB copy must never delay
    the MCP handshake (Claude Code gives it 30s). Failures go to stderr, which
    is the server log, and never reach a tool call."""
    def run() -> None:
        try:
            snapshot()
        except Exception as e:  # noqa: BLE001 - a backup must not take the server down
            print(f"coord backup: {e}", file=sys.stderr)

    t = threading.Thread(target=run, name="coord-backup", daemon=True)
    t.start()
    return t


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m coord_mcp.backup")
    ap.add_argument("cmd", nargs="?", default="take", choices=("take", "list"))
    ap.add_argument("--force", action="store_true", help="take one even if today's exists")
    ap.add_argument("--keep", type=int, default=KEEP)
    a = ap.parse_args(argv)
    dest = backup_dir()
    if a.cmd == "list":
        print(dest)
        for p in snapshots(dest):
            print(f"  {p.name}  {p.stat().st_size / 1e6:.1f} MB")
        return 0
    got = snapshot(keep=a.keep, force=a.force)
    print(got if got else f"today's snapshot already in {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
