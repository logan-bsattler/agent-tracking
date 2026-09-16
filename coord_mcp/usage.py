"""Usage tracking: what the subscription budget was spent on.

Source of truth is Claude Code's own transcript files under
~/.claude/projects/<project>/<session>.jsonl (and <session>/subagents/*.jsonl).
Every assistant turn there carries the API usage block for that request, with
model, timestamp, session and working directory. That gives exact per-request
attribution for every session on the machine, retroactively, with no hook.

Ingestion is incremental by byte offset, so re-running is cheap and safe.

    python -m coord_mcp.usage ingest            # pull new transcript data
    python -m coord_mcp.usage report [--days 7] [--open]
    python -m coord_mcp.usage status            # current limits + this week
    python -m coord_mcp.usage statusline        # stdin hook, see README
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .db import connect, now

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_DIR / "projects"

# API-equivalent pricing, $ per million tokens. Subscription limits are not
# billed in dollars, but this is the same figure Claude Code shows in /cost
# and it is the best available proxy for how heavily a request weighs.
# (input, output, cache_read, cache_write_5m, cache_write_1h)
PRICING: dict[str, tuple[float, float, float, float, float]] = {
    "claude-fable-5-1": (10, 50, 0.25, 12.5, 20),
    "claude-fable-5": (10, 50, 1.0, 12.5, 20),
    "claude-mythos-5-1": (10, 50, 0.25, 12.5, 20),
    "claude-opus-5": (5, 25, 0.5, 6.25, 10),
    "claude-opus-4-8": (5, 25, 0.5, 6.25, 10),
    "claude-opus-4-7": (5, 25, 0.5, 6.25, 10),
    "claude-opus-4-6": (5, 25, 0.5, 6.25, 10),
    "claude-sonnet-5": (2, 10, 0.2, 2.5, 4),
    "claude-sonnet-4-6": (3, 15, 0.3, 3.75, 6),
    "claude-haiku-4-5": (1, 5, 0.1, 1.25, 2),
    "<synthetic>": (0, 0, 0, 0, 0),
}
FALLBACK_PRICE = PRICING["claude-opus-5"]


def price(model: str) -> tuple[float, float, float, float, float]:
    if model in PRICING:
        return PRICING[model]
    for key, val in PRICING.items():
        if model.startswith(key):
            return val
    return FALLBACK_PRICE


def cost_usd(model: str, inp: int, out: int, cr: int, cw5: int, cw1: int) -> float:
    p = price(model)
    return (inp * p[0] + out * p[1] + cr * p[2] + cw5 * p[3] + cw1 * p[4]) / 1_000_000


# ------------------------------------------------------------------ ingest


def _project_name(cwd: str | None, dirname: str) -> str:
    if cwd:
        return Path(cwd).name or cwd
    return dirname


def when(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M") if ts else ""


def _iso_to_ts(s: str | None) -> int:
    if not s:
        return now()
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def _first_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                return block["text"]
    return None


def _ingest_line(conn: sqlite3.Connection, d: dict[str, Any], dirname: str,
                 agent_id: str | None, source: str, seen: set[str]) -> int:
    t = d.get("type")
    sid = d.get("sessionId")
    if t == "assistant":
        msg = d.get("message") or {}
        u = msg.get("usage")
        rid = d.get("requestId")
        if not u or not rid or rid in seen:
            return 0
        seen.add(rid)
        cc = u.get("cache_creation") or {}
        cw1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
        cw_total = int(u.get("cache_creation_input_tokens") or 0)
        cw5 = max(cw_total - cw1, 0) if cc else cw_total
        inp = int(u.get("input_tokens") or 0)
        out = int(u.get("output_tokens") or 0)
        cr = int(u.get("cache_read_input_tokens") or 0)
        think = int((u.get("output_tokens_details") or {}).get("thinking_tokens") or 0)
        model = msg.get("model") or "unknown"
        ts = _iso_to_ts(d.get("timestamp"))
        cwd = d.get("cwd")
        conn.execute(
            """INSERT OR IGNORE INTO requests(request_id, ts, session_id, agent_id, project, cwd,
                 model, input_tokens, cache_write_5m, cache_write_1h, cache_read, output_tokens,
                 thinking, cost_usd, effort, source_file)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, ts, sid or "", agent_id or d.get("agentId"), _project_name(cwd, dirname), cwd,
             model, inp, cw5, cw1, cr, out, think, cost_usd(model, inp, out, cr, cw5, cw1),
             d.get("effort"), source),
        )
        conn.execute(
            """INSERT INTO cc_sessions(session_id, project, cwd, first_ts, last_ts, git_branch)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET
                 first_ts = MIN(first_ts, excluded.first_ts),
                 last_ts = MAX(last_ts, excluded.last_ts),
                 project = COALESCE(cc_sessions.project, excluded.project),
                 cwd = COALESCE(cc_sessions.cwd, excluded.cwd),
                 git_branch = COALESCE(excluded.git_branch, cc_sessions.git_branch)""",
            (sid, _project_name(cwd, dirname), cwd, ts, ts, d.get("gitBranch")),
        )
        return 1
    if t in ("custom-title", "ai-title") and sid:
        title = d.get("customTitle") or d.get("aiTitle")
        if title:
            # A custom title always wins over an AI one.
            if t == "custom-title":
                conn.execute(
                    "INSERT INTO cc_sessions(session_id, title) VALUES (?,?) "
                    "ON CONFLICT(session_id) DO UPDATE SET title = excluded.title",
                    (sid, title[:120]),
                )
            else:
                conn.execute(
                    "INSERT INTO cc_sessions(session_id, title) VALUES (?,?) "
                    "ON CONFLICT(session_id) DO UPDATE SET title = COALESCE(cc_sessions.title, excluded.title)",
                    (sid, title[:120]),
                )
    elif t == "cost-state" and sid:
        conn.execute(
            "INSERT INTO cc_sessions(session_id, cc_cost_usd) VALUES (?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET cc_cost_usd = excluded.cc_cost_usd",
            (sid, float(d.get("totalCostUSD") or 0)),
        )
    elif t == "user" and sid and not d.get("isMeta") and not agent_id:
        text = _first_text((d.get("message") or {}).get("content"))
        if text and not text.startswith("<"):
            conn.execute(
                "INSERT INTO cc_sessions(session_id, title) VALUES (?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET title = COALESCE(cc_sessions.title, excluded.title)",
                (sid, text.strip().splitlines()[0][:120]),
            )
    return 0


def _transcripts() -> list[tuple[Path, str, str | None]]:
    """(path, project dirname, agent_id) for every transcript file."""
    out = []
    if not PROJECTS_DIR.is_dir():
        return out
    for proj in PROJECTS_DIR.iterdir():
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            out.append((f, proj.name, None))
        for f in proj.glob("*/subagents/agent-*.jsonl"):
            out.append((f, proj.name, f.stem.removeprefix("agent-")))
    return out


def ingest(conn: sqlite3.Connection) -> dict[str, int]:
    """Pull new lines from every transcript. Incremental by byte offset."""
    files = requests = 0
    for path, dirname, agent_id in _transcripts():
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            continue  # rotated or deleted between listing and reading; next run gets it
        row = conn.execute("SELECT offset, size FROM ingest_files WHERE path=?", (key,)).fetchone()
        offset = row["offset"] if row else 0
        if row and size < row["size"]:
            offset = 0  # file was rewritten
        if size <= offset:
            continue
        files += 1
        seen: set[str] = set()
        conn.execute("BEGIN")
        try:
            with path.open("rb") as fh:
                fh.seek(offset)
                while True:
                    line = fh.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        break  # partial write in progress; pick it up next run
                    offset += len(line)
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(d, dict):
                        requests += _ingest_line(conn, d, dirname, agent_id, key, seen)
            conn.execute(
                "INSERT OR REPLACE INTO ingest_files(path, offset, size) VALUES (?,?,?)",
                (key, offset, size),
            )
            conn.execute("COMMIT")
        except OSError:
            conn.execute("ROLLBACK")
            files -= 1
            continue  # same as a vanished file above: skip it, keep the rest
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"files_read": files, "requests_added": requests,
            "desktop_quota_added": ingest_desktop_quota(conn)}


# ------------------------------------------------- Claude Desktop quota file
#
# Claude Desktop keeps its own record of plan limits at
# %APPDATA%/Claude/plan-usage-history.json: {"version":2,"samples":[
#   {"t": <epoch ms>, "org": "...", "u": {"fh": <5h pct>, "sd": <7d pct>}}, ...]}
#
# This is the *shared pool* number, so it covers claude.ai chat, the Desktop
# app and mobile as well as Claude Code. It has no reset timestamps and is
# sampled sparsely (median 19 minutes, but hours when Desktop is closed), so
# it complements the statusLine hook rather than replacing it.
#
# Undocumented internals: treat as a bonus source, never a required one.


def desktop_quota_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return Path(os.environ.get("COORD_DESKTOP_USAGE", base / "Claude" / "plan-usage-history.json"))


def _read_bytes(path: Path) -> bytes | None:
    """Read a file, falling back to the shell.

    Python installed from the Microsoft Store runs in an AppContainer that
    redirects %APPDATA%, so a direct open of Claude Desktop's data raises
    FileNotFoundError even though the file is there. The shell is not
    redirected, so it can read what we cannot.
    """
    try:
        return path.read_bytes()
    except OSError:
        pass
    try:
        argv = ["cmd", "/c", "type", str(path)] if sys.platform == "win32" else ["cat", str(path)]
        out = subprocess.run(argv, capture_output=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0)
        return out.stdout if out.returncode == 0 and out.stdout else None
    except (OSError, subprocess.SubprocessError):
        return None


def desktop_quota_note() -> str | None:
    """None when the Desktop file is readable, otherwise why it isn't.

    The common Windows cause is subtle: Python installed from the Microsoft
    Store runs in an AppContainer that redirects %APPDATA%, and a child
    process it spawns inherits the same redirection, so even shelling out
    cannot reach the file. The fix is a different interpreter, not more code.
    """
    p = desktop_quota_path()
    if _read_bytes(p):
        return None
    if sys.platform == "win32" and "WindowsApps" in sys.prefix:
        return ("Claude Desktop's plan-usage history could not be read: this is the Microsoft Store "
                "build of Python, which is sandboxed away from %APPDATA%. Install coord-mcp under a "
                "non-Store Python (python.org, or whatever `py -0p` lists) and re-run setup, and the "
                "shared-pool limit history becomes available.")
    return f"No Claude Desktop plan-usage history at {p}, so shared-pool limit history is unavailable."


def ingest_desktop_quota(conn: sqlite3.Connection) -> int:
    """Load Claude Desktop's plan-usage samples as quota snapshots. Idempotent."""
    raw = _read_bytes(desktop_quota_path())
    if not raw:
        return 0
    try:
        doc = json.loads(raw.decode("utf-8-sig", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 0
    samples = doc.get("samples") if isinstance(doc, dict) else doc
    if not isinstance(samples, list):
        return 0
    rows = []
    for s in samples:
        if not isinstance(s, dict):
            continue
        u = s.get("u") or {}
        t = s.get("t")
        if not isinstance(t, (int, float)) or not isinstance(u, dict):
            continue
        fh, sd = u.get("fh"), u.get("sd")
        if fh is None and sd is None:
            continue
        rows.append((int(t / 1000 if t > 1e11 else t), "desktop", fh, sd))
    if not rows:
        return 0
    before = conn.execute("SELECT COUNT(*) n FROM quota_snapshots WHERE source='desktop'").fetchone()["n"]
    conn.executemany(
        "INSERT OR IGNORE INTO quota_snapshots(ts, source, five_hour_pct, seven_day_pct) VALUES (?,?,?,?)", rows)
    after = conn.execute("SELECT COUNT(*) n FROM quota_snapshots WHERE source='desktop'").fetchone()["n"]
    return after - before


# --------------------------------------------------------------- statusline


def _pct(obj: Any) -> float | None:
    if not isinstance(obj, dict):
        return None
    for k in ("used_percentage", "utilization", "used_pct", "percent"):
        if obj.get(k) is not None:
            v = float(obj[k])
            return v * 100 if 0 <= v <= 1 and k == "utilization" else v
    return None


def _reset(obj: Any) -> int | None:
    if not isinstance(obj, dict):
        return None
    for k in ("resets_at", "reset_at", "resetsAt"):
        v = obj.get(k)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            return int(v if v < 1e11 else v / 1000)
        try:
            return _iso_to_ts(str(v))
        except ValueError:
            pass
    return None


def record_statusline(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    rl = payload.get("rate_limits") or {}
    fh, sd = rl.get("five_hour"), rl.get("seven_day")
    ctx = payload.get("context_window") or {}
    ctx_pct = ctx.get("used_percentage")
    if ctx_pct is None and ctx.get("context_window_size") and ctx.get("total_input_tokens") is not None:
        ctx_pct = 100.0 * ctx["total_input_tokens"] / ctx["context_window_size"]
    snap = {
        "ts": now(),
        "five_hour_pct": _pct(fh),
        "five_hour_reset": _reset(fh),
        "seven_day_pct": _pct(sd),
        "seven_day_reset": _reset(sd),
        "session_id": payload.get("session_id"),
        "model": (payload.get("model") or {}).get("id") or (payload.get("model") or {}).get("display_name"),
        "context_pct": ctx_pct,
    }
    last = conn.execute(
        "SELECT ts, five_hour_pct, seven_day_pct FROM quota_snapshots ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    changed = (
        last is None
        or last["five_hour_pct"] != snap["five_hour_pct"]
        or last["seven_day_pct"] != snap["seven_day_pct"]
        or snap["ts"] - last["ts"] >= 300
    )
    if changed and (snap["five_hour_pct"] is not None or snap["seven_day_pct"] is not None):
        conn.execute(
            """INSERT INTO quota_snapshots(ts, five_hour_pct, five_hour_reset, seven_day_pct,
                 seven_day_reset, session_id, model, context_pct, raw) VALUES (?,?,?,?,?,?,?,?,?)""",
            (snap["ts"], snap["five_hour_pct"], snap["five_hour_reset"], snap["seven_day_pct"],
             snap["seven_day_reset"], snap["session_id"], snap["model"], snap["context_pct"],
             json.dumps({"rate_limits": rl, "context_window": ctx})[:2000]),
        )
    return snap


def statusline_text(snap: dict[str, Any], payload: dict[str, Any]) -> str:
    parts = []
    model = (payload.get("model") or {}).get("display_name") or snap.get("model")
    if model:
        parts.append(str(model))
    if snap.get("context_pct") is not None:
        parts.append(f"ctx {snap['context_pct']:.0f}%")
    if snap.get("five_hour_pct") is not None:
        parts.append(f"5h {snap['five_hour_pct']:.0f}%" + _until(snap.get("five_hour_reset")))
    if snap.get("seven_day_pct") is not None:
        parts.append(f"7d {snap['seven_day_pct']:.0f}%" + _until(snap.get("seven_day_reset")))
    if snap.get("five_hour_pct") is None and snap.get("seven_day_pct") is None:
        parts.append("limits: n/a")
    b = snap.get("burn") or {}
    if b.get("hits_limit_before_reset"):
        parts.append(f"!! 100% by {datetime.fromtimestamp(b['hit_at']).strftime('%H:%M')}")
    elif b.get("pct_per_hour"):
        parts.append(f"{b['pct_per_hour']:.0f}%/h")
    return " | ".join(parts)


def _until(reset: int | None) -> str:
    if not reset:
        return ""
    s = reset - now()
    if s <= 0:
        return ""
    h, m = divmod(s // 60, 60)
    if h >= 24:
        d, h = divmod(h, 24)
        return f" ({d}d{h}h)"
    return f" ({h}h{m:02d}m)" if h else f" ({m}m)"


# ---------------------------------------------------------------- queries


def latest_quota(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Most recent percentages from either source, with reset times borrowed
    from the last statusLine row if the newest sample is a Desktop one (which
    carries percentages but no resets).

    Each percentage carries its own timestamp in `<field>_ts`, because a
    back-filled one can be much older than the row it is returned on. The
    row's `ts` dates the newest reading of anything, not every field in it, so
    anything enforcing on a single percentage must age that percentage.
    """
    r = conn.execute("SELECT * FROM quota_snapshots ORDER BY ts DESC LIMIT 1").fetchone()
    if r is None:
        return None
    d = dict(r)
    # A row can carry one percentage and not the other; fall back to the most
    # recent reading of whichever is missing, so a tile is never blank because
    # of one partial sample.
    for k in ("five_hour_pct", "seven_day_pct"):
        d[f"{k}_ts"] = d["ts"] if d.get(k) is not None else None
        if d.get(k) is None:
            back = conn.execute(
                f"SELECT ts, {k} FROM quota_snapshots WHERE {k} IS NOT NULL AND ts >= ? "
                "ORDER BY ts DESC LIMIT 1",
                (d["ts"] - 86400,)).fetchone()
            if back:
                d[k] = back[k]
                d[f"{k}_ts"] = back["ts"]
    if d.get("five_hour_reset") is None:
        r2 = conn.execute(
            "SELECT five_hour_reset, seven_day_reset FROM quota_snapshots "
            "WHERE five_hour_reset IS NOT NULL ORDER BY ts DESC LIMIT 1").fetchone()
        if r2 and r2["five_hour_reset"] and r2["five_hour_reset"] > now():
            d["five_hour_reset"] = r2["five_hour_reset"]
            d["seven_day_reset"] = d.get("seven_day_reset") or r2["seven_day_reset"]
    return d


def quota_series(conn: sqlite3.Connection, start: int, end: int, bucket: int = 300) -> list[dict[str, Any]]:
    """Both sources merged into one series, bucketed so they can't double-count.

    Within a bucket the highest reading wins: the two sources report the same
    underlying number, so small disagreements would otherwise read as a
    sawtooth of fake rises and falls.
    """
    out: dict[int, dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT ts, source, five_hour_pct, seven_day_pct FROM quota_snapshots "
        "WHERE ts BETWEEN ? AND ? ORDER BY ts", (start, end)
    ):
        b = out.setdefault(r["ts"] // bucket, {"ts": r["ts"], "five_hour_pct": None, "seven_day_pct": None,
                                               "sources": set()})
        b["ts"] = max(b["ts"], r["ts"])
        b["sources"].add(r["source"])
        for k in ("five_hour_pct", "seven_day_pct"):
            if r[k] is not None:
                b[k] = r[k] if b[k] is None else max(b[k], r[k])
    return [out[k] for k in sorted(out)]


def current_window(conn: sqlite3.Connection, lookback_h: float = 6) -> dict[str, Any]:
    """The 5-hour window in progress: its start, its end, and every reading in it.

    The window is five hours by definition, so when a reset time is known the
    start is exactly five hours before it. Without one (Claude Desktop samples
    carry no resets) the start is the first reading after the last reset drop,
    and the end is five hours after that, which is an estimate.
    """
    t = now()
    rows = [r for r in quota_series(conn, t - int(lookback_h * 3600), t) if r["five_hour_pct"] is not None]
    cut = 0
    for i in range(1, len(rows)):
        if rows[i]["five_hour_pct"] < rows[i - 1]["five_hour_pct"] - 5:
            cut = i
    rows = rows[cut:]
    latest = latest_quota(conn)
    reset = latest.get("five_hour_reset") if latest else None
    if reset:
        start, end = reset - 5 * 3600, reset
        rows = [r for r in rows if r["ts"] >= start]
    elif rows:
        start = rows[0]["ts"]
        end = start + 5 * 3600
    else:
        start = end = None
    # An estimated end that has already passed without a reset is disproved by
    # its own evidence: the window is still open, so it started later than the
    # first reading we have.
    stale = bool(end and not reset and end < t)
    return {"rows": [{"ts": r["ts"], "pct": r["five_hour_pct"]} for r in rows],
            "start": start, "end": end, "reset_known": bool(reset), "stale_estimate": stale}


def burn(conn: sqlite3.Connection, window_min: int = 45) -> dict[str, Any]:
    """Burn rate of the 5-hour window from recent snapshots, and a projection.

    The series is trimmed at the last reset: a drop of more than 5 points is a
    new window, and fitting a slope across one would be meaningless. Works
    from either source, so it does not depend on reset timestamps.
    """
    t = now()
    out: dict[str, Any] = {"pct_per_hour": None, "hits_limit_before_reset": False}
    latest = latest_quota(conn)
    q = lambda s: conn.execute(f"SELECT COALESCE(SUM(cost_usd),0) c FROM requests WHERE {s}", (t - 3600,)).fetchone()["c"]  # noqa: E731
    out["spend_last_hour"] = q("ts >= ?")
    out["spend_last_15m"] = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0) c FROM requests WHERE ts >= ?", (t - 900,)).fetchone()["c"]
    if not latest or latest["five_hour_pct"] is None:
        return out
    out["current_pct"] = latest["five_hour_pct"]
    out["reset_at"] = latest.get("five_hour_reset")
    out["reset_text"] = when(latest["five_hour_reset"]) if latest.get("five_hour_reset") else "unknown"
    # Age the percentage being reported, not the row it came back on.
    out["age_seconds"] = t - (latest.get("five_hour_pct_ts") or latest["ts"])
    out["window"] = current_window(conn)

    rows = [r for r in quota_series(conn, t - window_min * 60, t) if r["five_hour_pct"] is not None]
    cut = 0
    for i in range(1, len(rows)):
        if rows[i]["five_hour_pct"] < rows[i - 1]["five_hour_pct"] - 5:
            cut = i
    rows = rows[cut:]
    if len(rows) < 2:
        return out
    dp = rows[-1]["five_hour_pct"] - rows[0]["five_hour_pct"]
    dt_h = (rows[-1]["ts"] - rows[0]["ts"]) / 3600
    if dp <= 0 or dt_h < 5 / 60:
        return out
    slope = dp / dt_h
    out["pct_per_hour"] = slope
    hit_at = rows[-1]["ts"] + int((100 - rows[-1]["five_hour_pct"]) / slope * 3600)
    out["hit_at"] = hit_at
    out["hit_at_text"] = when(hit_at)
    if latest.get("five_hour_reset") and hit_at < latest["five_hour_reset"]:
        out["hits_limit_before_reset"] = True
    spend = conn.execute("SELECT COALESCE(SUM(cost_usd),0) c FROM requests WHERE ts BETWEEN ? AND ?",
                         (rows[0]["ts"], rows[-1]["ts"])).fetchone()["c"]
    if spend > 0:
        out["usd_per_pct"] = spend / dp
        out["headroom_usd"] = (100 - rows[-1]["five_hour_pct"]) * out["usd_per_pct"]
    return out


def attribution(conn: sqlite3.Connection, start: int, end: int, max_gap: int = 6 * 3600) -> dict[str, Any]:
    """Split 5-hour limit consumption into what Claude Code can account for and
    what it cannot.

    Each rise between consecutive readings is credited to Claude Code if any
    local request fell in that interval, and to "elsewhere" otherwise, which
    means claude.ai chat, the Desktop app, mobile, or Claude Code on another
    machine. Intervals longer than max_gap are too coarse to attribute and are
    reported separately rather than guessed at.
    """
    rows = [r for r in quota_series(conn, start, end) if r["five_hour_pct"] is not None]
    per_day: dict[str, dict[str, float]] = {}
    totals = {"claude_code": 0.0, "elsewhere": 0.0}
    unattributed = 0.0
    for a, b in zip(rows, rows[1:]):
        dp = b["five_hour_pct"] - a["five_hour_pct"]
        if dp <= 0:
            continue
        if b["ts"] - a["ts"] > max_gap:
            unattributed += dp
            continue
        n = conn.execute("SELECT COUNT(*) n FROM requests WHERE ts > ? AND ts <= ?",
                         (a["ts"], b["ts"])).fetchone()["n"]
        key = "claude_code" if n else "elsewhere"
        day = datetime.fromtimestamp(b["ts"]).strftime("%Y-%m-%d")
        per_day.setdefault(day, {"claude_code": 0.0, "elsewhere": 0.0})[key] += dp
        totals[key] += dp
    tot = totals["claude_code"] + totals["elsewhere"]
    return {"per_day": per_day, "totals": totals, "unattributed": unattributed,
            "points": tot, "claude_code_share": (totals["claude_code"] / tot) if tot else None}


def live(conn: sqlite3.Connection) -> dict[str, Any]:
    """The current 5-hour window: who is spending it, right now."""
    t = now()
    q = latest_quota(conn)
    since = (q["five_hour_reset"] - 5 * 3600) if q and q.get("five_hour_reset") else t - 5 * 3600
    since = max(since, t - 5 * 3600)
    sessions = [dict(r) for r in conn.execute(
        """SELECT r.session_id, s.title, s.project, SUM(r.cost_usd) cost, COUNT(*) requests,
                  MAX(r.ts) last_ts, MAX(r.input_tokens + r.cache_read + r.cache_write_5m + r.cache_write_1h) peak_ctx,
                  SUM(CASE WHEN r.ts >= ? THEN r.cost_usd ELSE 0 END) cost_15m,
                  GROUP_CONCAT(DISTINCT r.model) models
           FROM requests r LEFT JOIN cc_sessions s ON s.session_id = r.session_id
           WHERE r.ts >= ? GROUP BY r.session_id ORDER BY cost DESC""", (t - 900, since))]
    for s in sessions:
        last = conn.execute(
            "SELECT input_tokens + cache_read + cache_write_5m + cache_write_1h ctx FROM requests "
            "WHERE session_id=? ORDER BY ts DESC LIMIT 1", (s["session_id"],)).fetchone()
        s["last_ctx"] = last["ctx"] if last else 0
        s["active"] = (t - s["last_ts"]) < 600
    buckets = [0.0] * 20  # 15-minute buckets over the window
    for r in conn.execute("SELECT ts, cost_usd FROM requests WHERE ts >= ?", (since,)):
        i = min(19, (r["ts"] - since) // 900)
        buckets[i] += r["cost_usd"]
    return {"as_of": t, "window_start": since, "quota": q, "burn": burn(conn), "sessions": sessions,
            "buckets": buckets, "total": sum(s["cost"] for s in sessions),
            "attribution": attribution(conn, t - 7 * 86400, t)}


def _range(days: int) -> tuple[int, int]:
    end = now()
    start_dt = (datetime.fromtimestamp(end) - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int(start_dt.timestamp()), end


def summary(conn: sqlite3.Connection, days: int = 7) -> dict[str, Any]:
    """Everything the report renders, as plain dicts."""
    start, end = _range(days)
    prev_start = start - (end - start)
    q = lambda sql, *a: [dict(r) for r in conn.execute(sql, a)]  # noqa: E731

    totals = q(
        """SELECT COUNT(*) requests, COUNT(DISTINCT session_id) sessions,
                  COALESCE(SUM(cost_usd),0) cost, COALESCE(SUM(input_tokens),0) input_tokens,
                  COALESCE(SUM(output_tokens),0) output_tokens, COALESCE(SUM(cache_read),0) cache_read,
                  COALESCE(SUM(cache_write_5m+cache_write_1h),0) cache_write,
                  COALESCE(SUM(CASE WHEN agent_id IS NOT NULL THEN cost_usd END),0) subagent_cost
           FROM requests WHERE ts BETWEEN ? AND ?""", start, end)[0]
    prev = q("SELECT COALESCE(SUM(cost_usd),0) cost FROM requests WHERE ts >= ? AND ts < ?",
             prev_start, start)[0]

    rows = q("SELECT ts, model, cost_usd, output_tokens, input_tokens+cache_read+cache_write_5m+cache_write_1h AS ctx "
             "FROM requests WHERE ts BETWEEN ? AND ?", start, end)
    by_day: dict[str, dict[str, float]] = {}
    by_hour = [0.0] * 24
    by_model: dict[str, dict[str, float]] = {}
    for r in rows:
        dt = datetime.fromtimestamp(r["ts"])
        day = dt.strftime("%Y-%m-%d")
        by_day.setdefault(day, {})
        by_day[day][r["model"]] = by_day[day].get(r["model"], 0) + r["cost_usd"]
        by_hour[dt.hour] += r["cost_usd"]
        m = by_model.setdefault(r["model"], {"cost": 0, "requests": 0, "output": 0, "ctx": 0})
        m["cost"] += r["cost_usd"]; m["requests"] += 1; m["output"] += r["output_tokens"]; m["ctx"] += r["ctx"]
    days_list = []
    d = datetime.fromtimestamp(start)
    while d.timestamp() <= end:
        days_list.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)

    projects = q(
        """SELECT project, COUNT(DISTINCT session_id) sessions, COUNT(*) requests,
                  SUM(cost_usd) cost, SUM(output_tokens) output_tokens,
                  SUM(input_tokens+cache_read+cache_write_5m+cache_write_1h) ctx_tokens
           FROM requests WHERE ts BETWEEN ? AND ? GROUP BY project ORDER BY cost DESC""", start, end)

    sessions = q(
        """SELECT r.session_id, s.title, s.project, s.cc_cost_usd, MIN(r.ts) first_ts, MAX(r.ts) last_ts,
                  COUNT(*) requests, SUM(r.cost_usd) cost,
                  SUM(CASE WHEN r.agent_id IS NOT NULL THEN r.cost_usd ELSE 0 END) subagent_cost,
                  SUM(r.output_tokens) output_tokens, SUM(r.cache_read) cache_read,
                  SUM(r.input_tokens+r.cache_write_5m+r.cache_write_1h) uncached,
                  GROUP_CONCAT(DISTINCT r.model) models
           FROM requests r LEFT JOIN cc_sessions s ON s.session_id = r.session_id
           WHERE r.ts BETWEEN ? AND ? GROUP BY r.session_id ORDER BY cost DESC LIMIT 40""", start, end)
    for s in sessions:
        total_in = s["cache_read"] + s["uncached"]
        s["cache_hit"] = (s["cache_read"] / total_in) if total_in else None

    snaps = [{k: r[k] for k in ("ts", "five_hour_pct", "seven_day_pct")} for r in quota_series(conn, start, end)]
    sources = {r["source"] for r in conn.execute("SELECT DISTINCT source FROM quota_snapshots")}

    return {
        "days": days, "start": start, "end": end, "generated": now(),
        "totals": totals, "prev_cost": prev["cost"],
        "days_list": days_list, "by_day": by_day, "by_hour": by_hour, "by_model": by_model,
        "projects": projects, "sessions": sessions, "quota": latest_quota(conn), "snapshots": snaps,
        "attribution": attribution(conn, start, end), "quota_sources": sorted(sources),
    }


# ------------------------------------------------------------------ setup

SETTINGS_PATH = CLAUDE_DIR / "settings.json"


def statusline_command() -> str:
    """Absolute interpreter path, so the hook works whatever PATH the shell has."""
    return f"{_py()} -m coord_mcp.usage statusline"


def _py() -> str:
    """The interpreter path, always quoted.

    Claude Code hands a hook's command string to a shell. On Windows that shell
    eats backslashes as escapes, so an unquoted
    C:\\dev\\agents\\.venv\\Scripts\\python.exe arrives as
    C:devagents.venvScriptspython.exe and the hook dies with exit 127 on every
    tool call -- silently, because only exit 2 means anything to Claude Code.
    Quoting on 'has a space' is not enough; the backslashes are the problem.
    """
    return f'"{sys.executable}"'


def hook_wired(settings_path: Path = SETTINGS_PATH) -> bool:
    """True if our statusLine hook is in settings.json. Distinguishes 'not set
    up' from 'set up, but no session has sent a prompt since'."""
    try:
        s = json.loads(settings_path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return False
    return "coord_mcp.usage statusline" in json.dumps(s.get("statusLine") or {})


def guard_hooks() -> dict[str, list[dict[str, Any]]]:
    py = _py()
    return {
        "PreToolUse": [{"hooks": [{"type": "command", "command": f"{py} -m coord_mcp.guard pretooluse", "timeout": 10}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": f"{py} -m coord_mcp.guard userpromptsubmit", "timeout": 10}]}],
    }


def _merge_hooks(existing: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
    """Add our hook entries, replacing any earlier coord_mcp ones, keeping everything else."""
    hooks = dict(existing or {})
    changed = False
    for event, ours in guard_hooks().items():
        kept = [g for g in hooks.get(event, []) if "coord_mcp.guard" not in json.dumps(g)]
        new = kept + ours
        if new != hooks.get(event):
            changed = True
        hooks[event] = new
    return hooks, changed


def setup(settings_path: Path = SETTINGS_PATH, force: bool = False, guard: bool = True) -> dict[str, Any]:
    """Merge the statusLine hook (and the guard hooks) into Claude Code's
    settings.json. Idempotent. Backs up the file first. Refuses to replace a
    statusLine that isn't ours unless force=True, and says so.
    """
    settings: dict[str, Any] = {}
    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding="utf-8") or "{}")
    current = settings.get("statusLine")
    want = {"type": "command", "command": statusline_command()}
    out: dict[str, Any] = {"changed": False, "settings": str(settings_path), "command": want["command"]}
    changed = False
    if current != want:
        if current and "coord_mcp" not in json.dumps(current) and not force:
            out["kept_existing"] = current
            out["note"] = "an unrelated statusLine is configured; rerun with --force to replace it"
        else:
            settings["statusLine"] = want
            changed = True
    if guard:
        hooks, hchanged = _merge_hooks(settings.get("hooks"))
        if hchanged:
            settings["hooks"] = hooks
            changed = True
        out["guard"] = "wired"
    if not changed:
        return out
    if settings_path.exists():
        backup = settings_path.with_suffix(f".json.bak-{int(time.time())}")
        backup.write_text(settings_path.read_text(encoding="utf-8"), encoding="utf-8")
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    out["changed"] = True
    return out


def live_text(v: dict[str, Any]) -> str:
    q, b = v["quota"], v["burn"]
    lines = []
    if q and q.get("five_hour_pct") is not None:
        age = (v["as_of"] - q["ts"]) // 60
        lines.append(f"5h {q['five_hour_pct']:.0f}%  7d {q['seven_day_pct']:.0f}%  (as of {age}m ago, "
                     f"5h resets {when(q.get('five_hour_reset')) or 'unknown'})")
    else:
        lines.append("limits: no snapshot yet (statusLine hook records one on each prompt)")
    if b.get("pct_per_hour"):
        line = f"burn {b['pct_per_hour']:.1f}%/h"
        if b.get("hit_at_text"):
            line += f", 100% around {b['hit_at_text']}"
            line += "  << BEFORE RESET" if b["hits_limit_before_reset"] else " (after reset, fine)"
        if b.get("headroom_usd") is not None:
            line += f"; headroom about ${b['headroom_usd']:.0f} at recent rate"
        lines.append(line)
    lines.append(f"spend  last 15m ${b['spend_last_15m']:.2f}   last hour ${b['spend_last_hour']:.2f}   "
                 f"this window ${v['total']:.2f}")
    a = v.get("attribution")
    if a and a.get("claude_code_share") is not None:
        lines.append(f"limit split (last 7d)  Claude Code {a['claude_code_share'] * 100:.0f}%   "
                     f"elsewhere {100 - a['claude_code_share'] * 100:.0f}%  "
                     f"(chat, Desktop, mobile, other machines)")
    if v["sessions"]:
        lines.append("sessions in this window:")
        for s in v["sessions"][:10]:
            flag = "*" if s["active"] else " "
            title = (s["title"] or s["session_id"][:8])[:44]
            lines.append(f" {flag} ${s['cost']:>7.2f}  {s['requests']:>4} req  ctx {s['last_ctx'] / 1000:>4.0f}k  "
                         f"{title:<44} {s['project'] or ''}")
        lines.append(" * = active in the last 10 minutes; ctx = tokens sent per request right now")
    return "\n".join(lines)


# -------------------------------------------------------------------- cli


def _open(path: Path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", str(path)], check=False)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="coord-usage", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest", help="pull new transcript data into the board db")
    r = sub.add_parser("report", help="write the usage history page")
    r.add_argument("--days", type=int, default=7)
    r.add_argument("--out", default=os.environ.get("COORD_REPORT", str(Path.home() / ".coord" / "usage.html")))
    r.add_argument("--open", action="store_true")
    r.add_argument("--no-ingest", action="store_true")
    sub.add_parser("status", help="current limits and spend this week, as text")
    sub.add_parser("statusline", help="stdin hook for Claude Code statusLine")
    st = sub.add_parser("setup", help="wire the statusLine and guard hooks into ~/.claude/settings.json")
    st.add_argument("--force", action="store_true", help="replace an unrelated statusLine")
    st.add_argument("--no-guard", action="store_true", help="statusLine only, no PreToolUse/UserPromptSubmit hooks")
    sub.add_parser("live", help="the current 5-hour window: burn rate, projection, who is spending")
    sv = sub.add_parser("serve", help="serve the report on localhost, regenerated on every load")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--days", type=int, default=7)
    sv.add_argument("--open", action="store_true")
    a = p.parse_args(argv)

    if a.cmd == "setup":
        out = setup(force=a.force, guard=not a.no_guard)
        print(json.dumps(out, indent=2))
        if out["changed"]:
            print("done: restart any open Claude Code session to pick it up")
        return 0

    if a.cmd == "statusline":
        # Never raise, never print more than one line: this runs on every prompt.
        try:
            payload = json.loads(sys.stdin.read() or "{}")
            conn = connect()
            snap = record_statusline(conn, payload)
            snap["burn"] = burn(conn)
            from . import alerts
            alerts.check_and_alert(conn, snap, snap["burn"])
            print(statusline_text(snap, payload))
        except Exception:
            print("")
        return 0

    if a.cmd == "live":
        conn = connect()
        ingest(conn)
        print(live_text(live(conn)))
        return 0

    if a.cmd == "serve":
        from . import report
        conn = connect()
        return report.serve(conn, a.port, a.days, a.open)

    conn = connect()
    if a.cmd == "ingest":
        print(json.dumps(ingest(conn)))
        return 0
    if a.cmd == "status":
        ingest(conn)
        s = summary(conn, 7)
        qv = s["quota"]
        if qv:
            age = now() - qv["ts"]
            print(f"limits  5h {qv['five_hour_pct']:.0f}%  7d {qv['seven_day_pct']:.0f}%  (as of {age // 60}m ago)")
        else:
            print("limits  not wired: add the statusLine hook (see README)")
        t = s["totals"]
        print(f"7 days  ${t['cost']:.2f} est  {t['requests']} requests  {t['sessions']} sessions  "
              f"(prev 7d ${s['prev_cost']:.2f})")
        at = s["attribution"]
        if at["claude_code_share"] is not None:
            print(f"limit   Claude Code {at['claude_code_share'] * 100:.0f}%  "
                  f"elsewhere {100 - at['claude_code_share'] * 100:.0f}% (chat, Desktop, mobile, other machines)")
        for pr in s["projects"][:8]:
            print(f"  {pr['project'][:32]:<32} ${pr['cost']:>8.2f}  {pr['sessions']:>3} sessions")
        note = desktop_quota_note()
        if note:
            print("\nnote: " + note)
        return 0
    if a.cmd == "report":
        from . import report
        if not a.no_ingest:
            ingest(conn)
        path = report.write(conn, Path(a.out), days=a.days)
        print(path)
        if a.open:
            _open(path)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
