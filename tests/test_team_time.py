"""Team Time: the GTK import endpoint, its CORS preflight, and the read-only tab."""

from __future__ import annotations

import functools
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from coord_mcp import report, team_time
from coord_mcp.db import connect

GTK = "https://globaltoolkit.com"
# Real colleagues' hours: read from the Internal folder, never copied into this repo.
SEED = (Path.home() / "OneDrive - Logan Consulting" / "Documents" / "Internal" / "work" / "team-time"
        / "team-time-2026-11-15.csv")


def _body(period="11/15/2026"):
    return {"period": period, "pulled_at": "2026-11-23T15:00:00Z", "days": ["11/3", "11/4"],
            "people": [
                {"name": "Jay Dybul", "user_id": "u1", "status": "Complete", "total": 9.5, "rows": [
                    {"charge_code": "MAG - MAG - Design", "phase": "Dev", "description": "Build",
                     "hours": {"11/3": 8}, "ppa": 0, "total": 8},
                    {"charge_code": "LQA - LC-QAD Administration", "phase": "", "description": "Staff",
                     "hours": {"11/4": 0.5}, "ppa": 1, "total": 1.5}]},
                {"name": "Allen Hu", "user_id": "u2", "status": None, "total": 0, "rows": []}]}


@pytest.fixture()
def server(tmp_path, monkeypatch):
    from coord_mcp import usage
    monkeypatch.setattr(usage, "ingest", lambda conn: None)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    make = functools.partial(connect, tmp_path / "srv.db")
    make().close()
    threading.Thread(target=report.serve, args=(make, port), daemon=True).start()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    return f"http://127.0.0.1:{port}", tmp_path / "team-time.json"


def _req(url, method="GET", data=None, headers=None):
    r = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _post(base, body, origin=GTK):
    h = {"Content-Type": "application/json"}
    if origin:
        h["Origin"] = origin
    return _req(f"{base}/team-time/import", "POST", json.dumps(body).encode(), h)


def test_preflight_allows_only_gtk(server):
    base, _ = server
    code, h, _ = _req(f"{base}/team-time/import", "OPTIONS", headers={
        "Origin": GTK, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type", "Access-Control-Request-Private-Network": "true"})
    assert code == 204
    assert h["Access-Control-Allow-Origin"] == GTK
    assert h["Access-Control-Allow-Private-Network"] == "true"
    assert "POST" in h["Access-Control-Allow-Methods"] and "Content-Type" in h["Access-Control-Allow-Headers"]
    code, h, _ = _req(f"{base}/team-time/import", "OPTIONS", headers={"Origin": "https://evil.example"})
    assert code == 403 and "Access-Control-Allow-Origin" not in h
    code, _, _ = _req(f"{base}/api/prompt", "OPTIONS", headers={"Origin": GTK})
    assert code == 403


def test_import_stores_and_replaces_a_period(server):
    base, store_file = server
    code, h, raw = _post(base, _body())
    assert code == 200 and h["Access-Control-Allow-Origin"] == GTK
    assert json.loads(raw) == {"ok": True, "period": "11/15/2026", "people": 2, "rows": 2}
    data = team_time.load(store_file)
    assert data["periods"]["11/15/2026"]["people"][0]["rows"][1]["ppa"] == 1
    # A second pull of the same period replaces it; another period sits beside it.
    b = _body()
    b["people"] = b["people"][:1]
    _post(base, b)
    _post(base, _body("10/31/2026"))
    data = team_time.load(store_file)
    assert len(data["periods"]["11/15/2026"]["people"]) == 1
    assert team_time.periods(data) == ["11/15/2026", "10/31/2026"]


def test_import_refuses_other_origins_and_bad_bodies(server):
    base, store_file = server
    assert _post(base, _body(), origin="https://evil.example")[0] == 403
    assert not store_file.exists()
    assert _post(base, _body(), origin=None)[0] == 200  # a local loader, no browser
    code, _, raw = _post(base, {"period": "Nov 15", "people": []})
    assert code == 400 and "MM/DD/YYYY" in json.loads(raw)["error"]
    bad = _body()
    bad["people"][0]["rows"][0]["hours"] = {"Monday": 3}
    assert _post(base, bad)[0] == 400
    # The other dashboard POSTs still refuse the GTK origin.
    code, _, _ = _req(f"{base}/api/prompt", "POST", b"action=board", {"Origin": GTK})
    assert code == 403


def test_tab_and_csv_render_with_no_edit_controls(server):
    base, _ = server
    code, _, raw = _req(f"{base}/team-time")
    assert code == 200 and b"No periods imported yet" in raw
    _post(base, _body("10/31/2026"))
    _post(base, _body())
    code, _, raw = _req(f"{base}/team-time")
    html = raw.decode()
    assert code == 200 and "period ending 11/15/2026" in html
    assert 'href="/team-time" aria-current=page' in html
    assert "Last pulled 2026-11-23T15:00:00Z" in html
    assert "MAG - MAG - Design" in html and "Build" in html and "No timesheet for this period" in html
    assert re.search(r'data-person="Jay Dybul"><b>9.5</b>', html)
    assert re.search(r'data-person="Allen Hu"><b>0</b>', html)
    assert 'method="post"' not in html.lower()  # read-only: the period picker is a GET
    html = _req(f"{base}/team-time?period=10/31/2026")[2].decode()
    assert "period ending 10/31/2026" in html
    code, h, raw = _req(f"{base}/team-time.csv?period=11/15/2026")
    assert code == 200 and h["Content-Type"].startswith("text/csv")
    assert "team-time-2026-11-15.csv" in h["Content-Disposition"]
    lines = raw.decode().splitlines()
    assert lines[0] == "period,person,status,period_total,charge_code,phase,description,11/3,11/4,ppa,row_total"
    assert "Jay Dybul" in lines[1] and lines[-1].startswith("11/15/2026,Allen Hu,,0,")


@pytest.mark.skipif(not SEED.exists(), reason="the 11/15 seed lives only on Ben's machine")
def test_seed_csv_totals_match_period_total():
    text = SEED.read_text(encoding="utf-8-sig")
    entry = team_time.clean(team_time.from_csv(text, "11/15/2026"))
    assert entry["days"][0] == "11/1" and entry["days"][-1] == "11/15"
    assert len(entry["people"]) == 9
    html = report.team_time_page({"periods": {"11/15/2026": entry}}, None)
    for p in entry["people"]:
        assert abs(sum(r["total"] for r in p["rows"]) - p["total"]) < 0.001, p["name"]
        assert re.search(rf'data-person="{re.escape(p["name"])}"><b>{p["total"]:g}</b>', html), p["name"]
    assert "≠" not in html
    assert {p["name"] for p in entry["people"] if not p["rows"]} == {"Allen Hu", "Erick Bondezan", "Sankar Nachimuthu"}


def test_period_days_are_semi_monthly():
    assert team_time.period_days("11/30/2026")[0] == "11/16"
    assert team_time.period_days("02/28/2027")[-1] == "2/28"
    assert len(team_time.period_days("11/15/2026")) == 15
