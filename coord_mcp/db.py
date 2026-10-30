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
SCHEMA_VERSION = 11

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
  picked_up_at   INTEGER,
  -- SharePoint pipeline item this task bills to. Hours roll up by it.
  pipeline_task_id TEXT,
  -- Last time the lead read this task's result (coord_get_task_result from
  -- a session that is not a client). Null, or earlier than completed_at,
  -- means the result is sitting unread; the minder wakes the master for it.
  result_read_at INTEGER
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

-- Every pickup, park and close, in order. picked_up_at keeps only the last
-- pickup; billable hours need each interval a client spent on a task.
CREATE TABLE IF NOT EXISTS task_events (
  id      TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  client  TEXT,
  event   TEXT NOT NULL CHECK (event IN ('picked_up','parked','completed','failed')),
  ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS task_events_task ON task_events(task_id, ts);

-- Operator time (review, Kiro runs, calls) that no transcript records.
-- Kept apart from agent time so the two are never confused.
CREATE TABLE IF NOT EXISTS time_entries (
  id         TEXT PRIMARY KEY,
  task_id    TEXT REFERENCES tasks(id),
  client     TEXT NOT NULL,
  day        TEXT NOT NULL,
  minutes    REAL NOT NULL,
  note       TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS time_entries_day ON time_entries(day, client);

-- Calendar events, keyed by Outlook id so a re-pull updates rather than adds.
-- Every event in a pulled range is kept, counted or not, so a new rule can
-- reclassify without a re-pull. See meetings.py.
CREATE TABLE IF NOT EXISTS meetings (
  id         TEXT PRIMARY KEY,
  start_ts   INTEGER NOT NULL,
  end_ts     INTEGER NOT NULL,
  subject    TEXT NOT NULL,
  organizer  TEXT,
  domains    TEXT,
  attendees  INTEGER,
  show_as    TEXT,
  cancelled  INTEGER NOT NULL DEFAULT 0,
  all_day    INTEGER NOT NULL DEFAULT 0,
  status     TEXT NOT NULL CHECK (status IN ('counted','skipped','unclassified')),
  client     TEXT,
  why        TEXT,
  pulled_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS meetings_start ON meetings(start_ts);

CREATE TABLE IF NOT EXISTS meeting_rules (
  id         TEXT PRIMARY KEY,
  kind       TEXT NOT NULL CHECK (kind IN ('subject','domain','skip','skip_org')),
  pattern    TEXT NOT NULL,
  client     TEXT,
  created_at INTEGER NOT NULL
);

-- Next steps a finished task named in its result. Each is owed a resolution:
-- 'tasked' (resolved_by = the child task) or 'dropped' (resolved_by = the
-- decision saying why). Open rows are the board's loose ends. 'deferred'
-- (resolved_by = the decision naming its trigger) is still owed but parked off
-- the board until it is reopened or resolved.
CREATE TABLE IF NOT EXISTS follow_ups (
  id          TEXT PRIMARY KEY,
  task_id     TEXT NOT NULL REFERENCES tasks(id),
  idx         INTEGER NOT NULL,
  who         TEXT NOT NULL,
  what        TEXT NOT NULL,
  state       TEXT NOT NULL CHECK (state IN ('open','tasked','dropped','deferred')),
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

-- Prompts the operator queued for the master from the dashboard (a button
-- press). The master-console mod takes them one at a time and submits each as
-- a prompt once the master is idle. `action` is from a fixed set; the text is
-- built here, never taken from the request, so a page cannot write the prompt.
CREATE TABLE IF NOT EXISTS operator_prompts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  action     TEXT NOT NULL,
  task_id    TEXT,
  text       TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  taken_at   INTEGER
);
CREATE INDEX IF NOT EXISTS operator_prompts_open ON operator_prompts(taken_at, created_at);

-- The question master is putting to Ben (CLAUDE.md "One question at a time"),
-- so each option can be a button in the master-console band and on the task
-- page. One is current: presenting another closes the last. closed_at is set
-- when an option is pressed or the question is superseded.
CREATE TABLE IF NOT EXISTS questions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id      TEXT NOT NULL,
  follow_up_id TEXT,
  question     TEXT NOT NULL,
  options      TEXT NOT NULL,
  recommended  INTEGER,
  created_at   INTEGER NOT NULL,
  closed_at    INTEGER
);
CREATE INDEX IF NOT EXISTS questions_open ON questions(closed_at, created_at);

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


# Task order: task_id waits on blocked_by. Kept out of DDL on purpose, and
# created on first use instead: folding it in means bumping SCHEMA_VERSION,
# and every coord server still running the old code would then see a version
# it does not know, re-run its DDL and write its own number back, so the two
# would ping-pong a full init on every guard-hook call until those sessions
# restart. It moves into DDL, with the bump, when agents get the tool side
# (docs/task-dependencies.md).
DEPS_DDL = """
CREATE TABLE IF NOT EXISTS task_deps (
  task_id    TEXT NOT NULL REFERENCES tasks(id),
  blocked_by TEXT NOT NULL REFERENCES tasks(id),
  note       TEXT,
  created_at INTEGER NOT NULL,
  cleared_at INTEGER,
  PRIMARY KEY (task_id, blocked_by)
);
CREATE INDEX IF NOT EXISTS task_deps_blocker ON task_deps(blocked_by);
"""


def ensure_deps(conn: sqlite3.Connection) -> None:
    """Create task_deps if this file has none yet. One indexed read when it
    exists, so every reader can call it."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_deps'").fetchone() is None:
        conn.executescript(DEPS_DDL)


# Deliverables Ben promoted into a client's deliverables folder. Created on
# first use, like task_deps and for the same reason: no SCHEMA_VERSION bump
# while older servers are still running.
PROMOTIONS_DDL = """
CREATE TABLE IF NOT EXISTS promotions (
  id         TEXT PRIMARY KEY,
  task_id    TEXT NOT NULL REFERENCES tasks(id),
  review_id  TEXT NOT NULL REFERENCES tasks(id),
  client     TEXT NOT NULL,
  src        TEXT NOT NULL,
  dest       TEXT NOT NULL,
  version    INTEGER NOT NULL,
  sha256     TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS promotions_task ON promotions(task_id);
"""


def ensure_prompt_taker(conn: sqlite3.Connection) -> None:
    """Add operator_prompts.taker (who took it: master, desk, or NULL from
    before the column) on first use, for the same reason as task_deps: no
    SCHEMA_VERSION bump while older servers are still running."""
    if "taker" not in {r[1] for r in conn.execute("PRAGMA table_info(operator_prompts)")}:
        conn.execute("ALTER TABLE operator_prompts ADD COLUMN taker TEXT")


def ensure_promotions(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='promotions'").fetchone() is None:
        conn.executescript(PROMOTIONS_DDL)


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations. CREATE TABLE IF NOT EXISTS won't add a column to a
    table that already exists, so do it here."""
    task_cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    if "picked_up_at" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN picked_up_at INTEGER")
    if "pipeline_task_id" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN pipeline_task_id TEXT")
    if "result_read_at" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN result_read_at INTEGER")
        # No read was recorded before this column existed, so every result on
        # file would count as unread and the first minder prompt would list
        # the whole history. Treat what was finished before now as read.
        conn.execute("UPDATE tasks SET result_read_at = completed_at WHERE completed_at IS NOT NULL")
    # v8 added 'deferred' to follow_ups.state. SQLite cannot alter a CHECK, so
    # rebuild the table from the current DDL and copy the rows across.
    fu_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='follow_ups'").fetchone()
    if fu_sql and "'deferred'" not in fu_sql[0]:
        ddl = DDL[DDL.index("CREATE TABLE IF NOT EXISTS follow_ups"):]
        ddl = ddl[:ddl.index("CREATE TABLE IF NOT EXISTS intents")]
        table, *indexes = [x.strip() for x in ddl.split(";") if x.strip()]
        # Build beside the old table and rename into place: renaming the old
        # one away would carry its indexes with it and leave the new one bare.
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(table.replace("IF NOT EXISTS follow_ups", "follow_ups_v8"))
            conn.execute("INSERT INTO follow_ups_v8 SELECT * FROM follow_ups")
            conn.execute("DROP TABLE follow_ups")
            conn.execute("ALTER TABLE follow_ups_v8 RENAME TO follow_ups")
            for ix in indexes:
                conn.execute(ix)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
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
