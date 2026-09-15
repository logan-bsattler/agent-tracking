"""Guard policy, burn projection, alerts and hook wiring."""

from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from coord_mcp import alerts, guard, usage
from coord_mcp.db import connect, now


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("COORD_DB", str(tmp_path / "t.db"))
    monkeypatch.setattr(guard, "PAUSE_FILE", tmp_path / "guard-pause")
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _transcript(tmp_path, ctx: int, n: int = 1, model="claude-opus-5", age_s: int = 0):
    p = tmp_path / "t.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for i in range(n):
            ts = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            fh.write(json.dumps({"type": "assistant", "requestId": f"r{i}", "timestamp": ts,
                                 "message": {"model": model, "usage": {"input_tokens": 5, "cache_read_input_tokens": ctx - 5,
                                                                        "cache_creation_input_tokens": 0, "output_tokens": 1}}}) + "\n")
    return str(p)


def _snap(conn, fh, sd=10, reset_in=3600, ts_offset=0):
    conn.execute("INSERT INTO quota_snapshots(ts, five_hour_pct, five_hour_reset, seven_day_pct) VALUES (?,?,?,?)",
                 (now() + ts_offset, fh, now() + reset_in, sd))


# ------------------------------------------------------------------ context


def test_session_context_reads_tail(tmp_path):
    ctx, model, recent = guard.session_context(_transcript(tmp_path, 120_000, n=3))
    assert ctx == 120_000 and model == "claude-opus-5" and recent == 3


def test_context_warn_and_block(conn, tmp_path):
    a = guard.assess(conn, _transcript(tmp_path, 100_000))
    assert a["level"] == "ok"
    a = guard.assess(conn, _transcript(tmp_path, 160_000))
    assert a["level"] == "warn" and "160k" in a["reasons"][0]
    a = guard.assess(conn, _transcript(tmp_path, 320_000))
    assert a["level"] == "block" and "/compact" in a["reasons"][0]
    assert "$0.16" in a["reasons"][0]  # 320k cache read on opus at $0.5/M


def test_missing_transcript_is_ok(conn):
    assert guard.assess(conn, None)["level"] == "ok"
    assert guard.assess(conn, "C:/nope/none.jsonl")["level"] == "ok"


# -------------------------------------------------------------------- limits


def test_limit_bands(conn, tmp_path):
    t = _transcript(tmp_path, 1000)
    _snap(conn, 80)
    a = guard.assess(conn, t)
    assert a["level"] == "warn" and "5-hour limit at 80%" in a["reasons"][0]
    _snap(conn, 95, ts_offset=1)
    a = guard.assess(conn, t)
    assert a["level"] == "block" and "95%" in a["reasons"][0]


def test_weekly_hard_stop(conn, tmp_path):
    _snap(conn, 10, sd=98)
    assert guard.assess(conn, _transcript(tmp_path, 1000))["level"] == "block"


# --------------------------------------------------------------------- burn


def test_burn_projects_hit_before_reset(conn):
    reset = now() + 3600
    for i, pct in enumerate((50, 60, 70)):
        conn.execute("INSERT INTO quota_snapshots(ts, five_hour_pct, five_hour_reset) VALUES (?,?,?)",
                     (now() - (2 - i) * 600, pct, reset))
    b = usage.burn(conn)
    assert b["pct_per_hour"] == pytest.approx(60, rel=0.05)
    assert b["hits_limit_before_reset"] is True
    assert abs(b["hit_at"] - (now() + 1800)) < 120


def test_burn_ignores_snapshots_across_reset(conn):
    conn.execute("INSERT INTO quota_snapshots(ts, five_hour_pct, five_hour_reset) VALUES (?,?,?)", (now() - 600, 90, now() - 100))
    conn.execute("INSERT INTO quota_snapshots(ts, five_hour_pct, five_hour_reset) VALUES (?,?,?)", (now(), 5, now() + 17000))
    b = usage.burn(conn)
    assert b["pct_per_hour"] is None and b["hits_limit_before_reset"] is False


def test_burn_with_no_data(conn):
    b = usage.burn(conn)
    assert b["pct_per_hour"] is None and b["spend_last_hour"] == 0


# ------------------------------------------------------------------- alerts


def test_alerts_fire_once_per_band_per_window(conn, monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "notify", lambda t, b: sent.append(b) or True)
    reset = now() + 3600
    snap = {"five_hour_pct": 80, "five_hour_reset": reset, "seven_day_pct": 10, "seven_day_reset": None}
    assert len(alerts.check_and_alert(conn, snap)) == 1
    assert alerts.check_and_alert(conn, snap) == []          # same band, silent
    snap["five_hour_pct"] = 85
    assert alerts.check_and_alert(conn, snap) == []          # still warn, silent
    snap["five_hour_pct"] = 93
    assert len(alerts.check_and_alert(conn, snap)) == 1      # hard: fires
    snap["five_hour_reset"] = reset + 18000
    snap["five_hour_pct"] = 80
    assert len(alerts.check_and_alert(conn, snap)) == 1      # new window: fires again
    assert all("5-hour" in s for s in sent)


def test_projection_alert_fires_once(conn, monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "notify", lambda t, b: sent.append(b) or True)
    snap = {"five_hour_pct": 30, "five_hour_reset": now() + 3600, "seven_day_pct": 10, "seven_day_reset": None}
    b = {"hits_limit_before_reset": True, "hit_at_text": "12:00", "reset_text": "13:00"}
    assert len(alerts.check_and_alert(conn, snap, b)) == 1
    assert alerts.check_and_alert(conn, snap, b) == []


# --------------------------------------------------------------------- hooks


def _run_hook(monkeypatch, capsys, event: str, payload: dict):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    code = guard.main([event])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_pretooluse_blocks_with_reason(conn, tmp_path, monkeypatch, capsys):
    code, out, err = _run_hook(monkeypatch, capsys, "pretooluse", {"transcript_path": _transcript(tmp_path, 350_000)})
    assert code == 2 and "Blocked by the usage guard" in err and "pause 30" in err


def test_userpromptsubmit_injects_context_on_warn(conn, tmp_path, monkeypatch, capsys):
    code, out, err = _run_hook(monkeypatch, capsys, "userpromptsubmit", {"transcript_path": _transcript(tmp_path, 200_000)})
    assert code == 0
    j = json.loads(out)
    assert j["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "200k" in j["hookSpecificOutput"]["additionalContext"]


def test_hook_passes_silently_when_ok(conn, tmp_path, monkeypatch, capsys):
    code, out, err = _run_hook(monkeypatch, capsys, "pretooluse", {"transcript_path": _transcript(tmp_path, 10_000)})
    assert code == 0 and out == "" and err == ""


def test_pause_disables_block(conn, tmp_path, monkeypatch, capsys):
    guard.main(["pause", "5"])
    capsys.readouterr()
    code, out, err = _run_hook(monkeypatch, capsys, "pretooluse", {"transcript_path": _transcript(tmp_path, 350_000)})
    assert code == 0
    guard.main(["resume"])
    capsys.readouterr()
    code, _, _ = _run_hook(monkeypatch, capsys, "pretooluse", {"transcript_path": _transcript(tmp_path, 350_000)})
    assert code == 2


def test_env_off_disables(conn, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COORD_GUARD", "off")
    code, _, _ = _run_hook(monkeypatch, capsys, "pretooluse", {"transcript_path": _transcript(tmp_path, 350_000)})
    assert code == 0


def test_hook_never_raises_on_garbage(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert guard.main(["pretooluse"]) == 0


# --------------------------------------------------------------------- setup


def test_setup_wires_guard_hooks_and_keeps_others(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "mine"}]}]}}))
    out = usage.setup(p)
    assert out["changed"] and out["guard"] == "wired"
    s = json.loads(p.read_text())
    pre = s["hooks"]["PreToolUse"]
    assert pre[0]["hooks"][0]["command"] == "mine"
    assert "coord_mcp.guard pretooluse" in pre[1]["hooks"][0]["command"]
    assert "coord_mcp.guard userpromptsubmit" in s["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    before = p.read_text()
    assert usage.setup(p)["changed"] is False and p.read_text() == before


def test_setup_no_guard(tmp_path):
    p = tmp_path / "settings.json"
    usage.setup(p, guard=False)
    assert "hooks" not in json.loads(p.read_text())
