"""The reply pipeline gaps found on 2026-11-16: email asks left as bare Ben
items, reply loose ends that waited for a step spawn, and finished drafts the
minder skipped (a loose Outlook match, and a gate on a settled Ben item)."""

from __future__ import annotations

import json

import pytest

from coord_mcp import holds, store
from coord_mcp.db import connect
from coord_mcp.workflows import cases as cs
from coord_mcp.workflows import engine

INV = {"done": True, "verdict": "v", "confidence": "high", "evidence": ["e"]}


@pytest.fixture()
def c(tmp_path):
    conn = connect(tmp_path / "t.db")
    cs.ensure_workflows(conn)
    engine._since(conn, write=True)
    conn.execute("UPDATE meta SET value=? WHERE key=?", (str(store.now() - 60), engine.SINCE_KEY))
    yield conn
    conn.close()


def _intent(c, source="email-reply", client="Moog"):
    return store.propose_intent(
        c, f"{client}: REPLY: Alex asks Ben to confirm access", source,
        context="msg AAMk1; from Alex; reply_from alex@client.example; reply_date 2026-11-16T15:07:42Z; "
                'subject "QAD AWS Migration"', origin="team", trust="trusted")["intent_id"]


# ------------------------------------------------------------ gap 1

def test_email_reply_from_a_wired_client_is_not_a_bare_ben_item(c):
    i = _intent(c)
    with pytest.raises(ValueError, match="draft_reply"):
        store.resolve_intent(c, i, ben_item={"what": "confirm access", "client": "Moog"})
    assert c.execute("SELECT COUNT(*) FROM follow_ups").fetchone()[0] == 0
    assert c.execute("SELECT state FROM intents WHERE id=?", (i,)).fetchone()["state"] == "open"


def test_a_draft_task_or_no_draft_reason_lets_it_through(c):
    i = _intent(c)
    t = store.create_task(c, "draft_reply", "Moog: reply to Alex", {"goal": "g"}, assigned_to="Moog")["task_id"]
    out = store.resolve_intent(c, i, [t], ben_item={"what": "confirm access", "client": "Moog"})
    assert out["state"] == "converted" and out["ben_item"]
    j = _intent(c)
    out = store.resolve_intent(c, j, ben_item={"what": "confirm access", "client": "Moog"},
                               no_draft="only Ben can check his own access first")
    assert "no draft: only Ben" in c.execute("SELECT note FROM intents WHERE id=?", (j,)).fetchone()["note"]


def test_unwired_clients_and_teams_asks_keep_the_bare_ben_item(c):
    i = _intent(c, client="Essex-Brownell")
    assert store.resolve_intent(c, i, ben_item={"what": "hours for FDD-005", "client": "Essex-Brownell"})["ben_item"]


def test_email_reply_intents_carry_the_draft_handling(c):
    _intent(c)
    d = store.list_intents(c)[0]
    assert d["reply_request"]["reply_from"] == "alex@client.example"
    assert "draft_reply" in d["handling"]


# ------------------------------------------------------------ gap 2

def _defect(c, follow_ups):
    t = store.create_task(c, "investigation", "LNK: why zero-dollar check clearing still occurs", {},
                          assigned_to="LNK")["task_id"]
    store.complete_task(c, t, {**INV, "follow_ups": follow_ups})
    cs.sync(c)
    return t


def test_a_reply_loose_end_is_drafted_while_ben_has_the_case(c):
    t = _defect(c, [{"who": "Ben", "what": "decide whether to bill the extra hours"},
                    {"who": "LNK", "what": "Draft reply to Shannon: 11/13 fix incomplete"}])
    did = engine.run(c)
    assert [(d["kind"], d["spawn"]) for d in did] == [("draft_reply", engine.INTERIM_STEP)]
    row = c.execute("SELECT parent_id, spec, case_step FROM tasks WHERE id=?", (did[0]["task_id"],)).fetchone()
    spec = json.loads(row["spec"])
    assert row["parent_id"] == t and row["case_step"] == engine.INTERIM_STEP
    assert "Draft reply to Shannon" in spec["goal"] and "interim reply" in spec["created_by"]
    fu = c.execute("SELECT state, resolved_by FROM follow_ups WHERE who='LNK'").fetchone()
    assert fu["state"] == "tasked" and fu["resolved_by"] == did[0]["task_id"]
    assert engine.run(c) == []  # the draft is open; nothing twice


def test_no_interim_reply_without_a_draft_ask_or_while_work_is_open(c):
    _defect(c, [{"who": "Ben", "what": "decide whether to bill the extra hours"}, {"who": "LNK", "what": "check the email log"}])
    assert engine.run(c) == []


def test_interim_step_does_not_move_the_case(c):
    _defect(c, [{"who": "Ben", "what": "decide whether to bill the extra hours"},
                {"who": "LNK", "what": "Draft a note to Shannon asking for the program names"}])
    engine.run(c)
    case = c.execute("SELECT * FROM workflow_cases").fetchone()
    assert case["step"] != "reply"


# ------------------------------------------------------------ gap 3

def _draft(c, title="MAG: note"):
    return store.create_task(c, "draft_reply", title, {"goal": "g"}, assigned_to="MAG")["task_id"]


def test_mentioning_outlook_is_not_being_in_drafts(c):
    a, b, h, s = _draft(c), _draft(c), _draft(c), _draft(c)
    store.record_decision(c, f"Master handoff: owed = Outlook draft of {a}", "handoff", a)
    store.record_decision(c, f"Draft {b} supersedes {s} (Darren note); only {b} goes to Outlook", "same asks", b)
    store.record_decision(c, f"Hold Outlook drafting of {h} until Ben answers q1", "promise", h)
    m = store.outlook_marks(c)
    assert a not in m and b not in m
    assert m[s]["mark"] == store.SET_ASIDE and m[h]["mark"] == store.SET_ASIDE
    store.record_decision(c, f"{a} drafted to Outlook Drafts", "minder", a)
    store.record_decision(c, f"Retry Draft in Outlook for {h} on the next minder", "lifted", h)
    m = store.outlook_marks(c)
    assert m[a]["mark"] == "drafted" and m[h]["mark"] == "owed"


def test_a_revised_draft_sets_its_parent_aside(c):
    p = _draft(c)
    store.create_task(c, "draft_reply", "MAG: redraft", {"goal": "g"}, assigned_to="MAG", parent_id=p)
    assert store.outlook_marks(c)[p]["mark"] == store.SET_ASIDE


def test_a_settled_ben_item_does_not_gate_outlook(c):
    w = store.create_task(c, "investigation", "MAG: r4", {}, assigned_to="MAG")["task_id"]
    store.complete_task(c, w, {**INV, "follow_ups": [{"who": "Ben", "what": "accept ladder; six other rulings"}]})
    fid = c.execute("SELECT id FROM follow_ups").fetchone()["id"]
    d = _draft(c)
    c.execute("UPDATE tasks SET parent_id=? WHERE id=?", (w, d))
    stmt = f"Ben item {fid} ('accept ladder') is settled by f28533937476; {w} is the working grade."
    dec = store.record_decision(c, stmt, "Ladder ruling already made by Ben; master default; Ben may override", w)
    assert holds.after_decision(c, dec["decision_id"], stmt, "master default; Ben may override", w, None) is None
    # A gate registered before the rule is read the same way.
    holds.register(c, dec["decision_id"], None, [fid], [w], w)
    assert holds.pending_gate(c, d) is None
    # A default that leaves the item open still gates.
    stmt2 = f"Proceed on master default for {w}: r4; Ben's {fid} stays open until resolved"
    dec2 = store.record_decision(c, stmt2, "master default; Ben may override", w)
    holds.register(c, dec2["decision_id"], None, [fid], [w], w)
    assert holds.pending_gate(c, d)["ben_follow_ups"] == [fid]
