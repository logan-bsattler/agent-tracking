"""Operator board: the dashboard's grouping of the board for a human."""

from __future__ import annotations

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


# ---------------------------------------------------------------- loose ends

FU = [{"who": "Shannon", "what": "Answer Q2, Q6, Q9-Q12"}, {"who": "Ben", "what": "Chase Shannon for a date"}]


def test_follow_ups_surface_on_board_and_operator_view(conn):
    t = _task(conn, "LNK", "AMEX state")
    store.complete_task(conn, t, {**INV, "follow_ups": FU})
    b = store.board(conn)
    assert [(x["who"], x["client"], x["task_id"]) for x in b["loose_ends"]] == [("Shannon", "LNK", t), ("Ben", "LNK", t)]
    v = store.operator_view(conn)
    assert [d["title"] for d in v["loose_ends"]] == [f["what"] for f in FU]
    assert [d["title"] for d in v["needs_you"]] == ["Chase Shannon for a date"]
    html = report.board_page(v)
    assert "Loose ends" in html and "Answer Q2, Q6, Q9-Q12" in html


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
