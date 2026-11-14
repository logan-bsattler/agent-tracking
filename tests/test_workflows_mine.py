"""Step 1 of the workflow build: the read-only miner tags tasks and intents
to catalog workflows and reports on them."""

from __future__ import annotations

import sqlite3

import pytest

from coord_mcp import store
from coord_mcp.db import connect
from coord_mcp.workflows import CATALOG
from coord_mcp.workflows import __main__ as cli
from coord_mcp.workflows import mine as mn
from coord_mcp.workflows.tag import pattern, tag_intent, tag_root


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "t.db"
    c = connect(p)
    yield p, c
    c.close()


@pytest.mark.parametrize("kind,title,client,source,want", [
    ("investigation", "Cascade: board cleanup evidence check 10/27", "Cascade", None, "D1"),
    ("code_change", "Royal: draft training-material DoD checklist (dod/training-material.md)", "Royal", None, "D5"),
    ("investigation", "List Royal DEVL state a PROD->DEVL refresh would wipe (#231)", "Royal", None, "B3"),
    ("draft_reply", "LNK: reply John Polich with weekend CVC deployment list", "LNK", "email-status", "A4"),
    ("investigation", "LNK: status of Voided checks in custom Positive Pay browse (RAID 695)", "LNK", "email", "A1"),
    ("investigation", "MAG: size two FDDs David Roe assigned Ben (P2F-968, P2R PO Change Tracking)", "MAG", "email", "A5"),
    ("investigation", "Furlani: PO Zero Price release check - options for Ben's ruling", "Furlani", "email-reply", "A6"),
    ("investigation", "LNK: why TEST Production Orders screen still lacks the Location field", "LNK", "email", "A3"),
    ("deliverable", "LNK: RAID log text for 695, 747/Dev 86, 689, 680 per John status 11/13", "LNK", None, "C2"),
    ("code_change", "MAG: build timeline r6 - overlapping dependency waves, generic resource names", "MAG", None, "C3"),
    ("code_change", "Create xxauto01.p + xxauto01qra.p Julian-date auto-lot programs (pipeline #286)", "LNK", None, "B1"),
    ("investigation", "Yamamoto: find where Nimbello export targets the demo site", "Yamamoto", "email", "A11"),
    ("draft_reply", "Royal: reply to Rick Van Baren on QRF training session availability", "Royal", "email-reply", "A6"),
])
def test_roots_tag_to_the_catalog(kind, title, client, source, want):
    code, rule = tag_root(kind, title, {}, client, source)
    assert code == want, rule
    assert code in CATALOG


def test_intents_tag_by_source_when_the_source_is_the_workflow():
    assert tag_intent("email-closes", ["A1"])[0] == "A7"
    assert tag_intent("teams-commitment", [])[0] == "A8"
    assert tag_intent("email", ["B1"])[0] == "B1"
    assert tag_intent("meeting", [])[0] == "A9"
    assert tag_intent("email", [])[0] is None


def test_pattern_strips_the_client():
    assert pattern("MAG: rebuild Shelton tracker", "code_change") == "code_change: rebuild shelton"


def test_children_inherit_their_root_and_ben_loose_ends_count(db):
    path, c = db
    root = store.create_task(c, "investigation", "LNK: why the AMEX report fails", {}, assigned_to="LNK")["task_id"]
    kid = store.create_task(c, "code_change", "LNK: fix it", {}, assigned_to="LNK", parent_id=root)["task_id"]
    gkid = store.create_task(c, "draft_reply", "LNK: tell John", {}, assigned_to="LNK", parent_id=kid)["task_id"]
    store.complete_task(c, root, {"done": True, "verdict": "v", "confidence": "high", "evidence": ["e"],
                                  "follow_ups": [{"who": "Ben", "what": "approve the fix"},
                                                 {"who": "LNK", "what": "fix it"}]})
    ro = mn.connect_ro(path)
    try:
        t = mn.tag_board(ro)
        assert {t["tasks"][x]["workflow"] for x in (root, kid, gkid)} == {"A3"}
        assert t["cases"][root]["tasks"] and t["cases"][root]["open"]
        m = mn.mine(ro)
        a3 = next(r for r in m["workflows"] if r["workflow"] == "A3")
        assert (a3["cases"], a3["tasks"], a3["ben_loose_ends"], a3["ben_open"]) == (1, 3, 1, 1)
        assert a3["median_cycle_h"] is None  # still open
    finally:
        ro.close()


def test_the_miner_cannot_write(db):
    path, _ = db
    ro = mn.connect_ro(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO meta(key, value) VALUES ('x', 'y')")
    finally:
        ro.close()


def test_cli_writes_the_html_report(db, tmp_path, capsys):
    path, c = db
    store.create_task(c, "investigation", "MAG: reconcile the keeper list", {}, assigned_to="MAG")
    out = tmp_path / "r.html"
    assert cli.main(["mine", "--db", str(path), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert text.startswith("<!doctype html>") and "Running analysis" in text and "Catalog revisions" in text
    assert "1/1 tagged" in capsys.readouterr().out
