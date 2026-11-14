"""Step 6 of the workflow build: precedent report; apply behind an off switch."""

from __future__ import annotations

import pytest

from coord_mcp import store
from coord_mcp.db import connect
from coord_mcp.workflows import __main__ as cli
from coord_mcp.workflows import precedent as pc

INV = {"done": True, "verdict": "v", "confidence": "high", "evidence": ["e"]}


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "t.db"
    c = connect(p)
    yield p, c
    c.close()


def _ben(c, client, what):
    t = store.create_task(c, "investigation", f"{client}: thing", {}, assigned_to=client)["task_id"]
    store.complete_task(c, t, {**INV, "follow_ups": [{"who": "Ben", "what": what}]})
    return c.execute("SELECT id FROM follow_ups WHERE task_id=?", (t,)).fetchone()["id"]


def test_shape_strips_entities():
    a = pc.shape("Confirm cancel for xxporecupl.p (#1372) per Maggie's list 11/13", "MAG")
    b = pc.shape("Confirm cancel for xxwostatld.p (#624) per Henric's list 11/02", "MAG")
    assert a == b and "maggie's" not in a


def test_report_settles_only_on_a_ruling_and_apply_is_switched(db, monkeypatch, tmp_path):
    path, c = db
    old = _ben(c, "MAG", "Rule whether to cancel xxporecupl.p (#1372) on the keeper list")
    dec = store.record_decision(c, "Cancel confirmed for 1372", because="Ben")["decision_id"]
    store.resolve_follow_up(c, old, decision_id=dec)
    swept = _ben(c, "MAG", "Decide whether the steering deck keeps the wave plan timeline")
    sweep = store.record_decision(c, "Board cleanup 10/26: drop stale loose ends")["decision_id"]
    store.resolve_follow_up(c, swept, decision_id=sweep)
    new = _ben(c, "MAG", "Rule whether to cancel xxwostatld.p (#624) on the keeper list")
    again = _ben(c, "MAG", "Decide whether the steering deck keeps the wave plan timeline")
    other = _ben(c, "LNK", "Rule whether to cancel xx902.p (#7) on the keeper list")

    rep = pc.report(c)
    by = {r["id"]: r for r in rep["rows"]}
    assert by[new]["verdict"] == "drop" and by[new]["cite"] == dec
    assert again not in by and other not in by  # a sweep is no ruling; precedent stays within a client
    assert rep["tally"] == {"drop": 1, "none": 2}

    monkeypatch.delenv("COORD_WORKFLOW_PRECEDENT", raising=False)
    with pytest.raises(PermissionError):
        pc.apply(c, rep)
    assert cli.main(["precedent", "--db", str(path), "--apply"]) == 1
    assert c.execute("SELECT state FROM follow_ups WHERE id=?", (new,)).fetchone()["state"] == "open"

    out = tmp_path / "p.html"
    assert cli.main(["precedent", "--db", str(path), "--out", str(out)]) == 0
    assert "openpyxl" in out.read_text(encoding="utf-8")

    monkeypatch.setenv("COORD_WORKFLOW_PRECEDENT", "1")
    done = pc.apply(c, rep)
    assert [d["follow_up"] for d in done] == [new]
    r = c.execute("SELECT state, resolved_by FROM follow_ups WHERE id=?", (new,)).fetchone()
    assert r["state"] == "dropped"
    st = c.execute("SELECT statement, because FROM decisions WHERE id=?", (r["resolved_by"],)).fetchone()
    assert dec in st["statement"] and "Ben may override" in st["because"]
    assert pc.apply(c, rep) == []
