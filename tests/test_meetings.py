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


def test_classify_button_assigns_from_the_hours_page(tmp_path):
    import functools
    import socket
    import threading
    import time
    import urllib.error
    import urllib.parse
    import urllib.request

    make = functools.partial(connect, tmp_path / "srv.db")
    c = make()
    pull(c, [ev(1, "Mystery & sync", 9, 10, [ME, "a@acme.com"]), ev(2, "MAG sync", 11, 12)])
    rs = hours.breakdown(c, DAY, DAY)
    page = report.hours_page(rs, DAY.isoformat(), DAY.isoformat(), None, hours.push_to_pipeline(c, rs), today=DAY,
                             unclassified=meetings.unclassified(c, DAY, DAY), choices=["Acument", "MAG"])
    assert page.count('action="/api/meeting/assign"') == 1 and page.count("<button>Classify</button>") == 1
    assert 'name="subject.0" value="Mystery &amp; sync"' in page and 'name="client.0"' in page
    assert '<option value="Acument">' in page and '<option value="Internal">' in page and 'value="skip"' in page
    c.close()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    threading.Thread(target=report.serve, args=(make, port), daemon=True).start()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    def post(fields, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/meeting/assign",
                                     data=urllib.parse.urlencode(fields).encode(), headers=headers or {})
        try:
            r = urllib.request.build_opener(NoRedirect).open(req, timeout=10)
            return r.status, r.headers.get("Location")
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Location")

    assert post({"subject": "Mystery & sync", "client": ""})[0] == 400
    assert post({"subject": "Mystery & sync", "client": "cascade"},
                headers={"Origin": "http://evil.example"})[0] == 403
    back = f"/hours?start={DAY.isoformat()}&end={DAY.isoformat()}"
    assert post({"subject": "Mystery & sync", "client": "cascade", "back": back}) == (303, back)
    c = make()
    assert stored(c)["e1"] == ("counted", "Cascade")
    c.close()
    assert post({"subject": "Mystery & sync", "client": "skip", "back": "http://evil.example/"}) == (303, "/hours")
    c = make()
    assert stored(c)["e1"][0] == "skipped"
    c.close()


def test_one_classify_button_assigns_every_picked_row(tmp_path):
    import functools
    import socket
    import threading
    import time
    import urllib.error
    import urllib.parse
    import urllib.request

    make = functools.partial(connect, tmp_path / "srv.db")
    c = make()
    pull(c, [ev(1, "Alpha", 9, 10, [ME, "a@acme.com"]), ev(2, "Beta", 11, 12, [ME, "a@acme.com"]),
             ev(3, "Gamma", 13, 14, [ME, "a@acme.com"])])
    c.close()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    threading.Thread(target=report.serve, args=(make, port), daemon=True).start()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    def post(fields):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/meeting/assign",
                                     data=urllib.parse.urlencode(fields).encode())
        try:
            return urllib.request.build_opener(NoRedirect).open(req, timeout=10).status
        except urllib.error.HTTPError as e:
            return e.code

    rows = {"subject.0": "Alpha", "subject.1": "Beta", "subject.2": "Gamma"}
    assert post({**rows, "client.0": "", "client.1": "", "client.2": ""}) == 400  # nothing picked
    assert post({**rows, "client.0": "cascade", "client.1": "skip", "client.2": ""}) == 303
    c = make()
    assert stored(c) == {"e1": ("counted", "Cascade"), "e2": ("skipped", None), "e3": ("unclassified", None)}
    c.close()
