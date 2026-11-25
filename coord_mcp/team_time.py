"""Team Time: Ben's team's GTK timesheets, read-only.

A script in Ben's GlobalToolkit tab posts one period's rows to
/team-time/import on the dashboard; each post replaces that period. Nothing
here ever writes to GTK.

Stored as one JSON file next to the board db (~/.coord/team-time.json), not a
table, so live boards need no schema change.
"""

from __future__ import annotations

import calendar
import csv
import io
import json
import os
import re
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .db import default_db_path

# The only page allowed to post an import from a browser.
ALLOWED_ORIGINS = ("https://globaltoolkit.com",)
MAX_BODY = 5 * 1024 * 1024
_LOCK = threading.Lock()
_PERIOD = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_DAY = re.compile(r"\d{1,2}/\d{1,2}")


def path_for(conn: sqlite3.Connection | None = None) -> Path:
    """team-time.json beside the board db the connection is on."""
    f = conn.execute("PRAGMA database_list").fetchone()[2] if conn is not None else ""
    return Path(f or default_db_path()).parent / "team-time.json"


def load(p: Path) -> dict[str, Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"periods": {}}


def period_date(period: str) -> date:
    m = _PERIOD.fullmatch(period.strip())
    if not m:
        raise ValueError(f"period must be MM/DD/YYYY, got {period!r}")
    return date(int(m[3]), int(m[1]), int(m[2]))


def period_days(period: str) -> list[str]:
    """Every day in a semi-monthly period ending on the 15th or month end, as M/D."""
    end = period_date(period)
    first = 1 if end.day <= 15 else 16
    return [f"{end.month}/{d}" for d in range(first, end.day + 1)]


def _num(v: Any, what: str) -> float:
    if v is None or v == "":
        return 0.0
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError(f"{what} must be a number")
    try:
        return float(v)
    except ValueError:
        raise ValueError(f"{what} must be a number, got {v!r}") from None


def clean(payload: Any) -> dict[str, Any]:
    """The import body checked and normalised. Raises ValueError on a bad shape."""
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    period = str(payload.get("period") or "")
    end = period_date(period)
    period = f"{end.month:02d}/{end.day:02d}/{end.year}"
    people = payload.get("people")
    if not isinstance(people, list):
        raise ValueError("people must be a list")
    days = payload.get("days") or period_days(period)
    if not isinstance(days, list) or not all(isinstance(d, str) and _DAY.fullmatch(d) for d in days):
        raise ValueError("days must be a list of M/D strings")
    out = []
    for i, p in enumerate(people):
        if not isinstance(p, dict) or not str(p.get("name") or "").strip():
            raise ValueError(f"people[{i}] needs a name")
        rows = p.get("rows") or []
        if not isinstance(rows, list):
            raise ValueError(f"people[{i}].rows must be a list")
        clean_rows = []
        for j, r in enumerate(rows):
            if not isinstance(r, dict):
                raise ValueError(f"people[{i}].rows[{j}] must be an object")
            hrs = r.get("hours") or {}
            if not isinstance(hrs, dict) or not all(_DAY.fullmatch(str(k)) for k in hrs):
                raise ValueError(f"people[{i}].rows[{j}].hours must map M/D to hours")
            hrs = {str(k): _num(v, f"hours[{k}]") for k, v in hrs.items() if _num(v, f"hours[{k}]")}
            ppa = _num(r.get("ppa"), "ppa")
            total = r.get("total")
            clean_rows.append({
                "charge_code": str(r.get("charge_code") or "").strip(),
                "phase": str(r.get("phase") or "").strip(),
                "description": str(r.get("description") or "").strip(),
                "hours": hrs, "ppa": ppa,
                "total": _num(total, "total") if total not in (None, "") else sum(hrs.values()) + ppa,
            })
        out.append({"name": str(p["name"]).strip(), "user_id": str(p.get("user_id") or ""),
                    "status": (str(p["status"]).strip() or None) if p.get("status") else None,
                    "total": _num(p.get("total"), "total"), "rows": clean_rows})
    return {"period": period, "pulled_at": str(payload.get("pulled_at") or ""), "days": days, "people": out}


def save(p: Path, payload: Any) -> dict[str, Any]:
    """Replace one period's rows. Returns what was stored."""
    entry = clean(payload)
    entry["imported_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _LOCK:
        data = load(p)
        data.setdefault("periods", {})[entry["period"]] = entry
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    return entry


def periods(data: dict[str, Any]) -> list[str]:
    """Stored periods, newest first."""
    return sorted(data.get("periods", {}), key=period_date, reverse=True)


def from_csv(text: str, period: str) -> dict[str, Any]:
    """The Internal session's CSV export (person, status, period_total,
    charge_code, description, day_hours "11/4:1 11/11:1", ppa, row_total) as
    an import body. A person with one blank row has no sheet."""
    people: dict[str, dict[str, Any]] = {}
    for r in csv.DictReader(io.StringIO(text)):
        p = people.setdefault(r["person"], {"name": r["person"], "status": r["status"] or None,
                                            "total": r["period_total"] or 0, "rows": []})
        if not r["charge_code"]:
            continue
        hrs = dict(x.split(":", 1) for x in (r["day_hours"] or "").split())
        p["rows"].append({"charge_code": r["charge_code"], "phase": r.get("phase") or "",
                          "description": r["description"], "hours": hrs,
                          "ppa": r["ppa"] or 0, "total": r["row_total"]})
    return {"period": period, "pulled_at": "", "days": period_days(period), "people": list(people.values())}


def to_csv(entry: dict[str, Any]) -> str:
    """One line per person and timesheet row, a column per day. A person with
    no sheet gets one line with zero hours."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    days = entry["days"]
    w.writerow(["period", "person", "status", "period_total", "charge_code", "phase", "description",
                *days, "ppa", "row_total"])
    for p in entry["people"]:
        base = [entry["period"], p["name"], p["status"] or "", f"{p['total']:g}"]
        if not p["rows"]:
            w.writerow([*base, "", "", "", *([""] * len(days)), "", "0"])
        for r in p["rows"]:
            w.writerow([*base, r["charge_code"], r["phase"], r["description"],
                        *(f"{r['hours'][d]:g}" if d in r["hours"] else "" for d in days),
                        f"{r['ppa']:g}", f"{r['total']:g}"])
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    """python -m coord_mcp.team_time load-csv <file.csv> <MM/DD/YYYY>"""
    import sys
    a = sys.argv[1:] if argv is None else argv
    if len(a) != 3 or a[0] != "load-csv":
        print(main.__doc__)
        return 2
    target = path_for(None)
    e = save(target, from_csv(Path(a[1]).read_text(encoding="utf-8-sig"), a[2]))
    print(f"{e['period']}: {len(e['people'])} people -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
