"""Usage ingestion, pricing and statusline tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coord_mcp import report, usage
from coord_mcp.db import connect


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


@pytest.fixture()
def projects(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setattr(usage, "PROJECTS_DIR", root)
    return root


def _rec(rid: str, sid: str, ts: str, model: str = "claude-sonnet-5", block: int = 0, **usage_over):
    u = {"input_tokens": 10, "output_tokens": 100, "cache_read_input_tokens": 1000,
         "cache_creation_input_tokens": 500,
         "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 400},
         "output_tokens_details": {"thinking_tokens": 30}}
    u.update(usage_over)
    return {"type": "assistant", "requestId": rid, "sessionId": sid, "timestamp": ts,
            "cwd": "C:\\dev\\proj", "apiBlockIndex": block, "message": {"model": model, "usage": u}}


def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def test_pricing_matches_documented_rates():
    # sonnet 5: 10 in, 100 out, 1000 cache read, 100 cw5m, 400 cw1h
    c = usage.cost_usd("claude-sonnet-5", 10, 100, 1000, 100, 400)
    expected = (10 * 2 + 100 * 10 + 1000 * 0.2 + 100 * 2.5 + 400 * 4) / 1e6
    assert c == pytest.approx(expected)
    assert usage.cost_usd("<synthetic>", 1, 1, 1, 1, 1) == 0
    assert usage.price("claude-haiku-4-5-20251001") == usage.PRICING["claude-haiku-4-5"]
    assert usage.price("claude-unknown-9") == usage.FALLBACK_PRICE


def test_ingest_dedupes_streamed_blocks(conn, projects):
    f = projects / "C--dev-proj" / "s1.jsonl"
    _write(f, [_rec("r1", "s1", "2026-09-14T10:00:00Z", block=0), _rec("r1", "s1", "2026-09-14T10:00:00Z", block=1),
               _rec("r2", "s1", "2026-09-14T10:01:00Z")])
    out = usage.ingest(conn)
    assert (out["files_read"], out["requests_added"]) == (1, 2)
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 2
    row = conn.execute("SELECT * FROM requests WHERE request_id='r1'").fetchone()
    assert row["cache_write_1h"] == 400 and row["cache_write_5m"] == 100 and row["thinking"] == 30
    assert row["project"] == "proj"


def test_ingest_is_incremental_and_skips_partial_line(conn, projects):
    f = projects / "C--dev-proj" / "s1.jsonl"
    _write(f, [_rec("r1", "s1", "2026-09-14T10:00:00Z")])
    usage.ingest(conn)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_rec("r2", "s1", "2026-09-14T10:02:00Z")) + "\n")
        fh.write('{"type":"assistant","requestId":"r3"')  # partial, no newline
    out = usage.ingest(conn)
    assert out["requests_added"] == 1
    with f.open("a", encoding="utf-8") as fh:
        fh.write(',"sessionId":"s1","timestamp":"2026-09-14T10:03:00Z","message":{"model":"claude-opus-5","usage":{"input_tokens":1,"output_tokens":1}}}\n')
    out = usage.ingest(conn)
    assert out["requests_added"] == 1
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 3
    again = usage.ingest(conn)
    assert (again["files_read"], again["requests_added"]) == (0, 0)


def test_subagent_files_are_attributed(conn, projects):
    _write(projects / "P" / "s1.jsonl", [_rec("r1", "s1", "2026-09-14T10:00:00Z")])
    _write(projects / "P" / "s1" / "subagents" / "agent-abc.jsonl", [_rec("r9", "s1", "2026-09-14T10:05:00Z")])
    usage.ingest(conn)
    assert conn.execute("SELECT agent_id FROM requests WHERE request_id='r9'").fetchone()[0] == "abc"
    s = usage.summary(conn, days=3650)
    assert s["totals"]["subagent_cost"] == pytest.approx(s["totals"]["cost"] / 2)


def test_titles_prefer_custom_then_ai_then_prompt(conn, projects):
    _write(projects / "P" / "s1.jsonl", [
        {"type": "user", "sessionId": "s1", "message": {"content": "fix the login bug\nmore detail"}},
        _rec("r1", "s1", "2026-09-14T10:00:00Z"),
    ])
    _write(projects / "P" / "s2.jsonl", [
        {"type": "user", "sessionId": "s2", "message": {"content": "prompt"}},
        {"type": "ai-title", "sessionId": "s2", "aiTitle": "AI title"},
        {"type": "custom-title", "sessionId": "s2", "customTitle": "Custom"},
        {"type": "cost-state", "sessionId": "s2", "totalCostUSD": 1.5},
        _rec("r2", "s2", "2026-09-14T10:00:00Z"),
    ])
    usage.ingest(conn)
    t = {r["session_id"]: r for r in conn.execute("SELECT * FROM cc_sessions")}
    assert t["s1"]["title"] == "fix the login bug"
    assert t["s2"]["title"] == "Custom" and t["s2"]["cc_cost_usd"] == 1.5


def test_statusline_records_and_prints(conn):
    payload = {"session_id": "abc", "model": {"id": "claude-opus-5", "display_name": "Opus"},
               "context_window": {"used_percentage": 42.5},
               "rate_limits": {"five_hour": {"used_percentage": 61, "resets_at": 2000000000},
                               "seven_day": {"used_percentage": 88, "resets_at": 2000000000}}}
    snap = usage.record_statusline(conn, payload)
    assert snap["five_hour_pct"] == 61 and snap["seven_day_pct"] == 88 and snap["context_pct"] == 42.5
    assert conn.execute("SELECT COUNT(*) FROM quota_snapshots").fetchone()[0] == 1
    usage.record_statusline(conn, payload)  # unchanged within 5 min: no new row
    assert conn.execute("SELECT COUNT(*) FROM quota_snapshots").fetchone()[0] == 1
    line = usage.statusline_text(snap, payload)
    assert line.startswith("Opus | ctx 42% | 5h 61%") and "7d 88%" in line


def test_statusline_tolerates_missing_limits(conn):
    snap = usage.record_statusline(conn, {"model": {"display_name": "Opus"}})
    assert conn.execute("SELECT COUNT(*) FROM quota_snapshots").fetchone()[0] == 0
    assert usage.statusline_text(snap, {}) == "Opus | limits: n/a"


def test_statusline_cli_never_raises(monkeypatch, capsys, tmp_path):
    import io
    import sys
    monkeypatch.setenv("COORD_DB", str(tmp_path / "x.db"))
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert usage.main(["statusline"]) == 0
    assert capsys.readouterr().out == "\n"


def test_report_renders_empty_and_full(conn, projects, tmp_path):
    out = report.write(conn, tmp_path / "r.html", days=7)
    html = out.read_text(encoding="utf-8")
    assert "No requests in this period" in html and "statusLine hook" in html
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write(projects / "P" / "s1.jsonl", [_rec("r1", "s1", ts), _rec("r2", "s1", ts, model="claude-opus-5")])
    usage.ingest(conn)
    usage.record_statusline(conn, {"rate_limits": {"five_hour": {"used_percentage": 10}, "seven_day": {"used_percentage": 20}}})
    html = report.write(conn, tmp_path / "r.html", days=7).read_text(encoding="utf-8")
    assert "Sonnet" in html and "Opus" in html and "<svg" in html
    assert "5-hour limit used" in html and "10%" in html


def test_context_tile_gets_loud_past_the_thresholds():
    """'ctx 42%' and 'ctx 92%' scan identically, which is how a session sails
    past the point where it should have parked."""
    assert usage._ctx_text(42) == "ctx 42%"
    assert usage._ctx_text(75).startswith("! ctx") and "park soon" in usage._ctx_text(75)
    loud = usage._ctx_text(91)
    assert loud.startswith("!! CTX") and "PARK + CLEAR NOW" in loud
