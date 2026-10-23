"""Meeting notes per client: attribution, listing, reading, and client scoping."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coord_mcp import meetings, notes
from coord_mcp.db import connect

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COORD_DB", str(tmp_path / "coord.db"))
    (tmp_path / "meetings").mkdir()
    c = connect(tmp_path / "coord.db")
    meetings.seed(c)
    yield c, tmp_path / "meetings"
    c.close()


def write(d: Path, stem: str, title: str, started: str, body: list[str], notes_md: str | None = None,
          ended: bool = True) -> None:
    lines = [f"# meeting: {title}", f"# started: {started}", "# source: teams", *body]
    if ended:
        lines.append("# end 2026-10-23T11:00:00")
    (d / f"{stem}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if notes_md is not None:
        (d / f"{stem}.txt.notes.md").write_text(notes_md, encoding="utf-8")


def utc(local: str) -> dict:
    t = datetime.fromisoformat(local).timestamp()
    return {"dateTime": datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}


def test_header_then_calendar_then_rule(env):
    c, d = env
    write(d, "2026-10-23-0900-Coupa-IDs", "Coupa IDs", "2026-10-23T09:00:00", ["09:00:01 A: hi"],
          "# Coupa IDs\n\nclient: mag, Royal\n\n## Summary\n- decided X (lines 4-4)\n\n## Notes\n- y\n")
    write(d, "2026-10-23-1003-Stand-up", "Stand up", "2026-10-23T10:03:00", ["10:03:01 A: hi"], "# Stand up\n")
    write(d, "2026-10-23-1200-PBE-sync", "PBE sync", "2026-10-23T12:00:00", ["12:00:01 A: hi"])
    write(d, "2026-10-23-1300-Mystery", "Mystery", "2026-10-23T13:00:00", ["13:00:01 A: hi"])
    meetings.import_events(c, "2026-10-23", "2026-10-23", 1, [{
        "id": "e1", "subject": "Stand up", "organizer": "x@markanthony.com",
        "attendees": ["x@markanthony.com"], "start": utc("2026-10-23T10:00:00"),
        "end": utc("2026-10-23T10:30:00"), "showAs": "busy"}])

    got = {m["meeting"]: (m["clients"], m["attributed_by"]) for m in notes.index(c)["meetings"]}
    assert got["2026-10-23-0900-Coupa-IDs"] == (["MAG", "Royal"], "header")
    assert got["2026-10-23-1003-Stand-up"] == (["MAG"], "calendar")
    assert got["2026-10-23-1200-PBE-sync"] == (["PBE"], "rule")
    assert got["2026-10-23-1300-Mystery"] == ([], None)
    assert list(got) == sorted(got, reverse=True)  # newest first


def test_client_filter_query_since_and_summary(env):
    c, d = env
    write(d, "2026-10-22-0900-MAG-a", "MAG a", "2026-10-22T09:00:00", [], "## Summary\n- old\n")
    write(d, "2026-10-23-0900-MAG-b", "MAG b", "2026-10-23T09:00:00", [],
          "## Summary\n- wave one Jan 5\n\n## Notes\n- Coupa store ids\n")
    write(d, "2026-10-23-1000-Royal-c", "Royal c", "2026-10-23T10:00:00", [], "## Summary\n- r\n")
    mag = notes.index(c, client="mag")["meetings"]
    assert [m["meeting"] for m in mag] == ["2026-10-23-0900-MAG-b", "2026-10-22-0900-MAG-a"]
    assert mag[0]["summary"] == "- wave one Jan 5"
    assert [m["meeting"] for m in notes.index(c, "MAG", since="2026-10-23")["meetings"]] == ["2026-10-23-0900-MAG-b"]
    assert [m["meeting"] for m in notes.index(c, "MAG", query="coupa")["meetings"]] == ["2026-10-23-0900-MAG-b"]
    one = notes.index(c, limit=1)
    assert len(one["meetings"]) == 1 and one["more"] is True


def test_read_notes_lines_and_scope(env):
    c, d = env
    write(d, "2026-10-23-0900-MAG-b", "MAG b", "2026-10-23T09:00:00",
          ["09:00:01 A: one", "09:00:02 B: two", "09:00:03 C: three"], "client: MAG\n## Notes\n- two (lines 5-5)\n")
    r = notes.read(c, "2026-10-23-0900-MAG-b", "MAG")
    assert "two (lines 5-5)" in r["notes"] and r["ended"] is True
    r = notes.read(c, "2026-10-23-0900-MAG-b", "MAG", lines="4-5")
    assert r["transcript"] == "09:00:01 A: one\n09:00:02 B: two" and r["lines"] == "4-5"
    with pytest.raises(KeyError, match="not attributed to Royal"):
        notes.read(c, "2026-10-23-0900-MAG-b", "Royal")
    with pytest.raises(ValueError, match="not a meeting id"):
        notes.read(c, "../coord", "MAG")
    with pytest.raises(ValueError, match="A-B"):
        notes.read(c, "2026-10-23-0900-MAG-b", "MAG", lines="5")


def _call(tmp_path: Path, cwd: Path, args: dict) -> str:
    code = ("import asyncio, coord_mcp.server as s, json, sys;"
            "print(asyncio.run(s.coord_meeting_notes(s.MeetingNotesInput(**json.loads(sys.argv[1])))))")
    env = dict(os.environ, COORD_ROLE="team", COORD_DB=str(tmp_path / "coord.db"), PYTHONPATH=str(ROOT))
    env.pop("COORD_CLIENT", None)
    out = subprocess.run([sys.executable, "-c", code, json.dumps(args)], cwd=cwd, env=env,
                         capture_output=True, text=True, check=True)
    return out.stdout


def test_a_client_session_sees_only_its_own(env, tmp_path):
    c, d = env
    write(d, "2026-10-23-0900-MAG-b", "MAG b", "2026-10-23T09:00:00", [], "## Summary\n- m\n")
    write(d, "2026-10-23-1000-Royal-c", "Royal c", "2026-10-23T10:00:00", [], "## Summary\n- r\n")
    mag = tmp_path / "MAG"
    mag.mkdir()
    listed = json.loads(_call(tmp_path, mag, {}))
    assert [m["meeting"] for m in listed["meetings"]] == ["2026-10-23-0900-MAG-b"]
    assert "reads only MAG's" in _call(tmp_path, mag, {"client": "Royal"})
    assert "not attributed to MAG" in _call(tmp_path, mag, {"meeting": "2026-10-23-1000-Royal-c"})
    master = tmp_path / "agents"
    master.mkdir()
    assert len(json.loads(_call(tmp_path, master, {}))["meetings"]) == 2
