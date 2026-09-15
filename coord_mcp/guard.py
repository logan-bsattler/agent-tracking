"""The guard: Claude Code hooks that stop a session before it burns the limit.

Two hook entry points, wired by `python -m coord_mcp.usage setup`:

    PreToolUse        every tool call in every session (lead, teammates, subagents)
    UserPromptSubmit  every prompt

Each one assesses the session and either passes, injects a warning into the
model's context, or blocks (exit 2, reason on stderr). Blocking a tool call
ends the turn with the reason shown to the model; blocking a prompt stops it
before anything is spent and shows the reason to the person.

What it watches, and why:

  context size   The number of tokens this session sends per request. Every
                 tool call re-reads the whole context, so cost per turn is
                 context x tool calls. This is the runaway case.
  5h / 7d limit  From the latest statusLine snapshot. Hard stop near the wall.
  burn rate      Projected time to 100% of the 5-hour window vs its reset.

Thresholds (env, defaults):
  COORD_CTX_WARN=150000   COORD_CTX_HARD=300000      tokens of context
  COORD_WARN_5H=75        COORD_HARD_5H=92           percent
  COORD_WARN_7D=85        COORD_HARD_7D=97

Pause it:   python -m coord_mcp.guard pause 30      (minutes)
Disable:    COORD_GUARD=off
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .db import connect, now

CTX_WARN = int(os.environ.get("COORD_CTX_WARN", "150000"))
CTX_HARD = int(os.environ.get("COORD_CTX_HARD", "300000"))
WARN_5H = float(os.environ.get("COORD_WARN_5H", "75"))
HARD_5H = float(os.environ.get("COORD_HARD_5H", "92"))
WARN_7D = float(os.environ.get("COORD_WARN_7D", "85"))
HARD_7D = float(os.environ.get("COORD_HARD_7D", "97"))

PAUSE_FILE = Path(os.environ.get("COORD_DB", Path.home() / ".coord" / "coord.db")).expanduser().parent / "guard-pause"


def _fmt_k(n: int) -> str:
    return f"{n / 1000:.0f}k"


def session_context(transcript_path: str | None) -> tuple[int, str | None, int]:
    """(context tokens, model, requests in the last 10 minutes) from the transcript tail.

    Reads only the last 256 KB, so it is cheap enough for every tool call.
    """
    if not transcript_path:
        return 0, None, 0
    p = Path(transcript_path)
    try:
        size = p.stat().st_size
        with p.open("rb") as fh:
            fh.seek(max(0, size - 256 * 1024))
            chunk = fh.read()
    except OSError:
        return 0, None, 0
    ctx, model, recent = 0, None, 0
    cutoff = now() - 600
    seen: set[str] = set()
    for line in reversed(chunk.splitlines()):
        if b'"usage"' not in line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "assistant":
            continue
        u = (d.get("message") or {}).get("usage") or {}
        rid = d.get("requestId")
        if not u or (rid and rid in seen):
            continue
        if rid:
            seen.add(rid)
        if model is None:
            model = (d.get("message") or {}).get("model")
            ctx = int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) \
                + int(u.get("cache_creation_input_tokens") or 0)
        ts = d.get("timestamp")
        if ts:
            from .usage import _iso_to_ts
            if _iso_to_ts(ts) >= cutoff:
                recent += 1
            else:
                break
    return ctx, model, recent


def paused() -> bool:
    if os.environ.get("COORD_GUARD", "").lower() in ("off", "0", "false"):
        return True
    try:
        return int(PAUSE_FILE.read_text().strip()) > now()
    except (OSError, ValueError):
        return False


def assess(conn: sqlite3.Connection, transcript_path: str | None) -> dict[str, Any]:
    """Return {level: ok|warn|block, reasons: [...], context, five_hour, seven_day}."""
    from .usage import burn, latest_quota

    ctx, model, recent = session_context(transcript_path)
    q = latest_quota(conn)
    fh = q["five_hour_pct"] if q else None
    sd = q["seven_day_pct"] if q else None
    reasons: list[str] = []
    level = "ok"

    def bump(to: str, msg: str) -> None:
        nonlocal level
        reasons.append(msg)
        if to == "block" or (to == "warn" and level == "ok"):
            level = to

    per_req = ""
    if model and ctx:
        from .usage import price
        per_req = f", about ${ctx * price(model)[2] / 1e6:.2f} of cache reads per tool call"
    if ctx >= CTX_HARD:
        bump("block", f"This session's context is {_fmt_k(ctx)} tokens{per_req}. "
                      f"Above the {_fmt_k(CTX_HARD)} hard limit: run /compact or /clear, or hand the work to a "
                      f"fresh teammate, before continuing.")
    elif ctx >= CTX_WARN:
        bump("warn", f"Context is {_fmt_k(ctx)} tokens{per_req}. Keep this turn short, avoid reading large "
                     f"files, and /compact soon. Hard stop at {_fmt_k(CTX_HARD)}.")
    if recent >= 40 and ctx >= CTX_WARN:
        bump("warn", f"{recent} requests in the last 10 minutes at this context size. That is the pattern that "
                     f"empties the 5-hour window.")

    if fh is not None:
        if fh >= HARD_5H:
            bump("block", f"5-hour limit is at {fh:.0f}%. Stop; it resets {_reset_text(q, 'five_hour_reset')}.")
        elif fh >= WARN_5H:
            bump("warn", f"5-hour limit at {fh:.0f}%, resets {_reset_text(q, 'five_hour_reset')}. Prefer cheap work; "
                         f"do not start anything large.")
    if sd is not None:
        if sd >= HARD_7D:
            bump("block", f"Weekly limit is at {sd:.0f}%. Stop; it resets {_reset_text(q, 'seven_day_reset')}.")
        elif sd >= WARN_7D:
            bump("warn", f"Weekly limit at {sd:.0f}%, resets {_reset_text(q, 'seven_day_reset')}.")

    b = burn(conn)
    if b.get("hits_limit_before_reset") and level != "block":
        bump("warn", f"At the current pace the 5-hour window hits 100% around {b['hit_at_text']}, "
                     f"before it resets at {b['reset_text']}. Slow down now or that is a forced stop.")

    return {"level": level, "reasons": reasons, "context": ctx, "model": model, "recent_requests": recent,
            "five_hour": fh, "seven_day": sd}


def _reset_text(q: dict[str, Any] | None, key: str) -> str:
    from .usage import when
    if not q or not q.get(key):
        return "soon"
    return "at " + when(q[key])


def _emit_block(reason: str) -> int:
    sys.stderr.write(reason + "\nPause the guard for 30 minutes with: python -m coord_mcp.guard pause 30\n")
    return 2


def hook(event: str) -> int:
    """Read the hook payload from stdin, decide, exit 0 (pass/context) or 2 (block)."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    if paused():
        return 0
    try:
        conn = connect()
        a = assess(conn, payload.get("transcript_path"))
    except Exception:
        return 0  # the guard must never break a session by itself
    if a["level"] == "block":
        return _emit_block("Blocked by the usage guard. " + " ".join(a["reasons"]))
    if a["level"] == "warn":
        text = "Usage guard: " + " ".join(a["reasons"])
        if event == "UserPromptSubmit":
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                     "additionalContext": text}}))
        elif event == "PreToolUse" and a["context"] >= CTX_WARN and a["recent_requests"] % 10 == 1:
            # Nudge once every ten tool calls, not on every one.
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                     "additionalContext": text}}))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "pretooluse":
        return hook("PreToolUse")
    if cmd == "userpromptsubmit":
        return hook("UserPromptSubmit")
    if cmd == "pause":
        minutes = int(argv[1]) if len(argv) > 1 else 30
        PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PAUSE_FILE.write_text(str(now() + minutes * 60))
        print(f"guard paused for {minutes} minutes")
        return 0
    if cmd == "resume":
        PAUSE_FILE.unlink(missing_ok=True)
        print("guard active")
        return 0
    if cmd == "check":
        conn = connect()
        a = assess(conn, argv[1] if len(argv) > 1 else None)
        print(json.dumps(a, indent=2))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
