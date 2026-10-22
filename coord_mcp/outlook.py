"""A draft_reply result as an Outlook draft: Save, never Send.

Outlook COM is not an option from an agent session (the MAPI namespace hangs
even with Outlook open), so a draft is written through Outlook on the web.
This module is the part that needs no browser: it turns a task's result into
the compose deeplink (to, cc, subject in the URL) and the body text to type
into the editor, which Outlook autosaves to Drafts. Nothing here sends mail.

    python -m coord_mcp.outlook <task_id>        # URL, then the body
    python -m coord_mcp.outlook <task_id> --json # the same as one object
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

COMPOSE = "https://outlook.office.com/mail/deeplink/compose"


def compose_url(to: list[str], cc: list[str] | None = None, subject: str = "") -> str:
    """The Outlook-on-the-web compose deeplink with the envelope filled in.
    The body is never in the URL: it is long, and the URL would be the one
    place it could be truncated without anyone noticing."""
    q: list[tuple[str, str]] = [("to", ";".join(a.strip() for a in to if a.strip()))]
    if cc:
        q.append(("cc", ";".join(a.strip() for a in cc if a.strip())))
    if subject:
        q.append(("subject", subject))
    return COMPOSE + "?" + urlencode(q)


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
    return draft_from_result(r["id"], r["assigned_to"], r["title"], res)


def draft_from_result(task_id: str, client: str | None, title: str, res: dict[str, Any]) -> dict[str, Any]:
    """The draft from a validated draft_reply result (see draft)."""
    path = Path(res["body_path"]).expanduser()
    exists = path.is_file()
    body = body_text(path.read_text(encoding="utf-8", errors="replace")) if exists else ""
    return {
        "task_id": task_id, "client": client, "title": title,
        "to": list(res.get("to") or []), "cc": list(res.get("cc") or []),
        "subject": res["subject"], "in_reply_to": res.get("in_reply_to"),
        "body_path": str(path), "exists": exists, "body": body,
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
