"""Defer return dates (coord_mcp/defer.py) and the engine drafting past a deferral."""

from __future__ import annotations

from datetime import datetime

import pytest

from coord_mcp import defer, queue, report, store
from coord_mcp.db import connect
from coord_mcp.workflows import cases as cs
from coord_mcp.workflows import engine

INV = {"done": True, "verdict": "v", "confidence": "high", "evidence": ["e"]}
DAY = 86400


@pytest.fixture(autouse=True)
def _defer_return_on(monkeypatch):
    # conftest turns every queue switch off; this one is the feature under test.
    monkeypatch.setitem(queue.SWITCHES, "defer_return", True)
    monkeypatch.setenv("COORD_HOLIDAYS", "")  # weekday arithmetic below; holidays have their own test


@pytest.fixture()
def c(tmp_path):
    conn = connect(tmp_path / "t.db")
    yield conn
    conn.close()


def _fu(c, who="Ben", what="decide the scope", client="LNK"):
    t = store.create_task(c, "investigation", f"{client}: read", {}, assigned_to=client)["task_id"]
    store.complete_task(c, t, {**INV, "follow_ups": [{"who": who, "what": what}]})
    return c.execute("SELECT id FROM follow_ups WHERE task_id=?", (t,)).fetchone()["id"]


def _weekdays(a: int, b: int) -> int:
    """Weekdays strictly after a's day up to and including b's day."""
    da, db = datetime.fromtimestamp(a).date(), datetime.fromtimestamp(b).date()
    n, d = 0, da
    from datetime import timedelta
    while d < db:
        d += timedelta(days=1)
        n += d.weekday() < 5
    return n


def test_parse_defaults_and_forms():
    t = store.now()
    assert _weekdays(t, defer.parse(None, t)) == 5
    assert _weekdays(t, defer.parse("2w", t)) == 10
    assert _weekdays(t, defer.parse("3", t)) == 3
    iso = datetime.fromtimestamp(t + 9 * DAY).date().isoformat()
    assert defer.day(defer.parse(iso, t)) == iso
    with pytest.raises(ValueError):
        defer.parse("2001-01-01", t)
    with pytest.raises(ValueError):
        defer.parse("whenever", t)


def test_defer_records_a_return_date_and_reopens_on_it(c):
    ben, lnk = _fu(c), _fu(c, who="LNK", what="re-run the harness")
    d = store.record_decision(c, "Ben parks these for later", "later")["decision_id"]
    r = store.resolve_follow_up(c, ben, decision_id=d, defer=True)
    assert r["state"] == "deferred" and r["return_on"]
    store.resolve_follow_up(c, lnk, decision_id=d, defer=True, return_on="3d")
    with pytest.raises(ValueError):
        store.resolve_follow_up(c, lnk, decision_id=d, return_on="3d")  # only with defer
    rets = defer.returns_for(c)
    assert set(rets) == {ben, lnk}
    assert defer.reopen_due(c)["count"] == 0  # not yet
    later = max(rets.values()) + 60
    out = defer.reopen_due(c, now_ts=later)
    assert out["applied"] and out["count"] == 2 and out["ben"] == 1
    states = dict(c.execute("SELECT id, state FROM follow_ups").fetchall())
    assert states[ben] == states[lnk] == "open"
    dec = c.execute("SELECT statement FROM decisions WHERE id=?", (out["decision_id"],)).fetchone()[0]
    assert dec.startswith("Return dates: reopened 2") and "Ben 1" in dec
    assert ben in {x["id"] for x in queue._ben_rows(c)}  # back on Ben's queue
    assert defer.reopen_due(c, now_ts=later)["count"] == 0  # once


def test_redefer_replaces_the_date_and_switch_off_keeps_it_parked(c):
    f = _fu(c)
    d1 = store.record_decision(c, "later 1", "x")["decision_id"]
    store.resolve_follow_up(c, f, decision_id=d1, defer=True, return_on="tomorrow")
    store.resolve_follow_up(c, f, reopen=True)
    d2 = store.record_decision(c, "later 2", "x")["decision_id"]
    store.resolve_follow_up(c, f, decision_id=d2, defer=True, return_on="2w")
    ret = defer.returns_for(c)[f]
    assert _weekdays(store.now(), ret) == 10
    queue.set_switch(c, "defer_return", False)
    assert not defer.reopen_due(c, now_ts=ret + 60)["applied"]
    assert c.execute("SELECT state FROM follow_ups WHERE id=?", (f,)).fetchone()[0] == "deferred"


def test_queue_defer_takes_a_return_date_and_pages_show_it(c):
    f = _fu(c)
    _fu(c, what="pick the cutover week")  # one left open, so the form renders
    r = queue.apply(c, "defer", [f], return_on="1w")
    assert r["return_on"] and defer.returns_for(c)[f]
    v = queue.ben_queue(c)
    assert v["deferred"][0]["id"] == f and v["deferred"][0]["return_on"]
    html = report.queue_page(v)
    assert "Deferred, coming back" in html and "returns " in html and 'name="return_on"' in html
    board = store.operator_view(c)
    row = next(d for d in board["deferred"] if d["fid"] == f)
    assert row["return_on"] and "returns " in row["note"]
    assert "return-chip" in report._items([row], store.now(), "")
    with pytest.raises(ValueError):
        queue.apply(c, "defer", [_fu(c)], return_on="nonsense")


def test_expiry_gives_a_return_date(c):
    f = _fu(c)
    c.execute("UPDATE follow_ups SET created_at=? WHERE id=?", (store.now() - 20 * DAY, f))
    out = queue.expire(c, apply=True)
    assert out["count"] == 1 and out["return_on"]
    assert _weekdays(store.now(), defer.returns_for(c)[f]) == queue.EXPIRE_RETURN_DAYS


def test_backfill_spreads_ben_first_oldest_first(c):
    d = store.record_decision(c, "old parking", "x")["decision_id"]
    ids = []
    for i in range(20):
        f = _fu(c, who="Ben" if i % 2 else "LNK", what=f"item {i}")
        c.execute("UPDATE follow_ups SET created_at=? WHERE id=?", (store.now() - (100 - i) * DAY, f))
        c.execute("UPDATE follow_ups SET state='deferred', resolved_by=? WHERE id=?", (d, f))
        ids.append(f)
    dry = defer.backfill(c)
    assert not dry["applied"] and dry["count"] == 20 and dry["ben"] == 10
    assert len(dry["per_day"]) == 10 and all(sum(v.values()) == 2 for v in dry["per_day"].values())
    first = min(dry["per_day"])
    assert dry["per_day"][first] == {"ben": 2, "other": 0}
    assert defer.returns_for(c) == {}  # dry run wrote nothing
    real = defer.backfill(c, apply=True)
    assert real["applied"] and len(defer.returns_for(c)) == 20
    items = sorted(real["items"], key=lambda r: r["return_on"])
    assert items[0]["what"] == "item 1"  # Ben's oldest first
    assert defer.backfill(c)["count"] == 0


def test_holidays_are_not_business_days(c, monkeypatch):
    from datetime import date
    monkeypatch.delenv("COORD_HOLIDAYS")
    assert defer._add_business(date(2026, 11, 25), 1) == date(2026, 11, 30)  # Thanksgiving Thu+Fri, weekend
    defer.set_holidays(c, ["2026-11-25"])
    assert defer.holidays(c) == {date(2026, 11, 25)}
    assert defer._add_business(date(2026, 11, 24), 1) == date(2026, 11, 26)


def test_an_item_back_on_its_date_may_expire_again(c):
    f = _fu(c)
    c.execute("UPDATE follow_ups SET created_at=? WHERE id=?", (store.now() - 20 * DAY, f))
    queue.expire(c, apply=True)
    ret = defer.returns_for(c)[f]
    defer.reopen_due(c, now_ts=ret + 60)
    assert queue.stale(c, now_ts=ret + 3 * DAY) == []  # just back: not stale yet
    assert [x["id"] for x in queue.stale(c, now_ts=ret + 15 * DAY)] == [f]  # 14 days after it returned


def test_an_item_reopened_by_undo_never_expires_again(c):
    f = _fu(c)
    c.execute("UPDATE follow_ups SET created_at=? WHERE id=?", (store.now() - 20 * DAY, f))
    d = queue.expire(c, apply=True)["decision_id"]
    queue.unexpire(c, d)
    assert queue.stale(c, now_ts=store.now() + 60 * DAY) == []


# ------------------------------------------------ engine and deferred replies

@pytest.fixture()
def e(tmp_path):
    conn = connect(tmp_path / "e.db")
    cs.ensure_workflows(conn)
    engine._since(conn, write=True)
    conn.execute("UPDATE meta SET value=? WHERE key=?", (str(store.now() - 60), engine.SINCE_KEY))
    yield conn
    conn.close()


def _landed_with_reply(e):
    i = store.propose_intent(e, "LNK: John asks where RAID 695 stands", "email-status",
                             context="msg X; reply_from john@client.example; reply_date 2026-11-13T10:00:00Z; "
                                     'subject "RAID 695"', origin="team", trust="trusted")["intent_id"]
    t = store.create_task(e, "investigation", "LNK: status of RAID 695 for John", {}, assigned_to="LNK",
                          from_intent=i)["task_id"]
    store.complete_task(e, t, {**INV, "follow_ups": [{"who": "LNK", "what": "draft the reply to John"}]})
    return t, e.execute("SELECT id FROM follow_ups WHERE task_id=?", (t,)).fetchone()["id"]


def test_engine_drafts_a_reply_whose_loose_end_is_deferred(e):
    t, f = _landed_with_reply(e)
    d = store.record_decision(e, f"Ben parks reply {f} for later", "later")["decision_id"]
    store.resolve_follow_up(e, f, decision_id=d, defer=True)
    cs.sync(e)
    did = engine.run(e)
    assert len(did) == 1 and did[0]["kind"] == "draft_reply" and did[0]["follow_up"] == f
    fu = e.execute("SELECT state, resolved_by FROM follow_ups WHERE id=?", (f,)).fetchone()
    assert (fu["state"], fu["resolved_by"]) == ("tasked", did[0]["task_id"])


def test_engine_drafts_past_an_internal_hold_and_gates_outlook(e):
    from coord_mcp import holds
    t, f = _landed_with_reply(e)
    ben = _fu(e, who="Ben", what="rule what John may hear")
    d = store.record_decision(e, f"Hold the reply {f} until Ben rules {ben}", "x", t)["decision_id"]
    store.resolve_follow_up(e, f, decision_id=d, defer=True)
    cs.sync(e)
    did = engine.run(e)
    assert len(did) == 1 and did[0]["draft_past"] == d
    tid = did[0]["task_id"]
    gate = holds.pending_gate(e, tid)
    assert gate and ben in gate["ben_follow_ups"]  # Outlook drafting waits on Ben
    assert e.execute("SELECT COUNT(*) FROM decisions WHERE supersedes=?", (d,)).fetchone()[0] == 0
    stmt = e.execute("SELECT statement FROM decisions WHERE task_id=? AND statement LIKE 'Engine drafted%'",
                     (tid,)).fetchone()[0]
    assert engine.REPLY_RULING in stmt and holds.classify(
        e, e.execute("SELECT * FROM decisions WHERE statement=?", (stmt,)).fetchone()) is None


def test_engine_still_stops_at_an_outward_hold(e):
    t, f = _landed_with_reply(e)
    ben = _fu(e, who="Ben", what="rule what John may hear")
    d = store.record_decision(e, f"Hold sending the reply {f} to the client until Ben rules {ben}", "x",
                              t)["decision_id"]
    store.resolve_follow_up(e, f, decision_id=d, defer=True)
    cs.sync(e)
    did = engine.run(e)
    assert did and "stop" in did[0] and "spawn" not in did[0]
    assert e.execute("SELECT COUNT(*) FROM tasks WHERE kind='draft_reply'").fetchone()[0] == 0
