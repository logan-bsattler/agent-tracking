"""Master's question to Ben, drawn as one button per option."""

from __future__ import annotations

import pytest

from coord_mcp import report, store
from coord_mcp.db import connect

INV = {"done": True, "verdict": "v", "confidence": "high", "evidence": ["spec"]}


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _loose_end(conn):
    t = store.create_task(conn, "investigation", "AMEX state", {"q": "x"}, assigned_to="LNK")["task_id"]
    store.complete_task(conn, t, {**INV, "follow_ups": [{"who": "Ben", "what": "Pick a cutover date"}]})
    return t, store.board(conn)["loose_ends"][0]["id"]


def test_present_names_the_task_from_the_loose_end(conn):
    t, fu = _loose_end(conn)
    r = store.present_question(conn, "Cut over when?", ["Monday", "After month end"], follow_up_id=fu,
                               recommended=1)
    assert r["task_id"] == t
    q = store.current_question(conn)
    assert q["ref"] == fu and q["options"] == ["Monday", "After month end"] and q["recommended"] == 1
    assert q["prompts"][0].startswith(f"Answer on {fu}: Monday")
    assert store.task_detail(conn, t)["question"]["id"] == q["id"]


def test_present_refuses_what_a_button_cannot_carry(conn):
    t, fu = _loose_end(conn)
    for bad in ([], ["only one"], ["a", "a"], ["x" * (store.OPTION_MAX + 1), "b"]):
        with pytest.raises(ValueError):
            store.present_question(conn, "Q?", bad, task_id=t)
    with pytest.raises(KeyError):
        store.present_question(conn, "Q?", ["a", "b"], task_id="nope")
    with pytest.raises(ValueError):
        store.present_question(conn, "Q?", ["a", "b"], task_id=t, recommended=2)


def test_board_wide_question_is_answered_on_its_own_id(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "AGENTS_ROOT", tmp_path)
    doc = tmp_path / "board-cleanup-review.md"
    doc.write_text("# cleanup", encoding="utf-8")
    r = store.present_question(conn, "Resolve the stale loose ends?", ["Yes", "No"], path=str(doc))
    assert r["task_id"] is None and r["ref"] == f"q{r['question_id']}" and r["path"] == str(doc.resolve())
    q = store.current_question(conn)
    assert q["task_id"] is None and q["ref"] == r["ref"]
    assert q["prompts"][0].startswith(f"Answer on {r['ref']}: Yes")
    assert store.answer_prompt(conn, q["id"], 1) == (q["prompts"][1], None)
    assert store.question_file(conn, q["id"]) == doc.resolve()
    html = report._next_up([], store.now(), q)
    assert report.question_doc_href(q) in html and "board-cleanup-review.md" in html and "/board/task/" not in html


def test_question_path_must_be_a_file_under_the_known_roots(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "AGENTS_ROOT", tmp_path / "agents")
    monkeypatch.setattr(store, "DOCS_ROOT", tmp_path / "docs")
    (tmp_path / "agents").mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("x", encoding="utf-8")
    for bad in (str(outside), "relative/review.md", str(tmp_path / "agents" / "missing.md"),
                str(tmp_path / "agents" / ".." / "secret.txt")):
        with pytest.raises(ValueError):
            store.present_question(conn, "Q?", ["a", "b"], path=bad)
    assert store.current_question(conn) is None
    assert store.question_file(conn, 999) is None


def test_a_new_question_closes_the_last(conn):
    t, fu = _loose_end(conn)
    first = store.present_question(conn, "One?", ["a", "b"], task_id=t)["question_id"]
    second = store.present_question(conn, "Two?", ["c", "d"], follow_up_id=fu)["question_id"]
    assert store.current_question(conn)["id"] == second
    with pytest.raises(ValueError, match="not open"):
        store.queue_operator_prompt(conn, "answer", question_id=first, option=0)


def test_answer_text_comes_from_the_stored_row_and_closes_it(conn):
    t, fu = _loose_end(conn)
    qid = store.present_question(conn, "Cut over when?", ["Monday", "After month end"], follow_up_id=fu)["question_id"]
    with pytest.raises(ValueError):
        store.queue_operator_prompt(conn, "answer", question_id=qid, option=5)
    with pytest.raises(ValueError):
        store.queue_operator_prompt(conn, "answer", question_id=qid, option="Monday")  # text is not an index
    assert store.queue_operator_prompt(conn, "answer", question_id=qid, option="1")["ok"]
    p = store.take_operator_prompt(conn)
    assert p["action"] == "answer" and p["task_id"] == t
    assert p["text"].startswith(f"Answer on {fu}: After month end")
    assert store.current_question(conn) is None


def test_resolving_the_loose_end_retires_its_question(conn):
    t, fu = _loose_end(conn)
    store.present_question(conn, "Cut over when?", ["Monday", "Later"], follow_up_id=fu)
    d = store.record_decision(conn, "Monday", "Ben said so", task_id=t)
    store.resolve_follow_up(conn, fu, decision_id=d["decision_id"])
    assert store.current_question(conn) is None


def test_close_is_idempotent(conn):
    t, _ = _loose_end(conn)
    qid = store.present_question(conn, "Q?", ["a", "b"], task_id=t)["question_id"]
    assert store.close_question(conn, qid) and not store.close_question(conn, qid)
    assert not store.close_question(conn, "junk")


def test_task_page_draws_one_form_per_option(conn):
    t, fu = _loose_end(conn)
    store.present_question(conn, "Cut <over>?", ["Monday", "After <month> end"], follow_up_id=fu, recommended=0)
    page = report.task_page(store.task_detail(conn, t), t)
    assert "Master asks" in page and "Cut &lt;over&gt;?" in page and "<month>" not in page
    assert page.count('name="action" value="answer"') == 2
    assert 'name="option" value="1"' in page


def test_answer_posts_through_the_server(tmp_path, monkeypatch):
    import functools
    import json
    import socket
    import threading
    import time
    import urllib.error
    import urllib.request

    from coord_mcp import usage
    from coord_mcp.db import connect as db_connect

    monkeypatch.setattr(usage, "ingest", lambda conn: None)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    make = functools.partial(db_connect, tmp_path / "srv.db")
    c = make()
    t, fu = _loose_end(c)
    qid = store.present_question(c, "Cut over when?", ["Monday", "Later"], follow_up_id=fu)["question_id"]
    threading.Thread(target=report.serve, args=(make, port), daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    with urllib.request.urlopen(base + "/api/parked", timeout=10) as r:
        assert json.loads(r.read())["question"]["id"] == qid
    form = f"action=answer&question={qid}&option=0".encode()
    evil = urllib.request.Request(base + "/api/prompt", data=form, headers={"Origin": "https://evil.example"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(evil, timeout=10)
    assert e.value.code == 403
    ok = urllib.request.Request(base + "/api/prompt", data=form, headers={"Accept": "application/json"})
    with urllib.request.urlopen(ok, timeout=10) as r:
        assert json.loads(r.read())["ok"]
    assert store.take_operator_prompt(c)["text"].startswith(f"Answer on {fu}: Monday")
    c.close()


def test_board_wide_question_document_is_served(tmp_path, monkeypatch):
    import functools
    import socket
    import threading
    import time
    import urllib.error
    import urllib.request

    from coord_mcp import usage
    from coord_mcp.db import connect as db_connect

    monkeypatch.setattr(usage, "ingest", lambda conn: None)
    monkeypatch.setattr(store, "AGENTS_ROOT", tmp_path)
    doc = tmp_path / "review.md"
    doc.write_text("# the review", encoding="utf-8")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    make = functools.partial(db_connect, tmp_path / "srv.db")
    c = make()
    qid = store.present_question(c, "Clean up?", ["Yes", "No"], path=str(doc))["question_id"]
    bare = store.present_question(c, "And this?", ["Yes", "No"])["question_id"]
    threading.Thread(target=report.serve, args=(make, port), daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    with urllib.request.urlopen(f"{base}/board/question/{qid}/file", timeout=10) as r:
        assert r.read() == b"# the review"
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"{base}/board/question/{bare}/file", timeout=10)
