"""SQLite layer. One file, WAL mode, opened per process.

Timestamps are integer epoch seconds. The file must live on local disk:
WAL plus a syncing client (OneDrive, Dropbox) corrupts it.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION = 1

DEFAULT_DB_PATH = Path(os.environ.get("COORD_DB", Path.home() / ".coord" / "coord.db")).expanduser()

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

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
  completed_at   INTEGER
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

CREATE TABLE IF NOT EXISTS quota_snapshots (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              INTEGER NOT NULL,
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


def now() -> int:
    return int(time.time())


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open (and initialise, if needed) the board database."""
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    return conn
