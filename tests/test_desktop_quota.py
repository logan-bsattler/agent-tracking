"""Claude Desktop plan-usage ingestion, merged series, and limit attribution."""

from __future__ import annotations

import json

import pytest

from coord_mcp import usage
from coord_mcp.db import connect, now


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _file(tmp_path, samples, monkeypatch, version=2):
    p = tmp_path / "plan-usage-history.json"
    p.write_text(json.dumps({"version": version, "samples": samples}), encoding="utf-8")
    monkeypatch.setenv("COORD_DESKTOP_USAGE", str(p))
    return p


def _s(ms_ago_min, fh, sd=5):
    return {"t": (now() - ms_ago_min * 60) * 1000, "org": "o1", "u": {"fh": fh, "sd": sd}}


# ---------------------------------------------------------------- ingestion


def test_ingests_desktop_samples(conn, tmp_path, monkeypatch):
    _file(tmp_path, [_s(60, 10), _s(30, 40), _s(0, 70)], monkeypatch)
    assert usage.ingest_desktop_quota(conn) == 3
    rows = conn.execute("SELECT ts, source, five_hour_pct, seven_day_pct FROM quota_snapshots ORDER BY ts").fetchall()
    assert [r["source"] for r in rows] == ["desktop"] * 3
    assert [r["five_hour_pct"] for r in rows] == [10, 40, 70]
    assert rows[0]["seven_day_pct"] == 5


def test_ingest_is_idempotent(conn, tmp_path, monkeypatch):
    # Build the samples once: rebuilt, they are re-stamped from now(), and a
    # second ticking over in between turns two old samples into two new ones.
    old = [_s(60, 10), _s(30, 40)]
    _file(tmp_path, old, monkeypatch)
    assert usage.ingest_desktop_quota(conn) == 2
    assert usage.ingest_desktop_quota(conn) == 0
    _file(tmp_path, old + [_s(0, 70)], monkeypatch)
    assert usage.ingest_desktop_quota(conn) == 1


def test_missing_or_bad_file_is_not_an_error(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("COORD_DESKTOP_USAGE", str(tmp_path / "nope.json"))
    assert usage.ingest_desktop_quota(conn) == 0
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("COORD_DESKTOP_USAGE", str(bad))
    assert usage.ingest_desktop_quota(conn) == 0
    odd = tmp_path / "odd.json"
    odd.write_text(json.dumps({"version": 9, "samples": [{"t": "x"}, {}, {"t": 1, "u": {}}, 5]}), encoding="utf-8")
    monkeypatch.setenv("COORD_DESKTOP_USAGE", str(odd))
    assert usage.ingest_desktop_quota(conn) == 0


def test_seconds_and_millisecond_timestamps_both_work(conn, tmp_path, monkeypatch):
    t = now() - 600
    _file(tmp_path, [{"t": t * 1000, "u": {"fh": 5}}, {"t": t + 60, "u": {"fh": 6}}], monkeypatch)
    usage.ingest_desktop_quota(conn)
    got = [r["ts"] for r in conn.execute("SELECT ts FROM quota_snapshots ORDER BY ts")]
    assert got == [t, t + 60]


def test_read_bytes_falls_back_to_shell(tmp_path, monkeypatch):
    p = tmp_path / "f.json"
    p.write_text("hello", encoding="utf-8")
    assert usage._read_bytes(p) == b"hello"
    # Simulate the Store-Python AppContainer case: direct open raises, shell works.
    real = type(p).read_bytes

    def boom(self):
        raise OSError(2, "redirected")

    monkeypatch.setattr(type(p), "read_bytes", boom)
    try:
        assert b"hello" in (usage._read_bytes(p) or b"")
    finally:
        monkeypatch.setattr(type(p), "read_bytes", real)


# ------------------------------------------------------------------- series


def test_series_merges_sources_without_double_counting(conn):
    t = (now() - 3600) // 300 * 300  # bucket-aligned: t+10 must share t's 300s bucket
    conn.execute("INSERT INTO quota_snapshots(ts, source, five_hour_pct) VALUES (?,?,?)", (t, "desktop", 20))
    conn.execute("INSERT INTO quota_snapshots(ts, source, five_hour_pct) VALUES (?,?,?)", (t + 10, "statusline", 21))
    conn.execute("INSERT INTO quota_snapshots(ts, source, five_hour_pct) VALUES (?,?,?)", (t + 900, "desktop", 40))
    rows = usage.quota_series(conn, t - 60, now())
    assert [r["five_hour_pct"] for r in rows] == [21, 40]  # same bucket collapsed, max wins
    a = usage.attribution(conn, t - 60, now())
    assert a["points"] == 19  # 21 -> 40, not 1 + 19 + ...


def test_both_sources_can_share_a_timestamp(conn):
    t = now()
    conn.execute("INSERT INTO quota_snapshots(ts, source, five_hour_pct) VALUES (?,?,?)", (t, "desktop", 20))
    conn.execute("INSERT OR IGNORE INTO quota_snapshots(ts, source, five_hour_pct) VALUES (?,?,?)", (t, "statusline", 22))
    assert conn.execute("SELECT COUNT(*) FROM quota_snapshots").fetchone()[0] == 2


# -------------------------------------------------------------- attribution


def _rise(conn, t0, t1, p0, p1, with_requests: bool):
    conn.execute("INSERT OR IGNORE INTO quota_snapshots(ts, source, five_hour_pct) VALUES (?,?,?)", (t0, "desktop", p0))
    conn.execute("INSERT OR IGNORE INTO quota_snapshots(ts, source, five_hour_pct) VALUES (?,?,?)", (t1, "desktop", p1))
    if with_requests:
        conn.execute(
            "INSERT OR IGNORE INTO requests(request_id, ts, session_id, model, cost_usd) VALUES (?,?,?,?,?)",
            (f"r{t1}", t1 - 5, "s1", "claude-opus-5", 1.0))


def test_attribution_splits_by_local_activity(conn):
    t = now() - 7200
    _rise(conn, t, t + 900, 0, 30, with_requests=True)
    _rise(conn, t + 900, t + 1800, 30, 50, with_requests=False)
    a = usage.attribution(conn, t - 60, now())
    assert a["totals"] == {"claude_code": 30.0, "elsewhere": 20.0}
    assert a["claude_code_share"] == pytest.approx(0.6)
    assert a["points"] == 50


def test_attribution_ignores_falls_and_wide_gaps(conn):
    t = now() - 30 * 3600
    _rise(conn, t, t + 900, 80, 10, with_requests=True)          # a reset, not a rise
    _rise(conn, t + 1800, t + 1800 + 8 * 3600, 10, 60, False)    # gap too wide to attribute
    a = usage.attribution(conn, t - 60, now())
    assert a["totals"] == {"claude_code": 0.0, "elsewhere": 0.0}
    assert a["unattributed"] == 50


def test_attribution_with_no_data(conn):
    a = usage.attribution(conn, now() - 3600, now())
    assert a["points"] == 0 and a["claude_code_share"] is None


# -------------------------------------------------------------------- burn


def test_burn_uses_desktop_rows_and_cuts_at_reset(conn):
    t = now()
    for mins, pct in ((40, 88), (30, 10), (20, 25), (10, 40)):
        conn.execute("INSERT INTO quota_snapshots(ts, source, five_hour_pct) VALUES (?,?,?)",
                     (t - mins * 60, "desktop", pct))
    b = usage.burn(conn, window_min=60)
    assert b["pct_per_hour"] == pytest.approx(90, rel=0.05)  # 10->40 over 20 min, not 88->40
    assert b["hits_limit_before_reset"] is False             # no reset time known from Desktop rows


def test_latest_quota_borrows_reset_from_statusline(conn):
    t = now()
    reset = t + 4000
    conn.execute("INSERT INTO quota_snapshots(ts, source, five_hour_pct, five_hour_reset) VALUES (?,?,?,?)",
                 (t - 600, "statusline", 30, reset))
    conn.execute("INSERT INTO quota_snapshots(ts, source, five_hour_pct) VALUES (?,?,?)", (t, "desktop", 55))
    q = usage.latest_quota(conn)
    assert q["five_hour_pct"] == 55 and q["five_hour_reset"] == reset


def test_latest_quota_ignores_a_stale_reset(conn):
    t = now()
    conn.execute("INSERT INTO quota_snapshots(ts, source, five_hour_pct, five_hour_reset) VALUES (?,?,?,?)",
                 (t - 90000, "statusline", 30, t - 80000))
    conn.execute("INSERT INTO quota_snapshots(ts, source, five_hour_pct) VALUES (?,?,?)", (t, "desktop", 55))
    assert usage.latest_quota(conn)["five_hour_reset"] is None


# ------------------------------------------------------------------ report


def test_report_renders_attribution(conn, tmp_path, monkeypatch):
    from coord_mcp import report
    t = now() - 3600
    _rise(conn, t, t + 900, 0, 30, with_requests=True)
    _rise(conn, t + 900, t + 1800, 30, 50, with_requests=False)
    html = report.write(conn, tmp_path / "r.html", days=7).read_text(encoding="utf-8")
    assert "Where the limit went" in html
    assert "Limit used by Claude Code" in html and "60%" in html
    assert "Elsewhere" in html
