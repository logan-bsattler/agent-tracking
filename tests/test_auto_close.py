"""Rec 3: id-match closes intents are settled in code by the dashboard loop.
topic-match, commitments and any id not open stay with master."""

from __future__ import annotations

import pytest

from coord_mcp import store
from coord_mcp.db import connect

INV = {"done": True, "verdict": "v", "confidence": "high", "evidence": ["e"]}


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _loose(conn, n=2, who="Ben"):
    tid = store.create_task(conn, "investigation", "t", {"q": "x"}, assigned_to="Royal")["task_id"]
    store.complete_task(conn, tid, {**INV, "follow_ups": [{"who": who, "what": f"step {i}"} for i in range(n)]})
    return tid, [r["id"] for r in conn.execute("SELECT id FROM follow_ups WHERE task_id=? ORDER BY idx", (tid,))]


def _email(conn, ids, strength="id-match", extra=""):
    ctx = (f"msg AAMk=; from French Williams; date 2026-11-09T19:27Z; follow_ups {', '.join(ids)}; "
           f"strength {strength}; evidence QSM moved to Deployment{extra}")
    return store.propose_intent(conn, "closes", "email-closes", ctx)["intent_id"]


def _teams(conn, ids, effect="closes", strength="id-match"):
    ctx = (f'msg teams:///m/1; conv "c"; from_email a@b.com; said 2026-11-13T10:00Z; effect {effect}; '
           f"strength {strength}; follow_ups {', '.join(ids)}; evidence deployed")
    return store.propose_intent(conn, "deployed", "teams-status", ctx)["intent_id"]


def _state(conn, table, i):
    return conn.execute(f"SELECT state FROM {table} WHERE id=?", (i,)).fetchone()["state"]


def test_email_id_match_closes_every_loose_end_with_one_decision(conn):
    tid, ids = _loose(conn)
    iid = _email(conn, ids)
    out = store.auto_close_intents(conn)
    assert len(out) == 1 and out[0]["follow_ups"] == ids
    did = out[0]["decision_id"]
    d = conn.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()
    assert d["task_id"] == tid
    assert d["statement"].startswith("Closed 2 loose end(s) on email-closes evidence: msg AAMk=, French Williams, "
                                     "2026-11-09T19:27Z: QSM moved")
    assert d["because"] == store.CLOSE_BECAUSE
    for f in ids:
        r = conn.execute("SELECT state, resolved_by FROM follow_ups WHERE id=?", (f,)).fetchone()
        assert (r["state"], r["resolved_by"]) == ("dropped", did)
    it = conn.execute("SELECT state, note FROM intents WHERE id=?", (iid,)).fetchone()
    assert it["state"] == "dismissed" and did in it["note"]
    assert store.auto_close_intents(conn) == []  # idempotent


def test_teams_status_closes_only_with_effect_closes(conn):
    _, ids = _loose(conn, 1)
    iid = _teams(conn, ids)
    assert [o["intent_id"] for o in store.auto_close_intents(conn)] == [iid]
    d = conn.execute("SELECT statement FROM decisions").fetchone()["statement"]
    assert "teams-status" in d and "a@b.com" in d and "2026-11-13T10:00Z" in d


def test_topic_match_is_left_for_master(conn):
    _, ids = _loose(conn)
    iid = _email(conn, ids, strength="topic-match")
    assert store.auto_close_intents(conn) == []
    assert _state(conn, "intents", iid) == "open"
    assert all(_state(conn, "follow_ups", f) == "open" for f in ids)


def test_an_id_not_open_leaves_the_whole_intent(conn):
    _, ids = _loose(conn)
    dec = store.record_decision(conn, "moot", "x")["decision_id"]
    store.resolve_follow_up(conn, ids[1], decision_id=dec)
    iid = _email(conn, ids)
    unknown = _email(conn, [ids[0], "ffffffffffff"])
    assert store.auto_close_intents(conn) == []
    assert _state(conn, "intents", iid) == "open" and _state(conn, "intents", unknown) == "open"
    assert _state(conn, "follow_ups", ids[0]) == "open"


def test_commitments_stay_with_master(conn):
    _, ids = _loose(conn)
    iid = _email(conn, ids, extra="; commitments c1")
    assert store.auto_close_intents(conn) == []
    assert _state(conn, "intents", iid) == "open"


def test_progress_and_other_sources_are_untouched(conn):
    _, ids = _loose(conn)
    t = _teams(conn, ids, effect="progress")
    other = store.propose_intent(conn, "x", "email", f"follow_ups {ids[0]}; strength id-match")["intent_id"]
    assert store.auto_close_intents(conn) == []
    assert _state(conn, "intents", t) == "open" and _state(conn, "intents", other) == "open"


def test_one_failure_rolls_back_only_its_intent(conn, monkeypatch):
    _, a = _loose(conn, 1)
    _, b = _loose(conn, 1)
    bad, good = _email(conn, a), _email(conn, b)
    real = store.resolve_intent

    def flaky(c, intent_id, *args, **kw):
        if intent_id == bad:
            raise ValueError("boom")
        return real(c, intent_id, *args, **kw)

    monkeypatch.setattr(store, "resolve_intent", flaky)
    with pytest.raises(RuntimeError, match="boom"):
        store.auto_close_intents(conn)
    assert _state(conn, "intents", bad) == "open" and _state(conn, "follow_ups", a[0]) == "open"
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    assert _state(conn, "intents", good) == "dismissed" and _state(conn, "follow_ups", b[0]) == "dropped"
