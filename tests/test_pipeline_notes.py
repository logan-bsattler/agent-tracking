"""Pipeline notes in code: the board writes the line, leases one at a time,
and an ok records the decision that clears queue.pipeline_owed."""

from __future__ import annotations

from datetime import datetime

import pytest

from coord_mcp import queue, store
from coord_mcp.db import connect, ensure_promotions, now


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    ensure_promotions(c)
    yield c
    c.close()


def _code(conn, pid="417", spec=None, summary="fixed the shipper", title="Shipper fix"):
    tid = store.create_task(conn, "code_change", title, spec or {"q": "x"}, assigned_to="Royal",
                            pipeline_task_id=pid)["task_id"]
    store.complete_task(conn, tid, {"done": True, "summary": summary, "files_changed": [{"path": "a.p"}]})
    return tid


def _promoted(conn, pid="88"):
    tid = store.create_task(conn, "deliverable", "Status report", {"format": "html"}, assigned_to="MAG",
                            pipeline_task_id=pid)["task_id"]
    store.complete_task(conn, tid, {"done": True, "summary": "s", "path": "C:/x/report.html",
                                    "deliverable_type": "status-report", "dod_path": "d", "self_check": [
                                        {"item": "a", "met": True}]})
    row = conn.execute("SELECT id FROM tasks WHERE parent_id=?", (tid,)).fetchone()
    rev = row["id"] if row else store.create_task(conn, "dod_review", "Review status report", {"q": "x"},
                                                  assigned_to="MAG", parent_id=tid)["task_id"]
    conn.execute("INSERT INTO promotions(id, task_id, review_id, client, src, dest, version, sha256, created_at) "
                 "VALUES ('p1',?,?,'MAG','C:/x/report.html','C:\\docs\\MAG\\deliverables\\report-v2.html',2,'h',?)",
                 (tid, rev, now()))
    return tid


TODAY = datetime.now().strftime("%Y-%m-%d")


def test_note_text_for_a_checkin_names_its_cvc_and_date(conn):
    tid = _code(conn, summary="Checked in CVC0707-81 to TEST")
    assert queue.pipeline_note_text(conn, tid) == f"{TODAY}: checked in Shipper fix (CVC0707-81)"
    other = _code(conn, spec={"q": "x", "ticket": "QSM-1069690"}, summary="s")
    assert "(QSM-1069690)" in queue.pipeline_note_text(conn, other)


def test_note_text_leaves_out_the_agent_summary(conn):
    tid = _code(conn, summary="Ben still needs to chase the client on this")
    assert queue.pipeline_note_text(conn, tid) == f"{TODAY}: checked in Shipper fix"


def test_note_text_for_a_promotion_names_the_promoted_copy(conn):
    tid = _promoted(conn)
    assert queue.pipeline_note_text(conn, tid) == f"{TODAY}: delivered report-v2.html (Status report)"


def test_note_text_fits_the_decision_cap(conn):
    tid = _code(conn, title="x" * 80, spec={"q": "x", "ticket": "T" * 60})
    note = queue.pipeline_note_text(conn, tid)
    assert len(f"Pipeline note 417: {note}") <= 200


def test_claim_leases_one_and_ok_records_the_clearing_decision(conn):
    a, b = _code(conn), _promoted(conn)
    assert queue.pipeline_owed(conn, now()) == [a, b]
    c = queue.pipeline_note_claim(conn)
    assert c["task_id"] == a and c["item_id"] == 417 and c["pipeline_id"] == "417"
    assert queue.pipeline_note_claim(conn) is None  # one at a time
    with pytest.raises(ValueError):
        queue.pipeline_note_done(conn, b, "ok")  # not the claimed one
    r = queue.pipeline_note_done(conn, a, "ok")
    d = conn.execute("SELECT statement, task_id FROM decisions WHERE id=?", (r["decision_id"],)).fetchone()
    assert d["statement"] == f"Pipeline note 417: {c['note']}" and d["task_id"] == a
    assert queue.pipeline_owed(conn, now()) == [b]
    assert queue.pipeline_note_claim(conn)["task_id"] == b


def test_an_error_waits_and_an_expired_lease_frees(conn):
    a = _code(conn)
    t = now()
    queue.pipeline_note_claim(conn, t)
    assert queue.pipeline_note_claim(conn, t + queue.NOTE_LEASE + 1)["task_id"] == a  # lapsed claim
    queue.pipeline_note_done(conn, a, "error", "401")
    assert queue.pipeline_owed(conn, now()) == [a]
    assert queue.pipeline_note_claim(conn, t + 5) is None
    assert queue.pipeline_note_claim(conn, t + queue.NOTE_RETRY + 5)["task_id"] == a
    with pytest.raises(ValueError):
        queue.pipeline_note_done(conn, a, "maybe")


def test_a_non_numeric_pipeline_id_is_left_for_master(conn):
    _code(conn, pid="SP-3")
    assert queue.pipeline_note_claim(conn) is None
