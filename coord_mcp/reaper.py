"""Idle-session reaper: end the Claude Code CLI process of idle client sessions.

Each Claude Code session keeps its own `claude.exe` (~600 MB private) loaded
after its turn ends, indefinitely, and the app's stop_session does nothing to
an idle one. Ending that process tree frees the memory; the desktop app starts
a new `claude.exe --resume=<uuid>` on the session's next message, with its
context intact (tested on Cascade, 2026-11-20). The one side effect seen: the
session's Remote Control switch turns off, so sessions with it on are kept
unless switch idle_reaper_keep_rc is off.

How a process is tied to a session. Every coord stdio server records, at
start, its own pid, its parent's pid and the session it serves
(CLAUDE_CODE_HOST_SESSION_ID, the app's local_<id>; CLAUDE_CODE_SESSION_ID,
the CLI's transcript uuid) in session_procs (record_self). The server runs
under the venv launcher, so its nearest `claude.exe` ancestor is the session's
CLI. When no row names the session, the app's own session file
(claude-code-sessions/**/local_<id>.json, cliSessionId) is matched against a
`claude.exe --resume=<uuid>` command line.

A process is ended only when every one of these holds:
  - its session is in the client registry (sessions.registry: primary and
    side sessions). Never Master, the desk, repo or server sessions,
    scheduled runs, or anything else;
  - no task is in flight for that session on the board (sessions._busy_sessions);
  - the session has been idle at least setting idle_reaper_minutes: its
    transcript .jsonl and the app's lastActivityAt are both that old;
  - no tool is running under it (no shell, git or ssh child process);
  - Remote Control is off for it (unless switch idle_reaper_keep_rc is off);
  - the pid is still a Claude Code CLI `claude.exe` (not the desktop app's
    WindowsApps Claude.exe) at the moment of the kill, so a reused pid is
    never touched.

Switch idle_reaper (off by default) is the real thing; off, every tick is a
dry run that logs what it would end and ends nothing. The dashboard ticks it
every setting idle_reaper_interval minutes. Every tick writes to
~/.coord/reaper.log (next to the board).

    python -m coord_mcp.reaper              # dry run now: what it would end, and why the rest stay
    python -m coord_mcp.reaper --yes        # end them now (whatever the switch says)
    python -m coord_mcp.reaper log [-n 40]  # the tail of reaper.log
    python -m coord_mcp.queue switch idle_reaper on      # let the dashboard end them
    python -m coord_mcp.queue setting idle_reaper_minutes 20
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .db import default_db_path, ensure_dispatch, now

PROCS_DDL = """
CREATE TABLE IF NOT EXISTS session_procs (
  server_pid      INTEGER PRIMARY KEY,
  parent_pid      INTEGER,
  claude_pid      INTEGER,
  host_session_id TEXT,
  cli_session_id  TEXT,
  cwd             TEXT,
  started_at      INTEGER NOT NULL
);
"""

# A child of these names means a tool is running in that session right now.
TOOL_PROCS = frozenset({"bash.exe", "sh.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "git.exe",
                        "ssh.exe", "scp.exe", "sftp.exe", "winscp.com", "winscp.exe", "_progres.exe",
                        "prowin.exe", "prowin32.exe", "npm.exe", "node.exe"})
LOG_MAX = 1_000_000          # reaper.log is cut to its newer half past this
_RESUME = re.compile(r"--resume[= ]\"?([0-9a-fA-F-]{36})")
_NO_WINDOW = 0x08000000


# ------------------------------------------------------------ the map

def ensure_procs(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_procs'").fetchone() is None:
        conn.executescript(PROCS_DDL)


def record_self(conn: sqlite3.Connection | None = None, env: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Called once as a coord stdio server starts: this server's pid, its
    parent's, and the session it serves. Nothing recorded outside a Claude
    Code session (no session id in the environment)."""
    env = os.environ if env is None else env
    host = (env.get("CLAUDE_CODE_HOST_SESSION_ID") or "").strip() or None
    cli = (env.get("CLAUDE_CODE_SESSION_ID") or "").strip() or None
    if not host and not cli:
        return None
    try:
        claude_pid = int(env.get("CLAUDE_PID") or 0) or None
    except ValueError:
        claude_pid = None
    row = {"server_pid": os.getpid(), "parent_pid": os.getppid(), "claude_pid": claude_pid,
           "host_session_id": host, "cli_session_id": cli, "cwd": os.getcwd(), "started_at": now()}
    own = conn is None
    if own:
        from .db import connect
        conn = connect()
    try:
        ensure_procs(conn)
        conn.execute("INSERT OR REPLACE INTO session_procs(server_pid, parent_pid, claude_pid, host_session_id, "
                     "cli_session_id, cwd, started_at) VALUES (:server_pid,:parent_pid,:claude_pid,"
                     ":host_session_id,:cli_session_id,:cwd,:started_at)", row)
        conn.commit()
    finally:
        if own:
            conn.close()
    return row


def record_self_background() -> None:
    """record_self on a daemon thread, so it never delays the MCP handshake;
    a failure (a locked board) only costs this session's row."""
    if not (os.environ.get("CLAUDE_CODE_HOST_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID")):
        return
    import threading

    def run() -> None:
        try:
            record_self()
        except Exception:  # noqa: BLE001 - never stops the server
            pass
    threading.Thread(target=run, name="record-session", daemon=True).start()


# ------------------------------------------------------------ the machine

def list_processes() -> dict[int, dict[str, Any]]:
    """pid -> {pid, ppid, name, cmd, mb} for every process (Windows, via CIM).
    Empty anywhere else, or on any failure: no listing, no kill."""
    if os.name != "nt":
        return {}
    ps = ("Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine,"
          "PrivatePageCount | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, timeout=90, creationflags=_NO_WINDOW).stdout
        rows = json.loads(out.decode("utf-8", "replace") or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    if isinstance(rows, dict):
        rows = [rows]
    return {int(r["ProcessId"]): {"pid": int(r["ProcessId"]), "ppid": int(r.get("ParentProcessId") or 0),
                                  "name": r.get("Name") or "", "cmd": r.get("CommandLine") or "",
                                  "mb": int(r.get("PrivatePageCount") or 0) / 1048576}
            for r in rows if r.get("ProcessId")}


def is_cli_claude(p: dict[str, Any] | None) -> bool:
    """A Claude Code CLI process: claude.exe, not the desktop app's WindowsApps one."""
    if not p or (p.get("name") or "").lower() != "claude.exe":
        return False
    cmd = (p.get("cmd") or "").lower()
    return "windowsapps" not in cmd and ("claude-code" in cmd or "stream-json" in cmd)


def claude_ancestor(procs: dict[int, dict[str, Any]], pid: int | None) -> int | None:
    """The nearest CLI claude.exe at or above pid (the coord server runs
    under the venv launcher, so it is usually the grandparent)."""
    seen: set[int] = set()
    while pid and pid in procs and pid not in seen:
        seen.add(pid)
        if is_cli_claude(procs[pid]):
            return pid
        pid = procs[pid]["ppid"]
    return None


def descendants(procs: dict[int, dict[str, Any]], pid: int) -> list[dict[str, Any]]:
    kids: dict[int, list[int]] = {}
    for p in procs.values():
        kids.setdefault(p["ppid"], []).append(p["pid"])
    out, todo = [], list(kids.get(pid, []))
    while todo:
        c = todo.pop()
        if c in procs and c != pid:
            out.append(procs[c])
            todo.extend(kids.get(c, []))
    return out


def kill_tree(pid: int) -> tuple[bool, str]:
    try:
        r = subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=30,
                           creationflags=_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return r.returncode == 0, (r.stdout or r.stderr or b"").decode("utf-8", "replace").strip()[:200]


# ------------------------------------------------------------ the app's files

def app_sessions_dir() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Claude" / "claude-code-sessions"


def projects_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def app_session(sid: str, root: Path | None = None) -> dict[str, Any]:
    """The desktop app's own record of one session ({} when there is none)."""
    root = root or app_sessions_dir()
    for f in root.glob(f"*/*/{sid}.json"):
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {}


def project_slug(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def transcript_mtime(cwd: str | None, cli: str | None, root: Path | None = None) -> float | None:
    if not cwd or not cli:
        return None
    f = (root or projects_dir()) / project_slug(cwd) / f"{cli}.jsonl"
    try:
        return f.stat().st_mtime
    except OSError:
        return None


# ------------------------------------------------------------ choosing

def _settings(conn: sqlite3.Connection) -> tuple[bool, bool, float, float]:
    from .queue import setting, switch
    return (switch(conn, "idle_reaper"), switch(conn, "idle_reaper_keep_rc"),
            setting(conn, "idle_reaper_minutes"), setting(conn, "idle_reaper_interval"))


def plan(conn: sqlite3.Connection, procs: dict[int, dict[str, Any]], now_ts: float | None = None, *,
         idle_minutes: float = 15.0, keep_rc: bool = True,
         app: Callable[[str], dict[str, Any]] | None = None,
         mtime: Callable[[str | None, str | None], float | None] | None = None) -> list[dict[str, Any]]:
    """One row per registered client session: {client, role, session_id, pid,
    mb, idle_min, end, why}. end True only when every rule in the module
    docstring holds. Reads only."""
    from .sessions import _busy_sessions, list_sessions
    t = time.time() if now_ts is None else now_ts
    app = app or app_session
    mtime = mtime or transcript_mtime
    ensure_dispatch(conn)
    ensure_procs(conn)
    rows = list_sessions(conn)
    reg: dict[str, dict[str, str]] = {}
    for r in rows:
        reg.setdefault(r["client"].casefold(), {})[r["role"]] = r["session_id"]
    busy = _busy_sessions(conn, reg, int(t))
    recorded = [dict(r) for r in conn.execute("SELECT * FROM session_procs ORDER BY started_at DESC")]
    out = []
    for r in rows:
        sid = r["session_id"]
        a = app(sid) or {}
        cli = a.get("cliSessionId")
        row: dict[str, Any] = {"client": r["client"], "role": r["role"], "session_id": sid, "pid": None,
                               "mb": 0, "idle_min": None, "end": False}
        out.append(row)
        pids = []
        for p in recorded:
            if p["host_session_id"] == sid or (cli and p["host_session_id"] is None and p["cli_session_id"] == cli):
                c = claude_ancestor(procs, p["server_pid"]) if p["server_pid"] in procs else None
                if c is None and p["claude_pid"] in procs and is_cli_claude(procs[p["claude_pid"]]):
                    c = p["claude_pid"]
                if c and c not in pids:
                    pids.append(c)
        if not pids and cli:
            pids = [q["pid"] for q in procs.values()
                    if is_cli_claude(q) and (m := _RESUME.search(q["cmd"])) and m.group(1).lower() == cli.lower()]
        if not pids:
            row["why"] = "no process (already unloaded)"
            continue
        if len(pids) > 1:
            row["pid"] = pids[0]
            row["why"] = f"more than one process ({', '.join(map(str, pids))}): left alone"
            continue
        pid = row["pid"] = pids[0]
        tree = [procs[pid], *descendants(procs, pid)]
        row["mb"] = round(sum(p["mb"] for p in tree))
        stamps = [s for s in (mtime(a.get("cwd"), cli), (a.get("lastActivityAt") or 0) / 1000 or None) if s]
        if stamps:
            row["idle_min"] = round((t - max(stamps)) / 60)
        if busy.get(sid):
            row["why"] = "task in flight: " + ", ".join(sorted(busy[sid]))
        elif keep_rc and a.get("remoteControl"):
            row["why"] = "Remote Control is on (switch idle_reaper_keep_rc)"
        elif tools := sorted({p["name"] for p in tree[1:] if p["name"].lower() in TOOL_PROCS}):
            row["why"] = "a tool is running: " + ", ".join(tools)
        elif row["idle_min"] is None:
            row["why"] = "no transcript or activity time found: left alone"
        elif row["idle_min"] < idle_minutes:
            row["why"] = f"active {row['idle_min']} min ago (< {idle_minutes:g})"
        else:
            row["end"] = True
            row["why"] = f"idle {row['idle_min']} min"
    return out


# ------------------------------------------------------------ running it

def log_path() -> Path:
    return default_db_path().parent / "reaper.log"


def _log(lines: list[str], path: Path | None = None) -> None:
    path = path or log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_MAX:
            data = path.read_bytes()
            path.write_bytes(data[len(data) // 2:].split(b"\n", 1)[-1])
        with path.open("a", encoding="utf-8") as f:
            f.writelines(line + "\n" for line in lines)
    except OSError:
        pass


def run(conn: sqlite3.Connection, *, kill: bool | None = None, procs: dict[int, dict[str, Any]] | None = None,
        killer: Callable[[int], tuple[bool, str]] = kill_tree,
        lister: Callable[[], dict[int, dict[str, Any]]] = list_processes, log: Path | None = None,
        **plan_kw: Any) -> dict[str, Any]:
    """One pass. kill None follows switch idle_reaper; False is a dry run.
    Every pass appends to reaper.log: one line per session it ends (or would
    end) and one summary line."""
    on, keep_rc, minutes, _ = _settings(conn)
    kill = on if kill is None else kill
    procs = lister() if procs is None else procs
    rows = plan(conn, procs, idle_minutes=plan_kw.pop("idle_minutes", minutes),
                keep_rc=plan_kw.pop("keep_rc", keep_rc), **plan_kw)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    lines, ended, freed = [], [], 0
    # The pid guard: list again right before killing, so a pid that ended and
    # was reused since the plan is never touched.
    fresh = lister() if kill and any(r["end"] for r in rows) else procs
    for r in rows:
        if not r["end"]:
            continue
        who = f"{r['client']} {r['role']} {r['session_id']} pid {r['pid']} ({r['why']}, ~{r['mb']} MB)"
        if not kill:
            lines.append(f"{stamp} DRY   would end {who}")
            continue
        if not is_cli_claude(fresh.get(r["pid"])) or fresh[r["pid"]]["cmd"] != procs[r["pid"]]["cmd"]:
            r["end"], r["why"] = False, "pid no longer a Claude Code claude.exe: left alone"
            lines.append(f"{stamp} SKIP  {who}: pid changed")
            continue
        ok, detail = killer(r["pid"])
        r["ended"] = ok
        if ok:
            ended.append(r)
            freed += r["mb"]
            lines.append(f"{stamp} ENDED {who}")
        else:
            lines.append(f"{stamp} FAIL  {who}: {detail}")
    if not procs:
        lines.append(f"{stamp} no process listing (not Windows, or CIM failed): nothing ended")
    want = sum(r["end"] for r in rows)
    lines.append(f"{stamp} {'live' if kill else 'dry run'}: {len(rows)} registered, "
                 f"{len(ended) if kill else want} {'ended' if kill else 'would end'}"
                 + (f", ~{freed} MB freed" if kill else "")
                 + f"; idle >= {minutes:g} min")
    _log(lines, log)
    _prune(conn, procs)
    return {"live": kill, "rows": rows, "ended": len(ended), "freed_mb": freed}


def _prune(conn: sqlite3.Connection, procs: dict[int, dict[str, Any]]) -> None:
    """Drop map rows whose server is gone (only with a real listing)."""
    if not procs:
        return
    try:
        dead = [r[0] for r in conn.execute("SELECT server_pid FROM session_procs") if r[0] not in procs]
        if dead:
            conn.executemany("DELETE FROM session_procs WHERE server_pid=?", [(p,) for p in dead])
            conn.commit()
    except sqlite3.Error:
        pass


def tick(conn: sqlite3.Connection, now_ts: int | None = None) -> dict[str, Any] | None:
    """The dashboard's call: a pass every idle_reaper_interval minutes, else None."""
    t = now() if now_ts is None else now_ts
    _, _, _, every = _settings(conn)
    r = conn.execute("SELECT value FROM meta WHERE key='reaper:last_tick'").fetchone()
    if r is not None and t - int(r["value"] or 0) < every * 60:
        return None
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('reaper:last_tick', ?)", (str(t),))
    conn.commit()
    return run(conn)


# ------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    from .db import connect
    p = argparse.ArgumentParser(prog="python -m coord_mcp.reaper",
                                description="End idle client sessions' claude.exe to free memory.")
    p.add_argument("cmd", nargs="?", choices=["run", "log"], default="run")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="list only (the default)")
    g.add_argument("--yes", action="store_true", help="end them now, whatever switch idle_reaper says")
    p.add_argument("-n", type=int, default=40, help="log lines to show")
    a = p.parse_args(argv)
    if a.cmd == "log":
        try:
            print("\n".join(log_path().read_text(encoding="utf-8").splitlines()[-a.n:]))
        except OSError:
            print(f"no log yet at {log_path()}")
        return 0
    conn = connect()
    try:
        on, keep_rc, minutes, every = _settings(conn)
        res = run(conn, kill=a.yes)
    finally:
        conn.close()
    for r in res["rows"]:
        mark = ("ENDED" if r.get("ended") else "end") if r["end"] else "keep"
        print(f"{mark:<6}{r['client']:<10} {r['role']:<8} {r['session_id']}  pid {r['pid'] or '-':<6} "
              f"{r['mb']:>5} MB  {r['why']}")
    print(f"{'live' if res['live'] else 'dry run'}; switch idle_reaper {'on' if on else 'off'}, "
          f"idle >= {minutes:g} min, every {every:g} min, keep RC {'on' if keep_rc else 'off'}. Log: {log_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
