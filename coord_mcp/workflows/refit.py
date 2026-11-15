"""`python -m coord_mcp.workflows refit`: step limits from observed time.

With 193 of 220 open cases past their step limit (2026-11-15), "over limit"
flagged the whole board and meant nothing. This refits each step's sla to
the 75th percentile of the business time cases actually spent there, so
about one case in four runs over: an outlier, not the norm (Ben, 2026-11-15).

Observations per (workflow, step), over the last WINDOW_DAYS:
  - every finished stay: a case event at the step, to the next event at a
    different step (workflow_case_events, as propose.py reads them)
  - every open case's stay so far: at least that long, so leaving it out
    would bias a slow step low

Kept as they are:
  - steps with fewer than MIN_OBS observations
  - an sla whose `why` cites a ruling (anything but an earlier refit), such
    as A2's one-business-day target-date rule (decision d1c3d5216d4f)
The new limit is capped at CAP_DAYS business days unless the step already
allowed more, and only ever raises a limit: backfilled cases logged their
steps at one instant, so fast steps observe ~0 h and would refit too low. Dry run by default; --apply rewrites defs/<code>.json.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import definitions as defs

WINDOW_DAYS = 90
MIN_OBS = 5
PCT = 0.75
CAP_DAYS = 10
REFIT_TAG = "refit"
DEFS_DIR = Path(__file__).with_name("defs")


def pct(xs: list[int], p: float = PCT) -> float:
    """Linear-interpolated percentile; xs non-empty."""
    s = sorted(xs)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def sla_for(seconds: float, current: dict[str, Any] | None) -> dict[str, Any]:
    """The sla that fits `seconds`, in hours under a day, days above; capped."""
    from . import sla
    day = sla.day_seconds()
    cur = sla.limit_seconds(current) or 0
    cap = max(CAP_DAYS * day, cur)
    s = min(max(seconds, sla.HOUR), cap)
    if s < day:
        return {"business_hours": max(1, math.ceil(s / sla.HOUR))}
    return {"business_days": max(1, math.ceil(s / day))}


def pinned(st: dict[str, Any]) -> bool:
    """An sla a ruling set: its why says so and is not an earlier refit."""
    why = ((st.get("sla") or {}).get("why") or "").strip().lower()
    return bool(why) and not why.startswith(REFIT_TAG)


def observe(conn: sqlite3.Connection, t: int | None = None) -> dict[tuple[str, str], list[int]]:
    """{(workflow, step): [business seconds]} over the window."""
    from . import sla
    t = int(time.time()) if t is None else t
    since = t - WINDOW_DAYS * 86400
    if not sla._has_cases(conn):
        return {}
    cases = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, workflow, state, step, step_at FROM workflow_cases "
        "WHERE workflow IS NOT NULL AND (state='open' OR closed_at >= ?)", (since,))}
    out: dict[tuple[str, str], list[int]] = defaultdict(list)
    if not cases:
        return out
    prev: dict[str, Any] = {}
    ids = list(cases)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for e in conn.execute(f"SELECT case_id, step, event, ts FROM workflow_case_events WHERE case_id IN ({marks}) "
                              "ORDER BY case_id, ts, rowid", chunk):
            p = prev.get(e["case_id"])
            if p is not None and p["step"] and p["event"] != "closed" and e["step"] != p["step"] \
                    and e["ts"] >= since:
                out[(cases[e["case_id"]]["workflow"], p["step"])].append(sla.business_seconds(p["ts"], e["ts"]))
            prev[e["case_id"]] = e
    for c in cases.values():
        if c["state"] == "open" and c["step"] and c["step_at"]:
            out[(c["workflow"], c["step"])].append(sla.business_seconds(c["step_at"], t))
    return out


def plan(conn: sqlite3.Connection, t: int | None = None) -> list[dict[str, Any]]:
    """One row per step whose limit would change."""
    from . import sla
    obs = observe(conn, t)
    rows = []
    for code, d in sorted(defs.load().items()):
        for st in d["steps"]:
            xs = obs.get((code, st["key"]), [])
            if len(xs) < MIN_OBS or pinned(st):
                continue
            new = sla_for(pct(xs), st.get("sla"))
            old = {k: v for k, v in (st.get("sla") or {}).items() if k != "why"}
            # Raise only. Backfilled cases logged their steps at one instant, so
            # agent steps observe ~0 h; lowering on that would flag every case.
            if sla.limit_seconds(new) <= (sla.limit_seconds(old) or 0):
                continue
            rows.append({"workflow": code, "step": st["key"], "name": st["name"], "who": st["who"], "n": len(xs),
                         "p75_h": round(pct(xs) / sla.HOUR, 1), "old": old, "new": new})
    return rows


def apply(rows: list[dict[str, Any]], day: str | None = None, root: Path = DEFS_DIR) -> list[str]:
    """Write the new limits into defs/; returns the files changed."""
    day = day or time.strftime("%Y-%m-%d")
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by[r["workflow"]].append(r)
    changed = []
    for code, rs in sorted(by.items()):
        f = root / f"{code}.json"
        d = json.loads(f.read_text(encoding="utf-8"))
        for r in rs:
            st = next(s for s in d["steps"] if s["key"] == r["step"])
            st["sla"] = {**r["new"], "why": f"{REFIT_TAG} to p75 of {r['n']} observed stays ({day}; Ben 2026-11-15)"}
        errs = defs.validate(d)
        if errs:
            raise ValueError(f"{code}: {'; '.join(errs)}")
        f.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        changed.append(f.name)
    defs._load.cache_clear()
    return changed


def fmt(s: dict[str, Any]) -> str:
    if not s:
        return "-"
    return f"{s['business_hours']}h" if "business_hours" in s else f"{s['business_days']}bd"
