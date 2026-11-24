"""What may land on Ben's queue (audit 2026-11-14): the who-Ben test,
repeats, covered items, the workbook rule across results, and settling by
an earlier ruling. All run with the board's real switch defaults."""

from __future__ import annotations

import pytest

from coord_mcp import contracts, queue, store
from coord_mcp.db import connect

pytestmark = pytest.mark.ben_checks

INV = {"done": True, "verdict": "v", "confidence": "high", "evidence": ["e"]}


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _task(conn, client="MAG", parent_id=None, kind="investigation"):
    tid = store.create_task(conn, kind, "t", {"q": "x"}, assigned_to=client, parent_id=parent_id)["task_id"]
    store.get_task(conn, tid, reader=client)
    return tid


def _done(conn, follow_ups, client="MAG", parent_id=None):
    tid = _task(conn, client, parent_id)
    return tid, store.complete_task(conn, tid, {**INV, "follow_ups": list(follow_ups)})


def _ben(what, reason=None):
    return {"who": "Ben", "what": what, **({"reason": reason} if reason else {})}


def _open(conn):
    return {r["what"]: r["id"] for r in store.list_follow_ups(conn, who="Ben")["follow_ups"]}


# ------------------------------------------------------------ 1. who: Ben

def test_defaults_are_on_except_precedent(conn):
    assert {k: queue.switch(conn, k) for k in queue.SWITCHES} == {
        "ben_test": True, "ben_dedupe": True, "ben_cover": True, "ben_group_wide": True, "ben_precedent": False,
        "hold_rule": True, "hold_apply": False, "case_checks": True, "defer_return": True,
        "workflow_propose": True, "auto_promote": True, "auto_estimate": True, "auto_checkin": True,
        "passive_close": False, "require_pipeline": True, "pipeline_create": True,
        "idle_reaper": False, "idle_reaper_keep_rc": True,
        "vpn_watch": True, "auto_redispatch": True, "clear_check": True, "code_restart": True,
        "auto_review_dispatch": True, "hold_lift": True, "auto_revise": True,
        "auto_task_loose": True, "minder_fast_wake": False,
        "auto_checkin_task": True, "auto_queue": True}


def test_reason_is_an_optional_enum_in_the_contract():
    ok = contracts.validate("investigation", {**INV, "follow_ups": [_ben("x", "send"), _ben("y")]})
    assert ok["follow_ups"][0]["reason"] == "send" and "reason" not in ok["follow_ups"][1]
    with pytest.raises(contracts.ValidationError):
        contracts.validate("investigation", {**INV, "follow_ups": [_ben("x", "because")]})


@pytest.mark.parametrize("what,want", [
    ("Ask QAD for the patch number", "reply"),
    ("Get the app version from Mayuri", "reply"),
    ("Confirm with Ari that the approvers are right", "reply"),
    ("Re-point the builders to r9s19h", "agent"),
    ("Update the FDD menu ids for xxsomkrp", "agent"),
    ("Ask Ben whether to keep it", None),
    ("Ask Shelton to approve the scope change", None),   # an approval: stays Ben's
    ("Update the SharePoint #262 estimate to 20-32 h", None),
    ("Review and promote RAID 2370 v6 sizing", None),
])
def test_route_for_after_the_audit(what, want):
    assert queue.route_for(what, "MAG") == want


def test_untagged_agent_work_for_ben_is_refused(conn):
    tid = _task(conn)
    with pytest.raises(ValueError, match="read as agent work"):
        store.complete_task(conn, tid, {**INV, "follow_ups": [_ben("Confirm the desiccant part number in QAD")]})
    assert store.get_task(conn, tid)["state"] != "done"  # nothing written


def test_a_reason_tag_lets_it_land_and_keeps_routing_off_it(conn):
    tid, out = _done(conn, [_ben("Run the load on the client VM", "access"), _ben("Query pt_mstr", "access")])
    assert "routed_to_you" not in out
    assert len(_open(conn)) == 2
    assert set(queue.reasons_for(conn, tid).values()) == {"access"}


def test_wording_that_is_bens_needs_no_tag(conn):
    _done(conn, [_ben("Approve the cutover plan"), _ben("Send the sizing to John")])
    assert len(_open(conn)) == 2


def test_ben_test_switch_turns_the_refusal_off(conn):
    queue.set_switch(conn, "ben_test", False)
    _done(conn, [_ben("Confirm the desiccant part number in QAD")])
    assert len(_open(conn)) == 1


# --------------------------------------------------------------- 2. repeats

def test_a_repeat_of_an_open_item_from_another_task_is_refused(conn):
    _done(conn, [_ben("Approve RAID 685 date and raise the CVC ticket for lnkintlapp 1.0.0.9", "approve")])
    tid = _task(conn)
    with pytest.raises(ValueError, match="repeats his open loose end"):
        store.complete_task(conn, tid, {**INV, "follow_ups": [
            _ben("Approve raising a CVC ticket for lnkintlapp 1.0.0.9 (RAID 685) and set its date", "approve")]})


def test_a_repeat_within_one_result_is_refused(conn):
    tid = _task(conn)
    with pytest.raises(ValueError, match="same ask twice"):
        store.complete_task(conn, tid, {**INV, "follow_ups": [
            _ben("Approve the #262 estimate of 20-32 h"), _ben("Approve #262 estimate at 20-32 h now")]})


def test_rows_with_different_anchors_are_not_repeats(conn):
    _done(conn, [_ben("Decide #968 keep or cancel")])
    _done(conn, [_ben("Decide #444 keep or cancel")])
    assert len(_open(conn)) == 2


def test_another_client_is_not_a_repeat(conn):
    _done(conn, [_ben("Approve the #262 estimate of 20-32 h")], client="MAG")
    _done(conn, [_ben("Approve the #262 estimate of 20-32 h")], client="Cascade")
    assert len(store.list_follow_ups(conn, who="Ben")["follow_ups"]) == 2


# ---------------------------------------------------------- 3. covered items

def test_a_revision_that_re_raises_closes_the_parents_item(conn):
    p, _ = _done(conn, [_ben("Review and promote RAID 2370 v5 sizing")])
    old = _open(conn)["Review and promote RAID 2370 v5 sizing"]
    c, out = _done(conn, [_ben("Review and promote RAID 2370 v6 sizing")], parent_id=p)
    assert list(_open(conn)) == ["Review and promote RAID 2370 v6 sizing"]
    assert out["covered"][0]["follow_up"] == old
    row = conn.execute("SELECT state, resolved_by FROM follow_ups WHERE id=?", (old,)).fetchone()
    assert row["state"] == "dropped"
    dec = conn.execute("SELECT statement, task_id FROM decisions WHERE id=?", (row["resolved_by"],)).fetchone()
    assert c in dec["statement"] and dec["task_id"] == p


def test_an_item_that_says_it_covers_open_ids_closes_them(conn):
    _done(conn, [_ben("Decide #968 keep or cancel")])
    old = _open(conn)["Decide #968 keep or cancel"]
    _, out = _done(conn, [_ben(f"Rule on the rows in work/cancels.xlsx; covers {old}")])
    assert [c["follow_up"] for c in out["covered"]] == [old]
    assert old not in _open(conn).values()


def test_naming_an_id_without_cover_wording_closes_nothing(conn):
    _done(conn, [_ben("Decide #968 keep or cancel")])
    old = _open(conn)["Decide #968 keep or cancel"]
    _, out = _done(conn, [_ben(f"Decide the scope of #1001 (see {old})")])
    assert "covered" not in out and old in _open(conn).values()


# --------------------------------------------------- 4. workbook across results

def test_the_third_same_shape_ruling_across_results_is_refused(conn):
    _done(conn, [_ben("Decide #968 keep or cancel in the tracker")])
    _done(conn, [_ben("Decide #444 keep or cancel in the tracker")])
    tid = _task(conn)
    with pytest.raises(ValueError, match="one workbook"):
        store.complete_task(conn, tid, {**INV, "follow_ups": [_ben("Decide #686 keep or cancel in the tracker")]})
    store.complete_task(conn, tid, {**INV, "follow_ups": [_ben("Decide the rows in work/cancels.xlsx")]})


def test_group_wide_switch_off_lets_rows_land(conn):
    queue.set_switch(conn, "ben_group_wide", False)
    for n in (968, 444, 686):
        _done(conn, [_ben(f"Decide #{n} keep or cancel in the tracker")])
    assert len(_open(conn)) == 3


# ------------------------------------------------ 5. deciding for Ben (off)

def _ruled(conn):
    """A Ben item for MAG he dropped by an individual ruling."""
    _done(conn, [_ben("Decide whether the staged loader may keep its hard-coded mailx paths")])
    fid = _open(conn)["Decide whether the staged loader may keep its hard-coded mailx paths"]
    dec = store.record_decision(conn, "Ben ruled: staged loaders keep hard-coded mailx paths", "Ben's answer")
    store.resolve_follow_up(conn, fid, decision_id=dec["decision_id"])
    return dec["decision_id"]


def test_precedent_is_off_by_default(conn):
    _ruled(conn)
    _, out = _done(conn, [_ben("Decide whether the staged loader may keep its hard-coded mailx paths")])
    assert "settled_by_ruling" not in out and len(_open(conn)) == 1


def test_precedent_on_settles_with_a_master_default_citing_the_ruling(conn):
    ruling = _ruled(conn)
    queue.set_switch(conn, "ben_precedent", True)
    tid, out = _done(conn, [_ben("Decide whether the staged loader may keep its hard-coded mailx paths")])
    s = out["settled_by_ruling"][0]
    assert s["ruling"] == ruling and not _open(conn)
    because = conn.execute("SELECT because FROM decisions WHERE id=?", (s["decision"],)).fetchone()["because"]
    assert "master default; Ben may override" in because


def test_a_ruling_on_the_tasks_lineage_settles_a_matching_item(conn):
    p = _task(conn)
    store.complete_task(conn, p, INV)
    ruling = store.record_decision(conn, "Ben ruled: MDM xxmdmpt01.p costed as integration at 24-60 h",
                                   "Ben's answer", p)["decision_id"]
    queue.set_switch(conn, "ben_precedent", True)
    _, out = _done(conn, [_ben("Decide MDM xxmdmpt01.p costed as integration at 24-60 h")], parent_id=p)
    assert out["settled_by_ruling"][0]["ruling"] == ruling


def test_landing_report_is_read_only(conn):
    _ruled(conn)
    queue.set_switch(conn, "ben_test", False)
    queue.set_switch(conn, "ben_dedupe", False)
    queue.set_switch(conn, "ben_cover", False)
    _done(conn, [_ben("Decide whether the staged loader may keep its hard-coded mailx paths")])
    before = _open(conn)
    rep = queue.landing_report(conn)
    assert [r["id"] for r in rep["settled"]] == list(before.values())
    assert _open(conn) == before


# ------------------------------------------- review fixes (2026-11-14 review)

def test_two_review_and_draft_items_to_one_contact_are_not_repeats(conn):
    # server.py's guidance: "Review and draft to <name>". A second thread to
    # the same person is a different draft, not a repeat of the open one.
    assert not queue.duplicate("Review and draft to John", "Review and draft to John")
    assert queue.duplicate("Review and draft to John", "Review and draft to John", same_task=True)
    _done(conn, [_ben("Review and draft to John")])
    _done(conn, [_ben("Review and draft to John")])
    assert len(store.list_follow_ups(conn, who="Ben")["follow_ups"]) == 2


def test_review_and_draft_items_are_never_rows_of_one_shape(conn):
    a = "Review and draft reply to Darren (RAID 695)"
    b = "Review and draft reply to John on ticket 4512"
    assert not queue.same_shape(a, b)
    for w in (a, b, "Review and draft reply to Ari on ticket 4513"):
        _done(conn, [_ben(w)])  # a client's third draft in a week lands
    assert len(_open(conn)) == 3


def test_same_shape_needs_the_whole_template_not_three_words():
    assert not queue.same_shape("Decide whether ticket 4512 is billable", "Decide whether ticket 4513 needs a CVC")
    assert queue.same_shape("Decide #968 keep or cancel in the tracker", "Decide #444 keep or cancel in the tracker")


def test_a_childs_different_question_on_the_same_ticket_does_not_cover(conn):
    p, _ = _done(conn, [_ben("Decide whether ticket 4512 is billable")])
    _, out = _done(conn, [_ben("Decide whether ticket 4512 needs a CVC")], parent_id=p)
    assert "covered" not in out and len(_open(conn)) == 2
    assert not queue.same_ask("Decide whether ticket 4512 needs a CVC", "Decide whether ticket 4512 is billable")


def test_a_year_is_not_an_anchor():
    assert queue.anchors("Approve the 2026 holiday calendar") == set()
    assert queue.anchors("Decide #2026 keep or cancel") == {"2026"}
    assert not queue.duplicate("Approve the 2026 holiday calendar", "Decide the 2026 rate card for support")


def test_get_from_needs_a_named_person():
    assert queue.route_for("Get the item list from the PROD table", "MAG") != "reply"
    assert queue.route_for("Get the app version from Mayuri", "MAG") == "reply"


@pytest.mark.parametrize("what", ["Call John about the go-live date", "Trust the MIGR workspace",
                                  "Meet with Darren on the cutover", "Sign in to the client VPN portal"])
def test_ben_only_acts_without_a_keyword_land_untagged(conn, what):
    _done(conn, [_ben(what)])
    assert list(_open(conn)) == [what]


@pytest.mark.parametrize("what", ["Call the API again with the fixed key", "Check the totals meet the spec"])
def test_the_new_acts_do_not_reopen_the_agent_work_hole(what):
    assert not queue.bens_own(what)
