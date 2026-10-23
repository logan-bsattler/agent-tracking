"""Task order: which task waits on which, shown to Ben on the dashboard."""

from __future__ import annotations

import pytest

from coord_mcp import deps, report, store
from coord_mcp.db import connect


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _task(conn, title, kind="investigation", client="LNK"):
    return store.create_task(conn, kind, title, {"q": "x"}, assigned_to=client)["task_id"]


def _draft(conn, tmp_path, title):
    tid = _task(conn, title, "draft_reply")
    f = tmp_path / f"{tid}.txt"
    f.write_text("Hi,\n\nThanks!\n", encoding="utf-8")
    store.get_task(conn, tid, reader="LNK")
    store.complete_task(conn, tid, {"done": True, "to": ["a@x.com"], "subject": title,
                                    "body_path": str(f), "summary": "s"})
    return tid


def test_add_refuses_self_unknown_and_loops(conn):
    a, b, c = _task(conn, "a"), _task(conn, "b"), _task(conn, "c")
    with pytest.raises(ValueError):
        store.add_dep(conn, a, a)
    with pytest.raises(ValueError):
        store.add_dep(conn, a, "000000000000")
    store.add_dep(conn, a, b)
    store.add_dep(conn, b, c)
    with pytest.raises(ValueError, match="loop"):
        store.add_dep(conn, c, a)


def test_plain_blocker_clears_when_done(conn):
    a, b = _task(conn, "reply"), _task(conn, "check data")
    store.add_dep(conn, a, b, "needs the numbers")
    assert [x["id"] for x in store.waiting_on(conn)[a]] == [b]
    store.get_task(conn, b, reader="LNK")
    store.complete_task(conn, b, {"done": True, "verdict": "x", "confidence": "high", "evidence": ["y"]})
    assert a not in store.waiting_on(conn)
    d = store.task_deps(conn, a)
    assert d["waits_on"][0]["met"] and d["waits_on"][0]["note"] == "needs the numbers"


def test_draft_blocker_holds_until_marked_sent(conn, tmp_path):
    rahul, reply = _draft(conn, tmp_path, "Rahul promote"), _draft(conn, tmp_path, "RE: RAID 692")
    store.add_dep(conn, reply, rahul)
    assert reply in store.waiting_on(conn)  # done is not sent
    assert store.task_deps(conn, rahul)["clear_as"] == "sent"
    assert store.clear_blocker(conn, rahul)["cleared"] == 1
    assert reply not in store.waiting_on(conn) and store.task_deps(conn, rahul)["clear_as"] is None
    store.add_dep(conn, reply, rahul)  # re-adding a cleared wait sets it again
    assert reply in store.waiting_on(conn)


def test_drafts_walk_puts_the_blocker_first(conn, tmp_path):
    reply = _draft(conn, tmp_path, "RE: RAID 692")  # older, so it would lead
    rahul = _draft(conn, tmp_path, "Rahul promote")
    store.add_dep(conn, reply, rahul)
    v = store.draft_replies(conn)
    assert [d["task_id"] for d in v["drafts"]] == [rahul, reply]
    page = report.drafts_page(v)
    assert "waits on" in page and "do first" in page and 'value="clear"' in page


def test_board_rows_and_task_page_show_the_order(conn, tmp_path):
    a, b = _task(conn, "reply to Riddhi"), _task(conn, "ask Rahul")
    store.add_dep(conn, a, b)
    v = store.operator_view(conn)
    row = next(d for d in v["waiting"] if d["id"] == a)
    assert row["waits_on"][0]["id"] == b and next(d for d in v["waiting"] if d["id"] == b)["blocks"] == [a]
    assert f'href="/board/task/{b}"' in report._items([row], v["as_of"], "")
    page = report.task_page(store.task_detail(conn, a), a)
    assert "<h2>Order</h2>" in page and "Waits on" in page and "Do the task it waits on first" in page
    page = report.task_page(store.task_detail(conn, b), b)
    assert "Holds up" in page and 'action="/api/dep"' in page


def test_next_up_never_leads_with_a_blocked_row(conn):
    a, b = _task(conn, "first made"), _task(conn, "second made")
    store.add_dep(conn, a, b)
    v = store.operator_view(conn)
    html = report._next_up(v["waiting"], v["as_of"])
    assert html.index(b[:8]) < html.index("first made")


def test_cli(conn, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COORD_DB", str(tmp_path / "t.db"))
    a, b = _task(conn, "a"), _task(conn, "b")
    assert deps.main(["add", a, b, "because"]) == 0
    assert deps.main(["show", a]) == 0 and b in capsys.readouterr().out
    assert deps.main(["add", b, a]) == 1  # a loop
    assert deps.main(["remove", a, b]) == 0 and a not in store.waiting_on(conn)
    assert deps.main([]) == 2
