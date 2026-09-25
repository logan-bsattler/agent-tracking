"""The 5-hour runway: window bounds, and the chart that shows the crossing."""

from __future__ import annotations

import re

import pytest

from coord_mcp import report, usage
from coord_mcp.db import connect, now


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _snap(conn, ts, pct, reset=None, source="desktop"):
    conn.execute(
        "INSERT OR IGNORE INTO quota_snapshots(ts, source, five_hour_pct, five_hour_reset) VALUES (?,?,?,?)",
        (ts, source, pct, reset))


# ------------------------------------------------------------------ window


def test_window_start_is_five_hours_before_a_known_reset(conn):
    t = now()
    reset = t + 3600
    _snap(conn, t - 600, 20, reset, "statusline")
    _snap(conn, t, 40, reset, "statusline")
    w = usage.current_window(conn)
    assert w["reset_known"] is True
    assert w["end"] == reset and w["start"] == reset - 5 * 3600
    assert [r["pct"] for r in w["rows"]] == [20, 40]


def test_window_is_estimated_without_a_reset(conn):
    t = now()
    _snap(conn, t - 1200, 10)
    _snap(conn, t, 30)
    w = usage.current_window(conn)
    assert w["reset_known"] is False
    assert w["start"] == t - 1200 and w["end"] == t - 1200 + 5 * 3600


def test_window_starts_after_the_last_reset_drop(conn):
    t = now()
    for mins, pct in ((120, 70), (100, 95), (80, 4), (40, 20), (10, 35)):
        _snap(conn, t - mins * 60, pct)
    w = usage.current_window(conn)
    assert [r["pct"] for r in w["rows"]] == [4, 20, 35]


def test_window_empty_when_no_readings(conn):
    w = usage.current_window(conn)
    assert w["rows"] == [] and w["start"] is None and w["end"] is None


# ------------------------------------------------------------------- chart


def _chart(conn):
    return report.runway_chart(usage.live(conn))


def test_chart_is_empty_without_readings(conn):
    assert _chart(conn) == ""


def test_chart_flags_a_crossing_before_reset(conn):
    t = now()
    reset = t + 2 * 3600
    for mins, pct in ((40, 20), (20, 50), (0, 80)):
        _snap(conn, t - mins * 60, pct, reset, "statusline")
    html = _chart(conn)
    assert "Will hit the limit" in html
    assert "var(--critical)" in html and "var(--good)" not in html
    assert re.search(r"100% at \d\d:\d\d", html)
    assert "window opened" in html and "resets" in html
    assert "now · 80%" in html


def test_chart_says_on_track_when_the_reset_comes_first(conn):
    t = now()
    reset = t + 600
    for mins, pct in ((40, 5), (20, 8), (0, 11)):
        _snap(conn, t - mins * 60, pct, reset, "statusline")
    html = _chart(conn)
    assert "On track" in html and "var(--good)" in html
    assert "var(--critical)" not in html
    assert "% by reset" in html


def test_chart_handles_a_flat_window(conn):
    t = now()
    reset = t + 3600
    _snap(conn, t - 1800, 30, reset, "statusline")
    _snap(conn, t, 30, reset, "statusline")
    html = _chart(conn)
    assert "Not enough readings" in html
    assert "stroke-dasharray" not in html  # no projection drawn


def test_unknown_reset_gives_no_green_or_red_verdict(conn):
    t = now()
    for mins, pct in ((40, 10), (20, 30), (0, 55)):
        _snap(conn, t - mins * 60, pct)          # desktop rows carry no reset
    html = _chart(conn)
    assert "reset?" in html and "cannot be said" in html
    assert "var(--warn)" in html
    assert "On track" not in html and "Will hit the limit" not in html
    assert "var(--good)" not in html and "var(--critical)" not in html


def test_unknown_reset_keeps_the_crossing_on_screen(conn):
    """The estimated window can end before the projection crosses; the chart
    must still show the crossing rather than clipping it at the edge."""
    t = now()
    _snap(conn, t - 5 * 3600 + 60, 5)            # window estimated to end ~now
    for secs, pct in ((1800, 20), (600, 30), (0, 40)):
        _snap(conn, t - secs, pct)               # recent readings give a pace
    html = _chart(conn)
    assert re.search(r"100% at \d\d:\d\d", html)
    xs = [float(m) for m in re.findall(r'(?:x|cx)="([-\d.]+)"', html)]
    assert max(xs) <= 1001


def test_chart_geometry_stays_inside_the_viewbox(conn):
    t = now()
    reset = t + 300  # very close reset, projection would run off the right
    for mins, pct in ((40, 10), (20, 55), (0, 95)):
        _snap(conn, t - mins * 60, pct, reset, "statusline")
    html = _chart(conn)
    xs = [float(m) for m in re.findall(r'(?:x|cx)="([-\d.]+)"', html)]
    ys = [float(m) for m in re.findall(r'(?:y|cy)="([-\d.]+)"', html)]
    assert xs and ys
    assert min(xs) >= -1 and max(xs) <= 1001
    assert min(ys) >= -1 and max(ys) <= 171


def test_now_section_embeds_the_runway(conn):
    t = now()
    reset = t + 3600
    for mins, pct in ((30, 10), (0, 40)):
        _snap(conn, t - mins * 60, pct, reset, "statusline")
    html = report.now_section(usage.live(conn))
    assert "<svg" in html and "Spend last 15 min" in html


def test_falsified_estimate_drops_the_reset_marker(conn):
    """Five hours past the first reading with no reset disproves the guess."""
    t = now()
    _snap(conn, t - 5 * 3600 - 1800, 5)          # guessed end is half an hour ago
    for secs, pct in ((1800, 20), (600, 30), (0, 40)):
        _snap(conn, t - secs, pct)
    w = usage.current_window(conn)
    assert w["stale_estimate"] is True
    html = _chart(conn)
    assert "reset?" not in html
    assert "cannot even be guessed" in html
    assert "var(--warn)" in html


def test_known_reset_is_never_stale(conn):
    t = now()
    reset = t + 1800
    _snap(conn, t - 600, 20, reset, "statusline")
    _snap(conn, t, 30, reset, "statusline")
    w = usage.current_window(conn)
    assert w["stale_estimate"] is False
    assert "resets" in _chart(conn)


# ------------------------------------------------------------------ weekly


def _week(conn, ts, pct, reset=None, source="statusline"):
    conn.execute(
        "INSERT OR IGNORE INTO quota_snapshots(ts, source, seven_day_pct, seven_day_reset) VALUES (?,?,?,?)",
        (ts, source, pct, reset))


def test_weekly_window_is_seven_days_before_its_reset(conn):
    t = now()
    reset = t + 2 * 86400
    _week(conn, t - 3 * 86400, 10, reset)
    _week(conn, t, 30, reset)
    w = usage.weekly_burn(conn)["window"]
    assert w["reset_known"] and w["start"] == reset - 7 * 86400 and w["end"] == reset
    assert [r["pct"] for r in w["rows"]] == [10, 30]


def test_weekly_runway_flags_a_crossing_before_reset(conn):
    t = now()
    reset = t + 3 * 86400
    for h, pct in ((20, 50), (10, 60), (0, 70)):
        _week(conn, t - h * 3600, pct, reset)
    html = report.runway_chart(usage.live(conn), "weekly", 7 * 24)
    assert "Will hit the limit" in html and "hours before the window resets" in html
    assert re.search(r"100% at \w{3} \d\d \w{3} \d\d:\d\d", html)


def test_weekly_runway_on_track(conn):
    t = now()
    reset = t + 86400
    for h, pct in ((20, 20), (10, 22), (0, 24)):
        _week(conn, t - h * 3600, pct, reset)
    html = report.runway_chart(usage.live(conn), "weekly", 7 * 24)
    assert "On track" in html and "hours." in html


def test_usage_page_has_a_weekly_card(conn):
    t = now()
    for h, pct in ((10, 20), (0, 24)):
        _week(conn, t - h * 3600, pct, t + 86400)
    v = usage.live(conn)
    assert "This week: the weekly window" in report.render(usage.summary(conn), v)


def test_weekly_reset_is_inferred_from_the_drop(conn):
    """Desktop samples carry no reset; the drop pins the weekly one to the hour."""
    t = now()
    opened = (t - 2 * 86400) // 3600 * 3600          # on the hour, two days ago
    for ts, pct in ((opened - 1200, 80), (opened + 600, 1), (t - 3600, 30), (t, 31)):
        _week(conn, ts, pct, source="desktop")
    w = usage.weekly_burn(conn)["window"]
    assert w["reset_inferred"] and w["reset_known"]
    assert w["start"] == opened and w["end"] == opened + 7 * 86400


def test_weekly_anchor_projects_to_the_next_reset(conn):
    t = now()
    usage.set_weekly_reset(conn, t - 3 * 86400)      # a reset three days ago
    _week(conn, t - 3600, 30, source="desktop")
    _week(conn, t, 31, source="desktop")
    w = usage.weekly_burn(conn)["window"]
    assert w["reset_known"] and w["end"] == t - 3 * 86400 + 7 * 86400
