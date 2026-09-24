"""SQLite layer. One file, WAL mode, opened per process.

Timestamps are integer epoch seconds. The file must live on local disk:
WAL plus a syncing client (OneDrive, Dropbox) corrupts it.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

# Bump this whenever DDL or _migrate changes. connect() skips both entirely
# when the file already reports this version, so a new table or column that
# ships without a bump will not be created.
SCHEMA_VERSION = 5

def default_db_path() -> Path:
    """COORD_DB if set, else ~/.coord/coord.db -- read on every call, not at import.

    Frozen at import, a COORD_DB set afterwards (as every test fixture does) was
    silently ignored and connect() opened the real board instead.
    """
    return Path(os.environ.get("COORD_DB", Path.home() / ".coord" / "coord.db")).expanduser()

# journal_mode is a property of the file, so it is set once at init rather than
# on every connection. busy_timeout is per-connection and is set in connect().
DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id             TEXT PRIMARY KEY,
  parent_id      TEXT REFERENCES tasks(id),
  kind           TEXT NOT NULL,
  title          TEXT NOT NULL,
  spec           TEXT NOT NULL,
  state          TEXT NOT NULL CHECK (state IN ('open','done','failed')),
  assigned_to    TEXT,
  result         TEXT,
  result_version INTEGER,
  from_intent    TEXT,
  created_at     INTEGER NOT NULL,
  completed_at   INTEGER,
  -- Last time the assigned client read the task (coord_get_task). Null means
  -- it was never picked up; a park after it means it is waiting on the master.
  picked_up_at   INTEGER
);
CREATE INDEX IF NOT EXISTS tasks_state ON tasks(state, created_at);
CREATE INDEX IF NOT EXISTS tasks_kind ON tasks(kind, state);

CREATE TABLE IF NOT EXISTS decisions (
  id         TEXT PRIMARY KEY,
  task_id    TEXT REFERENCES tasks(id),
  statement  TEXT NOT NULL,
  because    TEXT,
  supersedes TEXT REFERENCES decisions(id),
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_task ON decisions(task_id);

-- Progress handed forward when a session runs out of context mid-task. The
-- task stays open; the session clears itself and is re-dispatched, and
-- coord_get_task replays these rows so the fresh session does not start over.
-- One row per park, so a task parked twice keeps both handovers in order.
CREATE TABLE IF NOT EXISTS task_parks (
  id         TEXT PRIMARY KEY,
  task_id    TEXT NOT NULL REFERENCES tasks(id),
  progress   TEXT NOT NULL,
  session_id TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS parks_task ON task_parks(task_id, created_at);

-- Next steps a finished task named in its result. Each is owed a resolution:
-- 'tasked' (resolved_by = the child task) or 'dropped' (resolved_by = the
-- decision saying why). Open rows are the board's loose ends.
CREATE TABLE IF NOT EXISTS follow_ups (
  id          TEXT PRIMARY KEY,
  task_id     TEXT NOT NULL REFERENCES tasks(id),
  idx         INTEGER NOT NULL,
  who         TEXT NOT NULL,
  what        TEXT NOT NULL,
  state       TEXT NOT NULL CHECK (state IN ('open','tasked','dropped')),
  resolved_by TEXT,
  created_at  INTEGER NOT NULL,
  resolved_at INTEGER
);
CREATE INDEX IF NOT EXISTS follow_ups_state ON follow_ups(state, created_at);
CREATE INDEX IF NOT EXISTS follow_ups_task ON follow_ups(task_id, idx);

CREATE TABLE IF NOT EXISTS intents (
  id          TEXT PRIMARY KEY,
  summary     TEXT NOT NULL,
  source      TEXT NOT NULL,
  origin      TEXT NOT NULL,
  trust       TEXT NOT NULL CHECK (trust IN ('trusted','untrusted')),
  context     TEXT,
  state       TEXT NOT NULL CHECK (state IN ('open','converted','dismissed')),
  created_at  INTEGER NOT NULL,
  resolved_at INTEGER,
  task_ids    TEXT,
  note        TEXT
);
CREATE INDEX IF NOT EXISTS intents_state ON intents(state, created_at);

-- Usage tracking. One row per API request, ingested from Claude Code's own
-- transcript files, so it covers every session on the machine whether or
-- not it used the board.
CREATE TABLE IF NOT EXISTS requests (
  request_id     TEXT PRIMARY KEY,
  ts             INTEGER NOT NULL,
  session_id     TEXT NOT NULL,
  agent_id       TEXT,
  project        TEXT,
  cwd            TEXT,
  model          TEXT NOT NULL,
  input_tokens   INTEGER NOT NULL DEFAULT 0,
  cache_write_5m INTEGER NOT NULL DEFAULT 0,
  cache_write_1h INTEGER NOT NULL DEFAULT 0,
  cache_read     INTEGER NOT NULL DEFAULT 0,
  output_tokens  INTEGER NOT NULL DEFAULT 0,
  thinking       INTEGER NOT NULL DEFAULT 0,
  cost_usd       REAL NOT NULL DEFAULT 0,
  effort         TEXT,
  source_file    TEXT
);
CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS requests_session ON requests(session_id, ts);

CREATE TABLE IF NOT EXISTS cc_sessions (
  session_id  TEXT PRIMARY KEY,
  project     TEXT,
  cwd         TEXT,
  title       TEXT,
  first_ts    INTEGER,
  last_ts     INTEGER,
  cc_cost_usd REAL,
  git_branch  TEXT
);

CREATE TABLE IF NOT EXISTS ingest_files (
  path   TEXT PRIMARY KEY,
  offset INTEGER NOT NULL DEFAULT 0,
  size   INTEGER NOT NULL DEFAULT 0
);

-- Limit percentages. Two sources: our statusLine hook (fine-grained, carries
-- reset times, Claude Code only) and Claude Desktop's own plan-usage history
-- (coarser, no reset times, but covers the whole shared pool including chat).
CREATE TABLE IF NOT EXISTS quota_snapshots (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              INTEGER NOT NULL,
  source          TEXT NOT NULL DEFAULT 'statusline',
  five_hour_pct   REAL,
  five_hour_reset INTEGER,
  seven_day_pct   REAL,
  seven_day_reset INTEGER,
  session_id      TEXT,
  model           TEXT,
  context_pct     REAL,
  raw             TEXT
);
CREATE INDEX IF NOT EXISTS quota_ts ON quota_snapshots(ts);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations. CREATE TABLE IF NOT EXISTS won't add a column to a
    table that already exists, so do it here."""
    if "picked_up_at" not in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}:
        conn.execute("ALTER TABLE tasks ADD COLUMN picked_up_at INTEGER")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(quota_snapshots)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE quota_snapshots ADD COLUMN source TEXT NOT NULL DEFAULT 'statusline'")
    # One row per (source, ts) so re-ingesting Desktop history is a no-op.
    try:
        conn.execute(
            "DELETE FROM quota_snapshots WHERE id NOT IN "
            "(SELECT MIN(id) FROM quota_snapshots GROUP BY source, ts)"
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS quota_unique ON quota_snapshots(source, ts)")
    except sqlite3.OperationalError:
        pass


def now() -> int:
    return int(time.time())


def _schema_version(conn: sqlite3.Connection) -> int:
    """The version this file was last initialised at, or 0 if it is new.

    Reading it is one indexed lookup; running the DDL and _migrate is a write
    transaction plus a full scan of quota_snapshots. The guard hooks open this
    database on every tool call in every session, so the difference is the
    difference between a few milliseconds and a hundred.
    """
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        return 0  # no meta table: the file is new
    try:
        return int(row["value"]) if row else 0
    except (TypeError, ValueError):
        return 0


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open (and initialise, if needed) the board database."""
    path = Path(db_path) if db_path else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # Per-connection, so both of these are set every time.
    conn.execute("PRAGMA busy_timeout=5000")
    # The REFERENCES clauses in the DDL are inert without this; SQLite defaults
    # foreign key enforcement to off.
    conn.execute("PRAGMA foreign_keys=ON")
    if _schema_version(conn) != SCHEMA_VERSION:
        conn.executescript(DDL)
        _migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    return conn
