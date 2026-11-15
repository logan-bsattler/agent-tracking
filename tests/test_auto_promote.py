"""An approving dod_review promotes its deliverable (auto_promote; Ben,
2026-11-15): Ben's Promote click on /deliverables goes; his send stays."""

from __future__ import annotations

import os

import pytest

from coord_mcp import queue, store
from coord_mcp.db import connect, now

CHECK = [{"item": "Every FDD has an hour estimate", "met": True}]


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DOCS_ROOT", tmp_path)
    c = connect(tmp_path / "t.db")
    queue.set_switch(c, "auto_promote", True)
    yield c
    c.close()


def _deliverable(conn, root, name="sizing-draft.html", client="Furlani", extra=None):
    work = root / client / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / name).write_text("<p>v1</p>", encoding="utf-8")
    for e in extra or []:
        (work / e).write_text("x", encoding="utf-8")
    tid = store.create_task(conn, "deliverable", "Sizing doc", {"q": "x"}, assigned_to=client)["task_id"]
    path = " ; ".join([f"work/{name}"] + [f"work/{e}" for e in extra or []])
    store.complete_task(conn, tid, {"done": True, "summary": "sized", "path": path, "deliverable_type": "sizing",
                                    "dod_path": "dod/sizing.md", "self_check": CHECK})
    return tid


def _review(conn, tid, verdict="approve", follow_ups=None):
    rid = store.create_task(conn, "dod_review", "Review sizing", {"q": "x"}, assigned_to="Furlani",
                            parent_id=tid)["task_id"]
    res = {"done": True, "verdict": verdict, "summary": "checked", "path": "work/sizing-draft.html",
           "dod_path": "dod/sizing.md", "criteria": CHECK if verdict == "approve" else
           [{"item": "Every FDD has an hour estimate", "met": False}]}
    if follow_ups:
        res["follow_ups"] = follow_ups
    return rid, store.complete_task(conn, rid, res)


def _state(conn, fid):
    return conn.execute("SELECT state FROM follow_ups WHERE id=?", (fid,)).fetchone()["state"]


def _ben(conn, rid):
    return {r["what"]: r["id"] for r in conn.execute("SELECT id, what FROM follow_ups WHERE task_id=? AND who='Ben'",
                                                     (rid,))}


def test_an_approve_promotes_and_closes_bens_promote_item(conn, tmp_path):
    tid = _deliverable(conn, tmp_path, extra=["sizing-draft.xlsx"])
    rid, out = _review(conn, tid, follow_ups=[
        {"who": "Ben", "what": "Promote sizing-draft.html (Furlani) on /deliverables"},
        {"who": "Ben", "what": "Promote it and send it to John before Thursday"}])
    ap = out["auto_promote"]
    assert ap["ok"] and [p["version"] for p in ap["promoted"]] == [1]
    out_dir = tmp_path / "Furlani" / "deliverables"
    assert (out_dir / "sizing-v1.html").is_file() and (out_dir / "sizing-v1.xlsx").is_file()
    items = _ben(conn, rid)
    assert _state(conn, items["Promote sizing-draft.html (Furlani) on /deliverables"]) == "dropped"
    assert _state(conn, items["Promote it and send it to John before Thursday"]) == "open"  # the send is his
    decs = [d["statement"] + " | " + (d["because"] or "") for d in store.get_decisions(conn, tid)]
    assert any(s.startswith("Promoted sizing-draft.html") and rid in s and "auto_promote" in s for s in decs)
    assert any(s.startswith("Auto-promoted: Ben loose end") and rid in s for s in decs)
    assert next(d for d in store.deliverables(conn, root=tmp_path)["files"]
                if d["task_id"] == tid)["stage"] == "promoted"
    # manual Promote still works: the same file is a duplicate, not a v2
    assert store.promote(conn, tid, root=tmp_path)["duplicate"] is True


def test_each_file_of_a_batch_is_promoted(conn, tmp_path):
    tid = _deliverable(conn, tmp_path, name="FDD-1413 - DRAFT.docx", extra=["FDD-1414 - DRAFT.docx"])
    _, out = _review(conn, tid)
    assert out["auto_promote"]["ok"] and len(out["auto_promote"]["promoted"]) == 2
    assert sorted(p.name for p in (tmp_path / "Furlani" / "deliverables").iterdir()) == \
        ["FDD-1413-v1.docx", "FDD-1414-v1.docx"]


def test_switch_off_leaves_promote_to_ben(conn, tmp_path):
    queue.set_switch(conn, "auto_promote", False)
    tid = _deliverable(conn, tmp_path)
    rid, out = _review(conn, tid, follow_ups=[{"who": "Ben", "what": "Promote sizing-draft.html on /deliverables"}])
    assert "auto_promote" not in out
    assert not (tmp_path / "Furlani" / "deliverables").exists()
    assert _state(conn, _ben(conn, rid)["Promote sizing-draft.html on /deliverables"]) == "open"
    assert store.promote(conn, tid, root=tmp_path)["version"] == 1


def test_a_refused_promote_leaves_bens_item(conn, tmp_path):
    tid = _deliverable(conn, tmp_path)
    f = tmp_path / "Furlani" / "work" / "sizing-draft.html"
    later = now() + 120
    os.utime(f, (later, later))  # changed after the review approved it
    rid, out = _review(conn, tid, follow_ups=[{"who": "Ben", "what": "Promote sizing-draft.html on /deliverables"}])
    ap = out["auto_promote"]
    assert ap["ok"] is False and "review it again" in ap["refused"][0]
    assert not (tmp_path / "Furlani" / "deliverables").exists()
    assert _state(conn, _ben(conn, rid)["Promote sizing-draft.html on /deliverables"]) == "open"
    assert any(d["statement"].startswith("Auto-promote refused") for d in store.get_decisions(conn, tid))


def test_request_changes_promotes_nothing(conn, tmp_path):
    tid = _deliverable(conn, tmp_path)
    _, out = _review(conn, tid, "request_changes")
    assert "auto_promote" not in out and not (tmp_path / "Furlani" / "deliverables").exists()
