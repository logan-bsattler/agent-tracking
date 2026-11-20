"""The idle-session reaper (coord_mcp/reaper.py): which claude.exe it may
end. Process listing, the kill, the app's session files and transcript times
are all fakes; nothing here touches a real process."""

from __future__ import annotations

import time

import pytest

from coord_mcp import queue, reaper, sessions, store
from coord_mcp.db import connect

CLI = r"C:\Users\b\AppData\Roaming\Claude\claude-code\2.1.326\x\claude.exe --output-format stream-json"
APP = r'"C:\Program Files\WindowsApps\Claude_6.6395.0.0_x64__p\app\Claude.exe"'
T = 2_000_000_000.0
HOUR_AGO = T - 3600


def _proc(pid, ppid, name, cmd="", mb=500.0):
    return {"pid": pid, "ppid": ppid, "name": name, "cmd": cmd, "mb": mb}


def _machine(*extra):
    """App 1; MAG's CLI 100 (coord server 102 under launcher 101); a CLI 200
    that belongs to Master (not registered); the dashboard's own python 300."""
    ps = [_proc(1, 0, "Claude.exe", APP, 400),
          _proc(100, 1, "claude.exe", CLI, 600), _proc(101, 100, "python.exe", "python -m coord_mcp", 2),
          _proc(102, 101, "python3.13.exe", "python -m coord_mcp", 22), _proc(103, 100, "conhost.exe", "", 2),
          _proc(200, 1, "claude.exe", CLI, 600), _proc(201, 200, "python.exe", "python -m coord_mcp", 2),
          _proc(202, 201, "python3.13.exe", "python -m coord_mcp", 22),
          _proc(300, 0, "pythonw.exe", "pythonw -m coord_mcp report --serve", 80), *extra]
    return {p["pid"]: p for p in ps}


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    sessions.set_session(c, "MAG", "primary", "local_mag")
    reaper.ensure_procs(c)
    for server, host in ((102, "local_mag"), (202, "local_master")):
        c.execute("INSERT INTO session_procs(server_pid, parent_pid, claude_pid, host_session_id, cli_session_id, "
                  "cwd, started_at) VALUES (?,?,?,?,?,?,?)", (server, server - 1, None, host, None, "C:\\x", 0))
    c.commit()
    yield c
    c.close()


def _plan(conn, procs=None, *, app=None, last=HOUR_AGO, **kw):
    kw.setdefault("idle_minutes", 15)
    kw.setdefault("keep_rc", True)
    return {r["session_id"]: r for r in reaper.plan(
        conn, _machine() if procs is None else procs, T,
        app=app or (lambda sid: {}), mtime=lambda cwd, cli: last, **kw)}


def test_an_idle_registered_session_is_chosen_with_its_tree_size(conn):
    r = _plan(conn, app=lambda sid: {"cwd": "C:\\x", "cliSessionId": "u"})["local_mag"]
    assert r["end"] and r["pid"] == 100
    assert r["mb"] == 626  # claude.exe + launcher + server + conhost
    assert r["idle_min"] == 60


def test_only_registry_sessions_are_considered(conn):
    rows = _plan(conn)
    assert set(rows) == {"local_mag"}  # Master's CLI 200 is never a row, let alone ended
    assert all(r["pid"] != 200 for r in rows.values())


def test_a_session_with_a_task_in_flight_is_kept(conn):
    tid = store.create_task(conn, "investigation", "t", {"q": "x"}, assigned_to="MAG")["task_id"]
    store.get_task(conn, tid, reader="MAG")  # picked up: in flight
    r = _plan(conn)["local_mag"]
    assert not r["end"] and r["why"].startswith("task in flight") and tid in r["why"]


def test_recent_activity_keeps_it(conn):
    r = _plan(conn, last=T - 10 * 60)["local_mag"]
    assert not r["end"] and "active 10 min ago" in r["why"]
    # the app's lastActivityAt counts too, even with an old transcript
    r = _plan(conn, app=lambda sid: {"lastActivityAt": (T - 60) * 1000})["local_mag"]
    assert not r["end"]
    assert _plan(conn, last=T - 16 * 60)["local_mag"]["end"]


def test_no_activity_time_at_all_keeps_it(conn):
    assert not _plan(conn, last=None)["local_mag"]["end"]


def test_a_running_tool_keeps_it(conn):
    procs = _machine(_proc(110, 100, "cmd.exe"), _proc(111, 110, "powershell.exe"))
    r = _plan(conn, procs)["local_mag"]
    assert not r["end"] and "tool is running" in r["why"]


def test_remote_control_keeps_it_unless_switched_off(conn):
    app = lambda sid: {"remoteControl": True}  # noqa: E731
    assert not _plan(conn, app=app)["local_mag"]["end"]
    assert _plan(conn, app=app, keep_rc=False)["local_mag"]["end"]


def test_the_pid_must_be_a_cli_claude(conn):
    # the recorded server's ancestor is the desktop app, not a CLI claude.exe
    procs = _machine()
    procs[100] = _proc(100, 1, "Claude.exe", APP)
    assert _plan(conn, procs)["local_mag"]["pid"] is None
    # a reused pid: something else entirely
    procs[100] = _proc(100, 1, "notepad.exe", "notepad")
    assert not _plan(conn, procs)["local_mag"]["end"]
    assert not reaper.is_cli_claude(_proc(5, 1, "claude.exe", APP))
    assert reaper.is_cli_claude(_proc(5, 1, "claude.exe", CLI))


def test_server_gone_means_no_process(conn):
    procs = {k: v for k, v in _machine().items() if k not in (101, 102)}
    r = _plan(conn, procs)["local_mag"]
    assert r["pid"] is None and not r["end"]


def test_fallback_matches_the_apps_cli_id_to_a_resume_command_line(conn):
    conn.execute("DELETE FROM session_procs")
    uuid = "dd803db1-54c4-43d1-9688-b81ba484a1a2"
    procs = _machine()
    procs[100]["cmd"] = CLI + f" --resume={uuid}"
    r = _plan(conn, procs, app=lambda sid: {"cliSessionId": uuid} if sid == "local_mag" else {})["local_mag"]
    assert r["end"] and r["pid"] == 100
    # a different uuid matches nothing
    r = _plan(conn, procs, app=lambda sid: {"cliSessionId": "0" * 8 + uuid[8:]})["local_mag"]
    assert r["pid"] is None


def test_run_dry_kills_nothing_and_logs(conn, tmp_path):
    killed = []
    log = tmp_path / "reaper.log"
    res = reaper.run(conn, kill=False, procs=_machine(), killer=lambda pid: killed.append(pid) or (True, ""),
                     lister=_machine, log=log, idle_minutes=15, keep_rc=True,
                     app=lambda sid: {}, mtime=lambda cwd, cli: time.time() - 3600)
    assert killed == [] and res["ended"] == 0 and not res["live"]
    text = log.read_text(encoding="utf-8")
    assert "DRY   would end MAG primary local_mag pid 100" in text and "dry run: 1 registered, 1 would end" in text


def test_run_live_kills_the_tree_and_rechecks_the_pid(conn, tmp_path):
    killed = []
    kw = dict(killer=lambda pid: killed.append(pid) or (True, "ok"), log=tmp_path / "r.log",
              idle_minutes=15, keep_rc=True, app=lambda sid: {}, mtime=lambda cwd, cli: time.time() - 3600)
    res = reaper.run(conn, kill=True, procs=_machine(), lister=_machine, **kw)
    assert killed == [100] and res["ended"] == 1 and res["freed_mb"] == 626
    # the pid was reused between the plan and the kill: left alone
    killed.clear()
    reused = _machine()
    reused[100] = _proc(100, 1, "notepad.exe", "notepad")
    res = reaper.run(conn, kill=True, procs=_machine(), lister=lambda: reused, **kw)
    assert killed == [] and res["ended"] == 0


def test_switch_off_by_default_means_dry_run(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(queue, "SWITCHES", {**queue.SWITCHES, "idle_reaper": False, "idle_reaper_keep_rc": True})
    killed = []
    res = reaper.run(conn, procs=_machine(), lister=_machine, killer=lambda pid: killed.append(pid) or (True, ""),
                     log=tmp_path / "r.log", app=lambda sid: {}, mtime=lambda cwd, cli: time.time() - 3600)
    assert not res["live"] and killed == []


def test_tick_runs_once_per_interval(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(reaper, "run", lambda c: calls.append(1) or {})
    t = 1_000_000
    assert reaper.tick(conn, t) == {}
    assert reaper.tick(conn, t + 60) is None  # inside the 5-minute interval
    assert reaper.tick(conn, t + 5 * 60) == {}
    assert len(calls) == 2


def test_record_self_needs_a_session_and_maps_it(conn):
    assert reaper.record_self(conn, env={}) is None
    row = reaper.record_self(conn, env={"CLAUDE_CODE_HOST_SESSION_ID": "local_x", "CLAUDE_PID": "77",
                                        "CLAUDE_CODE_SESSION_ID": "u-1"})
    got = conn.execute("SELECT * FROM session_procs WHERE server_pid=?", (row["server_pid"],)).fetchone()
    assert got["host_session_id"] == "local_x" and got["claude_pid"] == 77 and got["cli_session_id"] == "u-1"


def test_project_slug_matches_claude_codes_folder_names():
    assert reaper.project_slug(r"C:\development\agents\agent-tracking") == "C--development-agents-agent-tracking"
