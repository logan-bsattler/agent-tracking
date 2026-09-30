"""Meeting hours from the operator's Outlook calendar.

Python cannot reach the calendar (New Outlook has no COM; Graph needs an app
registration), so a Claude session fetches events through the M365 connector
and hands them to `import_events` raw. Everything that decides what an event
means is here, where it is tested: the fetch is transport only.

A calendar says what the operator was invited to, not what he attended. Busy
and tentative both count (decision on the board); the page is a draft to edit.

Classification, first match wins:
  1. not counted: cancelled, all-day, shown as free / oof / working elsewhere
  2. a `skip` rule on the subject, or a `skip_org` rule on the organizer domain
  3. a `subject` rule (case-insensitive word match)
  4. a `domain` rule on the organizer, then on the most attendees
  5. a `domain` rule whose pattern is a whole address, on the organizer: how
     prospect calls land on the salesperson who set them up ("Kwo - Sales")
  6. only Logan addresses  ->  INTERNAL (shown, never pushed)
  7. otherwise unclassified, listed on the page for `assign`

Overlapping counted meetings share their overlap evenly, so an hour double
booked is one hour of the operator's time, not two.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from typing import Any

from .db import now
from .hours import CLIENTS, _clients, _midnight, parse_day

INTERNAL = "Internal"
HOME = "loganconsulting.com"
MEETINGS = "meetings"  # the task_id meeting rows carry on the Hours page
COUNTED = ("busy", "tentative")
RULE_KINDS = ("subject", "domain", "skip", "skip_org")

# Seeded once, from the calendar as first read (2026-09-29). Edited by
# `assign` and add_rule afterwards, never by editing this list.
SEED_RULES = (
    [("skip", "Focus time", None), ("skip", "Lunch", None), ("skip_org", "qad.com", None),
     ("domain", "markanthony.com", "MAG"), ("domain", "mabrewing.com", "MAG"),
     ("domain", "iconicwineries.com", "MAG")]
    + [("subject", c, c) for c in CLIENTS]
)


def seed(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM meeting_rules LIMIT 1").fetchone():
        return
    for kind, pattern, client in SEED_RULES:
        add_rule(conn, kind, pattern, client, reclassify=False)


def _domain(addr: str | None) -> str:
    return (addr or "").rpartition("@")[2].strip().casefold()


def _ts(v: Any) -> int:
    """Epoch seconds from the connector's {dateTime, timeZone} or an ISO string.
    The connector reports UTC unless it names a zone; a naive string is UTC."""
    if isinstance(v, dict):
        s, tz = v.get("dateTime", ""), (v.get("timeZone") or "UTC")
        if tz.upper() not in ("UTC", "Z", "COORDINATED UNIVERSAL TIME"):
            raise ValueError(f"time zone '{tz}' not handled; ask the connector for UTC")
    else:
        s = str(v)
    # Graph writes seven fractional digits; minutes are all that matter here.
    dt = datetime.fromisoformat(re.sub(r"\.\d+", "", s).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _word(pattern: str, text: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(pattern)}(?!\w)", text, re.IGNORECASE) is not None


# -------------------------------------------------------------- rules


def add_rule(conn: sqlite3.Connection, kind: str, pattern: str, client: str | None = None,
             reclassify: bool = True) -> dict[str, Any]:
    if kind not in RULE_KINDS:
        raise ValueError(f"kind '{kind}' is not one of {RULE_KINDS}")
    pattern = pattern.strip()
    if not pattern:
        raise ValueError("pattern is empty")
    if kind in ("subject", "domain"):
        if not client:
            raise ValueError(f"a {kind} rule needs a client")
        client = INTERNAL if client.casefold() == INTERNAL.casefold() else _clients(conn).get(
            client.casefold(), client)
    else:
        client = None
    if kind in ("domain", "skip_org"):
        pattern = pattern.lstrip("@").casefold()
    conn.execute("DELETE FROM meeting_rules WHERE kind=? AND pattern=? COLLATE NOCASE", (kind, pattern))
    rid = uuid.uuid4().hex[:12]
    conn.execute("INSERT INTO meeting_rules(id, kind, pattern, client, created_at) VALUES (?,?,?,?,?)",
                 (rid, kind, pattern, client, now()))
    n = reclassify_all(conn) if reclassify else 0
    return {"rule_id": rid, "kind": kind, "pattern": pattern, "client": client, "reclassified": n}


def _rules(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    out: dict[str, list[sqlite3.Row]] = defaultdict(list)
    # Newest first, so a rule added by `assign` beats a seeded one.
    for r in conn.execute("SELECT kind, pattern, client FROM meeting_rules ORDER BY created_at DESC, rowid DESC"):
        out[r["kind"]].append(r)
    return out


def classify(ev: dict[str, Any], rules: dict[str, list[sqlite3.Row]]) -> tuple[str, str | None, str]:
    """(status, client, why). status is counted | skipped | unclassified."""
    show = (ev.get("show_as") or "").casefold()
    if ev.get("cancelled"):
        return "skipped", None, "cancelled"
    if ev.get("all_day"):
        return "skipped", None, "all-day"
    if show not in COUNTED:
        return "skipped", None, f"shown as {show or 'unknown'}"
    subj, org = ev.get("subject") or "", _domain(ev.get("organizer"))
    for r in rules["skip"]:
        if _word(r["pattern"], subj):
            return "skipped", None, f"subject '{r['pattern']}'"
    for r in rules["skip_org"]:
        if org == r["pattern"] or org.endswith("." + r["pattern"]):
            return "skipped", None, f"organizer {r['pattern']}"
    for r in rules["subject"]:
        if _word(r["pattern"], subj):
            return "counted", r["client"], f"subject '{r['pattern']}'"
    doms = {r["pattern"]: r["client"] for r in rules["domain"]}
    if org in doms:
        return "counted", doms[org], f"organizer {org}"
    votes = Counter(doms[d] for d in ev.get("domains", []) if d in doms)
    if votes:
        c, _ = votes.most_common(1)[0]
        return "counted", c, "attendee domains"
    who = (ev.get("organizer") or "").strip().casefold()
    if who in doms:
        return "counted", doms[who], f"organizer {who}"
    if ev.get("attendees", 0) > 1 and ev.get("domains") == [HOME]:
        return "counted", INTERNAL, "Logan attendees only"
    return "unclassified", None, "no rule matched"


def reclassify_all(conn: sqlite3.Connection) -> int:
    rules = _rules(conn)
    n = 0
    for m in conn.execute("SELECT * FROM meetings").fetchall():
        ev = {"subject": m["subject"], "organizer": m["organizer"], "show_as": m["show_as"],
              "cancelled": bool(m["cancelled"]), "all_day": bool(m["all_day"]),
              "domains": json.loads(m["domains"] or "[]"), "attendees": m["attendees"] or 0}
        status, client, why = classify(ev, rules)
        if (status, client, why) != (m["status"], m["client"], m["why"]):
            conn.execute("UPDATE meetings SET status=?, client=?, why=? WHERE id=?", (status, client, why, m["id"]))
            n += 1
    return n


def assign(conn: sqlite3.Connection, subject: str, client: str) -> dict[str, Any]:
    """Teach the classifier a subject: `client` is a client key, Internal, or skip."""
    if client.strip().casefold() == "skip":
        return add_rule(conn, "skip", subject)
    return add_rule(conn, "subject", subject, client)


# ------------------------------------------------------------- import


def import_events(conn: sqlite3.Connection, start: str, end: str, total: int,
                  events: list[dict[str, Any]]) -> dict[str, Any]:
    """Replace the stored meetings starting in [start, end] with `events`.

    `total` is the connector's totalResultCount. A short payload (a page not
    fetched) is refused whole, because the replace would read the missing
    events as cancelled and delete them.
    """
    lo_d, hi_d = parse_day(start), parse_day(end)
    if hi_d < lo_d:
        raise ValueError(f"end {end} is before start {start}")
    ids = [e.get("id") for e in events]
    if len(set(ids)) != len(ids) or None in ids:
        raise ValueError("every event needs a distinct id")
    if len(events) != total:
        raise ValueError(f"got {len(events)} events but the search reported {total}; fetch every page "
                         "(offset = nextOffset) and send them in one call. Nothing was stored.")
    seed(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        out = _replace(conn, lo_d, hi_d, events)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return out


def _replace(conn: sqlite3.Connection, lo_d: date, hi_d: date, events: list[dict[str, Any]]) -> dict[str, Any]:
    rules = _rules(conn)
    lo, hi = _midnight(lo_d), _midnight(date.fromordinal(hi_d.toordinal() + 1))
    kept, counts = set(), Counter()
    for e in events:
        a, b = _ts(e["start"]), _ts(e["end"])
        if not (lo <= a < hi):
            continue  # the search matches on overlap; only events starting in range are ours
        doms = sorted({_domain(x) for x in (e.get("attendees") or []) if x} - {""})
        ev = {"subject": e.get("subject") or "", "organizer": e.get("organizer"),
              "show_as": e.get("showAs") or e.get("show_as"), "cancelled": bool(e.get("isCancelled")),
              "all_day": bool(e.get("isAllDay")), "domains": doms,
              "attendees": len({x.casefold() for x in (e.get("attendees") or []) if x})}
        status, client, why = classify(ev, rules)
        counts[status] += 1
        kept.add(e["id"])
        conn.execute(
            """INSERT INTO meetings(id, start_ts, end_ts, subject, organizer, domains, show_as, cancelled,
                                    all_day, attendees, status, client, why, pulled_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET start_ts=excluded.start_ts, end_ts=excluded.end_ts,
                 subject=excluded.subject, organizer=excluded.organizer, domains=excluded.domains,
                 show_as=excluded.show_as, cancelled=excluded.cancelled, all_day=excluded.all_day,
                 attendees=excluded.attendees, status=excluded.status, client=excluded.client, why=excluded.why,
                 pulled_at=excluded.pulled_at""",
            (e["id"], a, max(a, b), ev["subject"][:200], ev["organizer"], json.dumps(doms), ev["show_as"],
             int(ev["cancelled"]), int(ev["all_day"]), ev["attendees"], status, client, why, now()))
    gone = [r["id"] for r in conn.execute("SELECT id FROM meetings WHERE start_ts >= ? AND start_ts < ?",
                                          (lo, hi)) if r["id"] not in kept]
    conn.executemany("DELETE FROM meetings WHERE id=?", [(g,) for g in gone])
    return {"stored": len(kept), "removed": len(gone), **{k: counts[k] for k in
                                                          ("counted", "skipped", "unclassified")}}


# ------------------------------------------------------------- output


def meeting_minutes(conn: sqlite3.Connection, start: date, end: date, client: str | None = None
                    ) -> dict[tuple[str, str], float]:
    """(day, client) -> minutes of counted meetings, overlaps shared evenly."""
    lo, hi = _midnight(start), _midnight(date.fromordinal(end.toordinal() + 1))
    ms = conn.execute("SELECT start_ts a, end_ts b, client FROM meetings WHERE status='counted' "
                      "AND start_ts < ? AND end_ts > ?", (hi, lo)).fetchall()
    cuts = sorted({x for m in ms for x in (max(m["a"], lo), min(m["b"], hi))})
    out: dict[tuple[str, str], float] = defaultdict(float)
    for pa, pb in zip(cuts, cuts[1:]):
        cover = [m for m in ms if m["a"] <= pa and pb <= m["b"]]
        for m in cover:
            if client and m["client"].casefold() != client.casefold():
                continue
            day = datetime.fromtimestamp(pa).date().isoformat()
            out[(day, m["client"])] += (pb - pa) / 60 / len(cover)
    return dict(out)


def unclassified(conn: sqlite3.Connection, start: date, end: date) -> list[dict[str, Any]]:
    """Distinct subjects awaiting `assign`, with how many hours they hold."""
    lo, hi = _midnight(start), _midnight(date.fromordinal(end.toordinal() + 1))
    rows = conn.execute("""SELECT subject, organizer, COUNT(*) n, SUM(end_ts - start_ts) / 60.0 m
                           FROM meetings WHERE status='unclassified' AND start_ts >= ? AND start_ts < ?
                           GROUP BY subject ORDER BY m DESC""", (lo, hi)).fetchall()
    return [dict(r) for r in rows]
