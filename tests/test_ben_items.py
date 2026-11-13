"""Ben items: loose ends with no task, and questions never closed silently
(docs/ben-only-replies.md, rollout step 1)."""

from __future__ import annotations

import pytest

from coord_mcp import commitments, queue, report, store
from coord_mcp.db import BEN_ITEMS_KEY, connect, ensure_commitments, now

INV = {"done": True, "verdict": "v", "confidence": "high", "evidence": ["e"]}

OLD_FOLLOW_UPS = """CREATE TABLE follow_ups (
  id          TEXT PRIMARY KEY,
  task_id     TEXT NOT NULL REFERENCES tasks(id),
  idx         INTEGER NOT NULL,
  who         TEXT NOT NULL,
  what        TEXT NOT NULL,
  state       TEXT NOT NULL CHECK (state IN ('open','tasked','dropped','deferred')),
  resolved_by TEXT,
  created_at  INTEGER NOT NULL,
  resolved_at INTEGER
)"""


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _done(conn, client="MAG", follow_ups=()):
    tid = store.create_task(conn, "investigation", "t", {"q": "x"}, assigned_to=client)["task_id"]
    store.get_task(conn, tid, reader=client)
    store.complete_task(conn, tid, {**INV, "follow_ups": list(follow_ups)})
    return tid


def _intent(conn, source="email-reply", ctx="from Homero Rendon; received 2026-11-11 14:02Z"):
    return store.propose_intent(conn, "Essex-Brownell: hours and target date for FDD-005", source,
                                context=ctx)["intent_id"]


# ------------------------------------------------------------- migration


def test_old_board_upgrades_and_keeps_task_ids(tmp_path):
    path = tmp_path / "old.db"
    c = connect(path)
    tid = _done(c, follow_ups=[{"who": "Ben", "what": "approve it"}, {"who": "MAG", "what": "read it"}])
    fids = [r["id"] for r in c.execute("SELECT id FROM follow_ups ORDER BY idx")]
    ensure_commitments(c)
    cid = commitments.add(c, "MAG", "Dave", "send it", due="2026-11-20", follow_up_id=fids[0])["id"]
    # Put the file back to the shape before this change.
    c.execute("PRAGMA foreign_keys=OFF")
    c.executescript(f"""
        CREATE TABLE fx AS SELECT id, task_id, idx, who, what, state, resolved_by, created_at, resolved_at
          FROM follow_ups;
        DROP TABLE follow_ups;
        {OLD_FOLLOW_UPS};
        INSERT INTO follow_ups SELECT * FROM fx; DROP TABLE fx;
        ALTER TABLE questions DROP COLUMN superseded_at;
        DELETE FROM meta WHERE key='{BEN_ITEMS_KEY}';""")
    c.close()

    c = connect(path)
    cols = {r[1]: r for r in c.execute("PRAGMA table_info(follow_ups)")}
    assert {"client", "intent_id", "due_at", "ref"} <= set(cols)
    assert cols["task_id"][3] == 0  # nullable now
    assert "superseded_at" in {r[1] for r in c.execute("PRAGMA table_info(questions)")}
    rows = c.execute("SELECT id, task_id, who FROM follow_ups ORDER BY idx").fetchall()
    assert [r["id"] for r in rows] == fids and {r["task_id"] for r in rows} == {tid}
    assert {r[1] for r in c.execute("PRAGMA index_list(follow_ups)")} >= {"follow_ups_state", "follow_ups_task"}
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert c.execute("SELECT follow_up_id FROM commitments WHERE id=?", (cid,)).fetchone()[0] == fids[0]
    assert c.execute("SELECT value FROM meta WHERE key=?", (BEN_ITEMS_KEY,)).fetchone()[0] == "1"
    c.close()
    connect(path).close()  # a second open is a no-op


def test_task_less_row_must_name_an_intent(conn):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO follow_ups(id, task_id, idx, who, what, state, created_at) "
                     "VALUES ('x', NULL, 0, 'Ben', 'w', 'open', 1)")


# -------------------------------------------------------------- ben_item


def test_ben_item_round_trip(conn):
    iid = _intent(conn, ctx="from Homero; msg AAMkAD123; said 2026-11-11T14:02Z")
    r = store.resolve_intent(conn, iid, note="unwired client",
                             ben_item={"what": "Reply to Homero: hours and date for FDD-005",
                                       "client": "Essex-Brownell", "due": "2026-11-14"})
    fid = r["ben_item"]["follow_up_id"]
    assert r["state"] == "dismissed" and r["tasks"] == []
    row = conn.execute("SELECT * FROM follow_ups WHERE id=?", (fid,)).fetchone()
    assert (row["task_id"], row["who"], row["idx"], row["client"], row["intent_id"]) == \
        (None, "Ben", 0, "Essex-Brownell", iid)
    assert row["due_at"] == r["ben_item"]["due_at"] and row["due_at"]
    note = conn.execute("SELECT note FROM intents WHERE id=?", (iid,)).fetchone()["note"]
    assert note == f"Ben item {fid}; unwired client"

    # /queue: under its client, Send to agent off for an unwired client.
    q = queue.ben_queue(conn)
    g = {x["client"]: x["items"] for x in q["groups"]}
    item = g["Essex-Brownell"][0]
    assert item["id"] == fid and item["agent_ok"] is False and item["due"] == row["due_at"]
    html = report.queue_page(q)
    assert "data-noagent" in html and fid in html
    with pytest.raises(ValueError, match="not wired"):
        queue.apply(conn, "agent", [fid])

    # The board's loose ends and Ben queue, and list_follow_ups.
    b = store.board(conn)
    le = [x for x in b["loose_ends"] if x["id"] == fid][0]
    assert le["client"] == "Essex-Brownell" and le["task_id"] is None and le["intent_id"] == iid
    assert [x["id"] for x in store.list_follow_ups(conn, client="Essex-Brownell")["follow_ups"]] == [fid]
    assert [x["id"] for x in store.list_follow_ups(conn, who="Ben")["follow_ups"]] == [fid]
    v = store.operator_view(conn)
    mine = [d for d in v["needs_you"] if d.get("fid") == fid][0]
    assert mine["id"] == "" and mine["client"] == "Essex-Brownell"
    report.board_page(v)  # renders with no task link

    # Presentable by follow-up, and the question is not board-wide.
    p = store.present_question(conn, "Answer Homero?", ["Today", "Defer"], follow_up_id=fid)
    assert p["ref"] == fid and p["task_id"] is None
    cur = store.current_question(conn)
    assert cur["follow_up_id"] == fid and cur["prompts"][0].startswith(f"Answer on {fid}:")
    with pytest.raises(ValueError, match="no task"):
        tid = _done(conn)
        store.present_question(conn, "x?", ["a", "b"], task_id=tid, follow_up_id=fid)

    # The minder: a Ben loose end is never a client loose end.
    assert fid not in store.minder_owed(conn, now() + 10**6)["loose"]

    # Defer and typed answers work as today.
    out = queue.apply(conn, "answer", [fid], answers={fid: "Friday"})
    assert out["ok"]
    text = conn.execute("SELECT text FROM operator_prompts ORDER BY id DESC").fetchone()["text"]
    assert f"- {fid} (Essex-Brownell, intent {iid};" in text
    d = queue.apply(conn, "defer", [fid])
    assert conn.execute("SELECT state FROM follow_ups WHERE id=?", (fid,)).fetchone()[0] == "deferred"
    assert d["decision_id"]


def test_ben_item_for_a_wired_client_can_go_to_the_agent(conn):
    _done(conn, client="MAG")
    iid = _intent(conn)
    fid = store.resolve_intent(conn, iid, ben_item={"what": "Reply to Dave", "client": "MAG"})["ben_item"]["follow_up_id"]
    item = queue.ben_queue(conn)["groups"][0]["items"][0]
    assert item["agent_ok"] is True and item["due"] is None
    queue.apply(conn, "agent", [fid])
    assert conn.execute("SELECT who FROM follow_ups WHERE id=?", (fid,)).fetchone()[0] == "MAG"
    report.queue_page(queue.ben_queue(conn))  # the moved list renders a task-less row


def test_ben_item_refusals_leave_nothing(conn):
    iid = _intent(conn)
    for bad in ({"what": "", "client": "X"}, {"what": "w", "client": ""}, {"what": "w", "client": "X", "x": 1}):
        with pytest.raises(ValueError):
            store.resolve_intent(conn, iid, ben_item=bad)
    with pytest.raises(KeyError):
        store.resolve_intent(conn, iid, task_ids=["nope"], ben_item={"what": "w", "client": "X"})
    assert conn.execute("SELECT COUNT(*) FROM follow_ups").fetchone()[0] == 0
    assert conn.execute("SELECT state FROM intents WHERE id=?", (iid,)).fetchone()[0] == "open"
    with pytest.raises(KeyError):
        store.ben_item(conn, "nope", "w", "X")


def test_plain_note_still_dismisses(conn):
    iid = _intent(conn)
    r = store.resolve_intent(conn, iid, note="spam")
    assert r == {"ok": True, "intent_id": iid, "state": "dismissed", "tasks": []}


def test_triage_emails_shows_the_ben_item_as_owed(conn, tmp_path):
    iid = _intent(conn)
    store.resolve_intent(conn, iid, note="answered later",
                         ben_item={"what": "Reply to Homero with hours", "client": "Essex-Brownell"})
    row = [e for e in store.triage_emails(conn, log_dir=tmp_path)["emails"] if e["intent_id"] == iid][0]
    assert row["owed"] == ["Reply to Homero with hours"]
    fid = conn.execute("SELECT id FROM follow_ups WHERE intent_id=?", (iid,)).fetchone()[0]
    dec = store.record_decision(conn, "Ben replied to Homero", "sent mail 11-13")["decision_id"]
    store.resolve_follow_up(conn, fid, decision_id=dec)
    row = [e for e in store.triage_emails(conn, log_dir=tmp_path)["emails"] if e["intent_id"] == iid][0]
    assert row["owed"] == []  # settled: no heuristic "dismissed with no task" either


# ------------------------------------------------------------- questions


def test_second_question_supersedes_the_first_and_the_minder_lists_it(conn):
    a = store.present_question(conn, "First?", ["yes", "no"])["question_id"]
    b = store.present_question(conn, "Second?", ["yes", "no"])["question_id"]
    first = conn.execute("SELECT superseded_at, closed_at FROM questions WHERE id=?", (a,)).fetchone()
    assert first["superseded_at"] and first["closed_at"] is None
    assert store.current_question(conn)["id"] == b
    owed = store.minder_owed(conn, now())
    assert owed["superseded"] == [f"q{a}"]
    assert f"1 questions superseded unanswered (q ids; re-present each, or record why not) (q{a})" in store.minder_text(owed, now())
    # A decision citing a longer id does not count; one citing q<id> does.
    store.record_decision(conn, f"About q{a}0", "unrelated")
    assert store.minder_owed(conn, now())["superseded"] == [f"q{a}"]
    store.record_decision(conn, "Ben answered by mail", f"re q{a}: yes")
    assert store.minder_owed(conn, now())["superseded"] == []


def test_answered_or_task_bound_questions_are_not_listed(conn):
    a = store.present_question(conn, "First?", ["yes", "no"])["question_id"]
    assert store.close_question(conn, a)
    tid = _done(conn)
    store.present_question(conn, "On a task?", ["yes", "no"], task_id=tid)
    store.present_question(conn, "Third?", ["yes", "no"])
    assert store.minder_owed(conn, now())["superseded"] == []
