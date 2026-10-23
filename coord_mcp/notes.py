"""Meeting notes, per client, for the agents that work for that client.

The listener (examples/meeting/CLAUDE.md) writes `<transcript>.notes.md`
beside each transcript in `captions.meetings_dir()`. Nothing in a file name
says whose meeting it was, so each meeting is attributed here, first match
wins:

  1. header   a `client:` line near the top of the notes ("client: MAG",
              or "client: MAG, Royal" for a meeting that covers several)
  2. calendar a counted calendar meeting (coord_import_meetings) that was
              running when captions started, preferring one whose subject
              matches the title
  3. rule     a `subject` classifier rule matching the title

A meeting none of these place is unattributed: listed for the master, never
for a client. Internal meetings stay internal.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import meetings
from .captions import END, meetings_dir
from .hours import _clients

SUMMARY_CAP = 1500
READ_CAP = 40_000
LINES_CAP = 400
EARLY = 15 * 60  # captions often start a few minutes before the invite does

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
_HEADER = re.compile(r"^client:\s*(.+)$", re.IGNORECASE)


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _meta(transcript: Path) -> tuple[str, datetime | None, bool]:
    """(title, started, ended) from the transcript's `#` header and end marker."""
    title, started = transcript.stem, None
    text = _read(transcript)
    for line in text.splitlines()[:5]:
        if line.startswith("# meeting:"):
            title = line.partition(":")[2].strip()
        elif line.startswith("# started:"):
            try:
                started = datetime.fromisoformat(line.partition(":")[2].strip())
            except ValueError:
                pass
    tail = text.rstrip().rpartition("\n")[2]
    return title, started, tail.startswith(END)


def section(notes: str, name: str) -> str:
    m = re.search(rf"^## {re.escape(name)}\s*$(.*?)(?=^## |\Z)", notes, re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ""


def _header_clients(notes: str, keys: dict[str, str]) -> list[str]:
    for line in notes.splitlines()[:10]:
        m = _HEADER.match(line.strip())
        if m:
            names = [n.strip() for n in m.group(1).split(",")]
            return [keys.get(n.casefold(), n) for n in names if n]
    return []


def _calendar_clients(conn: sqlite3.Connection, title: str, started: datetime) -> list[str]:
    t = int(started.timestamp())
    rows = conn.execute("SELECT subject, client FROM meetings WHERE status='counted' AND start_ts <= ? "
                        "AND end_ts > ?", (t + EARLY, t)).fetchall()
    rows = [r for r in rows if r["client"] and r["client"] != meetings.INTERNAL]
    tf = title.casefold()
    named = [r for r in rows if r["subject"] and (r["subject"].casefold() in tf or tf in r["subject"].casefold())]
    pick = named or (rows if len({r["client"] for r in rows}) == 1 else [])
    return sorted({r["client"] for r in pick})


def _rule_clients(conn: sqlite3.Connection, title: str) -> list[str]:
    for r in meetings._rules(conn)["subject"]:
        if meetings._word(r["pattern"], title):
            return [] if r["client"] == meetings.INTERNAL else [r["client"]]
    return []


def attribute(conn: sqlite3.Connection, title: str, started: datetime | None, notes: str
              ) -> tuple[list[str], str | None]:
    """(client keys, how). how is header | calendar | rule, or None."""
    found = _header_clients(notes, _clients(conn))
    if found:
        return found, "header"
    if started and (found := _calendar_clients(conn, title, started)):
        return found, "calendar"
    if found := _rule_clients(conn, title):
        return found, "rule"
    return [], None


def _for(clients: list[str], client: str) -> bool:
    return client.casefold() in {c.casefold() for c in clients}


def entry(conn: sqlite3.Connection, transcript: Path) -> dict[str, Any]:
    title, started, ended = _meta(transcript)
    notes_path = transcript.with_name(transcript.name + ".notes.md")
    notes = _read(notes_path)
    clients, how = attribute(conn, title, started, notes)
    summary = section(notes, "Summary")
    return {"meeting": transcript.stem, "title": title,
            "started": started.isoformat(timespec="minutes") if started else None, "ended": ended,
            "clients": clients, "attributed_by": how, "has_notes": bool(notes),
            "summary": summary[:SUMMARY_CAP], "summary_truncated": len(summary) > SUMMARY_CAP,
            "_notes": notes}


def index(conn: sqlite3.Connection, client: str | None = None, since: str | None = None,
          query: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Meetings newest first. `client` keeps only that client's; None is all."""
    d = meetings_dir()
    paths = sorted(d.glob("*.txt"), key=lambda p: p.name, reverse=True) if d.exists() else []
    if since:
        lo = date.fromisoformat(since).isoformat()
        paths = [p for p in paths if p.name[:10] >= lo]
    q = (query or "").casefold()
    out, more = [], False
    for p in paths:
        if p.name in ("skip.txt",):
            continue
        e = entry(conn, p)
        if client and not _for(e["clients"], client):
            continue
        if q and q not in e["title"].casefold() and q not in e["_notes"].casefold():
            continue
        if len(out) == limit:
            more = True
            break
        del e["_notes"]
        out.append(e)
    return {"meetings": out, "more": more}


def _transcript(meeting: str) -> Path:
    if not _ID.match(meeting or ""):
        raise ValueError(f"meeting '{meeting}' is not a meeting id; take one from the list")
    p = meetings_dir() / f"{meeting}.txt"
    if not p.exists():
        raise KeyError(f"no meeting '{meeting}'; list them without `meeting` to see the ids")
    return p


def read(conn: sqlite3.Connection, meeting: str, client: str | None = None,
         lines: str | None = None) -> dict[str, Any]:
    """One meeting's notes, or transcript lines A-B (the numbers the notes cite).
    With `client`, a meeting not attributed to that client is refused."""
    p = _transcript(meeting)
    e = entry(conn, p)
    if client and not _for(e["clients"], client):
        raise KeyError(f"meeting '{meeting}' is not attributed to {client}")
    head = {k: e[k] for k in ("meeting", "title", "started", "ended", "clients", "attributed_by")}
    if lines:
        m = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", lines)
        if not m or int(m.group(1)) < 1 or int(m.group(2)) < int(m.group(1)):
            raise ValueError(f"lines '{lines}' should be A-B, as the notes cite them (e.g. 120-180)")
        a, b = int(m.group(1)), int(m.group(2))
        if b - a + 1 > LINES_CAP:
            raise ValueError(f"at most {LINES_CAP} lines at once; narrow {lines}")
        got = _read(p).splitlines()[a - 1:b]
        return {**head, "lines": f"{a}-{a + len(got) - 1}" if got else None,
                "transcript": "\n".join(got)}
    notes = e["_notes"]
    return {**head, "notes": notes[:READ_CAP], "truncated": len(notes) > READ_CAP}
