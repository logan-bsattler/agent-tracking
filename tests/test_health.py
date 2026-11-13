"""Health: the ingest/minder loop's heartbeat and last error, and the hourly
triage log. Silent when all is well; each problem shows alone; the triage
checks read a log folder in the shapes the real routine writes."""

from __future__ import annotations

from datetime import datetime

import pytest

from coord_mcp import report, store
from coord_mcp.db import connect

MIN = 60
# Thursday 2026-11-12, inside the weekday 07:00-18:30 window.
NOW = int(datetime(2026, 11, 12, 14, 30).timestamp())

GOOD = """## {hhmm} run
- email 04:24Z Someone (Logan) — LNK — "Re: a subject": FYI; no intent
- wake: error: undelivered (no start within 20s; Master may pick it up later)
- counts: emails seen 1 / intents proposed 0
"""
BAD = """## {hhmm} run
- error: M365 connector unavailable — outlook_email_search returned Graph 502 UnknownError on 3 tries
- coord reachable (0 open intents). No intents proposed; no wake sent.
"""


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _log(tmp_path, day: str, *runs: tuple[str, str]):
    d = tmp_path / "log"
    d.mkdir(exist_ok=True)
    (d / f"{day}.md").write_text("\n".join(t.format(hhmm=h) for h, t in runs), encoding="utf-8")
    return d


def _keys(problems):
    return [p["key"] for p in problems]


def test_healthy(conn, tmp_path):
    d = _log(tmp_path, "2026-11-12", ("12:09", BAD), ("13:09", GOOD), ("14:09", GOOD))
    store.heartbeat(conn, NOW - 20)
    assert store.health(conn, NOW, log_dir=d) == []
    assert store.heartbeat_age(conn, NOW) == 20
    assert report.health_banner([]) == ""


def test_stale_heartbeat(conn, tmp_path):
    d = _log(tmp_path, "2026-11-12", ("14:09", GOOD))
    assert _keys(store.health(conn, NOW, log_dir=d)) == ["heartbeat"]  # never stamped
    store.heartbeat(conn, NOW - store.HEARTBEAT_STALE)
    assert store.health(conn, NOW, log_dir=d) == []
    store.heartbeat(conn, NOW - store.HEARTBEAT_STALE - 61)
    p = store.health(conn, NOW, log_dir=d)
    assert _keys(p) == ["heartbeat"] and "6 min" in p[0]["text"]


def test_loop_error_recorded(conn, tmp_path):
    d = _log(tmp_path, "2026-11-12", ("14:09", GOOD))
    store.heartbeat(conn, NOW)
    store.record_loop_error(conn, "minder", "KeyError: 'x'\n  second line", t=NOW - 10 * MIN)
    p = store.health(conn, NOW, log_dir=d)
    assert _keys(p) == ["loop_error"]
    assert "minder" in p[0]["text"] and p[0]["detail"] == "KeyError: 'x' second line"
    assert store.health(conn, NOW + 21 * MIN, log_dir=d)[1:] == []  # only the heartbeat, by then stale
    store.heartbeat(conn, NOW + 31 * MIN)
    _log(tmp_path, "2026-11-12", ("15:00", GOOD))
    assert store.health(conn, NOW + 31 * MIN, log_dir=d) == []


def test_loop_error_logs_and_keeps_going(tmp_path):
    db, log = tmp_path / "t.db", tmp_path / "errors.log"
    connect(db).close()
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        report.loop_error(lambda: connect(db), "minder", log=log)
    assert "RuntimeError: boom" in log.read_text(encoding="utf-8")
    c = connect(db)
    try:
        store.heartbeat(c)
        p = store.health(c, log_dir=tmp_path / "none")
        assert _keys(p) == ["loop_error"] and "boom" in p[0]["detail"]
    finally:
        c.close()
    # Over the cap nothing more is written, and a connect that fails is swallowed.
    log.write_bytes(b"x" * (report.ERROR_LOG_MAX + 1))

    def broken():
        raise OSError("no db")
    try:
        raise RuntimeError("again")
    except RuntimeError:
        report.loop_error(broken, "ingest", log=log)
    assert log.stat().st_size == report.ERROR_LOG_MAX + 1


def test_two_failed_triage_runs(conn, tmp_path):
    store.heartbeat(conn, NOW)
    d = _log(tmp_path, "2026-11-12", ("11:09", GOOD), ("12:09", BAD), ("13:09", GOOD), ("14:09", BAD))
    assert store.health(conn, NOW, log_dir=d) == []  # one failure, then one more: not in a row
    d = _log(tmp_path, "2026-11-12", ("11:09", GOOD), ("13:09", BAD), ("14:09", BAD))
    p = store.health(conn, NOW, log_dir=d)
    assert _keys(p) == ["triage_failing"]
    assert p[0]["text"] == "triage failing: 2 runs since 13:09" and "M365 connector" in p[0]["detail"]
    # The streak carries over midnight, and a good run ends it.
    _log(tmp_path, "2026-11-13", ("07:09", BAD))
    nxt = int(datetime(2026, 11, 13, 7, 30).timestamp())
    store.heartbeat(conn, nxt)
    assert store.health(conn, nxt, log_dir=d)[0]["text"] == "triage failing: 3 runs since 11-12 13:09"
    _log(tmp_path, "2026-11-13", ("07:09", BAD), ("07:20", GOOD))
    assert store.health(conn, nxt, log_dir=d) == []
    banner = report.health_banner(p)
    assert 'id="health"' in banner and "triage failing: 2 runs since 13:09" in banner


def test_quiet_only_inside_the_schedule_window(conn, tmp_path):
    d = _log(tmp_path, "2026-11-12", ("11:09", GOOD))

    def at(*ymdhm):
        t = int(datetime(*ymdhm).timestamp())
        store.heartbeat(conn, t)
        return _keys(store.health(conn, t, log_dir=d))

    assert at(2026, 11, 12, 13, 30) == []                # 2 h 21 min: not yet
    assert at(2026, 11, 12, 14, 30) == ["triage_quiet"]  # 3 h 21 min on a Thursday afternoon
    assert at(2026, 11, 12, 19, 0) == []                 # after 18:30
    assert at(2026, 11, 13, 9, 0) == []                  # Friday: the day's first run has until 09:30
    assert at(2026, 11, 13, 9, 45) == ["triage_quiet"]
    assert at(2026, 11, 14, 12, 0) == []                 # Saturday
    assert store.triage_trouble(NOW, log_dir=tmp_path / "absent") == []  # no routine on this machine


def test_board_page_shows_the_banner_only_when_unhealthy(conn):
    v = store.operator_view(conn)
    assert 'id="health"' not in report.board_page(v)
    v["health"] = [{"key": "heartbeat", "text": "minder loop stalled: no heartbeat for 9 min", "detail": ""}]
    assert "minder loop stalled: no heartbeat for 9 min" in report.board_page(v)
