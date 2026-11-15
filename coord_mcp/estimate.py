"""The estimate band: Ben's approval of a sizing, settled in code when the
estimate is tight and small (Ben, 2026-11-15).

Workflow B1's `estimate_ok` step was Ben's on every case. Now, when a sizing
result lands with `estimate: {low_hours, high_hours}` (contracts._ESTIMATE)
and the range is inside the band, the Ben loose end on that task that asks
him to approve the estimate closes behind a decision

    Estimate auto-accepted <low>-<high> h (ratio r) on task <id>

whose `because` ends "auto band; Ben may override". Anything outside the
band, a result without the numbers, or an item that also asks him to send,
reply, or set status, dates or SharePoint fields stays his, as before. Only
the approval is automatic: nothing here touches SharePoint or a client.

The band, as working defaults Ben may change (meta keys, read and set with
`python -m coord_mcp.queue switch` / `queue setting`):

    auto_estimate        switch, default on
    estimate_max_ratio   high/low at most this, default 1.5 (the sizing limit)
    estimate_max_high    high end at most this many hours, default 24
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from typing import Any

SWITCH = "auto_estimate"
RULE = "estimate_band"

# The Ben item that asks for the approval: an approval verb (or the follow-up's
# `reason: approve` tag) and something that names the estimate.
_APPROVE = re.compile(r"\b(approv\w*|accept\w*|sign[- ]?off|ok)\b", re.I)
_ESTIMATEISH = re.compile(r"\b(estimates?|sizing|sized|hours?|quote)\b|\b\d+(\.\d+)?\s*[-–]\s*\d+(\.\d+)?\s*h\b", re.I)
# Outward or Ben-owned fields in the same item keep it his (rule 3: status,
# dates, percent complete and anything sent stay Ben's).
_OUTWARD = re.compile(r"\b(send|sent|sends|reply|replies|e-?mail|forward|post|publish|share|sharepoint|status|"
                      r"dates?|percent|due|deadline|client[- ]facing|goes to|before it goes)\b", re.I)


def band(conn: sqlite3.Connection) -> dict[str, float]:
    from . import queue
    return {"max_ratio": queue.setting(conn, "estimate_max_ratio"),
            "max_high": queue.setting(conn, "estimate_max_high")}


def check(low: Any, high: Any, max_ratio: float, max_high: float) -> tuple[bool, float | None, str]:
    """(inside, ratio, why). Missing or nonsense numbers are outside."""
    nums = all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (low, high))
    if not nums:
        return False, None, "no low/high hours"
    if low <= 0 or high < low:
        return False, None, f"range {low:g}-{high:g} h is not a range"
    r = high / low
    if r > max_ratio + 1e-9:
        return False, r, f"ratio {r:.2f} over {max_ratio:g}"
    if high > max_high + 1e-9:
        return False, r, f"high end {high:g} h over {max_high:g} h"
    return True, r, "inside the band"


def is_approval(what: str, reason: str | None = None) -> bool:
    if _OUTWARD.search(what) or not _ESTIMATEISH.search(what):
        return False
    return reason == "approve" or bool(_APPROVE.search(what))


def settle(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    """Inside the band, close this task's open estimate-approval Ben items
    behind one decision each. Caller holds the transaction and checks the switch."""
    from . import queue, store
    row = conn.execute("SELECT result FROM tasks WHERE id=?", (task_id,)).fetchone()
    try:
        est = (json.loads(row["result"]) if row and row["result"] else {}).get("estimate") or {}
    except (TypeError, ValueError):
        return []
    if not isinstance(est, dict):
        return []
    b = band(conn)
    low, high = est.get("low_hours"), est.get("high_hours")
    ok, r, _why = check(low, high, b["max_ratio"], b["max_high"])
    if not ok:
        return []
    reasons = queue.reasons_for(conn, task_id)
    out = []
    for f in conn.execute("SELECT id, who, what FROM follow_ups WHERE task_id=? AND state='open' "
                          "ORDER BY idx, rowid", (task_id,)).fetchall():
        if not queue.is_ben(f["who"]) or not is_approval(f["what"], reasons.get(f["id"])):
            continue
        dec = store.record_decision(
            conn, f"Estimate auto-accepted {low:g}-{high:g} h (ratio {r:.2f}) on task {task_id}",
            f"inside the estimate band (ratio <= {b['max_ratio']:g}, high <= {b['max_high']:g} h); approval only, "
            "status, dates and anything sent stay Ben's; auto band; Ben may override", task_id)
        store.resolve_follow_up(conn, f["id"], decision_id=dec["decision_id"])
        out.append({"follow_up": f["id"], "decision": dec["decision_id"], "low_hours": low,
                    "high_hours": high, "ratio": round(r, 2)})
    return out
