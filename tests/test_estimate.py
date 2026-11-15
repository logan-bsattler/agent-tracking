"""The estimate band (coord_mcp/estimate.py, Ben 2026-11-15): a sizing inside
the band closes Ben's approval item; anything else leaves it his."""

from __future__ import annotations

import pytest

from coord_mcp import contracts, estimate, queue, store
from coord_mcp.db import connect
from coord_mcp.workflows import definitions as defs

INV = {"done": True, "verdict": "v", "confidence": "high", "evidence": ["e"]}
pytestmark = pytest.mark.ben_checks

APPROVE = {"who": "Ben", "what": "Approve the estimate for #260 (16-22 h)", "reason": "approve"}


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _land(conn, estimate_=None, follow_ups=(APPROVE,), client="Moog"):
    tid = store.create_task(conn, "investigation", "Size #260", {"q": "x"}, assigned_to=client)["task_id"]
    store.get_task(conn, tid, reader=client)
    res = {**INV, "follow_ups": list(follow_ups)}
    if estimate_ is not None:
        res["estimate"] = estimate_
    out = store.complete_task(conn, tid, res)
    fus = {r["what"]: r["state"] for r in conn.execute("SELECT what, state FROM follow_ups WHERE task_id=?", (tid,))}
    return tid, out, fus


def test_inside_band_auto_accepts_and_closes(conn):
    tid, out, fus = _land(conn, {"low_hours": 16, "high_hours": 22})
    assert fus[APPROVE["what"]] == "dropped"
    acc = out["estimate_auto_accepted"]
    assert len(acc) == 1 and acc[0]["ratio"] == 1.38
    dec = [d for d in store.get_decisions(conn, task_id=tid) if d["statement"].startswith("Estimate auto-accepted")]
    assert dec[0]["statement"] == f"Estimate auto-accepted 16-22 h (ratio 1.38) on task {tid}"
    assert dec[0]["because"].endswith("auto band; Ben may override")
    row = conn.execute("SELECT resolved_by FROM follow_ups WHERE task_id=?", (tid,)).fetchone()
    assert row["resolved_by"] == dec[0]["id"]


@pytest.mark.parametrize("est", [
    {"low_hours": 10, "high_hours": 16},     # ratio 1.6 > 1.5
    {"low_hours": 20, "high_hours": 28},     # high end 28 > 24
    None,                                    # no numbers
])
def test_outside_band_or_missing_stays_ben(conn, est):
    _, out, fus = _land(conn, est)
    assert fus[APPROVE["what"]] == "open"
    assert "estimate_auto_accepted" not in out


def test_switch_off_restores_old_behaviour(conn):
    queue.set_switch(conn, "auto_estimate", False)
    _, out, fus = _land(conn, {"low_hours": 16, "high_hours": 22})
    assert fus[APPROVE["what"]] == "open"
    assert "estimate_auto_accepted" not in out


def test_band_settings_change_the_limits(conn):
    queue.set_setting(conn, "estimate_max_high", 40)
    queue.set_setting(conn, "estimate_max_ratio", 2)
    _, _, fus = _land(conn, {"low_hours": 20, "high_hours": 36})
    assert fus[APPROVE["what"]] == "dropped"


def test_only_the_approval_closes(conn):
    other = {"who": "Ben", "what": "Approve the estimate and set the SharePoint dates", "reason": "approve"}
    send = {"who": "Ben", "what": "Review and send the quote to Ari", "reason": "send"}
    _, _, fus = _land(conn, {"low_hours": 16, "high_hours": 22}, follow_ups=(APPROVE, other, send))
    assert fus[APPROVE["what"]] == "dropped"
    assert fus[other["what"]] == "open" and fus[send["what"]] == "open"


def test_defaults():
    assert queue.SWITCHES["auto_estimate"] is True
    assert queue.SETTINGS == {"estimate_max_ratio": 1.5, "estimate_max_high": 24.0, "auto_checkin_hours": 4.0}


@pytest.mark.parametrize("low,high,ok", [
    (16, 24, True), (16, 24.5, False), (10, 15, True), (10, 15.1, False),
    (0, 4, False), (8, 4, False), ("8", 12, False), (True, 2, False), (float("nan"), 4, False),
])
def test_check(low, high, ok):
    assert estimate.check(low, high, 1.5, 24)[0] is ok


def test_contract_estimate_is_optional_and_numeric():
    contracts.validate("investigation", INV)
    contracts.validate("investigation", {**INV, "estimate": {"low_hours": 2.5, "high_hours": 3}})
    with pytest.raises(contracts.ValidationError):
        contracts.validate("investigation", {**INV, "estimate": {"low_hours": "2", "high_hours": 3}})
    assert contracts.expected_shape("deliverable")["estimate?"] == {"low_hours": "num", "high_hours": "num"}


def test_b1_estimate_ok_is_ben_with_auto_band():
    st = next(s for s in defs.get("B1")["steps"] if s["key"] == "estimate_ok")
    assert st["who"] == "ben" and st["auto"] == {"rule": "estimate_band", "fallback": "ben"}
    bad = {**defs.get("B1"), "steps": [dict(s, auto={"rule": "nope", "fallback": "ben"}) if s["key"] == "estimate_ok"
                                        else s for s in defs.get("B1")["steps"]]}
    assert any("auto.rule" in e for e in defs.validate(bad))
