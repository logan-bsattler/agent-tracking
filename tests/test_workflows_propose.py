"""The weekly workflow miner (coord_mcp/workflows/propose.py)."""

from __future__ import annotations

import json
import shutil
import time
from datetime import datetime

import pytest

from coord_mcp import queue, store
from coord_mcp.db import connect
from coord_mcp.workflows import CATALOG
from coord_mcp.workflows import cases as cs
from coord_mcp.workflows import definitions as defs
from coord_mcp.workflows import propose as pp


@pytest.fixture()
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(queue, "SWITCHES", {**{k: False for k in queue.SWITCHES}})
    ddir = tmp_path / "defs"
    shutil.copytree(defs.DEFS_DIR, ddir)
    monkeypatch.setattr(defs, "DEFS_DIR", ddir)
    monkeypatch.setattr(pp, "PROPOSED_DIR", tmp_path / "proposed")
    saved = dict(CATALOG)
    conn = connect(tmp_path / "t.db")
    cs.ensure_workflows(conn)
    yield conn
    conn.close()
    CATALOG.clear()
    CATALOG.update(saved)


def _status_cases(c, n=5, client="LNK"):
    """n status requests that each grew a code change (A1 has no step for one)."""
    roots = []
    for i in range(n):
        r = store.create_task(c, "investigation", f"{client}: status of label program {i}", {}, assigned_to=client)
        store.create_task(c, "code_change", f"{client}: fix label program {i}", {}, assigned_to=client,
                          parent_id=r["task_id"])
        roots.append(r["task_id"])
    cs.sync(c)
    return roots


def _untagged(c, n=5):
    for i in range(n):
        store.create_task(c, "dod_review", f"LNK: widget harmonise pass {i}", {}, assigned_to="LNK")
    cs.sync(c)


def test_drift_finds_a_kind_the_definition_lacks(c):
    _status_cases(c)
    f = [x for x in pp.find(c) if x["type"] == "lacks"]
    assert len(f) == 1 and f[0]["workflow"] == "A1" and f[0]["kind"] == "code_change"
    assert len(f[0]["cases"]) == 5 and 3 <= len(f[0]["examples"]) <= 5


def test_below_threshold_is_quiet(c):
    _status_cases(c, n=4)
    assert pp.find(c) == []


def test_draft_change_validates_and_adds_an_optional_step(c):
    _status_cases(c)
    [p] = pp.draft(c, pp.find(c))
    assert p["kind"] == "change" and p["code"] == "A1" and p["errors"] == []
    added = [s for s in p["definition"]["steps"] if s["kinds"] == ["code_change"]]
    assert added and added[0]["optional"] is True and added[0]["who"] == "agent"
    assert p["base_sha"] == pp._sha(defs.get("A1"))


def test_new_cluster_drafts_a_new_workflow_with_a_tag_rule(c):
    _untagged(c)
    props = pp.draft(c, pp.find(c))
    [p] = [x for x in props if x["kind"] == "new"]
    assert p["errors"] == [] and p["code"] not in CATALOG and p["cases"] == 5
    assert p["definition"]["engine"] is False and p["definition"]["tag"]["kinds"] == ["dod_review"]


def test_file_puts_ben_items_in_the_queue_and_never_refiles(c):
    _status_cases(c)
    props = pp.draft(c, pp.find(c))
    pp.write(props)
    out = pp.file(c, props)
    assert len(out["filed"]) == 1
    fid = out["filed"][0]["follow_up_id"]
    row = c.execute("SELECT * FROM follow_ups WHERE id=?", (fid,)).fetchone()
    assert row["who"] == "Ben" and row["task_id"] is None and row["client"] == pp.CLIENT
    assert row["what"].startswith(f"Approve workflow {props[0]['id']} Status request (5 cases)")
    assert "/workflows/proposed/" in row["ref"]
    assert c.execute("SELECT state FROM intents WHERE id=?", (row["intent_id"],)).fetchone()[0] == "dismissed"
    again = pp.file(c, props)
    assert again["filed"] == [] and "open" in again["skipped"][0]["why"]
    # Ben defers it from /queue: never filed again.
    queue.apply(c, "defer", [fid])
    assert pp.file(c, props)["filed"] == []


def test_weekly_cap(c):
    props = [{"id": f"X{i}", "code": "A1", "kind": "change", "name": "Status request", "cases": 9 - i,
              "errors": []} for i in range(5)]
    for p in props:
        p["definition"] = defs.get("A1")
    out = pp.file(c, props)
    assert [f["id"] for f in out["filed"]] == ["X0", "X1", "X2"]
    assert all("cap" in s["why"] for s in out["skipped"])


def test_adopt_change_writes_defs_records_and_backfills(c):
    _status_cases(c)
    props = pp.draft(c, pp.find(c))
    pp.write(props)
    fid = pp.file(c, props)["filed"][0]["follow_up_id"]
    out = pp.answer(c, fid, "Approve, thanks")
    assert out["ok"] and out["code"] == "A1"
    d = json.loads((defs.DEFS_DIR / "A1.json").read_text(encoding="utf-8"))
    assert any(s["kinds"] == ["code_change"] for s in d["steps"])
    assert defs.get("A1") == d  # the loader sees it without a restart
    assert c.execute("SELECT state FROM follow_ups WHERE id=?", (fid,)).fetchone()[0] == "dropped"
    assert c.execute("SELECT COUNT(*) FROM decisions WHERE statement LIKE 'Adopted workflow%'").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM tasks WHERE kind='code_change' AND case_step IS NULL").fetchone()[0] == 0
    assert not (pp.PROPOSED_DIR / f"{out['id']}.json").exists()
    assert pp.find(c) == []  # the drift is gone


def test_adopt_new_workflow_engine_off_and_retags(c):
    _untagged(c)
    props = pp.draft(c, pp.find(c))
    pp.write(props)
    [p] = [x for x in props if x["kind"] == "new"]
    out = pp.adopt(c, p["id"])
    assert out["engine"] is False and out["backfill"]["retagged"] == 5
    assert p["code"] in CATALOG
    assert c.execute("SELECT COUNT(*) FROM workflow_cases WHERE workflow=?", (p["code"],)).fetchone()[0] == 5


def test_adopt_refuses_a_stale_base(c):
    _status_cases(c)
    props = pp.draft(c, pp.find(c))
    pp.write(props)
    f = defs.DEFS_DIR / "A1.json"
    d = json.loads(f.read_text(encoding="utf-8"))
    d["notes"] = "edited by hand"
    f.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(ValueError, match="changed since"):
        pp.adopt(c, props[0]["id"])


def test_reject_and_answer_words(c):
    _status_cases(c)
    props = pp.draft(c, pp.find(c))
    pp.write(props)
    fid = pp.file(c, props)["filed"][0]["follow_up_id"]
    assert pp.answer(c, fid, "what does this change?") is None
    assert pp.answer(c, fid, "reject")["state"] == "rejected"
    assert c.execute("SELECT state FROM follow_ups WHERE id=?", (fid,)).fetchone()[0] == "dropped"
    assert pp.file(c, props)["filed"] == []


def test_weekly_runs_once_from_monday_0630(c):
    mon = datetime(2026, 11, 16, 6, 29).timestamp()
    assert pp.due(c, mon) is None
    assert pp.due(c, mon + 120) == "2026-W47"
    _status_cases(c)
    out = pp.weekly(c, mon + 120)
    assert out["week"] == "2026-W47" and len(out["filed"]) == 1
    assert pp.weekly(c, mon + 3600) is None  # once a week
    assert pp.due(c, mon + 86400 * 3) is None  # Thursday, same week
    c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('switch:workflow_propose', 'off')")
    assert pp.weekly(c, mon + 7 * 86400 + 120) is None


def test_every_shipped_definition_still_validates():
    for code, d in defs.load().items():
        assert defs.validate(d) == [], code


def test_proposal_page_renders(c):
    _status_cases(c)
    props = pp.draft(c, pp.find(c))
    pp.write(props)
    html = pp.render(c, props[0]["id"])
    assert "Approve (adopt)" in html and "code_change" in html and "<b>new</b>" in html
    assert "Workflow proposals" in pp.render(c, None)
    assert pp.render(c, "nope") is None
    _ = time


@pytest.mark.parametrize("code,after", [("A1", "status_read"), ("A3", "investigate"), ("A5", "read")])
def test_drift_insert_keeps_each_spawn_waiting_on_the_same_step(code, after):
    # An inserted optional step must not become what an engine spawn waits on.
    def waits(d):
        st = d["steps"]
        return {s["key"]: s["spawn"].get("after") or st[j - 1]["key"] for j, s in enumerate(st) if s.get("spawn")}
    before = waits(defs.get(code))
    d = pp._draft_change(code, [{"type": "lacks", "kind": "data_pull", "after": after, "secs": []}])
    assert waits(d) == before
    assert defs.validate(d) == []


def test_uncommitted_defs_lists_untracked_and_changed_definitions(tmp_path):
    import subprocess
    run = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    d = tmp_path / "defs"
    d.mkdir()
    (d / "A1.json").write_text("{}", encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", "x")
    assert pp.uncommitted_defs(d) == [] and pp.commit_note([]) == ""
    (d / "A1.json").write_text('{"a": 1}', encoding="utf-8")
    (d / "C5.json").write_text("{}", encoding="utf-8")
    (d / "notes.txt").write_text("x", encoding="utf-8")
    assert pp.uncommitted_defs(d) == ["A1.json", "C5.json"]
    assert "C5.json" in pp.commit_note(pp.uncommitted_defs(d))
    assert pp.uncommitted_defs(tmp_path / "nope") == []
