"""Pipeline lines: every task rolls up to one SharePoint pipeline item
(coord_mcp/pipeline.py, docs/pipeline-lines.md). No test here calls
SharePoint: the mod's get_pipeline and create_task are stood in for by
payloads and answers posted to the board."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from coord_mcp import pipeline, pipeline_page, queue, report, store
from coord_mcp.db import connect

NOW = 1_800_000_000  # 2027-01-15

PAYLOAD = {
    "items": [
        {"id": 231, "title": "DEV-39 Sales Order Change Audit", "client": "MAG", "status": "In Development",
         "priority": "Medium", "endDate": "2099-01-01", "estHoursLow": 8, "estHoursHigh": 12, "pct": 40},
        {"id": 232, "title": "L04102-86 4.23 / 4.24 Sales Order Alloc - No Update", "client": "Royal Technologies",
         "status": "Scheduled", "priority": "High", "endDate": "2099-06-01"},
        {"id": 233, "title": "Old overdue fix", "client": "Cascade Die Casting", "status": "In Repair",
         "priority": "High", "endDate": "2020-01-01"},
        {"id": 234, "title": "Shipped long ago", "client": "MAG", "status": "Complete", "priority": "Low"},
    ],
    "unscheduledItems": [
        {"id": 240, "title": "Unscheduled thing", "client": "Acme Widgets", "status": "Needs Scheduling"},
    ],
}


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _task(conn, title="root", client="MAG", parent=None, spec=None, item=None, proposal=None, kind="investigation"):
    return store.create_task(conn, kind, title, spec or {"q": "x"}, assigned_to=client, parent_id=parent,
                             pipeline_task_id=item, pipeline_proposal=proposal)


def _ben_items(conn, client):
    return [dict(r) for r in conn.execute("SELECT * FROM follow_ups WHERE ref=? AND state='open'",
                                          (f"pipeline:{client}",))]


# ------------------------------------------------------------ clients and cache


def test_map_client():
    keys = ["Royal", "Cascade", "MAG", "TS Tech", "P&C"]
    assert pipeline.map_client("Royal Technologies", keys, {}) == "Royal"
    assert pipeline.map_client("Cascade Die Casting", keys, {}) == "Cascade"
    assert pipeline.map_client("MAG", keys, {}) == "MAG"
    assert pipeline.map_client("TS Tech Americas", keys, {}) == "TS Tech"
    assert pipeline.map_client("P&C Group", keys, {}) == "P&C"
    assert pipeline.map_client("Magnetic Corp", keys, {}) is None  # a word, not a prefix of one
    assert pipeline.map_client("Acme Widgets", keys, {}) is None
    assert pipeline.map_client("Acme Widgets", keys, {"Acme Widgets": "MAG"}) == "MAG"
    assert pipeline.map_client(None, keys, {}) is None


def test_sync_fills_and_keeps_cache(conn):
    r = pipeline.sync(conn, json.dumps(PAYLOAD), now_ts=NOW)
    assert r["items"] == 5 and r["unmapped_clients"] == {"Acme Widgets": 1}
    assert pipeline.synced_at(conn) == NOW
    cache = pipeline.items(conn)
    assert cache["232"]["client"] == "Royal" and cache["232"]["sp_client"] == "Royal Technologies"
    assert cache["231"]["est_low"] == 8 and cache["240"]["client"] is None
    assert set(pipeline.items(conn, "MAG")) == {"231", "234"}
    with pytest.raises(ValueError):
        pipeline.sync(conn, {"items": [], "unscheduledItems": []})
    assert len(pipeline.items(conn)) == 5 and pipeline.synced_at(conn) == NOW  # a failed read keeps the cache
    pipeline.sync(conn, {"items": PAYLOAD["items"][:1]}, now_ts=NOW + 60)
    assert set(pipeline.items(conn)) == {"231"}  # replaced, not merged


def test_sync_records_last_attempt(conn):
    """2026-11-16: a sync that never lands must say why on the board."""
    assert pipeline.sync_last(conn) is None
    stub = "Error: result (224,641 characters) exceeds maximum allowed tokens. Output has been saved to x.txt."
    with pytest.raises(ValueError, match="not JSON"):
        pipeline.sync(conn, stub, now_ts=NOW)
    last = pipeline.sync_last(conn)
    assert last["stage"] == "refused" and not last["ok"] and "not JSON" in last["detail"]
    pipeline.sync_report(conn, "get_pipeline failed", False, "no answer in 120s", now_ts=NOW + 5)
    assert pipeline.lines(conn, now_ts=NOW + 10)["sync_last"]["stage"] == "get_pipeline failed"
    v = pipeline.lines(conn, now_ts=NOW + 10)
    html = pipeline_page.pipeline_page(v, None, ["MAG"])
    assert "last sync" in html and "no answer in 120s" in html
    pipeline.sync(conn, json.dumps(PAYLOAD), now_ts=NOW + 20)
    last = pipeline.sync_last(conn)
    assert last["ok"] and last["stage"] == "synced" and last["detail"].startswith("5 items")


def test_client_map_override_used_by_sync_and_create(conn):
    pipeline._set_meta(conn, pipeline.MAP_KEY, {"Acme Widgets": "MAG"})
    pipeline.sync(conn, PAYLOAD, now_ts=NOW)
    assert pipeline.items(conn)["240"]["client"] == "MAG"
    assert pipeline.sp_client(conn, "MAG") == "Acme Widgets"  # the override wins
    assert pipeline.sp_client(conn, "Royal") == "Royal Technologies"  # the name its items use
    assert pipeline.sp_client(conn, "Furlani") == "Furlani"  # neither: the key


# ------------------------------------------------------------ the rule


@pytest.mark.pipeline_rule
def test_rule_refuses_root_client_task_with_no_line(conn):
    queue.set_switch(conn, "require_pipeline", True)
    with pytest.raises(ValueError, match="no pipeline item"):
        _task(conn)
    root = _task(conn, item="231")["task_id"]
    _task(conn, "child", parent=root)  # inherits
    code = _task(conn, "code", kind="code_change", item="231")["task_id"]
    conn.execute("UPDATE tasks SET state='done' WHERE id=?", (code,))
    _task(conn, "check in", kind="code_change", spec={"checkin_of": code})  # inherits
    _task(conn, "master work", client="Master")  # not a client
    store.create_task(conn, "investigation", "unassigned", {"q": "x"})  # no client yet
    with pytest.raises(ValueError, match="not both"):
        _task(conn, item="231", proposal={"title": "x"})
    with pytest.raises(ValueError, match="needs assigned_to"):
        store.create_task(conn, "investigation", "x", {"q": "x"}, pipeline_proposal={"title": "x"})
    with pytest.raises(ValueError, match="unknown keys"):
        _task(conn, proposal={"title": "x", "due": "friday"})
    with pytest.raises(ValueError, match="priority"):
        _task(conn, proposal={"title": "x", "priority": "Urgent"})
    queue.set_switch(conn, "require_pipeline", False)
    _task(conn, "allowed with the switch off")


@pytest.mark.pipeline_rule
def test_rule_warns_on_item_missing_from_cache(conn):
    queue.set_switch(conn, "require_pipeline", True)
    assert "warnings" not in _task(conn, item="999")  # no cache yet: nothing to check against
    pipeline.sync(conn, PAYLOAD, now_ts=NOW)
    assert "warnings" not in _task(conn, item="231") or not any(
        "not in the cached" in w for w in _task(conn, item="231").get("warnings", []))
    out = _task(conn, item="999")
    assert any("999 is not in the cached pipeline" in w for w in out["warnings"])


@pytest.mark.pipeline_rule
def test_rule_on_proposal_links(conn):
    queue.set_switch(conn, "require_pipeline", True)
    t = _task(conn, proposal={"title": "Change audit", "origin_id": "DEV-40"})
    pid = t["pipeline_task_id"]
    assert pid.startswith("p:")
    _task(conn, "second on it", item=pid)  # an open proposal is a line
    pipeline.decide(conn, pid, "drop")
    with pytest.raises(ValueError, match="dropped"):
        _task(conn, "after drop", item=pid)


# ------------------------------------------------------------ proposals


def test_proposals_reuse_and_one_ben_item_per_client(conn):
    a = _task(conn, "a", proposal={"title": "Sales Order Change Audit", "origin_id": "DEV-39"})
    b = _task(conn, "b", proposal={"title": "sales order change audit", "origin_id": "dev-39"})  # same line
    assert a["pipeline_task_id"] == b["pipeline_task_id"]
    assert a["pipeline_proposal"]["reused"] is False and b["pipeline_proposal"]["reused"] is True
    c = _task(conn, "c", proposal={"title": "Pricing loader", "priority": "High"})
    [item] = _ben_items(conn, "MAG")
    assert item["who"] == "Ben" and item["what"].startswith("Approve 2 proposed pipeline items for MAG")
    assert item["task_id"] == a["task_id"]
    _task(conn, "r", client="Royal", proposal={"title": "Royal thing"})
    assert len(_ben_items(conn, "Royal")) == 1 and len(_ben_items(conn, "MAG")) == 1
    props = pipeline.proposals(conn, "MAG")
    assert [p["tasks"] for p in props] == [2, 1]
    assert pipeline.label(props[0], "") == "DEV-39 Sales Order Change Audit"
    assert conn.execute("SELECT pipeline_task_id FROM tasks WHERE id=?", (c["task_id"],)).fetchone()[0] \
        == c["pipeline_task_id"]


def test_decide_approve_drop_link(conn):
    a = _task(conn, "a", proposal={"title": "One"})
    b = _task(conn, "b", proposal={"title": "Two"})
    c = _task(conn, "c", proposal={"title": "Three"})
    kid = _task(conn, "kid", parent=c["task_id"])["task_id"]
    r = pipeline.decide(conn, a["pipeline_task_id"], "approve")
    assert r["state"] == "approved" and r["decision_id"]
    assert pipeline.decide(conn, a["pipeline_task_id"], "approve")["unchanged"]
    assert "Approve 2 proposed" in _ben_items(conn, "MAG")[0]["what"]  # the count follows
    pipeline.decide(conn, b["pipeline_task_id"], "drop")
    assert conn.execute("SELECT pipeline_task_id FROM tasks WHERE id=?", (b["task_id"],)).fetchone()[0] is None
    with pytest.raises(ValueError, match="dropped"):
        pipeline.decide(conn, b["pipeline_task_id"], "approve")
    with pytest.raises(ValueError, match="item number"):
        pipeline.decide(conn, c["pipeline_task_id"], "link", "DEV-1")
    r = pipeline.decide(conn, c["pipeline_task_id"], "link", "#231")
    assert r["pipeline_id"] == "231" and r["relinked"] == 1
    assert queue.pipeline_id(conn, kid) == "231"
    assert _ben_items(conn, "MAG") == []  # nothing left proposed: Ben's item closed
    with pytest.raises(ValueError, match="unknown action"):
        pipeline.decide(conn, a["pipeline_task_id"], "maybe")
    with pytest.raises(KeyError):
        pipeline.decide(conn, "p:nope", "approve")


# ------------------------------------------------------------ the create lease


def test_create_lease_and_relink(conn):
    pipeline.sync(conn, PAYLOAD, now_ts=NOW)
    t = _task(conn, "royal root", client="Royal", proposal={"title": "Alloc", "origin_id": "L04102-87",
                                                             "priority": "High", "note": "From Ann's email"})
    kid = _task(conn, "royal kid", client="Royal", parent=t["task_id"])["task_id"]
    pid = t["pipeline_proposal"]["id"]
    pipeline.decide(conn, pid, "approve")
    assert pipeline.claim_create(conn, now_ts=NOW) is None  # switch pipeline_create off (tests)
    queue.set_switch(conn, "pipeline_create", True)
    c = pipeline.claim_create(conn, now_ts=NOW)
    assert c == {"proposal_id": pid, "title": "L04102-87 Alloc", "client": "Royal Technologies",
                 "priority": "High", "note": c["note"]}
    assert t["task_id"] in c["note"] and "From Ann's email" in c["note"]
    assert pipeline.claim_create(conn, now_ts=NOW + 10) is None  # leased
    with pytest.raises(ValueError, match="no open create claim"):
        pipeline.create_done(conn, "other", "ok", '{"id": 1}')
    r = pipeline.create_done(conn, pid, "ok", 'Created task {"id": 512, "title": "L04102-87 Alloc"}', now_ts=NOW + 20)
    assert r["status"] == "ok" and r["pipeline_id"] == "512" and r["relinked"] == 1
    assert queue.pipeline_id(conn, kid) == "512"
    assert pipeline.items(conn)["512"]["title"] == "L04102-87 Alloc"  # in the cache until the next sync
    assert pipeline.proposals(conn, states=("created",))[0]["pipeline_id"] == "512"
    assert pipeline.claim_create(conn, now_ts=NOW + 30) is None  # nothing approved left


def test_create_error_retries_later_and_ok_without_id_holds(conn):
    queue.set_switch(conn, "pipeline_create", True)
    a = _task(conn, "a", proposal={"title": "One"})["pipeline_proposal"]["id"]
    b = _task(conn, "b", proposal={"title": "Two"})["pipeline_proposal"]["id"]
    pipeline.decide(conn, a, "approve", now_ts=NOW)
    pipeline.decide(conn, b, "approve", now_ts=NOW + 1)
    assert pipeline.claim_create(conn, now_ts=NOW)["proposal_id"] == a
    r = pipeline.create_done(conn, a, "error", "SharePoint 503", now_ts=NOW)
    assert r["status"] == "error"
    assert pipeline.proposals(conn, states=("approved",))[0]["detail"] == "SharePoint 503"
    assert pipeline.claim_create(conn, now_ts=NOW + 5)["proposal_id"] == b  # a waits out its retry
    pipeline.create_done(conn, b, "ok", "Done, the item is live.", now_ts=NOW + 5)  # no id in the answer
    held = {p["id"]: p for p in pipeline.proposals(conn, states=("approved",))}
    assert "not retried" in held[b]["detail"]
    later = NOW + pipeline.CREATE_RETRY + 10
    assert pipeline.claim_create(conn, now_ts=later)["proposal_id"] == a  # a again, never b
    pipeline.create_done(conn, a, "ok", "", item_id="600", now_ts=later)
    assert pipeline.claim_create(conn, now_ts=later + 1) is None
    assert pipeline.decide(conn, b, "link", "601")["pipeline_id"] == "601"  # Ben links the held one


def test_item_id_in_answers():
    assert pipeline._item_id_in('{"id": 77}') == "77"
    assert pipeline._item_id_in('{"item": {"id": 78}}') == "78"
    assert pipeline._item_id_in("Created task #79 (verified)") == "79"
    assert pipeline._item_id_in("nothing here") is None


# ------------------------------------------------------------ the view


def test_lines_ranked_with_board_work(conn):
    pipeline.sync(conn, PAYLOAD, now_ts=NOW)
    root = _task(conn, "audit root", item="231")["task_id"]
    _task(conn, "audit kid", parent=root)
    _task(conn, "royal", client="Royal", item="232")
    _task(conn, "proposed", proposal={"title": "New line", "priority": "Low"})
    v = pipeline.lines(conn, now_ts=NOW)
    ids = [x["pipeline_id"] for x in v["lines"]]
    assert ids[:3] == ["233", "232", "231"]  # High overdue, High, Medium
    assert "234" not in ids  # complete with no board work
    assert "240" not in ids  # no board client
    audit = next(x for x in v["lines"] if x["pipeline_id"] == "231")
    assert audit["open"] == 2 and audit["tasks"] == 2 and audit["client"] == "MAG"
    assert next(x for x in v["lines"] if x["pipeline_id"] == "233")["overdue"] is True
    prop = next(x for x in v["lines"] if x["proposal"])
    assert prop["status"] == "Proposed" and prop["label"] == "New line" and prop["open"] == 1
    mag = [x["pipeline_id"] for x in pipeline.lines(conn, "MAG", now_ts=NOW)["lines"]]
    assert "232" not in mag and "231" in mag
    assert len(v["proposals"]) == 1 and v["synced_at"] == NOW


@pytest.mark.pipeline_rule
def test_internal_needs_no_line(conn):
    """Logan's own work bills to no pipeline item (Ben, 2026-11-23)."""
    from coord_mcp import hours
    queue.set_switch(conn, "require_pipeline", True)
    assert "Internal" in hours.CLIENTS and "internal" in store.client_keys(conn)
    root = _task(conn, "template work", client="Internal")["task_id"]
    _task(conn, "named line", client="Internal", item="231")  # a line is still allowed
    assert pipeline.unfiled(conn) == [] and pipeline.unfiled(conn, "Internal") == []
    assert not store.is_lead(conn, "Internal")  # its session is a client, not master
    assert store.get_task(conn, root)["assigned_to"] == "Internal"


def test_unfiled_and_set_task(conn):
    pipeline.sync(conn, PAYLOAD, now_ts=NOW)
    bare = _task(conn, "bare root")["task_id"]
    _task(conn, "bare kid", parent=bare)
    _task(conn, "filed", item="231")
    _task(conn, "master", client="Master")
    rows = pipeline.unfiled(conn)
    assert [r["task_id"] for r in rows] == [bare]
    assert pipeline.unfiled(conn, "Royal") == []
    with pytest.raises(ValueError, match="neither"):
        pipeline.set_task(conn, bare, "DEV-39")
    pipeline.set_task(conn, bare, "#231")
    assert pipeline.unfiled(conn) == []
    pipeline.set_task(conn, bare, "none")
    assert [r["task_id"] for r in pipeline.unfiled(conn)] == [bare]


def test_backfill_dump_and_apply(conn, tmp_path):
    r1 = _task(conn, "loader work")["task_id"]
    _task(conn, "loader kid", parent=r1)
    r2 = _task(conn, "design A")["task_id"]
    r3 = _task(conn, "design B")["task_id"]
    _task(conn, "other client", client="Royal")
    rows = pipeline.backfill_dump(conn, "MAG")
    assert {r["task_id"] for r in rows} == {r1, r2, r3}
    assert next(r for r in rows if r["task_id"] == r1)["tasks_in_chain"] == 2
    for r in rows:
        if r["task_id"] == r1:
            r["pipeline_id"] = "231"
        else:
            r["proposal_title"], r["origin_id"] = "MAG design", "MAG-7"
    rows.append({"task_id": "nope", "pipeline_id": "1"})
    dry = pipeline.backfill_apply(conn, rows)
    assert dry["dry_run"] and dry["would_link"] == 3 and dry["errors"] == ["row 4: unknown task nope"]
    assert len(pipeline.unfiled(conn, "MAG")) == 3  # a dry run changes nothing
    path = tmp_path / "map.csv"
    import csv
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=pipeline.BACKFILL_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows[:3])
    done = pipeline.backfill_apply(conn, pipeline._read_rows(str(path)), yes=True)
    assert done["linked"] == 3
    assert pipeline.unfiled(conn, "MAG") == []
    p2 = conn.execute("SELECT pipeline_task_id FROM tasks WHERE id=?", (r2,)).fetchone()[0]
    p3 = conn.execute("SELECT pipeline_task_id FROM tasks WHERE id=?", (r3,)).fetchone()[0]
    assert p2 == p3 and p2.startswith("p:")  # the same title shares one proposal
    assert len(_ben_items(conn, "MAG")) == 1


# ------------------------------------------------------------ the page


def test_page_render(conn):
    pipeline.sync(conn, PAYLOAD, now_ts=NOW)
    _task(conn, "filed", item="231")
    _task(conn, "bare <root>")
    p = _task(conn, "p", proposal={"title": "Pricing & loader", "origin_id": "DEV-41"})["pipeline_proposal"]["id"]
    q = _task(conn, "q", proposal={"title": "Approved one"})["pipeline_proposal"]["id"]
    pipeline.decide(conn, q, "approve")
    conn.execute("UPDATE pipeline_proposals SET detail='SharePoint 503' WHERE id=?", (q,))
    keys = ["MAG", "Royal"]
    html = pipeline_page.pipeline_page(pipeline.lines(conn), None, keys)
    assert "DEV-39 Sales Order Change Audit" in html and "DEV-41 Pricing &amp; loader" in html
    assert "bare &lt;root&gt;" in html and "No pipeline item" in html
    assert f'name="proposal" value="{p}"' in html and ">Create</button>" in html and ">Drop</button>" in html
    assert "Approved, awaiting creation" in html and "SharePoint 503" in html
    assert 'action="/api/pipeline/set"' in html and "cache " in html
    assert 'href="/pipeline" aria-current=page>Pipeline</a>' in html  # the nav marks this page
    one = pipeline_page.pipeline_page(pipeline.lines(conn, "Royal"), "Royal", keys, "proposal x: approved")
    assert "<option selected>Royal</option>" in one and "proposal x: approved" in one
    assert "DEV-39" not in one
    empty = pipeline_page.pipeline_page(pipeline.lines(connect(":memory:")), None, keys)
    assert "never synced" in empty
    strip = pipeline_page.board_strip(conn)
    assert "1 proposed item await" in strip and "1 root task with no line" in strip and "/pipeline" in strip
    assert ("/pipeline", "Pipeline") in report.NAV
    assert report.NAV.index(("/pipeline", "Pipeline")) == report.NAV.index(("/board", "Board")) + 1


def test_dashboard_routes(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    c = connect(db)
    queue.set_switch(c, "pipeline_create", True)
    bare = _task(c, "bare root")["task_id"]
    prop = _task(c, "p", proposal={"title": "Make me"})["pipeline_proposal"]["id"]
    drop = _task(c, "d", proposal={"title": "Drop me"})["pipeline_proposal"]["id"]
    c.close()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    th = threading.Thread(target=report.serve, args=(lambda: connect(db),), kwargs={"port": port}, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/pipeline", timeout=2)
            break
        except OSError:
            time.sleep(0.1)

    def post(path, form, accept=None):
        req = urllib.request.Request(base + path, data=urllib.parse.urlencode(form).encode(), method="POST")
        if accept:
            req.add_header("Accept", accept)
        return urllib.request.urlopen(req)

    page = urllib.request.urlopen(base + "/pipeline").read().decode()
    assert "Make me" in page and "bare root" in page
    assert "Pipeline:" in urllib.request.urlopen(base + "/board").read().decode()
    assert urllib.request.urlopen(base + "/pipeline?client=MAG").status == 200
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(base + "/pipeline?client=Nobody")

    r = post("/api/pipeline/sync", {"payload": json.dumps(PAYLOAD)})
    assert json.loads(r.read())["items"] == 5
    with pytest.raises(urllib.error.HTTPError) as e:
        post("/api/pipeline/sync", {"payload": "{}"})
    assert e.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as e:
        post("/api/pipeline/sync", {"payload": "Error: result exceeds maximum allowed tokens."})
    assert e.value.code == 400  # not JSON is a refusal, not a 500
    r = post("/api/pipeline/sync/report", {"stage": "get_pipeline failed", "ok": "0", "detail": "timeout"})
    assert json.loads(r.read())["stage"] == "get_pipeline failed"

    r = post("/api/pipeline/proposal", {"proposal": prop, "action": "approve", "back": "/pipeline?client=MAG"})
    assert "/pipeline?client=MAG&done=" in r.url
    r = post("/api/pipeline/proposal", {"proposal": drop, "action": "approve", "back": "https://evil.example"})
    assert urllib.parse.urlparse(r.url).path == "/pipeline"
    r = post("/api/pipeline/proposal", {"proposal": drop, "action": "link", "item": "abc"})
    assert "error=" in r.url  # Ben sees why, on the page
    with pytest.raises(urllib.error.HTTPError) as e:
        post("/api/pipeline/proposal", {"proposal": drop, "action": "link", "item": "abc"}, accept="application/json")
    assert e.value.code == 400
    post("/api/pipeline/set", {"task": bare, "item": "231"})

    claim = json.loads(post("/api/pipeline/create/claim", {}).read())["create"]
    assert claim["proposal_id"] in (prop, drop)
    done = json.loads(post("/api/pipeline/create/done", {"proposal_id": claim["proposal_id"], "status": "ok",
                                                         "detail": '{"id": 777}'}).read())
    assert done["pipeline_id"] == "777"
    with pytest.raises(urllib.error.HTTPError) as e:
        post("/api/pipeline/create/done", {"proposal_id": "nope", "status": "ok"})
    assert e.value.code == 400

    c = connect(db)
    assert queue.pipeline_id(c, bare) == "231"
    assert c.execute("SELECT COUNT(*) FROM pipeline_proposals WHERE state='created'").fetchone()[0] == 1
    c.close()
