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
        size = path.stat().st_size
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
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"files_read": files, "requests_added": requests}


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
    r = conn.execute("SELECT * FROM quota_snapshots ORDER BY ts DESC LIMIT 1").fetchone()
    return dict(r) if r else None


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

    snaps = q("SELECT ts, five_hour_pct, seven_day_pct FROM quota_snapshots WHERE ts BETWEEN ? AND ? ORDER BY ts",
              start, end)

    return {
        "days": days, "start": start, "end": end, "generated": now(),
        "totals": totals, "prev_cost": prev["cost"],
        "days_list": days_list, "by_day": by_day, "by_hour": by_hour, "by_model": by_model,
        "projects": projects, "sessions": sessions, "quota": latest_quota(conn), "snapshots": snaps,
    }


# ------------------------------------------------------------------ setup

SETTINGS_PATH = CLAUDE_DIR / "settings.json"


def statusline_command() -> str:
    """Absolute interpreter path, so the hook works whatever PATH the shell has."""
    exe = sys.executable
    return f'"{exe}" -m coord_mcp.usage statusline' if " " in exe else f"{exe} -m coord_mcp.usage statusline"


def setup(settings_path: Path = SETTINGS_PATH, force: bool = False) -> dict[str, Any]:
    """Merge the statusLine hook into Claude Code's settings.json. Idempotent.

    Backs up the file first. Refuses to replace a statusLine that isn't ours
    unless force=True, and says so.
    """
    settings: dict[str, Any] = {}
    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding="utf-8") or "{}")
    current = settings.get("statusLine")
    want = {"type": "command", "command": statusline_command()}
    if current == want:
        return {"changed": False, "settings": str(settings_path), "command": want["command"]}
    if current and "coord_mcp" not in json.dumps(current) and not force:
        return {"changed": False, "settings": str(settings_path), "command": want["command"],
                "kept_existing": current,
                "note": "an unrelated statusLine is configured; rerun with --force to replace it"}
    if settings_path.exists():
        backup = settings_path.with_suffix(f".json.bak-{int(time.time())}")
        backup.write_text(settings_path.read_text(encoding="utf-8"), encoding="utf-8")
    settings["statusLine"] = want
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return {"changed": True, "settings": str(settings_path), "command": want["command"]}


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
    st = sub.add_parser("setup", help="wire the statusLine hook into ~/.claude/settings.json")
    st.add_argument("--force", action="store_true", help="replace an unrelated statusLine")
    a = p.parse_args(argv)

    if a.cmd == "setup":
        out = setup(force=a.force)
        print(json.dumps(out, indent=2))
        if out["changed"]:
            print("done: restart any open Claude Code session to see the status line")
        return 0

    if a.cmd == "statusline":
        # Never raise, never print more than one line: this runs on every prompt.
        try:
            payload = json.loads(sys.stdin.read() or "{}")
            conn = connect()
            snap = record_statusline(conn, payload)
            print(statusline_text(snap, payload))
        except Exception:
            print("")
        return 0

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
        for pr in s["projects"][:8]:
            print(f"  {pr['project'][:32]:<32} ${pr['cost']:>8.2f}  {pr['sessions']:>3} sessions")
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
