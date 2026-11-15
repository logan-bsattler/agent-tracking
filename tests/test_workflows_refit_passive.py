"""Step-limit refit (workflows/refit.py) and passive-step auto-close (workflows/passive.py)."""

from __future__ import annotations

import json

import pytest

from coord_mcp import queue, store
from coord_mcp.db import connect, now
from coord_mcp.workflows import cases as cs
from coord_mcp.workflows import definitions as defs
from coord_mcp.workflows import passive, refit

INV = {"done": True, "verdict": "v", "confidence": "high", "evidence": ["e"]}
DAY = 86400


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("COORD_TZ", "UTC")
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def test_pct_interpolates():
    assert refit.pct([1, 2, 3, 4, 5]) == 4
    assert refit.pct([7]) == 7


def test_sla_for_caps_and_units():
    from coord_mcp.workflows import sla
    day = sla.day_seconds()
    assert refit.sla_for(3 * sla.HOUR, None) == {"business_hours": 3}
    assert refit.sla_for(2.2 * day, None) == {"business_days": 3}
    assert refit.sla_for(40 * day, {"business_days": 2}) == {"business_days": refit.CAP_DAYS}
    assert refit.sla_for(40 * day, {"business_days": 20}) == {"business_days": 20}


def test_a_ruled_sla_is_pinned():
    a2 = defs.get("A2")["steps"]
    assert all(refit.pinned(s) for s in a2)
    assert not refit.pinned({"sla": {"business_days": 1}})
    assert not refit.pinned({"sla": {"business_days": 1, "why": "refit to p75 of 9 observed stays"}})


def test_plan_only_raises(monkeypatch, conn):
    from coord_mcp.workflows import sla
    day = sla.day_seconds()
    obs = {("C3", "input"): [0] * 10, ("C3", "regen"): [4 * day] * 10, ("C3", "rulings"): [day] * 3}
    monkeypatch.setattr(refit, "observe", lambda c, t=None: obs)
    rows = {(r["workflow"], r["step"]): r for r in refit.plan(conn)}
    assert ("C3", "input") not in rows          # would lower: kept
    assert ("C3", "rulings") not in rows        # too few observations
    assert rows[("C3", "regen")]["new"] == {"business_days": 4}


def test_apply_writes_valid_defs(tmp_path):
    for code in ("C3",):
        (tmp_path / f"{code}.json").write_text(json.dumps(defs.get(code)), encoding="utf-8")
    rows = [{"workflow": "C3", "step": "use", "n": 9, "new": {"business_days": 6}}]
    assert refit.apply(rows, day="2026-11-15", root=tmp_path) == ["C3.json"]
    d = json.loads((tmp_path / "C3.json").read_text(encoding="utf-8"))
    st = next(s for s in d["steps"] if s["key"] == "use")
    assert st["sla"]["business_days"] == 6 and st["sla"]["why"].startswith("refit")
    assert defs.validate(d) == []


def test_passive_flag_validates():
    d = json.loads(json.dumps(defs.get("C3")))
    assert defs.validate(d) == []
    d["steps"][0]["passive"] = True            # an agent step
    assert any("passive" in e for e in defs.validate(d))
    assert passive.steps()["C3"] == {"use"}


def _case_at_use(conn, what, age_days):
    """A C3 case whose last task landed with a Ben loose end, waiting at 'use'."""
    tid = store.create_task(conn, "investigation", "MAG breakdown input", {"q": "x"}, assigned_to="MAG")["task_id"]
    store.get_task(conn, tid, reader="MAG")
    store.complete_task(conn, tid, {**INV, "follow_ups": [{"who": "Ben", "what": what}]})
    cs.ensure_workflows(conn)
    cid = cs.open_case(conn, tid)
    conn.execute("UPDATE workflow_cases SET workflow='C3' WHERE id=?", (cid,))
    conn.execute("UPDATE tasks SET case_id=?, case_step='apply' WHERE id=?", (cid, tid))
    conn.execute("UPDATE workflow_cases SET state='open', step='use', waits_on='ben' WHERE id=?", (cid,))
    fid = conn.execute("SELECT id FROM follow_ups WHERE task_id=?", (tid,)).fetchone()["id"]
    conn.execute("UPDATE follow_ups SET created_at=? WHERE id=?", (now() - age_days * DAY, fid))
    conn.commit()
    return fid


def test_passive_closes_old_reading_items_and_keeps_asks(conn, monkeypatch):
    monkeypatch.setattr(cs, "sync", lambda *a, **k: {})
    old = _case_at_use(conn, "Updated breakdown is in work/; skim the new tiers", 20)
    young = _case_at_use(conn, "Updated breakdown is in work/; skim tier B", 1)
    ask = _case_at_use(conn, "Approve the retire list", 20)
    ids = {r["id"] for r in passive.closable(conn)}
    assert ids == {old} and young not in ids and ask not in ids
    out = passive.close(conn, apply=True)
    assert out["count"] == 1
    assert conn.execute("SELECT state FROM follow_ups WHERE id=?", (old,)).fetchone()["state"] == "dropped"
    assert passive.undo(conn, out["decision_id"])["reopened"] == 1
    assert passive.closable(conn) == []        # reopened by hand: left alone


def test_daily_pass_is_off_by_default(conn):
    assert queue.switch(conn, "passive_close") is False
    assert passive.due(conn) is None
