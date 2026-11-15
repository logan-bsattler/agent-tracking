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
    assert "Workbook rows" in out.read_text(encoding="utf-8")

    monkeypatch.setenv("COORD_WORKFLOW_PRECEDENT", "1")
    done = pc.apply(c, rep)
    assert [d["follow_up"] for d in done] == [new]
    r = c.execute("SELECT state, resolved_by FROM follow_ups WHERE id=?", (new,)).fetchone()
    assert r["state"] == "dropped"
    st = c.execute("SELECT statement, because FROM decisions WHERE id=?", (r["resolved_by"],)).fetchone()
    assert dec in st["statement"] and "Ben may override" in st["because"]
    assert pc.apply(c, rep) == []


def _xlsx(path, sheets):
    import openpyxl
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for r in rows:
            ws.append(r)
    wb.save(path)


def test_workbook_rows_counts_past_rulings_and_apply_leaves_workbooks_alone(db, monkeypatch, tmp_path):
    path, c = db
    work = tmp_path / "docs" / "MAG" / "work"
    work.mkdir(parents=True)
    target = work / "MAG-Keeper-List-DRAFT-20261112-r2.xlsx"
    _xlsx(target, {"Read me": [["DRAFT"]],
                   "Keeper - Ben to rule (3)": [["FDD #", "Title", "Consultant"],
                                                ["826", "FDD-O2S-826 SO flags", "Lou"],
                                                ["#900", "FDD-P2F-900 thing", "Dave"],
                                                ["901", "FDD-P2F-901 other", "Dave"]],
                   "Flagged (1)": [["FDD #", "Title", "Consultant"], ["5", "x", "y"]]})
    _xlsx(work / "MAG-Old-Rulings-20261020.xlsx", {"Rulings": [["Tracker ID", "Title", "Ben's ruling"],
                                                                ["900", "t", "Keep"], ["901", "t", "Open"]]})
    _xlsx(work / "MAG-Client-20261020.xlsx", {"P": [["Tracker ID", "Title", "Client decision source"],
                                                     ["901", "t", "client workbook"]]})
    _xlsx(work / "MAG-Old-Rulings-20261020.bak-1.xlsx", {"Rulings": [["Tracker ID", "T", "Ruling"], ["901", "t", "Keep"]]})
    store.record_decision(c, "Ben: keep FDD-O2S-826 as a keeper", because="Ben")
    store.record_decision(c, "Proceed on master default for FDD 901: cancel", because="master default; Ben may override")
    le = _ben(c, "MAG", f"Rule the 3 keepers in work/{target.name} and work/missing.xlsx")

    rep = pc.report(c, rows_root=tmp_path / "docs")
    wb = rep["workbooks"]
    f = {x["file"]: x for x in wb["files"]}
    hit = f[f"work/{target.name}"]
    assert hit["follow_up"] == le and hit["sheets"] == ["Keeper - Ben to rule (3)"]
    assert (hit["open_rows"], hit["by_decision"], hit["by_workbook"], hit["unruled"]) == (3, 1, 1, 1)
    assert {x["key"] for x in hit["examples"]} == {"FDD 826", "FDD 900"}
    assert f["work/missing.xlsx"]["status"] == "not found"
    assert wb["totals"]["loose_ends"] == 1 and wb["totals"]["open_rows"] == 3

    out = tmp_path / "p.html"
    pc.write_report(rep, out)
    assert "Workbook rows" in out.read_text(encoding="utf-8")

    before = {p: p.stat().st_mtime_ns for p in work.iterdir()}
    monkeypatch.setenv("COORD_WORKFLOW_PRECEDENT", "1")
    pc.apply(c, rep)
    assert {p: p.stat().st_mtime_ns for p in work.iterdir()} == before
    assert c.execute("SELECT state FROM follow_ups WHERE id=?", (le,)).fetchone()["state"] == "open"


def test_workbook_rows_status_ruling_and_single_sheet(tmp_path):
    from coord_mcp.workflows import workbook_rows as wr
    a = tmp_path / "a.xlsx"
    _xlsx(a, {"Summary": [["#", "Integration", "Now"]],
              "Fields": [["Integration", "Field / group", "Status"], ["E80", "ADP-E80-04 Transfer", "Ruling"],
                         ["E80", "ADP-E80-05 Other", "Open"]]})
    assert wr.open_rows(a) == (["ADP-E80-04"], ["Fields (Status=Ruling)"])
    b = tmp_path / "b.xlsx"
    _xlsx(b, {"Eliminated": [["112 rows"], ["Program", "Source", "Action"], ["xxgldab.p", "SE", "Keep"]]})
    assert wr.open_rows(b) == (["xxgldab.p"], ["Eliminated"])


def test_closes_that_are_not_bens_answer_are_no_precedent(db, monkeypatch):
    # Review 2026-11-14: a "Covered:" close matched its own re-raise at 1.0 and
    # apply would have closed the live question. Only Ben's answers count.
    path, c = db
    what = "Decide whether the steering deck keeps the wave plan timeline"
    closes = [
        ("Covered: Ben loose end X closed, task Y re-raised it as Z", "ben_cover: newer item on the same lineage"),
        ("Loose end done on evidence: John's mail of 11/12", None),
        ("Closed per email (msg AAMk, from John, 11/12)", "email-closes"),
        ("Master default: Ben loose end X settled by Ben's ruling Y", "same shape; master default; Ben may override"),
        ("Precedent: Ben loose end X settled as Y was, by decision Z", "precedent; Ben may override"),
        ("Auto-deferred 1 Ben loose end(s) open over 14 days", None),
        ("Return dates: reopened 1 loose end(s)", None),
        ("Pipeline note 123: deck landed", None),
        ("Promoted deck.html to MAG/deliverables as deck-v1", None),
    ]
    for stmt, because in closes:
        fu = _ben(c, "MAG", what)
        store.resolve_follow_up(c, fu, decision_id=store.record_decision(c, stmt, because)["decision_id"])
    live = _ben(c, "MAG", what)
    assert pc.precedents(c) == []
    rep = pc.report(c)
    assert live not in {r["id"] for r in rep["rows"]} and rep["tally"] == {"none": 1}
    monkeypatch.setenv("COORD_WORKFLOW_PRECEDENT", "1")
    assert pc.apply(c, rep) == []
    # Ben's own answer still counts.
    fu = _ben(c, "MAG", what)
    store.resolve_follow_up(c, fu, decision_id=store.record_decision(c, "Per Ben's answer: drop it")["decision_id"])
    assert [p["id"] for p in pc.precedents(c)] == [fu]


def test_workbook_row_keys_need_their_prefix_and_bens_decisions_exclude_board_records(db):
    from coord_mcp.workflows import workbook_rows as wr
    path, c = db
    rx = wr._key_rx("FDD 12")
    assert rx.search("keep FDD-O2S-12") and rx.search("FDD #12") and rx.search("tracker id #12")
    assert not rx.search("RAID #12 is closed") and not rx.search("see PR #12")
    store.record_decision(c, "Pipeline note 77: FDD 12 landed", None)
    store.record_decision(c, "Promoted FDD-12.html to MAG/deliverables as FDD-12-v1", None)
    store.record_decision(c, "Workflow engine: case 1 step reply for FDD 12", None)
    store.record_decision(c, "Covered: Ben loose end X closed (FDD 12)", None)
    keep = store.record_decision(c, "Ben: keep FDD 12", "Ben")["decision_id"]
    assert [d["id"] for d in wr.ben_decisions(c)] == [keep]


def test_workbook_rows_odd_paths_are_skipped_not_fatal(db, monkeypatch, tmp_path):
    from pathlib import Path
    from coord_mcp.workflows import workbook_rows as wr
    path, c = db
    work = tmp_path / "docs" / "MAG" / "work"
    work.mkdir(parents=True)
    (work / "bad.xlsx").write_bytes(b"not a workbook")
    real_is_file = Path.is_file

    def is_file(self):
        if "placeholder" in self.name:
            raise OSError(22, "cloud file provider is not running")
        return real_is_file(self)
    monkeypatch.setattr(Path, "is_file", is_file)
    _ben(c, "MAG", "Rule the rows in work/placeholder.xlsx and work/bad.xlsx")
    rep = pc.report(c, rows_root=tmp_path / "docs")
    f = {x["file"]: x for x in rep["workbooks"]["files"]}
    assert f["work/placeholder.xlsx"]["status"] == "not found"
    assert f["work/bad.xlsx"]["status"].startswith("unreadable")

    real_is_dir = Path.is_dir
    monkeypatch.setattr(Path, "is_dir", lambda self: (_ for _ in ()).throw(OSError(5, "denied"))
                        if self == work else real_is_dir(self))
    assert wr.workbook_rulings(work, set()) == {}
