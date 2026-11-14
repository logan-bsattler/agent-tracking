"""Workbook rows: the per-row rulings an open Ben loose end points at.

Some Ben loose ends are not one question but a workbook of them ("Rule on the
112 rows in work/X.xlsx", "Keeper - Ben to rule" tabs). The precedent report
reads those workbooks and counts, per row, whether Ben has ruled that row
before. Counts only: nothing here writes a workbook, a loose end or the board.

Which workbook: every `*.xlsx` name in the loose end's text, resolved under
the client's folder (`<docs>/<client>/<path>`, then `<docs>/<client>/work/<name>`).
`*.bak-*` copies and Office lock files are skipped.

Which rows wait on a ruling, first that applies:
  1. sheets whose title asks for one ("Ben to rule", "Awaiting ruling",
     "QAD to confirm", "Needs ruling")
  2. rows whose Status column reads "Ruling"
  3. the only data sheet, when the workbook has just one besides "Read me"

The row key is the first key-like column (FDD #, Tracker ID, Cancelled FDD,
Item, Program, Field): an FDD or tracker number becomes "FDD <n>" (MAG's
tracker ids are FDD numbers), an ADP-/IF item or a .p program is kept as is.

A past ruling of a row, either:
  decision  a decision naming the key that is Ben's: not housekeeping, and not
            a master default or precedent ("Ben may override")
  workbook  the same key in another workbook of the client's work/ with a
            ruling column of Ben's (Ruling, Ben's decision, Settled or open;
            never a client's "decision source") filled with a settled value
            (not open, pending, TBD, needs ...)
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

RULE_SHEET = re.compile(r"to rule|ruling|to confirm|needs[- ]?rul|awaiting|to decide", re.I)
README = re.compile(r"read ?me|method|summary|notes?$", re.I)
KEY_COLS = [re.compile(p, re.I) for p in (
    r"^fdd\s*#?$", r"^tracker\s*id$", r"^cancelled fdd$", r"^item$", r"^program$", r"^fdd$", r"^field\b")]
FDD_COLS = re.compile(r"fdd|tracker", re.I)
# Ben's ruling column: "Ruling", "Ben's ruling", "Decision", "Settled or open".
# Not a client's ("Client decision source", "Already decided by MAG").
RULING_COL = re.compile(r"\b(ruling|ruled)\b|^(ben'?s? )?decision$|^settled", re.I)
NOT_RULING_COL = re.compile(r"needs|awaiting|to rule|waiting|\btab\b|\?|client|source|\bmag\b|dependency", re.I)
UNSETTLED = re.compile(r"\b(open|pending|tbd|needs|awaiting|not yet|to rule|unclear|unruled)\b|^(no|n|none|-+|\?+)$", re.I)
NOT_BENS = re.compile(r"ben may override", re.I)
HOUSEKEEPING = re.compile(r"^(auto-deferred|board cleanup|desk cleanup|cleanup|return dates|covered:)", re.I)
XLSX = re.compile(r"(?:[\w.&-]+[/\\])*[\w.&-]+\.xlsx\b", re.I)
ITEM = re.compile(r"\b(ADP-[A-Z0-9]+(?:-\d+)?|IF\d+)\b", re.I)
PROG = re.compile(r"\b[a-z][\w-]*\.p\b", re.I)
EXAMPLES = 5


def skip_file(p: Path) -> bool:
    return p.name.startswith("~$") or ".bak-" in p.name or p.suffix.lower() != ".xlsx"


def files_in(what: str, client: str | None, root: Path) -> list[tuple[str, Path | None]]:
    """(name as written, resolved path or None) for each workbook the text names."""
    out, seen = [], set()
    for m in XLSX.finditer(what or ""):
        raw = m.group(0).replace("\\", "/")
        if raw in seen:
            continue
        seen.add(raw)
        found = None
        if client:
            base = root / client
            for cand in (base / raw, base / "work" / Path(raw).name):
                if cand.is_file() and not skip_file(cand):
                    found = cand
                    break
        out.append((raw, found))
    return out


def _cell(v: Any) -> str:
    return "" if v is None else str(v).strip()


def key_of(value: Any, fdd_col: bool) -> str | None:
    s = _cell(value)
    if not s:
        return None
    if fdd_col:
        m = re.search(r"\d+", s)
        return f"FDD {int(m.group(0))}" if m else None
    m = ITEM.search(s)
    if m:
        return m.group(1).upper()
    m = PROG.search(s)
    if m:
        return m.group(0).lower()
    return None


def _header(rows: list[tuple]) -> tuple[int, list[str]] | None:
    """The header is the first of the top rows with three or more filled cells."""
    for i, r in enumerate(rows[:5]):
        cells = [_cell(v) for v in r]
        if sum(1 for c in cells if c) >= 3:
            return i, cells
    return None


def _key_col(head: list[str]) -> int | None:
    for rx in KEY_COLS:
        for i, h in enumerate(head):
            if rx.search(h):
                return i
    return None


def _sheets(path: Path, want=None) -> Iterable[tuple[str, list[str], int, list[tuple]]]:
    """(title, header, key column, data rows) per sheet that has a key column.
    `want(title, header)` is asked before the rows are read, so a scan skips
    the sheets it has no use for without parsing them."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            h = _header(list(ws.iter_rows(max_row=5, values_only=True)))
            if not h:
                continue
            i, head = h
            k = _key_col(head)
            if k is None or (want and not want(ws.title, head)):
                continue
            data = [r for r in ws.iter_rows(min_row=i + 2, values_only=True) if any(_cell(v) for v in r)]
            yield ws.title, head, k, data
    finally:
        wb.close()


def open_rows(path: Path) -> tuple[list[str], list[str]]:
    """(keys of the rows waiting on a ruling, sheets they came from)."""
    sheets = list(_sheets(path))
    picked = [s for s in sheets if RULE_SHEET.search(s[0])]
    keys, used = [], []
    if picked:
        for title, head, k, data in picked:
            fdd = bool(FDD_COLS.search(head[k]))
            keys += [x for r in data for x in [key_of(r[k] if k < len(r) else None, fdd)] if x]
            used.append(title)
        return keys, used
    for title, head, k, data in sheets:
        st = next((i for i, h in enumerate(head) if h.lower() == "status"), None)
        if st is None:
            continue
        fdd = bool(FDD_COLS.search(head[k]))
        hit = [x for r in data if st < len(r) and _cell(r[st]).lower().startswith("ruling")
               for x in [key_of(r[k] if k < len(r) else None, fdd)] if x]
        if hit:
            keys += hit
            used.append(f"{title} (Status=Ruling)")
    if keys:
        return keys, used
    data_sheets = [s for s in sheets if not README.search(s[0])]
    if len(data_sheets) == 1:
        title, head, k, data = data_sheets[0]
        fdd = bool(FDD_COLS.search(head[k]))
        return [x for r in data for x in [key_of(r[k] if k < len(r) else None, fdd)] if x], [title]
    return [], []


def _ruling_cols(head: list[str]) -> list[int]:
    return [i for i, h in enumerate(head) if RULING_COL.search(h) and not NOT_RULING_COL.search(h)]


def workbook_rulings(folder: Path, exclude: set[Path]) -> dict[str, str]:
    """key -> 'file:sheet' where another workbook records a settled ruling for it."""
    out: dict[str, str] = {}
    if not folder.is_dir():
        return out
    for p in sorted(folder.glob("*.xlsx")):
        if skip_file(p) or p.resolve() in exclude:
            continue
        try:
            for title, head, k, data in _sheets(p, lambda _t, h: bool(_ruling_cols(h))):
                cols = _ruling_cols(head)
                fdd = bool(FDD_COLS.search(head[k]))
                for r in data:
                    key = key_of(r[k] if k < len(r) else None, fdd)
                    if key and key not in out and any(
                            c < len(r) and _cell(r[c]) and not UNSETTLED.search(_cell(r[c])) for c in cols):
                        out[key] = f"{p.name}:{title}"
        except Exception:  # a locked or corrupt workbook is skipped, not fatal
            continue
    return out


def _key_rx(key: str) -> re.Pattern:
    if key.startswith("FDD "):
        n = key[4:]
        return re.compile(rf"(?:\bFDD[- ]?(?:[A-Z0-9]{{2,4}}-){{0,2}}#?|#|\btracker(?: id)? #?)0*{n}\b", re.I)
    return re.compile(rf"(?<![\w-]){re.escape(key)}(?![\w-])", re.I)


def ben_decisions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT id, statement, because FROM decisions")
            if r["statement"] and not HOUSEKEEPING.match(r["statement"])
            and not NOT_BENS.search(r["because"] or "")]


def scan(conn: sqlite3.Connection, loose_ends: list[dict[str, Any]], root: Path) -> dict[str, Any]:
    """Per open Ben loose end naming a workbook: its rows waiting on a ruling and
    how many a past ruling covers. `loose_ends` rows carry id, client, what."""
    entries, decisions, by_folder = [], None, {}
    tot = {"loose_ends": 0, "files_read": 0, "open_rows": 0, "by_decision": 0, "by_workbook": 0, "unruled": 0}
    for le in loose_ends:
        named = files_in(le["what"], le["client"], root)
        if not named:
            continue
        tot["loose_ends"] += 1
        for raw, path in named:
            e = {"follow_up": le["id"], "client": le["client"], "file": raw, "status": "read", "sheets": [],
                 "open_rows": 0, "by_decision": 0, "by_workbook": 0, "unruled": 0, "examples": []}
            entries.append(e)
            if path is None:
                e["status"] = "not found"
                continue
            try:
                keys, e["sheets"] = open_rows(path)
            except Exception as ex:
                e["status"] = f"unreadable: {type(ex).__name__}"
                continue
            tot["files_read"] += 1
            if not keys:
                e["status"] = "no ruling rows"
                continue
            if decisions is None:
                decisions = ben_decisions(conn)
            folder = path.parent
            if folder not in by_folder:
                by_folder[folder] = workbook_rulings(folder, {path.resolve()})
            wbr = by_folder[folder]
            for key in dict.fromkeys(keys):
                rx = _key_rx(key)
                d = next((d for d in decisions if rx.search(f"{d['statement']} {d['because'] or ''}")), None)
                if d:
                    e["by_decision"] += 1
                    cite = d["id"]
                elif key in wbr:
                    e["by_workbook"] += 1
                    cite = wbr[key]
                else:
                    e["unruled"] += 1
                    continue
                if len(e["examples"]) < EXAMPLES:
                    e["examples"].append({"key": key, "cite": cite})
            e["open_rows"] = e["by_decision"] + e["by_workbook"] + e["unruled"]
            for k in ("open_rows", "by_decision", "by_workbook", "unruled"):
                tot[k] += e[k]
    return {"root": str(root), "totals": tot, "files": entries}
