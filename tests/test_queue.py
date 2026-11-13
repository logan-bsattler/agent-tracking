"""Ben's queue: agent work routed back to clients, the /queue batch page,
and what master owes on the way out (drafts to Outlook, pipeline notes)."""

from __future__ import annotations

import json

import pytest

from coord_mcp import queue, report, store
from coord_mcp.db import connect, ensure_promotions, now

INV = {"done": True, "verdict": "v", "confidence": "high", "evidence": ["e"]}


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _done(conn, client="MAG", follow_ups=(), kind="investigation", spec=None, result=None, **kw):
    tid = store.create_task(conn, kind, "t", spec or {"q": "x"}, assigned_to=client, **kw)["task_id"]
    store.get_task(conn, tid, reader=client)
    out = store.complete_task(conn, tid, {**(result or INV), "follow_ups": list(follow_ups)})
    return tid, out


def _ben(what):
    return {"who": "Ben", "what": what}


@pytest.mark.parametrize("what,client,want", [
    ("Reply to Dave: push back, browse is out of plan", "MAG", "reply"),
    ("Draft a reply to Jeff with the 6 questions", "Cascade", "reply"),
    ("DB read on TEST: PostingLine with zero debit+credit", "MAG", "agent"),
    ("Query pt_mstr for items with a blank prod line", "Moog", "agent"),
    ("Confirm xxsocomm.p is still called from the menu", "MAG", "agent"),
    ("Review and send DRAFT-274 reply to Kelly", "MAG", None),
    ("Approve the cvc-code checklist", "MAG", None),
    ("Decide whether to keep xxsonwivpald.p", "MAG", None),
    ("Run the package on the MAG VM with Darren", "MAG", None),
    ("Run the Kiro package against DEVL", "MAG", None),
    ("Confirm with the client that the 34 group conflicts are fine", "MAG", None),
    ("Run DEVL tests 1-15 from the test plan", "LNK", None),  # LNK has no server leg
    ("Run DEVL tests 1-15 from the test plan", "Moog", "agent"),
    ("Reply to Dave", None, None),  # no client to send it to
])
def test_route_for(what, client, want):
    assert queue.route_for(what, client) == want


def test_completion_routes_agent_work_back_to_the_client(conn):
    tid, out = _done(conn, follow_ups=[_ben("Query so_mstr for open orders"), _ben("Approve the plan"),
                                       {"who": "MAG", "what": "Rerun the load"}])
    assert out["routed_to_you"] == 1
    whos = [r["who"] for r in conn.execute("SELECT who FROM follow_ups WHERE task_id=? ORDER BY idx", (tid,))]
    assert whos == ["MAG", "Ben", "MAG"]
    # The minder now owes it as a client loose end, not Ben.
    assert len(store.list_follow_ups(conn, who="Ben")["follow_ups"]) == 1


def test_completion_with_nothing_to_route_says_nothing(conn):
    _, out = _done(conn, follow_ups=[_ben("Approve the plan")])
    assert "routed_to_you" not in out


def test_backfill_is_a_dry_run_unless_applied(conn):
    # Written before routing existed: put it on Ben directly.
    tid, _ = _done(conn)
    conn.execute("INSERT INTO follow_ups(id, task_id, idx, who, what, state, created_at) "
                 "VALUES ('f1', ?, 0, 'Ben', 'Reply to Dave about the browse', 'open', ?)", (tid, now()))
    assert [r["id"] for r in queue.backfill(conn)] == ["f1"]
    assert conn.execute("SELECT who FROM follow_ups WHERE id='f1'").fetchone()["who"] == "Ben"
    queue.backfill(conn, apply=True)
    assert conn.execute("SELECT who FROM follow_ups WHERE id='f1'").fetchone()["who"] == "MAG"
    assert queue.ben_queue(conn)["moved"][0]["why"] == "reply"


def _ids(conn):
    return [r["id"] for r in conn.execute("SELECT id FROM follow_ups WHERE who='Ben' AND state='open' ORDER BY idx")]


def test_batch_send_to_agent(conn):
    _done(conn, follow_ups=[_ben("Approve A"), _ben("Approve B")])
    ids = _ids(conn)
    assert queue.apply(conn, "agent", ids)["count"] == 2
    assert _ids(conn) == [] and {m["why"] for m in queue.ben_queue(conn)["moved"]} == {"ben"}


@pytest.mark.parametrize("action,state", [("defer", "deferred"), ("drop", "dropped")])
def test_batch_defer_and_drop_record_one_decision(conn, action, state):
    _done(conn, follow_ups=[_ben("Approve A"), _ben("Approve B")])
    ids = _ids(conn)
    r = queue.apply(conn, action, ids, note="after go-live")
    rows = conn.execute("SELECT state, resolved_by FROM follow_ups WHERE id IN (?,?)", ids).fetchall()
    assert {(x["state"], x["resolved_by"]) for x in rows} == {(state, r["decision_id"])}
    assert store.get_decisions(conn)[0]["because"] == "after go-live"


def test_batch_answers_go_to_master_as_one_prompt(conn):
    tid, _ = _done(conn, follow_ups=[_ben("Approve A"), _ben("Approve B"), _ben("Approve C")])
    a, b, c = _ids(conn)
    r = queue.apply(conn, "answer", [], {a: "yes", b: "", c: "no, wait for Kelly"})
    assert r["count"] == 2
    p = store.take_operator_prompt(conn)
    assert p["action"] == "batch" and p["text"].startswith("Batch answers from /queue (2)")
    assert f"- {a} (MAG, task {tid}; \"Approve A\"): yes" in p["text"] and b not in p["text"]
    assert _ids(conn) == [a, b, c]  # master resolves them, not the page


def test_batch_refuses_what_is_not_bens(conn):
    _done(conn, follow_ups=[{"who": "MAG", "what": "Rerun"}])
    fid = conn.execute("SELECT id FROM follow_ups").fetchone()["id"]
    with pytest.raises(ValueError):
        queue.apply(conn, "drop", [fid])
    with pytest.raises(ValueError):
        queue.apply(conn, "send", [])
    with pytest.raises(ValueError):
        queue.apply(conn, "answer", [], {})


def test_a_page_cannot_queue_a_batch_prompt_directly(conn):
    with pytest.raises(ValueError):
        store.queue_operator_prompt(conn, "batch")


def test_queue_page_groups_by_client_oldest_first(conn):
    _done(conn, "LNK", follow_ups=[_ben("Approve LNK thing")])
    conn.execute("UPDATE follow_ups SET created_at = created_at - 86400")
    _done(conn, "MAG", follow_ups=[_ben("Approve MAG thing <b>")])
    v = queue.ben_queue(conn)
    assert [g["client"] for g in v["groups"]] == ["LNK", "MAG"] and v["total"] == 2
    html = report.queue_page(v, "2 dropped")
    assert "(2) Queue" in html and "2 dropped" in html and "&lt;b&gt;" in html and 'action="/api/queue"' in html


def _draft(conn, tmp_path, completed_ago=0, spec=None):
    f = tmp_path / "body.md"
    f.write_text("Hi,\n\nDone.\n\nThanks!\n", encoding="utf-8")
    tid, _ = _done(conn, "Moog", kind="draft_reply", spec=spec or {"q": "x", "reply_date": "2026-10-20T09:00Z"},
                   result={"done": True, "to": ["a@moog.com"], "subject": "RE: x", "body_path": str(f),
                           "summary": "s"})
    conn.execute("UPDATE tasks SET completed_at = completed_at - ? WHERE id=?", (completed_ago, tid))
    return tid


def test_undrafted_waits_a_little_then_owes_until_drafted(conn, tmp_path):
    tid = _draft(conn, tmp_path)
    assert queue.undrafted(conn, now()) == []
    assert queue.undrafted(conn, now() + 3600) == [tid]
    assert tid in store.minder_owed(conn, now() + 3600)["undrafted"]
    store.record_decision(conn, f"{tid} drafted to Outlook Drafts", "x", tid)
    assert queue.undrafted(conn, now() + 3600) == []


def test_old_drafts_are_left_to_ben(conn, tmp_path):
    _draft(conn, tmp_path, completed_ago=3 * 86400)
    assert queue.undrafted(conn, now()) == []


def test_drafts_mark_late_from_the_spec_reply_date(conn, tmp_path):
    _draft(conn, tmp_path)
    v = store.draft_replies(conn)
    assert v["drafts"][0]["client_since_from"] == "email" and v["drafts"][0]["late"] and v["late"] == 1
    assert "late" in report.drafts_page(v)


def test_pipeline_owed_after_promotion_or_checkin_until_noted(conn):
    ensure_promotions(conn)
    _done(conn, pipeline_task_id="SP-1")  # an investigation: not client-visible
    built, _ = _done(conn, kind="code_change", spec={"cvc": True}, pipeline_task_id="SP-2",
                     result={"done": True, "files_changed": [{"path": "a.p"}], "summary": "s"})
    plain, _ = _done(conn, kind="code_change", pipeline_task_id="SP-3",
                     result={"done": True, "files_changed": [{"path": "b.p"}], "summary": "s"})
    unlinked, _ = _done(conn, kind="code_change", result={"done": True, "files_changed": [{"path": "c.p"}], "summary": "s"})
    assert queue.pipeline_owed(conn, now()) == [plain]
    store.record_decision(conn, "Pipeline note SP-3: checked in b.p", "x", plain)
    assert queue.pipeline_owed(conn, now()) == []
    # A child inherits its parent's link.
    child, _ = _done(conn, kind="code_change", parent_id=built,
                     result={"done": True, "files_changed": [{"path": "a.p"}], "summary": "s"})
    assert queue.pipeline_id(conn, child) == "SP-2" and queue.pipeline_owed(conn, now()) == [child]


def test_create_warns_about_missing_link_and_reply_date(conn):
    r = store.create_task(conn, "draft_reply", "t", {"q": "x"}, assigned_to="MAG")
    assert len(r["warnings"]) == 2
    r = store.create_task(conn, "draft_reply", "t", {"reply_date": "2026-10-27"}, assigned_to="MAG",
                          pipeline_task_id="SP-9")
    assert "warnings" not in r
    assert "warnings" not in store.create_task(conn, "investigation", "t", {}, assigned_to="MAG")


def test_queue_posts_through_the_server(tmp_path, monkeypatch):
    import functools
    import socket
    import threading
    import time
    import urllib.request

    from coord_mcp import usage

    monkeypatch.setattr(usage, "ingest", lambda conn: None)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    make = functools.partial(connect, tmp_path / "srv.db")
    c = make()
    _done(c, follow_ups=[_ben("Approve A")])
    (fid,) = _ids(c)
    threading.Thread(target=report.serve, args=(make, port), daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    with urllib.request.urlopen(base + "/queue", timeout=10) as r:
        assert fid in r.read().decode()
    req = urllib.request.Request(base + "/api/queue", data=f"action=drop&id={fid}".encode(),
                                 headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        assert json.loads(r.read())["count"] == 1
    assert _ids(c) == []
    c.close()


def test_queue_page_leads_with_client_cards_that_filter_it(conn):
    _done(conn, "LNK", follow_ups=[_ben("Approve LNK thing")])
    _done(conn, "MAG", follow_ups=[_ben("Approve MAG thing")])
    fleet = store.fleet(conn, store.operator_view(conn), ("LNK", "MAG"))
    html = report.queue_page(queue.ben_queue(conn), fleet=fleet, client="MAG")
    assert html.index('class="fleet"') < html.index('action="/api/queue"')
    assert 'href="/queue?client=LNK"' in html and 'href="/queue"' in html
    assert "Approve MAG thing" in html and "Approve LNK thing" not in html


# ------------------------------------------------ grouping and expiry (11-13)

ROWS = ["Rule on row 12: keep UM EA or change to CS", "Rule on row 13: keep UM EA or change to BX",
        "Rule on row 27: keep UM LB or change to KG"]


def test_same_shape_rulings_for_ben_are_refused_until_grouped(conn):
    tid = store.create_task(conn, "investigation", "t", {"q": "x"}, assigned_to="MAG")["task_id"]
    store.get_task(conn, tid, reader="MAG")
    with pytest.raises(ValueError, match="one workbook"):
        store.complete_task(conn, tid, {**INV, "follow_ups": [_ben(w) for w in ROWS]})
    # Nothing was written: the task is still open and has no loose ends.
    assert conn.execute("SELECT state FROM tasks WHERE id=?", (tid,)).fetchone()["state"] != "done"
    assert conn.execute("SELECT COUNT(*) n FROM follow_ups WHERE task_id=?", (tid,)).fetchone()["n"] == 0
    out = store.complete_task(conn, tid, {**INV, "follow_ups": [
        _ben("Rule on the 3 UM rows in work/um-rulings.xlsx (answer column D)")]})
    assert out["state"] == "done" and out["follow_ups"] == 1


@pytest.mark.parametrize("whats", [
    ROWS[:2],                                                        # two is not a pattern
    ["Approve the plan", "Call Darren about the VM", "Promote the status report on /deliverables"],
    ["Query so_mstr for site 10", "Query so_mstr for site 20", "Query so_mstr for site 30"],  # routed to the client
    ["Review work/a.xlsx", "Review work/b.xlsx", "Review work/c.xlsx"],  # each already names a file
])
def test_distinct_or_few_items_are_not_grouped(whats):
    assert queue.ungrouped([_ben(w) for w in whats], "MAG") == []


def test_client_items_never_count_toward_grouping():
    assert queue.ungrouped([{"who": "MAG", "what": w} for w in ROWS], "MAG") == []


DAY = 86400


def _aged(conn, fid, days):
    conn.execute("UPDATE follow_ups SET created_at=? WHERE id=?", (now() - days * DAY, fid))


def _fid(conn, tid):
    return conn.execute("SELECT id FROM follow_ups WHERE task_id=? ORDER BY idx", (tid,)).fetchone()["id"]


def test_expire_defers_old_undated_ben_items_behind_one_decision(conn):
    old, _ = _done(conn, follow_ups=[_ben("Approve the plan")])
    new, _ = _done(conn, follow_ups=[_ben("Decide whether to keep the report")])
    mag, _ = _done(conn, follow_ups=[{"who": "MAG", "what": "Rerun the load"}])
    for t in (old, mag):
        _aged(conn, _fid(conn, t), 20)
    dry = queue.expire(conn)
    assert dry["count"] == 1 and not dry["applied"] and dry["by_client"] == {"MAG": 1}
    assert conn.execute("SELECT COUNT(*) n FROM follow_ups WHERE state='deferred'").fetchone()["n"] == 0
    out = queue.expire(conn, apply=True)
    assert out["applied"] and out["count"] == 1
    states = {t: conn.execute("SELECT state, resolved_by FROM follow_ups WHERE task_id=?", (t,)).fetchone()
              for t in (old, new, mag)}
    assert states[old]["state"] == "deferred" and states[old]["resolved_by"] == out["decision_id"]
    assert states[new]["state"] == "open" and states[mag]["state"] == "open"
    d = conn.execute("SELECT statement FROM decisions WHERE id=?", (out["decision_id"],)).fetchone()
    assert d["statement"].startswith("Auto-deferred 1 Ben loose end(s) open over 14 days: MAG 1")


def test_expire_keeps_dated_items_replies_and_the_presented_question(conn):
    dated, _ = _done(conn, follow_ups=[_ben("Approve the plan")])
    conn.execute("UPDATE follow_ups SET due_at=? WHERE task_id=?", (now() + DAY, dated))
    reply, _ = _done(conn, kind="draft_reply", follow_ups=[_ben("Review and draft")], result={
        "done": True, "to": ["a@b.c"], "subject": "s", "body_path": "work/r.txt", "summary": "s"})
    asked, _ = _done(conn, follow_ups=[_ben("Decide whether to keep the report")])
    for t in (dated, reply, asked):
        _aged(conn, _fid(conn, t), 30)
    store.present_question(conn, "Keep it?", ["Yes", "No"], follow_up_id=_fid(conn, asked))
    assert queue.expire(conn, apply=True)["count"] == 0


def test_undo_reopens_and_a_reopened_item_is_not_expired_again(conn):
    tid, _ = _done(conn, follow_ups=[_ben("Approve the plan")])
    _aged(conn, _fid(conn, tid), 20)
    dec = queue.expire(conn, apply=True)["decision_id"]
    assert queue.unexpire(conn, dec)["reopened"] == 1
    assert conn.execute("SELECT state FROM follow_ups WHERE task_id=?", (tid,)).fetchone()["state"] == "open"
    assert queue.expire(conn, apply=True)["count"] == 0


def test_daily_pass_stamps_first_then_expires_once_a_day(conn):
    tid, _ = _done(conn, follow_ups=[_ben("Approve the plan")])
    _aged(conn, _fid(conn, tid), 20)
    t = now()
    assert queue.expire_due(conn, t) is None          # first pass on a board: clock only
    assert queue.expire_due(conn, t + 3600) is None   # not due yet
    out = queue.expire_due(conn, t + DAY + 1)
    assert out["applied"] and out["count"] == 1
