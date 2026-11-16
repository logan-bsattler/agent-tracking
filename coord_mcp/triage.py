"""Deterministic pieces of the hourly-triage routine (2026-11-16 fixes).

The routine itself is a scheduled Claude run
(~/.claude/scheduled-tasks/hourly-triage/SKILL.md). Two gaps found on
2026-11-16 came from judgement calls the run kept getting wrong, so the
calls live here, in code, and the SKILL tells the run to use them.

1. Teams sweep returned nothing. ``chat_message_search`` with BOTH
   ``afterDateTime`` and ``beforeDateTime`` intermittently answers with no
   output at all (not an empty list, not an error). Replayed on 2026-11-16:
   after 18:45Z / before 20:10Z -> nothing; after 18:45Z alone -> 9 messages,
   including Joseph Setty's 19:27Z MAG note the 20:09Z run missed. Every run
   that day passed a beforeDateTime and still advanced teams.last_check, so
   each silent empty answer lost its hour for good. Fix: search with
   ``afterDateTime`` only and cut the window here, by ``createdDateTime``
   (``teams_window``). A run whose sweep came back empty keeps last_check
   unless an unbounded check also came back empty (``teams_next_check``).

2. Overnight email was read and dropped. The six messages Ben raised by hand
   (QSM-1048881, CVC0707-32 deploy note, CVC0423-195 / QSM-1069690,
   two RaidLogAdmin overdue notices, the LNK AUX maintenance notice) were all
   inside windows the runs read. The SKILL said to skip "notifications and
   automated mail" with a count line *before* the closing-evidence check, so
   ticket mail - the main closing evidence there is - never reached it.
   Fix: ``email_kind`` separates ticket/tracker/outage mail ("signal": never
   skipped, always checked against the client's loose ends and given an
   intent) from real noise (spam digests, surveys, meeting responses).

``python -m coord_mcp.triage email-kind --subject S [--sender X]`` and
``python -m coord_mcp.triage teams-window RESULTS.json --after ISO --before
ISO [--state state.json]`` are what the routine calls.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Iterable

OVERLAP = timedelta(minutes=15)

# Ticket, tracker and environment mail: evidence about board work. Never
# skipped as "automated".
_SIGNAL_SUBJECT = [
    r"\[JIRA\]",
    r"\bQAD ticket\b",
    r"\bQSM-\d+",
    r"\bCS-?\d{5,}",
    r"\bINC\d+",
    r"\bCVC\d{3,4}-\d+",
    r"\bRAID\b",
    r"\bDEV-\d+",
    r"\bFDD\b",
    r"\b(deploy(ed|ment)?|promot(e|ed)|migrat(e|ed|ion))\b",
    r"\b(maintenance|outage|downtime)\b",
]
_SIGNAL_SENDER = [
    r"raidlog",
    r"\bjira\b",
    r"atlassian",
    r"qad\.com",
    r"arista",
    r"servicenow",
]
# Real noise: counted, never read for evidence.
_NOISE = [
    r"^(accepted|declined|tentative|canceled|cancelled):",
    r"\brate your experience\b",
    r"\bsurvey\b",
    r"barracuda",
    r"smartsheet",
    r"\bnewsletter\b",
    r"\bunsubscribe\b",
    r"anthropic",
]


def email_kind(subject: str, sender: str = "") -> str:
    """'noise', 'signal' (ticket/tracker/outage mail) or 'mail' (judge it).

    Surveys about a ticket ("rate your experience with QSM-...") are noise
    even though they name a ticket; a meeting response is noise. Everything
    naming a ticket, RAID, CVC, deploy or maintenance window is signal.
    """
    subj = (subject or "").strip()
    who = (sender or "").lower()
    low = subj.lower()
    if any(re.search(p, low) for p in _NOISE) or any(
        re.search(p, who) for p in _NOISE
    ):
        return "noise"
    if any(re.search(p, subj, re.I) for p in _SIGNAL_SUBJECT):
        return "signal"
    if any(re.search(p, who) for p in _SIGNAL_SENDER):
        return "signal"
    return "mail"


def _ts(value: str) -> datetime:
    v = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _messages(items: Iterable[dict]) -> list[dict]:
    """Drop pagination markers ({"moreResults": ..., "nextOffset": ...})."""
    return [i for i in items if isinstance(i, dict) and i.get("uri")]


def teams_window(
    results: Iterable[dict],
    after: str,
    before: str,
    seen: Iterable[str] = (),
    overlap: timedelta = OVERLAP,
) -> dict:
    """Cut an afterDateTime-only sweep down to this run's window.

    Keeps messages created in [after - overlap, before) whose uri is not in
    ``seen``, oldest first. ``before`` is the run's window_end. Messages
    newer than it are left for the next run, not marked seen.
    """
    lo = _ts(after) - overlap
    hi = _ts(before)
    seen_set = set(seen)
    keep, already, later = [], 0, 0
    for m in _messages(results):
        created = _ts(m.get("createdDateTime") or m.get("lastModifiedDateTime"))
        if created >= hi:
            later += 1
            continue
        if created < lo:
            continue
        if m["uri"] in seen_set:
            already += 1
            continue
        keep.append(m)
    keep.sort(key=lambda m: m.get("createdDateTime") or "")
    return {
        "search_after": _iso(lo),
        "window_end": _iso(hi),
        "new": keep,
        "already_seen": already,
        "after_window": later,
    }


def teams_next_check(
    last_check: str, window_end: str, swept: int, probe_empty: bool
) -> str:
    """The teams.last_check to save.

    ``swept`` is how many messages the afterDateTime-only search returned
    (before filtering). A sweep that returned nothing is trusted only when a
    second, unfiltered probe (query "*", no dates, limit 1) also returned
    nothing new, i.e. ``probe_empty``; otherwise last_check stays put and
    the next run re-covers the window. A silent empty answer must never
    advance the high-water mark (2026-11-16, 20:09Z run).
    """
    if swept == 0 and not probe_empty:
        return last_check
    return window_end


def _cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m coord_mcp.triage")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("email-kind", help="noise / signal / mail for one email")
    e.add_argument("--subject", required=True)
    e.add_argument("--sender", default="")
    t = sub.add_parser("teams-window", help="cut a sweep to the run window")
    t.add_argument("results", help="JSON file: list of chat_message_search items")
    t.add_argument("--after", required=True, help="state.teams.last_check")
    t.add_argument("--before", required=True, help="Teams window_end")
    t.add_argument("--state", help="state.json, for teams.seen_uris")
    a = p.parse_args(argv)
    if a.cmd == "email-kind":
        print(email_kind(a.subject, a.sender))
        return 0
    with open(a.results, encoding="utf-8") as f:
        items = json.load(f)
    seen: list[str] = []
    if a.state:
        with open(a.state, encoding="utf-8") as f:
            seen = (json.load(f).get("teams") or {}).get("seen_uris") or []
    out = teams_window(items, a.after, a.before, seen)
    out["new"] = [
        {k: m.get(k) for k in ("uri", "chatId", "createdDateTime", "from", "summary")}
        for m in out["new"]
    ]
    json.dump(out, sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
