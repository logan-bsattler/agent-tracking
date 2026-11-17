from __future__ import annotations

import pytest

from coord_mcp import store, wake
from coord_mcp.db import connect


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "coord.db")
    yield c
    c.close()


def _age(conn, pid, secs):
    conn.execute("UPDATE operator_prompts SET created_at = created_at - ? WHERE id=?", (secs, pid))


def test_no_prompt_no_wake(conn):
    assert wake.check(conn) is None


def test_waiting_prompt_wakes_once_and_peek_claims_nothing(conn, monkeypatch, capsys):
    monkeypatch.delenv("COORD_WORKER_ID", raising=False)
    pid = store.queue_operator_prompt(conn, "board")["id"]
    _age(conn, pid, store.WAKE_AFTER)
    p = wake.check(conn, peek=True)
    assert p["id"] == pid and p["to"] == wake.WORKER_ID
    w = wake.check(conn)
    assert w["id"] == pid and w["text"].startswith(wake.WAKE_MARK)
    assert store._seen_at(conn, "worker_woken") is None  # a check never stamps
    assert wake.check(conn)["id"] == pid  # a refused send is retried next run


def test_sent_stamps_and_holds_the_next_wake(conn):
    pid = store.queue_operator_prompt(conn, "board")["id"]
    _age(conn, pid, store.WAKE_AFTER)
    assert wake.check(conn)["id"] == pid
    store.mark_worker_woken(conn)
    assert store._seen_at(conn, "worker_woken") is not None
    assert wake.check(conn) is None  # one wake per WAKE_EVERY, counted from the send


def test_live_worker_is_not_woken(conn):
    pid = store.queue_operator_prompt(conn, "board")["id"]
    _age(conn, pid, store.WAKE_AFTER)
    store._mark(conn, "worker_seen", store.now())
    assert wake.check(conn) is None


def test_worker_id_from_env(conn, monkeypatch):
    monkeypatch.setenv("COORD_WORKER_ID", "local_x")
    pid = store.queue_operator_prompt(conn, "board")["id"]
    _age(conn, pid, store.WAKE_AFTER)
    assert wake.check(conn)["to"] == "local_x"


def test_mark_matches_the_mod():
    from pathlib import Path
    src = (Path(__file__).parent.parent / "mods/master-console/hooks/register.tsx").read_text(encoding="utf-8")
    assert f"const WAKE_MARK = '{wake.WAKE_MARK}'" in src
