"""A draft_reply result as an Outlook draft: Save, never Send.

Outlook COM is not an option from an agent session (the MAPI namespace hangs
even with Outlook open), so a draft is written through Outlook on the web.
This module is the part that needs no browser: it turns a task's result into
the body text to type and one of two ways into an editor, which Outlook
autosaves to Drafts. Nothing here sends mail.

- Reply all, when the result names the message it answers (`reply_from`):
  the search that finds that message in Outlook on the web, where master
  opens it and presses Reply all. Threaded, with the quoted history, and the
  recipients are Outlook's own; the result's to/cc only add to them.
- New message otherwise: the compose deeplink (to, cc, subject in the URL).

    python -m coord_mcp.outlook <task_id>        # URL, then the body
    python -m coord_mcp.outlook <task_id> --json # the same as one object
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
import re
from typing import Any
from email.utils import parseaddr
from urllib.parse import quote, urlencode

COMPOSE = "https://outlook.office.com/mail/deeplink/compose"
MAIL = "https://outlook.office.com/mail/"
_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd)\s*:\s*)+", re.I)


def _addrs(xs: list[str]) -> str:
    """Bare addresses, ';'-joined. Outlook does not parse 'Name <addr>' from
    the deeplink: on 2026-10-22 it kept a garbled To and dropped the Cc."""
    return ";".join(parseaddr(a)[1] or a.strip() for a in xs if a.strip())


def compose_url(to: list[str], cc: list[str] | None = None, subject: str = "") -> str:
    """The Outlook-on-the-web compose deeplink with the envelope filled in.
    The body is never in the URL: it is long, and the URL would be the one
    place it could be truncated without anyone noticing. Spaces go as %20:
    Outlook shows a '+' literally."""
    q: list[tuple[str, str]] = [("to", _addrs(to))]
    if cc:
        q.append(("cc", _addrs(cc)))
    if subject:
        q.append(("subject", subject))
    return COMPOSE + "?" + urlencode(q, quote_via=quote)


def search_query(reply_from: str, subject: str) -> str:
    """The Outlook search that finds the message being answered: its sender
    and its subject without the RE:/FW: chain, which differs from message to
    message along a thread."""
    core = _PREFIX.sub("", subject).replace('"', "").strip()
    q = f"from:{parseaddr(reply_from)[1] or reply_from.strip()}"
    return f'{q} subject:"{core}"' if core else q


def body_text(raw: str) -> str:
    """The body as it should read in the email.

    The file is the body. One concession to how agents already write drafts:
    a leading '# DRAFT ...' heading with To/Cc lines, closed by a '---' rule
    within the first dozen lines, is a header for the reader, not the body,
    and is dropped."""
    lines = raw.replace("\r\n", "\n").split("\n")
    if lines and lines[0].startswith("# "):
        for i, line in enumerate(lines[1:13], 1):
            if line.strip() == "---":
                lines = lines[i + 1:]
                break
    return "\n".join(lines).strip("\n") + "\n"


def draft(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    """Everything needed to put one task's reply into Outlook Drafts.

    Raises ValueError when the task is not a finished draft_reply: the button
    and the CLI both refuse rather than compose an empty draft."""
    r = conn.execute("SELECT id, kind, title, assigned_to, result FROM tasks WHERE id=?", (task_id,)).fetchone()
    if r is None:
        raise ValueError(f"no task {task_id!r}")
    if r["kind"] != "draft_reply":
        raise ValueError(f"task {task_id} is a {r['kind']}, not a draft_reply")
    res = json.loads(r["result"]) if r["result"] else None
    if not res or not res.get("done"):
        raise ValueError(f"task {task_id} has no finished draft")
    from .gate import require_approved
    require_approved(conn, task_id, "Draft in Outlook")
    return draft_from_result(r["id"], r["assigned_to"], r["title"], res)


def draft_from_result(task_id: str, client: str | None, title: str, res: dict[str, Any]) -> dict[str, Any]:
    """The draft from a validated draft_reply result (see draft)."""
    from .store import DOCS_ROOT, _resolve  # lazy: store imports this module lazily too

    # Agents write body_path relative to their client folder, not our cwd.
    path = _resolve(str(Path(res["body_path"]).expanduser()), client, DOCS_ROOT)
    exists = path.is_file()
    body = body_text(path.read_text(encoding="utf-8", errors="replace")) if exists else ""
    reply_from = (res.get("reply_from") or "").strip()
    return {
        "task_id": task_id, "client": client, "title": title,
        "to": list(res.get("to") or []), "cc": list(res.get("cc") or []),
        "subject": res["subject"], "in_reply_to": res.get("in_reply_to"),
        "reply_all": bool(reply_from), "reply_from": reply_from or None,
        "reply_date": res.get("reply_date"),
        "search": search_query(reply_from, res["subject"]) if reply_from else None,
        "body_path": str(path), "exists": exists, "body": body,
        # The compose deeplink stays even for a reply all: it is the fallback
        # when the original cannot be found, and Ben's own-browser link.
        "url": compose_url(res.get("to") or [], res.get("cc"), res["subject"]),
    }


def main(argv: list[str] | None = None) -> int:
    from .db import connect
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 2
    conn = connect()
    try:
        d = draft(conn, argv[0])
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    if "--json" in argv:
        print(json.dumps(d, indent=2))
        return 0
    if d["reply_all"]:
        print(MAIL)
        print()
        print("Reply all. Search Outlook for the message being answered:")
        print(f"  {d['search']}")
        if d["reply_date"]:
            print(f"  sent {d['reply_date']}")
        print("Open it, press Reply all, add any To/Cc below that Outlook left off,")
        print("type the body above the quoted thread. Not found: use the compose URL.")
        print(f"Compose URL: {d['url']}")
    else:
        print(d["url"])
    print()
    print(f"To: {'; '.join(d['to'])}")
    if d["cc"]:
        print(f"Cc: {'; '.join(d['cc'])}")
    print(f"Subject: {d['subject']}")
    if d["in_reply_to"]:
        print(f"In reply to: {d['in_reply_to']}")
    print(f"Body: {d['body_path']}" + ("" if d["exists"] else "  (NOT ON DISK)"))
    print("-" * 40)
    print(d["body"], end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
