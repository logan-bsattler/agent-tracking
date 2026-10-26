"""Deliverables: a working file, a definition-of-done review, and Ben's promote."""

from __future__ import annotations

import os

import pytest

from coord_mcp import contracts, report, store
from coord_mcp.db import connect, now

CHECK = [{"item": "Every FDD has an hour estimate", "met": True}]


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _deliverable(conn, root, name="sizing-draft.md", client="Furlani", text="# v1"):
    (root / client / "work").mkdir(parents=True, exist_ok=True)
    (root / client / "work" / name).write_text(text, encoding="utf-8")
    tid = store.create_task(conn, "deliverable", "Sizing doc", {"q": "x"}, assigned_to=client)["task_id"]
    store.complete_task(conn, tid, {"done": True, "summary": "sized", "path": f"work/{name}",
                                    "deliverable_type": "sizing", "dod_path": "dod/sizing.md",
                                    "self_check": CHECK})
    return tid


def _review(conn, tid, verdict="approve", criteria=CHECK, findings=None):
    rid = store.create_task(conn, "dod_review", "Review sizing", {"q": "x"}, assigned_to="Furlani",
                            parent_id=tid)["task_id"]
    res = {"done": True, "verdict": verdict, "summary": "checked", "path": "work/sizing-draft.md",
           "dod_path": "dod/sizing.md", "criteria": criteria}
    if findings:
        res["findings"] = findings
    store.complete_task(conn, rid, res)
    return rid


def _stage(conn, root, tid):
    return next(d for d in store.deliverables(conn, root=root)["files"] if d["task_id"] == tid)


def test_an_approval_with_an_unmet_item_or_a_blocker_is_refused():
    base = {"done": True, "summary": "s", "path": "p.md", "dod_path": "d.md"}
    with pytest.raises(contracts.ValidationError, match="not met"):
        contracts.validate("dod_review", {**base, "verdict": "approve",
                                          "criteria": [{"item": "a", "met": False}]})
    with pytest.raises(contracts.ValidationError, match="blocker"):
        contracts.validate("dod_review", {**base, "verdict": "approve", "criteria": CHECK,
                                          "findings": [{"criterion": "a", "severity": "blocker", "note": "n"}]})
    with pytest.raises(contracts.ValidationError, match="every definition-of-done item"):
        contracts.validate("dod_review", {**base, "verdict": "approve", "criteria": []})
    ok = contracts.validate("dod_review", {**base, "verdict": "request_changes",
                                           "criteria": [{"item": "a", "met": False}]})
    assert ok["verdict"] == "request_changes"
    assert contracts.validate("dod_review", {"done": False, "reason": "no file", "retryable": True})


def test_stages_follow_the_newest_review(conn, tmp_path):
    tid = _deliverable(conn, tmp_path)
    assert _stage(conn, tmp_path, tid)["stage"] == "review"
    _review(conn, tid, "request_changes", [{"item": "Every FDD has an hour estimate", "met": False}])
    d = _stage(conn, tmp_path, tid)
    assert (d["stage"], d["reviews"]) == ("changes", 1)
    _review(conn, tid)
    d = _stage(conn, tmp_path, tid)
    assert (d["stage"], d["reviews"]) == ("approved", 2)
    names = [x["name"] for x in store.deliverables(conn, root=tmp_path)["files"]]
    assert names == ["sizing-draft.md"]  # the reviews name the same file; they are not deliverables


def test_promote_refuses_until_approved_and_unchanged(conn, tmp_path):
    tid = _deliverable(conn, tmp_path)
    with pytest.raises(ValueError, match="does not approve"):
        store.promote(conn, tid, root=tmp_path)
    _review(conn, tid, "escalate")
    with pytest.raises(ValueError, match="does not approve"):
        store.promote(conn, tid, root=tmp_path)
    _review(conn, tid)
    f = tmp_path / "Furlani" / "work" / "sizing-draft.md"
    later = now() + 120
    os.utime(f, (later, later))
    assert _stage(conn, tmp_path, tid)["stage"] == "stale"
    with pytest.raises(ValueError, match="review it again"):
        store.promote(conn, tid, root=tmp_path)
    other = store.create_task(conn, "investigation", "x", {"q": "x"})["task_id"]
    with pytest.raises(ValueError, match="only a finished deliverable"):
        store.promote(conn, other, root=tmp_path)


def test_promote_copies_a_new_version_and_never_overwrites(conn, tmp_path):
    tid = _deliverable(conn, tmp_path)
    _review(conn, tid)
    r = store.promote(conn, tid, root=tmp_path)
    out = tmp_path / "Furlani" / "deliverables" / "sizing-v1.md"
    assert r["version"] == 1 and r["dest"] == str(out) and out.read_text(encoding="utf-8") == "# v1"
    assert store.promote(conn, tid, root=tmp_path)["duplicate"] is True
    assert any("Promoted sizing-draft.md" in d["statement"] for d in store.get_decisions(conn, tid))
    assert _stage(conn, tmp_path, tid)["stage"] == "promoted"

    t2 = _deliverable(conn, tmp_path, text="# v2")
    _review(conn, t2)
    assert store.promote(conn, t2, root=tmp_path)["version"] == 2
    assert out.read_text(encoding="utf-8") == "# v1"
    final = [d for d in store.deliverables(conn, root=tmp_path)["files"] if d["stage"] == "final"]
    assert [(d["name"], d["version"]) for d in final] == [("sizing-v2.md", 2), ("sizing-v1.md", 1)]
    assert store.deliverable_path(conn, tid, str(out), root=tmp_path) == out


def test_promote_needs_the_client_folder(conn, tmp_path):
    tid = store.create_task(conn, "deliverable", "x", {"q": "x"}, assigned_to="Nobody")["task_id"]
    store.complete_task(conn, tid, {"done": True, "summary": "s", "path": "a.md", "deliverable_type": "t",
                                    "dod_path": "d.md", "self_check": CHECK})
    _review(conn, tid)
    with pytest.raises(ValueError, match="no client folder"):
        store.promote(conn, tid, root=tmp_path)


def test_page_splits_final_from_in_progress_and_offers_promote(conn, tmp_path):
    tid = _deliverable(conn, tmp_path)
    _review(conn, tid)
    html = report.deliverables_page(store.deliverables(conn, root=tmp_path))
    assert "In progress" in html and 'action="/api/promote"' in html and "ready to promote" in html
    assert '<h2 class="section">Final</h2>' not in html
    store.promote(conn, tid, root=tmp_path)
    html = report.deliverables_page(store.deliverables(conn, root=tmp_path))
    assert '<h2 class="section">Final</h2>' in html and "sizing-v1.md" in html and "final v1" in html
    assert 'action="/api/promote"' not in html
