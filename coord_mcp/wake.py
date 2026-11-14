"""Scheduled wake for the master worker (2026-11-14).

The master-console mod's 10s tick (auto-dispatch, taking the minder's
prompts, pipeline notes) runs only inside a live worker process. A
self-clear ends that process, and only the desk's tick woke it again, so
with the desk closed too (an evening, a weekend) nothing ticked: on
11-13/14 the worker stopped at 19:28 and eleven minder prompts sat untaken
for 20 hours.

The `worker-wake` scheduled task runs this every 15 minutes. It prints the
wake line only when the board's own rule says so (store.claim_worker_wake:
a worker prompt has waited WAKE_AFTER, the worker has not polled in
WORKER_STALE, no wake in WAKE_EVERY), and the task send_messages that line
to the worker. The worker's mod consumes the marked line without a turn and
takes the prompt at once. A minder prompt lives PROMPT_TTL (30 minutes), so
a 15-minute check never misses one.

    python -m coord_mcp.wake          # claim and print the wake line, or nothing
    python -m coord_mcp.wake --peek   # say what it would do; claims nothing
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from . import store

# Must match WAKE_MARK in mods/master-console/hooks/register.tsx.
WAKE_MARK = "master-console wake:"
WORKER_ID = "local_198c7d6a-e9a9-4ca0-969e-714f29c9e560"


def wake_text(w: dict[str, Any]) -> str:
    return (f"{WAKE_MARK} queued prompt {w['id']} ({w['action']}) has waited {w['waited']}s "
            "and your poll is not running. No action needed: the mod takes it once you are up.")


def check(conn, peek: bool = False) -> dict[str, Any] | None:
    """{"to", "text", "id", "action", "waited"} when the worker should be woken now.

    peek runs the same rule inside a transaction it rolls back, so the wake
    is not claimed."""
    if peek:
        conn.execute("SAVEPOINT peek")
        try:
            w = store.claim_worker_wake(conn)
        finally:
            conn.execute("ROLLBACK TO peek")
            conn.execute("RELEASE peek")
    else:
        w = store.claim_worker_wake(conn)
    if w is None:
        return None
    to = (os.environ.get("COORD_WORKER_ID") or "").strip() or WORKER_ID
    return {**w, "to": to, "text": wake_text(w)}


def main(argv: list[str] | None = None) -> int:
    from .db import connect
    p = argparse.ArgumentParser(prog="python -m coord_mcp.wake", description="Wake a stopped master worker.")
    p.add_argument("--peek", action="store_true", help="say what would happen; claim nothing")
    a = p.parse_args(argv)
    conn = connect()
    try:
        w = check(conn, peek=a.peek)
        if w is None:
            seen = store._seen_at(conn, "worker_seen")
            live = store.worker_live(conn)
            print(f"no wake (worker {'live' if live else 'not live'}, last poll {seen})" if a.peek else "", end="")
            return 0
        if a.peek:
            print(f"would wake {w['to']}: prompt {w['id']} ({w['action']}) waited {w['waited']}s")
        else:
            print(f"to: {w['to']}")
            print(w["text"])
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
