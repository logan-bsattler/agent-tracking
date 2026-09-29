"""Billable hours: active time, attribution to tasks, operator time, the push gate."""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta

import pytest

from coord_mcp import hours, report, store
from coord_mcp.db import connect

DOCS = r"C:\Users\x\OneDrive - Logan Consulting\Documents"
INV = {"done": True, "verdict": "v", "confidence": "high", "evidence": ["e"]}
DAY = date(2026, 9, 21)


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def at(h: float, d: date = DAY) -> int:
    """Local epoch seconds h hours into day d, so tests hold in any timezone."""
    return int(datetime.combine(d, dtime()).timestamp() + h * 3600)


def req(conn, ts: int, client: str = "LNK", sub: str = "") -> None:
    cwd = f"{DOCS}\\{client}" + (f"\\{sub}" if sub else "")
    conn.execute("INSERT INTO requests(request_id, ts, session_id, cwd, model) VALUES (?,?,?,?,?)",
                 (f"r{ts}{client}{sub}", ts, "s", cwd, "m"))


def task(conn, client="LNK", title="t", pipeline=None):
    return store.create_task(conn, "investigation", title, {"q": "x"}, assigned_to=client,
                             pipeline_task_id=pipeline)["task_id"]


def ev(conn, task_id, event, ts):
    conn.execute("INSERT INTO task_events(id, task_id, client, event, ts) VALUES (?,?,?,?,?)",
                 (f"e{task_id}{event}{ts}", task_id, "LNK", event, ts))


def rows(conn, start=DAY, end=DAY, **kw):
    return {(r["day"], r["client"], r["task_id"]): r
            for r in hours.breakdown(conn, start, end, t_now=at(48), **kw)}


def test_idle_gaps_are_capped_not_dropped():
    # 60s, then a 2h gap (counts 300s), then the last request owns 300s.
    assert hours.active_segments([0, 60, 7260]) == [(0, 60), (60, 360), (7260, 7560)]
    assert hours.active_segments([0, 60], until=90) == [(0, 60), (60, 90)]


def test_client_from_cwd_including_subfolders_and_not_posey(conn):
    keys = hours._clients(conn)
    assert hours.client_of(DOCS + r"\TS Tech\sub\deeper", keys) == "TS Tech"
    assert hours.client_of(DOCS.replace("\\", "/") + "/lnk", keys) == "LNK"
    assert hours.client_of(DOCS + r"\Posey", keys) is None
    assert hours.client_of(r"C:\development\agents", keys) is None
    assert hours.client_of(r"C:\development\LNK\src", keys) == "LNK"


def test_time_splits_at_local_midnight(conn):
    req(conn, at(24) - 120)  # 23:58, owns 300s: 120s today, 180s tomorrow
    r = rows(conn, DAY, DAY + timedelta(days=1))
    assert r[(DAY.isoformat(), "LNK", "unassigned")]["agent_min"] == pytest.approx(2)
    assert r[((DAY + timedelta(days=1)).isoformat(), "LNK", "unassigned")]["agent_min"] == pytest.approx(3)


def test_attribution_inside_and_outside_a_task(conn):
    t = task(conn)
    ev(conn, t, "picked_up", at(10))
    ev(conn, t, "completed", at(11))
    for m in (0, 1, 2):  # 09:58..10:00 -> before pickup
        req(conn, at(10) - 120 + 60 * m)
    r = rows(conn)
    # 09:58-10:00 is 2 min unassigned; the 10:00 request owns 5 min inside the task.
    assert r[(DAY.isoformat(), "LNK", "unassigned")]["agent_min"] == pytest.approx(2)
    assert r[(DAY.isoformat(), "LNK", t)]["agent_min"] == pytest.approx(5)
    assert r[(DAY.isoformat(), "LNK", t)]["estimated"] is False


def test_park_then_repickup_is_two_spans_and_the_gap_is_unassigned(conn):
    t = task(conn)
    ev(conn, t, "picked_up", at(9))
    ev(conn, t, "picked_up", at(9.5))  # a re-read while open does not reopen
    ev(conn, t, "parked", at(10))
    ev(conn, t, "picked_up", at(12))
    ev(conn, t, "completed", at(13))
    for h in (9, 11, 12):
        req(conn, at(h))
    r = rows(conn)
    assert r[(DAY.isoformat(), "LNK", t)]["agent_min"] == pytest.approx(10)
    assert r[(DAY.isoformat(), "LNK", "unassigned")]["agent_min"] == pytest.approx(5)


def test_overlapping_tasks_split_evenly(conn):
    a, b = task(conn, title="a"), task(conn, title="b")
    for t in (a, b):
        ev(conn, t, "picked_up", at(9))
        ev(conn, t, "completed", at(10))
    req(conn, at(9.5))
    r = rows(conn)
    assert r[(DAY.isoformat(), "LNK", a)]["agent_min"] == pytest.approx(2.5)
    assert r[(DAY.isoformat(), "LNK", b)]["agent_min"] == pytest.approx(2.5)


def test_tasks_before_events_are_estimated_from_create_to_close(conn):
    t = task(conn)
    conn.execute("UPDATE tasks SET state='done', created_at=?, completed_at=? WHERE id=?", (at(9), at(10), t))
    req(conn, at(9.5))
    r = rows(conn)[(DAY.isoformat(), "LNK", t)]
    assert r["agent_min"] == pytest.approx(5) and r["estimated"] is True


def test_store_writes_events_for_the_assignee_only(conn):
    t = task(conn)
    store.get_task(conn, t)                    # the master: no event
    store.get_task(conn, t, reader="agents")   # still the master
    store.get_task(conn, t, reader="lnk")
    store.park_task(conn, t, next_step="go on")
    store.get_task(conn, t, reader="LNK")
    store.complete_task(conn, t, INV)
    evs = [r[0] for r in conn.execute("SELECT event FROM task_events WHERE task_id=? ORDER BY ts, rowid", (t,))]
    assert evs == ["picked_up", "parked", "picked_up", "completed"]
    f = task(conn)
    store.complete_task(conn, f, {"done": False, "reason": "VPN down", "retryable": True})
    assert conn.execute("SELECT event FROM task_events WHERE task_id=?", (f,)).fetchone()[0] == "failed"


def test_operator_time_sums_separately(conn):
    t = task(conn, pipeline="SP-36")
    ev(conn, t, "picked_up", at(9))
    ev(conn, t, "completed", at(10))
    req(conn, at(9))
    hours.log_time(conn, 30, DAY.isoformat(), task_id=t, note="review")
    hours.log_time(conn, 15, DAY.isoformat(), task_id=t)
    hours.log_time(conn, 20, DAY.isoformat(), client="lnk", note="call")
    r = rows(conn)
    assert (r[(DAY.isoformat(), "LNK", t)]["agent_min"], r[(DAY.isoformat(), "LNK", t)]["operator_min"]) \
        == (pytest.approx(5), 45)
    assert r[(DAY.isoformat(), "LNK", t)]["total_min"] == pytest.approx(50)
    assert r[(DAY.isoformat(), "LNK", "unassigned")]["operator_min"] == 20
    with pytest.raises(ValueError):
        hours.log_time(conn, 10, DAY.isoformat(), task_id=t, client="Moog")
    with pytest.raises(ValueError):
        hours.log_time(conn, 0, DAY.isoformat(), client="LNK")


def test_rollup_by_pipeline_item(conn):
    a, b = task(conn, title="a", pipeline="SP-1"), task(conn, title="b")
    hours.log_time(conn, 30, DAY.isoformat(), task_id=a)
    hours.log_time(conn, 30, DAY.isoformat(), task_id=b)
    store.set_pipeline_task(conn, b, "SP-1")
    up = hours.rollup(hours.breakdown(conn, DAY, DAY))
    assert [(r["pipeline_task_id"], r["operator_min"], sorted(r["tasks"])) for r in up] \
        == [("SP-1", 60, sorted([a, b]))]


def test_push_stays_dry_without_the_flag(conn):
    t = task(conn, pipeline="SP-9")
    hours.log_time(conn, 50, DAY.isoformat(), task_id=t)
    hours.log_time(conn, 30, DAY.isoformat(), client="LNK")  # no pipeline item: not pushable
    rs = hours.breakdown(conn, DAY, DAY)
    out = hours.push_to_pipeline(conn, rs, dry_run=False)
    assert out["enabled"] is False and out["dry_run"] is True and out["sent"] == 0
    assert out["payload"] == [{"pipeline_task_id": "SP-9", "date": DAY.isoformat(), "hours": 0.75,
                               "note": f"0h agent, 0.75h operator; board {t}"}]
    conn.execute("INSERT INTO meta(key, value) VALUES (?, '1')", (hours.PUSH_FLAG,))
    assert hours.push_to_pipeline(conn, rs)["dry_run"] is True
    with pytest.raises(NotImplementedError):
        hours.push_to_pipeline(conn, rs, dry_run=False)


def test_page_and_csv_render(conn):
    t = task(conn, title="Lot numbering")
    ev(conn, t, "picked_up", at(9))
    req(conn, at(9))
    rs = hours.breakdown(conn, DAY, DAY, t_now=at(10))
    page = report.hours_page(rs, DAY.isoformat(), DAY.isoformat(), None, hours.push_to_pipeline(conn, rs))
    assert "Lot numbering" in page and "push disabled" in page and "Draft, not a bill" in page
    csv = report.hours_csv(rs).splitlines()
    assert csv[0].startswith("day,client,task_id") and csv[1].startswith(f"{DAY.isoformat()},LNK,{t}")


def test_page_lists_empty_days_with_weekday_up_to_today(conn):
    t = task(conn)
    ev(conn, t, "picked_up", at(9))
    req(conn, at(9))
    rs = hours.breakdown(conn, DAY, DAY, t_now=at(10))
    nxt, end = DAY + timedelta(1), DAY + timedelta(9)
    page = report.hours_page(rs, (DAY - timedelta(1)).isoformat(), end.isoformat(), None,
                             hours.push_to_pipeline(conn, rs), today=nxt)
    assert f"Sun</span> {(DAY - timedelta(1)).isoformat()}" in page
    assert f"Mon</span> {DAY.isoformat()}" in page and f"Tue</span> {nxt.isoformat()}" in page
    assert (nxt + timedelta(1)).isoformat() not in page


def test_default_range_is_the_half_month_billing_period():
    assert report.billing_period(date(2026, 9, 15)) == (date(2026, 9, 1), date(2026, 9, 15))
    assert report.billing_period(date(2026, 9, 16)) == (date(2026, 9, 16), date(2026, 9, 30))
    assert report.billing_period(date(2028, 2, 20)) == (date(2028, 2, 16), date(2028, 2, 29))
