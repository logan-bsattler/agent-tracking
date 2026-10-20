"""Operator board: the dashboard's grouping of the board for a human."""

from __future__ import annotations

from pathlib import Path

import pytest

from coord_mcp import report, store
from coord_mcp.db import connect, now

INV = {"done": True, "verdict": "Browse is superseded by FDD-037.", "confidence": "high", "evidence": ["spec"]}


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _task(conn, client, title="t"):
    return store.create_task(conn, "investigation", title, {"q": "x"}, assigned_to=client)["task_id"]


def test_each_state_lands_in_its_group(conn):
    running = _task(conn, "LNK", "still going")
    store.get_task(conn, running, reader="LNK")
    waiting = _task(conn, "Royal", "never delivered")
    parked = _task(conn, "MAG", "ran out of context")
    store.park_task(conn, parked, next_step="resume at step 3")
    failed = _task(conn, "Moog", "needs the box")
    store.complete_task(conn, failed, {"done": False, "reason": "VPN down", "retryable": True})
    done = _task(conn, "LNK", "answered")
    store.complete_task(conn, done, INV)

    v = store.operator_view(conn)
    ids = {g: [d["id"] for d in v[g]] for g in ("needs_you", "master_owes", "running", "waiting", "recent")}
    assert ids == {"needs_you": [failed], "master_owes": [parked], "running": [running],
                   "waiting": [waiting], "recent": [done]}
    assert v["needs_you"][0]["note"] == "VPN down"
    assert v["recent"][0]["note"] == INV["verdict"]


def test_only_the_assignee_picks_a_task_up(conn):
    t = _task(conn, "TS Tech")
    store.get_task(conn, t)
    store.get_task(conn, t, reader="agents")  # the master's folder
    assert [d["id"] for d in store.operator_view(conn)["waiting"]] == [t]
    assert "picked_up" not in store.board(conn)["live"][0]
    store.get_task(conn, t, reader="ts tech")
    assert [d["id"] for d in store.operator_view(conn)["running"]] == [t]
    assert store.board(conn)["live"][0]["picked_up"] is True


def test_redispatch_after_park_is_running_again(conn):
    t = _task(conn, "MAG")
    store.get_task(conn, t, reader="MAG")
    store.park_task(conn, t, next_step="resume")
    assert store.board(conn)["live"][0].get("awaiting_redispatch") is True
    conn.execute("UPDATE task_parks SET created_at=created_at-10 WHERE task_id=?", (t,))
    store.get_task(conn, t, reader="MAG")
    live = store.board(conn)["live"][0]
    assert "awaiting_redispatch" not in live and live["picked_up"] is True
    assert [d["id"] for d in store.operator_view(conn)["running"]] == [t]


def test_page_splits_running_from_not_picked_up(conn):
    a, b = _task(conn, "LNK", "in flight"), _task(conn, "Moog", "undelivered")
    store.get_task(conn, a, reader="LNK")
    html = report.board_page(store.operator_view(conn))
    assert html.index("Running") < html.index("in flight") < html.index("Not picked up") < html.index("undelivered")


def test_non_retryable_failure_says_so(conn):
    t = _task(conn, "LNK")
    store.complete_task(conn, t, {"done": False, "reason": "spec names a table that does not exist", "retryable": False})
    assert store.operator_view(conn)["needs_you"][0]["note"].endswith("(not retryable)")


def test_old_done_drops_off(conn):
    t = _task(conn, "LNK")
    store.complete_task(conn, t, INV)
    conn.execute("UPDATE tasks SET completed_at=? WHERE id=?", (now() - 49 * 3600, t))
    assert store.operator_view(conn, recent_h=48)["recent"] == []


def test_pbe_done_is_the_operators_to_deliver(conn):
    t = _task(conn, "PBE")
    store.complete_task(conn, t, INV)
    v = store.operator_view(conn)
    assert [d["id"] for d in v["needs_you"]] == [t]
    assert "WinSCP" in v["needs_you"][0]["note"]
    assert [d["id"] for d in v["recent"]] == [t]


def test_open_intents_are_the_masters(conn):
    store.propose_intent(conn, "Email asks for a report", "email")
    v = store.operator_view(conn)
    assert v["needs_you"] == []
    assert v["master_owes"][0]["title"].startswith("1 open intent")


def test_page_says_nothing_needs_you_when_clear(conn):
    _task(conn, "LNK")
    html = report.board_page(store.operator_view(conn))
    assert "Nothing needs you." in html
    assert "<title>Coord board</title>" in html


def test_page_counts_needs_in_title_and_escapes(conn):
    t = _task(conn, "Moog", "<script>x</script>")
    store.complete_task(conn, t, {"done": False, "reason": "VPN down", "retryable": True})
    html = report.board_page(store.operator_view(conn))
    assert "<title>(1) Coord board</title>" in html
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html


def test_board_rows_link_to_detail(conn):
    t = _task(conn, "LNK")
    assert f'href="/board/task/{t}"' in report.board_page(store.operator_view(conn))


def test_detail_shows_spec_result_parks_and_decisions(conn):
    t = store.create_task(conn, "investigation", "Check AMEX", {"question": "Is FDD-025 current?"},
                          assigned_to="LNK")["task_id"]
    store.park_task(conn, t, next_step="read the tracker", done=["read the spec"])
    store.complete_task(conn, t, {**INV, "evidence": ["approved 8/14", "no code exists"]})
    store.record_decision(conn, "Re-baseline LNK AMEX at 10%", because="scope moved to FDD-037", task_id=t)
    d = store.task_detail(conn, t)
    assert d["spec"] == {"question": "Is FDD-025 current?"} and d["result"]["confidence"] == "high"
    html = report.task_page(d)
    for s in ("Is FDD-025 current?", "no code exists", "read the tracker", "Park 1 of 1",
              "Re-baseline LNK AMEX at 10%", "st-done"):
        assert s in html


def test_detail_for_unknown_task_is_not_an_error(conn):
    assert store.task_detail(conn, "nope") is None
    page = report.task_page(None, "<x>")
    assert "No such task" in page and "<x>" not in page


def test_server_answers_while_another_connection_hangs(tmp_path):
    """A browser holding a socket open must not stall the next page load."""
    import functools
    import socket
    import threading
    import urllib.request

    from coord_mcp.db import connect as db_connect

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    make = functools.partial(db_connect, tmp_path / "srv.db")
    make().close()
    threading.Thread(target=report.serve, args=(make, port), daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            import time
            time.sleep(0.05)
    idle = socket.create_connection(("127.0.0.1", port))  # sends nothing, ever
    try:
        with urllib.request.urlopen(base + "/board", timeout=5) as r:
            assert r.status == 200 and b"Coord board" in r.read()
    finally:
        idle.close()


def test_every_page_answers_through_the_server(tmp_path, monkeypatch):
    """Route bodies share serve()'s closure; a local that shadows `days` kills "/"."""
    import functools
    import socket
    import threading
    import time
    import urllib.request

    from coord_mcp import usage
    from coord_mcp.db import connect as db_connect

    monkeypatch.setattr(usage, "ingest", lambda conn: None)  # no real transcripts
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    make = functools.partial(db_connect, tmp_path / "srv.db")
    make().close()
    threading.Thread(target=report.serve, args=(make, port), daemon=True).start()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    for path in ("/", "/board", "/deliverables", "/hours"):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
            assert r.status == 200, path


def test_home_is_served_from_the_ingest_threads_copy(tmp_path, monkeypatch):
    """"/" must not query or render per request: the ingest thread builds it."""
    import functools
    import socket
    import threading
    import time
    import urllib.request

    from coord_mcp import usage
    from coord_mcp.db import connect as db_connect

    monkeypatch.setattr(usage, "ingest", lambda conn: None)
    renders = []
    real = report.render
    monkeypatch.setattr(report, "render", lambda *a, **k: renders.append(1) or real(*a, **k))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    make = functools.partial(db_connect, tmp_path / "srv.db")
    make().close()
    threading.Thread(target=report.serve, args=(make, port), daemon=True).start()
    for _ in range(100):
        if renders:
            break
        time.sleep(0.05)
    assert renders == [1]  # the ingest thread's first pass built it
    for _ in range(3):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
            assert r.status == 200 and b"<html" in r.read().lower()
    assert renders == [1]


# ---------------------------------------------------------------- loose ends

FU = [{"who": "Shannon", "what": "Answer Q2, Q6, Q9-Q12"}, {"who": "Ben", "what": "Chase Shannon for a date"}]


def test_follow_ups_surface_on_board_and_operator_view(conn):
    t = _task(conn, "LNK", "AMEX state")
    store.complete_task(conn, t, {**INV, "follow_ups": FU})
    b = store.board(conn)
    assert [(x["who"], x["client"], x["task_id"]) for x in b["loose_ends"]] == [("Shannon", "LNK", t), ("Ben", "LNK", t)]
    v = store.operator_view(conn)
    # Ben's own items are in Needs you only; the page no longer lists them twice.
    assert [d["title"] for d in v["loose_ends"]] == ["Answer Q2, Q6, Q9-Q12"]
    assert [d["title"] for d in v["needs_you"]] == ["Chase Shannon for a date"]
    html = report.board_page(v)
    assert "Others owe" in html and "Answer Q2, Q6, Q9-Q12" in html
    assert html.count("Chase Shannon for a date") == 2  # Next up, and its Needs-you type group


def test_resolving_clears_the_loose_end(conn):
    t = _task(conn, "LNK")
    store.complete_task(conn, t, {**INV, "follow_ups": FU})
    a, b = [x["id"] for x in store.board(conn)["loose_ends"]]
    child = store.create_task(conn, "investigation", "Get answers", {"q": "x"}, assigned_to="LNK", parent_id=t)["task_id"]
    assert store.resolve_follow_up(conn, a, task_id=child)["loose_ends_left"] == 1
    dec = store.record_decision(conn, "Not chasing; Shannon owns the date", task_id=t)["decision_id"]
    assert store.resolve_follow_up(conn, b, decision_id=dec)["state"] == "dropped"
    assert store.board(conn)["loose_ends"] == [] and store.operator_view(conn)["needs_you"] == []
    page = report.task_page(store.task_detail(conn, t))
    assert f'href="/board/task/{child}"' in page and "dropped" in page


def test_resolve_needs_exactly_one_real_target(conn):
    t = _task(conn, "LNK")
    store.complete_task(conn, t, {**INV, "follow_ups": FU[:1]})
    fid = store.board(conn)["loose_ends"][0]["id"]
    with pytest.raises(ValueError):
        store.resolve_follow_up(conn, fid)
    with pytest.raises(KeyError):
        store.resolve_follow_up(conn, fid, task_id="nope")
    with pytest.raises(KeyError):
        store.resolve_follow_up(conn, fid, decision_id="nope")
    store.resolve_follow_up(conn, fid, task_id=t)
    with pytest.raises(ValueError):
        store.resolve_follow_up(conn, fid, task_id=t)


def test_needs_you_groups_by_client(conn):
    for client in ("Moog", "Moog", "LNK"):
        t = _task(conn, client, f"{client} blocked")
        store.complete_task(conn, t, {"done": False, "reason": "VPN down", "retryable": True})
    html = report.board_page(store.operator_view(conn))
    assert html.count('<details class="client"') == 2
    assert html.index("<summary>Moog") < html.index("<summary>LNK")


def test_board_reports_loose_end_total_past_the_cap(conn, monkeypatch):
    monkeypatch.setattr(store, "LOOSE_ENDS_CAP", 1)
    t = _task(conn, "LNK")
    store.complete_task(conn, t, {**INV, "follow_ups": FU})
    b = store.board(conn)
    assert [x["what"] for x in b["loose_ends"]] == [FU[0]["what"]] and b["loose_ends_total"] == 2


# ------------------------------------------------------- board readability


def test_master_follow_ups_land_in_master_owes(conn):
    t = _task(conn, "LNK")
    store.complete_task(conn, t, {**INV, "follow_ups": [{"who": "Master", "what": "Fix LNK CLAUDE.md"}]})
    v = store.operator_view(conn)
    assert [d["title"] for d in v["master_owes"]] == ["Fix LNK CLAUDE.md"]
    assert v["loose_ends"] == [] and v["needs_you"] == []


def test_defer_takes_it_off_the_board_until_reopened(conn):
    t = _task(conn, "MAG")
    store.complete_task(conn, t, {**INV, "follow_ups": FU})
    a, b = [x["id"] for x in store.board(conn)["loose_ends"]]
    with pytest.raises(ValueError):
        store.resolve_follow_up(conn, b, defer=True)  # a deferral names its trigger
    dec = store.record_decision(conn, "Defer until first builds", task_id=t)["decision_id"]
    r = store.resolve_follow_up(conn, b, decision_id=dec, defer=True)
    assert r["state"] == "deferred" and r["loose_ends_left"] == 1
    board = store.board(conn)
    assert [x["id"] for x in board["loose_ends"]] == [a] and board["deferred_total"] == 1
    v = store.operator_view(conn)
    assert v["needs_you"] == [] and [d["title"] for d in v["deferred"]] == ["Chase Shannon for a date"]
    assert "Deferred" in report.board_page(v)
    with pytest.raises(ValueError):
        store.resolve_follow_up(conn, b, decision_id=dec, defer=True)
    with pytest.raises(ValueError):
        store.resolve_follow_up(conn, a, reopen=True)  # only a deferred one reopens
    assert store.resolve_follow_up(conn, b, reopen=True)["state"] == "open"
    assert store.board(conn)["deferred_total"] == 0
    assert [d["title"] for d in store.operator_view(conn)["needs_you"]] == ["Chase Shannon for a date"]
    store.resolve_follow_up(conn, b, decision_id=dec, defer=True)
    assert store.resolve_follow_up(conn, b, task_id=t)["state"] == "tasked"  # a deferred one still resolves


def test_failure_with_a_later_decision_is_acknowledged(conn):
    t = _task(conn, "Moog", "needs the box")
    store.complete_task(conn, t, {"done": False, "reason": "VPN down", "retryable": True})
    conn.execute("UPDATE tasks SET completed_at=completed_at-10 WHERE id=?", (t,))
    store.record_decision(conn, "Superseded by the retry", task_id=t)
    v = store.operator_view(conn)
    assert v["needs_you"] == [] and [d["id"] for d in v["acknowledged"]] == [t]
    html = report.board_page(v)
    assert "Acknowledged failures" in html and 'data-fold="closed"' in html


def test_decision_before_the_failure_does_not_acknowledge_it(conn):
    t = _task(conn, "Moog")
    store.record_decision(conn, "Dispatch when the VPN is up", task_id=t)
    conn.execute("UPDATE decisions SET created_at=created_at-10 WHERE task_id=?", (t,))
    store.complete_task(conn, t, {"done": False, "reason": "VPN down", "retryable": True})
    assert [d["id"] for d in store.operator_view(conn)["needs_you"]] == [t]


def test_needs_you_splits_by_type_and_ages_out(conn):
    t = _task(conn, "LNK")
    store.complete_task(conn, t, {**INV, "follow_ups": [
        {"who": "Ben", "what": "Compile xxauto01.p in DEVL"},
        {"who": "Ben", "what": "Review and send the DRAFT to Shelton"},
        {"who": "Ben", "what": "Pick batch-only vs streamline"},
        {"who": "Ben", "what": "Old question nobody answered"}]})
    conn.execute("UPDATE follow_ups SET created_at=? WHERE what LIKE 'Old%'", (now() - 8 * 86400,))
    f = _task(conn, "Moog")
    store.complete_task(conn, f, {"done": False, "reason": "VPN down", "retryable": True})
    v = store.operator_view(conn)
    assert {report.need_type(d) for d in v["needs_you"]} == {
        "Fix failed", "Do in DEVL / on a server", "Review & send", "Decide"}
    html = report.board_page(v)
    assert html.index("Next up") < html.index("Needs you")
    assert html.index("Fix failed") < html.index("Do in DEVL") < html.index("Review &amp; send") < html.index("Decide")
    assert "<title>(4) Coord board</title>" in html  # the stale one does not count
    assert html.index("Stale, over 7 days") < html.index("Old question nobody answered")


def test_v7_database_migrates_follow_ups_in_place(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    c = connect(path)
    t = _task(c, "LNK")
    store.complete_task(c, t, {**INV, "follow_ups": FU})
    c.close()
    raw = sqlite3.connect(str(path))
    sql = raw.execute("SELECT sql FROM sqlite_master WHERE name='follow_ups'").fetchone()[0]
    raw.executescript(
        "ALTER TABLE follow_ups RENAME TO fx;"
        + sql.replace(",'deferred'", "") + ";"
        "INSERT INTO follow_ups SELECT * FROM fx; DROP TABLE fx;"
        "UPDATE meta SET value='7' WHERE key='schema_version';")
    raw.close()
    c = connect(path)
    assert "'deferred'" in c.execute("SELECT sql FROM sqlite_master WHERE name='follow_ups'").fetchone()[0]
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE tbl_name='follow_ups' AND type='index'")}
    assert {"follow_ups_state", "follow_ups_task"} <= names
    assert store.board(c)["loose_ends_total"] == 2
    c.close()


# -------------------------------------------------------------- deliverables

def _pull(conn, client, out, title="pull"):
    tid = store.create_task(conn, "data_pull", title, {"q": "x"}, assigned_to=client)["task_id"]
    store.complete_task(conn, tid, {"done": True, "output_path": out, "rows": 1,
                                    "follow_ups": [{"who": "Ben", "what": "Review it & send"}]})
    return tid


def test_deliverables_resolve_against_the_client_folder(conn, tmp_path):
    (tmp_path / "MAG" / "work").mkdir(parents=True)
    (tmp_path / "MAG" / "work" / "a.md").write_text("# hi", encoding="utf-8")
    tid = _pull(conn, "MAG", "work/a.md (report, per-group tables)")
    _pull(conn, "MAG", "harness.p")  # source, not a deliverable
    v = store.deliverables(conn, root=tmp_path)
    assert [d["name"] for d in v["files"]] == ["a.md"]
    d = v["files"][0]
    assert d["exists"] and d["task_id"] == tid and d["ben"] == ["Review it & send"]
    assert store.deliverable_path(conn, tid, d["path"], root=tmp_path) == tmp_path / "MAG" / "work" / "a.md"
    assert store.deliverable_path(conn, tid, str(tmp_path / "secret.md"), root=tmp_path) is None


def test_deliverables_page_links_files_and_flags_missing(conn, tmp_path):
    (tmp_path / "LNK").mkdir()
    (tmp_path / "LNK" / "x.csv").write_text("a,b", encoding="utf-8")
    _pull(conn, "LNK", "x.csv")
    _pull(conn, "LNK", "gone.xlsx", title="<b>esc</b>")
    html = report.deliverables_page(store.deliverables(conn, root=tmp_path))
    assert "/deliverables/file?task=" in html and "x.csv" in html
    assert "missing" in html and "&lt;b&gt;esc" in html and "Ben: Review it &amp; send" in html
    assert "No deliverables" in report.deliverables_page(store.deliverables(conn, root=tmp_path, client="MAG"))


def test_deliverables_page_is_searchable(conn, tmp_path):
    (tmp_path / "LNK").mkdir()
    (tmp_path / "LNK" / "Payments.csv").write_text("a", encoding="utf-8")
    tid = _pull(conn, "LNK", "Payments.csv", title="AMEX pull")
    v = store.deliverables(conn, root=tmp_path)
    v["q"] = 'amex "x'
    html = report.deliverables_page(v)
    assert 'id="dl-q"' in html and 'value="amex &quot;x"' in html
    row = next(l for l in html.split("<tr") if "Payments.csv" in l)
    assert "payments.csv" in row and "amex pull" in row and tid in row and "review it &amp; send" in row


# ------------------------------------------------------------ opening files

def _named(conn, client, spec_text, result=None):
    tid = store.create_task(conn, "investigation", "t", {"q": spec_text}, assigned_to=client)["task_id"]
    if result:
        store.complete_task(conn, tid, result)
    return tid


def test_task_files_finds_paths_in_spec_result_and_follow_ups(conn, tmp_path):
    (tmp_path / "MAG" / "work" / "Unapproved Docs").mkdir(parents=True)
    for n in ("a.md", "b.csv", "Unapproved Docs/FDD x copy.docx"):
        (tmp_path / "MAG" / "work" / n).write_text("x", encoding="utf-8")
    absolute = tmp_path / "MAG" / "work" / "b.csv"
    tid = _named(conn, "MAG", "update work/a.md, then see xmfagtcp.p", {
        **INV, "evidence": [f"{absolute} (rows)", "work/Unapproved Docs/FDD x copy.docx"],
        "follow_ups": [{"who": "Ben", "what": "send work/gone.xlsx to Joanne"}]})
    files = {Path(f["path"]).name: f for f in store.task_files(conn, tid, root=tmp_path)}
    assert set(files) == {"a.md", "b.csv", "FDD x copy.docx", "gone.xlsx"}  # not the bare program name
    assert files["a.md"]["exists"] and files["a.md"]["raw"] == ["work/a.md"]
    assert files["FDD x copy.docx"]["exists"]  # a name with spaces, found by growing left
    assert not files["gone.xlsx"]["exists"]


def test_task_page_links_files_in_place(conn, tmp_path):
    (tmp_path / "LNK").mkdir()
    (tmp_path / "LNK" / "x.md").write_text("x", encoding="utf-8")
    tid = _named(conn, "LNK", "read x.md and work/old.csv")
    d = store.task_detail(conn, tid, root=tmp_path)
    html = report.task_page(d, tid)
    good = str(tmp_path / "LNK" / "x.md")
    assert f'data-path="{good}" data-name="x.md" data-how="open"' in html
    assert 'data-how="reveal"' in html and "/api/open" in html
    assert '<span class="file gone"' in html and ">work/old.csv</span>" in html  # greyed, not a link
    assert 'data-path="' + str(tmp_path / "LNK" / "work" / "old.csv") not in html


def test_task_file_guard(conn, tmp_path):
    (tmp_path / "LNK" / "work").mkdir(parents=True)
    ok = tmp_path / "LNK" / "work" / "ok.md"
    ok.write_text("x", encoding="utf-8")
    (tmp_path / "secret.md").write_text("x", encoding="utf-8")
    tid = _named(conn, "LNK", "see work/ok.md and work/gone.md")
    other = _named(conn, "LNK", "see secret.md")  # resolves to root/secret.md via the docs root
    assert store.task_file(conn, tid, str(ok), root=tmp_path) == ok.resolve()
    assert store.task_file(conn, tid, str(tmp_path / "LNK" / "work" / "gone.md"), root=tmp_path) is None  # missing
    secret = next(f["path"] for f in store.task_files(conn, other, root=tmp_path))
    assert store.task_file(conn, tid, secret, root=tmp_path) is None  # another task's file
    assert store.task_file(conn, tid, str(tmp_path / "LNK" / "work" / ".." / ".." / "secret.md"), root=tmp_path) is None
    assert store.task_file(conn, "nope", str(ok), root=tmp_path) is None


def test_open_endpoint_opens_only_the_tasks_files(tmp_path, monkeypatch):
    import functools
    import json
    import socket
    import threading
    import time
    import urllib.error
    import urllib.parse
    import urllib.request

    from coord_mcp import usage
    from coord_mcp.db import connect as db_connect

    monkeypatch.setattr(usage, "ingest", lambda conn: None)
    monkeypatch.setattr(store, "DOCS_ROOT", tmp_path)
    opened = []
    monkeypatch.setattr(report.os, "startfile", lambda p: opened.append(("open", p)), raising=False)
    monkeypatch.setattr(report.subprocess, "Popen", lambda cmd: opened.append(("reveal", cmd)))
    (tmp_path / "LNK").mkdir()
    ok = tmp_path / "LNK" / "ok.md"
    ok.write_text("x", encoding="utf-8")
    (tmp_path / "other.md").write_text("x", encoding="utf-8")
    make = functools.partial(db_connect, tmp_path / "srv.db")
    c = make()
    tid = _named(c, "LNK", "see ok.md and gone/x.md")
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

    def post(path, how="open", task=tid, headers=None):
        body = urllib.parse.urlencode({"task": task, "path": path, "how": how}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/open", data=body, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, None

    assert post(str(ok))[0] == 200
    assert post(str(ok), how="reveal")[0] == 200
    assert opened == [("open", str(ok.resolve())), ("reveal", f'explorer /select,"{ok.resolve()}"')]
    for refused in (str(tmp_path / "other.md"),                      # not on the task
                    str(tmp_path / "LNK" / "gone" / "x.md"),         # on the task, not on disk
                    str(tmp_path / "LNK" / ".." / "other.md"),       # traversal
                    r"C:\Windows\System32\cmd.exe"):
        assert post(refused)[0] == 404, refused
    assert post(str(ok), how="run")[0] == 404
    assert post(str(ok), task="0" * 12)[0] == 404
    assert post(str(ok), headers={"Origin": "http://evil.example"})[0] == 403
    assert len(opened) == 2
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/board/task/{tid}", timeout=10) as r:
        assert b'data-how="reveal"' in r.read()
