"""Meeting hours: classification, idempotent re-pulls, overlap sharing, the page."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from coord_mcp import hours, meetings, report
from coord_mcp.db import connect

DAY = date(2026, 9, 22)
ME = "bsattler@loganconsulting.com"


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def utc(h: float, d: date = DAY) -> dict:
    """Connector-shaped UTC time for local hour h on day d, so tests hold in any zone."""
    local = datetime(d.year, d.month, d.day).timestamp() + h * 3600
    s = datetime.fromtimestamp(local, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000")
    return {"dateTime": s, "timeZone": "UTC"}


def ev(i, subject, a, b, attendees=(ME,), organizer=ME, show="busy", **kw):
    return {"id": f"e{i}", "subject": subject, "organizer": organizer, "attendees": list(attendees),
            "start": utc(a), "end": utc(b), "showAs": show, **kw}


def pull(conn, events, d=DAY):
    return meetings.import_events(conn, d.isoformat(), d.isoformat(), len(events), events)


def stored(conn):
    return {r["id"]: (r["status"], r["client"]) for r in conn.execute("SELECT * FROM meetings")}


def test_classification_follows_the_rule_order(conn):
    pull(conn, [
        ev(1, "Coupa standup", 9, 10, [ME, "x@markanthony.com"], organizer="x@markanthony.com"),
        ev(2, "MAG Internal Stand-Up", 8, 8.5, [ME, "s@loganconsulting.com"], show="tentative"),
        ev(3, "Staffing call", 10, 10.5, [ME, "m@loganconsulting.com"]),
        ev(4, "Stage prep", 11, 12, [ME, "f@royaltechnologies.com"], organizer="k@qad.com"),
        ev(5, "Lunch", 12, 13),
        ev(6, "Focus time", 13, 15, attendees=()),
        ev(7, "Offsite", 0, 24, isAllDay=True),
        ev(8, "Away", 9, 17, show="oof"),
        ev(9, "Mystery", 15, 16, [ME, "a@acme.com"], organizer="a@acme.com"),
        ev(10, "Prep block", 16, 17),
        ev(11, "Old", 17, 18, [ME, "x@markanthony.com"], isCancelled=True),
    ])
    s = stored(conn)
    assert s["e1"] == ("counted", "MAG") and s["e2"] == ("counted", "MAG")
    assert s["e3"] == ("counted", meetings.INTERNAL)
    assert all(s[f"e{i}"][0] == "skipped" for i in (4, 5, 6, 7, 8, 11))
    assert s["e9"] == ("unclassified", None) and s["e10"] == ("unclassified", None)


def test_repull_updates_moves_and_drops_cancelled(conn):
    pull(conn, [ev(1, "MAG sync", 9, 10), ev(2, "MAG review", 11, 12)])
    out = pull(conn, [ev(1, "MAG sync", 9, 9.5)])
    assert out["removed"] == 1 and set(stored(conn)) == {"e1"}
    assert hours.breakdown(conn, DAY, DAY)[0]["meeting_min"] == pytest.approx(30)


def test_short_payload_is_refused_and_changes_nothing(conn):
    pull(conn, [ev(1, "MAG sync", 9, 10), ev(2, "MAG review", 11, 12)])
    with pytest.raises(ValueError, match="fetch every page"):
        meetings.import_events(conn, DAY.isoformat(), DAY.isoformat(), 2, [ev(1, "MAG sync", 9, 10)])
    assert set(stored(conn)) == {"e1", "e2"}


def test_overlap_is_one_hour_of_operator_time_shared(conn):
    pull(conn, [ev(1, "MAG sync", 9, 10), ev(2, "LNK call", 9.5, 10.5)])
    m = meetings.meeting_minutes(conn, DAY, DAY)
    assert m[(DAY.isoformat(), "MAG")] == pytest.approx(45)
    assert m[(DAY.isoformat(), "LNK")] == pytest.approx(45)


def test_assign_reclassifies_stored_meetings_and_later_pulls(conn):
    pull(conn, [ev(1, "Mystery sync", 9, 10, [ME, "a@acme.com"])])
    assert meetings.unclassified(conn, DAY, DAY)[0]["subject"] == "Mystery sync"
    assert meetings.assign(conn, "Mystery sync", "cascade")["reclassified"] == 1
    assert stored(conn)["e1"] == ("counted", "Cascade")
    meetings.assign(conn, "Mystery sync", "skip")
    assert stored(conn)["e1"][0] == "skipped"


def test_meetings_show_on_page_and_never_in_push(conn):
    pull(conn, [ev(1, "MAG sync", 9, 10), ev(2, "Mystery", 11, 12, [ME, "a@acme.com"])])
    rs = hours.breakdown(conn, DAY, DAY)
    assert [(r["client"], r["task_id"]) for r in rs] == [("MAG", meetings.MEETINGS)]
    assert hours.push_payload(rs) == []
    page = report.hours_page(rs, DAY.isoformat(), DAY.isoformat(), None, hours.push_to_pipeline(conn, rs),
                             today=DAY, unclassified=meetings.unclassified(conn, DAY, DAY))
    assert "meetings (Outlook)" in page and "Unclassified meetings" in page and "Mystery" in page
    assert report.hours_csv(rs).splitlines()[1].split(",")[7] == "60.0"
