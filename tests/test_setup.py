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
    p.write_text(json.dumps({"theme": "auto", "hooks": {"x": 1}}))
    out = usage.setup(p)
    assert out["changed"] is True
    s = json.loads(p.read_text())
    assert s["theme"] == "auto" and s["hooks"] == {"x": 1} and "statusLine" in s
    assert list(tmp_path.glob("settings.json.bak-*"))


def test_setup_is_idempotent(tmp_path):
    p = tmp_path / "settings.json"
    usage.setup(p)
    before = p.read_text()
    out = usage.setup(p)
    assert out["changed"] is False and p.read_text() == before
    assert len(list(tmp_path.glob("settings.json.bak-*"))) == 1


def test_setup_refuses_to_replace_foreign_statusline_unless_forced(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"statusLine": {"type": "command", "command": "my-other-thing"}}))
    out = usage.setup(p)
    assert out["changed"] is False and "kept_existing" in out
    assert json.loads(p.read_text())["statusLine"]["command"] == "my-other-thing"
    out = usage.setup(p, force=True)
    assert out["changed"] is True
    assert "coord_mcp" in json.loads(p.read_text())["statusLine"]["command"]


def test_setup_upgrades_our_own_older_command(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"statusLine": {"type": "command", "command": "python -m coord_mcp.usage statusline"}}))
    out = usage.setup(p)
    assert out["changed"] is True  # ours, but not the absolute-path form: replaced without --force
