"""Setup command: merges the statusLine hook into settings.json without clobbering."""

from __future__ import annotations

import json

from coord_mcp import usage


def test_setup_creates_settings_when_missing(tmp_path):
    p = tmp_path / "settings.json"
    out = usage.setup(p)
    assert out["changed"] is True
    s = json.loads(p.read_text())
    assert s["statusLine"]["type"] == "command"
    assert s["statusLine"]["command"].endswith("-m coord_mcp.usage statusline")
    assert "coord_mcp" in s["statusLine"]["command"]


def test_setup_preserves_other_keys_and_backs_up(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"theme": "auto", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}}))
    out = usage.setup(p)
    assert out["changed"] is True
    s = json.loads(p.read_text())
    assert s["theme"] == "auto" and "statusLine" in s
    assert s["hooks"]["Stop"][0]["hooks"][0]["command"] == "x"  # untouched
    assert "PreToolUse" in s["hooks"]
    assert list(tmp_path.glob("settings.json.bak-*"))


def test_setup_is_idempotent(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"theme": "auto"}))
    usage.setup(p)
    before = p.read_text()
    out = usage.setup(p)
    assert out["changed"] is False and p.read_text() == before
    assert len(list(tmp_path.glob("settings.json.bak-*"))) == 1  # only the first run backs up


def test_setup_refuses_to_replace_foreign_statusline_unless_forced(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"statusLine": {"type": "command", "command": "my-other-thing"}}))
    out = usage.setup(p, guard=False)
    assert out["changed"] is False and "kept_existing" in out
    assert json.loads(p.read_text())["statusLine"]["command"] == "my-other-thing"
    out = usage.setup(p, force=True, guard=False)
    assert out["changed"] is True
    assert "coord_mcp" in json.loads(p.read_text())["statusLine"]["command"]


def test_hook_wired_detection(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    assert usage.hook_wired(p) is False          # missing file
    p.write_text("{}")
    assert usage.hook_wired(p) is False
    p.write_text(json.dumps({"statusLine": {"type": "command", "command": "something-else"}}))
    assert usage.hook_wired(p) is False
    usage.setup(p)            # refuses to replace a foreign statusLine
    assert usage.hook_wired(p) is False
    usage.setup(p, force=True)
    assert usage.hook_wired(p) is True


def test_report_distinguishes_pending_from_unwired(tmp_path, monkeypatch):
    from coord_mcp import report
    from coord_mcp.db import connect
    conn = connect(tmp_path / "t.db")
    monkeypatch.setattr(usage, "hook_wired", lambda *a: False)
    assert "usage setup" in report.write(conn, tmp_path / "a.html").read_text(encoding="utf-8")
    monkeypatch.setattr(usage, "hook_wired", lambda *a: True)
    html = report.write(conn, tmp_path / "b.html").read_text(encoding="utf-8")
    assert "hook is wired" in html and "not reported yet" in html
    conn.close()


def test_setup_upgrades_our_own_older_command(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"statusLine": {"type": "command", "command": "python -m coord_mcp.usage statusline"}}))
    out = usage.setup(p)
    assert out["changed"] is True  # ours, but not the absolute-path form: replaced without --force
