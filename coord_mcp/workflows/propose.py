"""`python -m coord_mcp.workflows propose`: the weekly workflow miner.

Finds, over the last WINDOW_DAYS of the board:

  new    untagged cases (a root task no rule in tag.py places) or untagged
         intents with no task, grouped by tag.pattern; MIN_CASES or more
         of one pattern make a candidate workflow
  drift  for a catalog workflow, in MIN_CASES or more of its cases:
           lacks  a task kind its definition has no step for (case_step
                  stays empty), e.g. a reply in a status request
           skips  an agent step most cases pass without
           stalls open cases past a step's time limit, where the cases that
                  left the step took longer than the limit says

Each finding becomes a proposal: a full JSON definition (new, or the
current one changed), 3-5 example task ids and the reasons, saved to
proposed/<id>.json and checked by definitions.validate. All drift for one
workflow is one proposal, so Ben gets one item per workflow, not per row.

`--file` puts up to WEEKLY_CAP proposals a week in Ben's queue (the most
cases first) as Ben items with no task: an intent from this miner, resolved
at once with a loose end for Ben (store.ben_item), linking
/workflows/proposed/<id>. A proposal Ben dropped, deferred or still has open
is never filed again under the same id.

adopt() moves an approved proposal into defs/, records a decision, resolves
the item and backfills its cases. A new workflow lands with engine false.
reject() and defer() close or defer the item. The dashboard loop runs
weekly() once a week (Monday 06:30 local) behind the `workflow_propose`
switch.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from . import CATALOG, name
from . import definitions as defs
from .tag import pattern

PROPOSED_DIR = Path(__file__).with_name("proposed")
WINDOW_DAYS = 90
MIN_CASES = 5
SKIP_SHARE = 0.6
WEEKLY_CAP = 3
CLIENT = "Workflows"
SOURCE = "workflow-miner"
BASE_URL = os.environ.get("COORD_DASHBOARD_URL", "http://127.0.0.1:8765")
META_WEEK = "workflow_propose_week"
RUN_AT = (0, 6, 30)  # Monday 06:30 local

DDL = """
CREATE TABLE IF NOT EXISTS workflow_proposals (
  id            TEXT PRIMARY KEY,
  code          TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('new','change')),
  name          TEXT NOT NULL,
  cases         INTEGER NOT NULL,
  state         TEXT NOT NULL CHECK (state IN ('drafted','filed','adopted','rejected','deferred')),
  follow_up_id  TEXT,
  intent_id     TEXT,
  decision_id   TEXT,
  created_at    INTEGER NOT NULL,
  updated_at    INTEGER NOT NULL,
  filed_at      INTEGER
);
"""

STEP_KEY = {"draft_reply": "reply", "code_change": "code", "deliverable": "document", "dod_review": "dod_review",
            "investigation": "investigate", "data_pull": "data", "review": "review"}
STEP_NAME = {"draft_reply": "Draft reply", "code_change": "Code change", "deliverable": "Deliverable",
             "dod_review": "DoD review", "investigation": "Investigate", "data_pull": "Data pull",
             "review": "Review"}


def ensure(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_proposals'").fetchone() is None:
        conn.executescript(DDL)


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _sha(d: dict[str, Any] | None) -> str | None:
    return hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()[:12] if d else None


def _sla_for(seconds: list[int]) -> dict[str, Any]:
    """A time limit that fits the median business time observed."""
    from .sla import DAY, HOUR
    m = statistics.median(seconds) if seconds else DAY
    if m < DAY:
        return {"business_hours": max(1, math.ceil(m / HOUR))}
    return {"business_days": max(1, math.ceil(m / DAY))}


def _examples(ids: list[str]) -> list[str]:
    return list(dict.fromkeys(ids))[:5]


# ------------------------------------------------------------------ finding

def _drift(conn: sqlite3.Connection, since: int, t: int) -> list[dict[str, Any]]:
    from . import sla
    if not _has(conn, "workflow_cases"):
        return []
    cases = {r["id"]: dict(r) for r in conn.execute(
        "SELECT * FROM workflow_cases WHERE workflow IS NOT NULL AND created_at >= ?", (since,))}
    tasks: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in conn.execute("SELECT id, parent_id, kind, state, case_id, case_step, created_at, completed_at "
                          "FROM tasks WHERE case_id IS NOT NULL ORDER BY created_at, id"):
        if r["case_id"] in cases:
            tasks[r["case_id"]].append(dict(r))
    step_of = {x["id"]: x["case_step"] for ts in tasks.values() for x in ts}
    by_wf: dict[str, list[str]] = defaultdict(list)
    for cid, c in cases.items():
        by_wf[c["workflow"]].append(cid)
    out: list[dict[str, Any]] = []
    for code, cids in sorted(by_wf.items()):
        d = defs.get(code)
        if not d:
            continue
        steps = d["steps"]
        keys = [s["key"] for s in steps]
        # lacks: a kind no step takes, in MIN_CASES cases or more
        lack: dict[str, dict[str, Any]] = defaultdict(lambda: {"cases": set(), "tasks": [], "after": Counter(),
                                                               "secs": []})
        for cid in cids:
            for x in tasks[cid]:
                if x["case_step"] is None and defs.step_for(d, x["kind"], {}) is None:
                    g = lack[x["kind"]]
                    g["cases"].add(cid)
                    g["tasks"].append(x["id"])
                    g["after"][step_of.get(x["parent_id"] or "") or ""] += 1
                    if x["completed_at"]:
                        g["secs"].append(sla.business_seconds(x["created_at"], x["completed_at"]))
        for kind, g in lack.items():
            if len(g["cases"]) >= MIN_CASES:
                after = next((k for k, _ in g["after"].most_common() if k in keys), None)
                out.append({"type": "lacks", "workflow": code, "kind": kind, "cases": sorted(g["cases"]),
                            "examples": _examples(g["tasks"]), "after": after, "secs": g["secs"],
                            "why": f"{len(g['cases'])} {code} cases had a {kind} task that no step of the "
                                   f"definition takes"})
        # skips: an agent step that most cases reaching past it never had
        done = [cid for cid in cids if cases[cid]["state"] == "done"]
        for i, st in enumerate(steps):
            if st["who"] != "agent" or st.get("optional") or i == 0:
                continue
            past, without = [], []
            for cid in done:
                idx = {keys.index(x["case_step"]) for x in tasks[cid] if x["case_step"] in keys}
                if idx and max(idx) > i:
                    past.append(cid)
                    if i not in idx:
                        without.append(cid)
            if len(without) >= MIN_CASES and len(without) >= SKIP_SHARE * len(past):
                ex = [tasks[c][0]["id"] for c in without if tasks[c]]
                out.append({"type": "skips", "workflow": code, "step": st["key"], "cases": sorted(without),
                            "examples": _examples(ex),
                            "why": f"{len(without)} of {len(past)} finished {code} cases went past "
                                   f"'{st['name']}' without it"})
        # stalls: open cases past a step's limit, where cases that left the step took longer
        over: dict[str, list[str]] = defaultdict(list)
        for cid in cids:
            c = cases[cid]
            s = sla.status(c, t)
            if s and s["over"] > 0:
                over[c["step"]].append(cid)
        if over:
            dwell: dict[str, list[int]] = defaultdict(list)
            prev: dict[str, sqlite3.Row] = {}
            marks = ",".join("?" * len(cids))
            for e in conn.execute(f"SELECT case_id, step, event, ts FROM workflow_case_events WHERE case_id IN ({marks}) "
                                  "ORDER BY case_id, ts, rowid", cids):
                p = prev.get(e["case_id"])
                if p is not None and p["step"] and p["event"] != "closed" and e["step"] != p["step"]:
                    dwell[p["step"]].append(sla.business_seconds(p["ts"], e["ts"]))
                prev[e["case_id"]] = e
            for key, stuck in over.items():
                st = steps[keys.index(key)] if key in keys else None
                lim = sla.limit_seconds((st or {}).get("sla"))
                left = dwell.get(key, [])
                if len(stuck) < MIN_CASES or not lim or len(left) < 3 or statistics.median(left) <= lim:
                    continue
                ex = [cases[c]["root_task_id"] for c in stuck]
                out.append({"type": "stalls", "workflow": code, "step": key, "cases": sorted(stuck),
                            "examples": _examples(ex), "secs": left,
                            "why": f"{len(stuck)} open {code} cases are past the '{st['name']}' limit, and the "
                                   f"{len(left)} that left that step took a median "
                                   f"{statistics.median(left) / 3600:.0f} business hours"})
    return out


def _new(conn: sqlite3.Connection, since: int) -> list[dict[str, Any]]:
    from .mine import tag_board
    b = tag_board(conn)
    groups: dict[str, dict[str, Any]] = defaultdict(lambda: {"cases": [], "kinds": Counter(), "sources": Counter(),
                                                             "titles": [], "intents": []})
    for c in b["cases"].values():
        if c["workflow"] is None and c["start"] >= since:
            g = groups[pattern(c["title"], c["kind"])]
            g["cases"].append(c["root"])
            g["kinds"][c["kind"]] += 1
            g["titles"].append(c["title"])
            if c["source"]:
                g["sources"][c["source"]] += 1
    created = {r["id"]: r["created_at"] for r in conn.execute("SELECT id, created_at FROM intents")}
    for iid, i in b["intents"].items():
        if i["workflow"] is None and created.get(iid, 0) >= since:
            g = groups[pattern(i["summary"] or "", f"intent {i['source']}")]
            g["intents"].append(iid)
            g["sources"][i["source"]] += 1
            g["titles"].append(i["summary"] or "")
    out = []
    for pat, g in groups.items():
        n = len(g["cases"]) + len(g["intents"])
        if n < MIN_CASES:
            continue
        out.append({"type": "new", "pattern": pat, "cases": g["cases"] + g["intents"],
                    "examples": _examples(g["cases"] or g["intents"]), "kinds": dict(g["kinds"]),
                    "sources": dict(g["sources"]), "titles": g["titles"][:5],
                    "why": f"{n} untagged cases or intents share the pattern '{pat}'"})
    return out


def find(conn: sqlite3.Connection, t: int | None = None) -> list[dict[str, Any]]:
    t = t or int(time.time())
    since = t - WINDOW_DAYS * 86400
    return _drift(conn, since, t) + _new(conn, since)


# ------------------------------------------------------------------ drafting

def _next_code(group: str, taken: set[str]) -> str:
    n = 1
    while f"{group}{n}" in taken:
        n += 1
    return f"{group}{n}"


def _draft_new(f: dict[str, Any], conn: sqlite3.Connection, taken: set[str]) -> dict[str, Any]:
    kinds = f["kinds"]
    top = max(kinds, key=kinds.get) if kinds else None
    group = ("B" if top == "code_change" else "C" if top in ("deliverable", "data_pull", "review")
             else "A" if f["sources"] else "D")
    code = _next_code(group, taken)
    words = f["pattern"].split(": ", 1)[-1]
    wname = f"{words.capitalize()} ({top or 'intent'})"
    steps: list[dict[str, Any]] = []
    if f["sources"]:
        steps.append({"key": "ingest", "name": "Ingest", "who": "code", "kinds": [], "gate": "triage",
                      "next": "master tasks it", "sla": {"business_hours": 4}})
    # Agent steps: the case's task kinds in the order they first appear.
    order: Counter = Counter()
    secs: dict[str, list[int]] = defaultdict(list)
    roots = [c for c in f["cases"] if conn.execute("SELECT 1 FROM tasks WHERE id=?", (c,)).fetchone()]
    from . import sla
    for rid in roots:
        seen = []
        for r in conn.execute("SELECT kind, created_at, completed_at FROM tasks WHERE id=? OR case_id="
                              "(SELECT case_id FROM tasks WHERE id=?) ORDER BY created_at", (rid, rid)):
            if r["kind"] not in seen:
                seen.append(r["kind"])
                if r["completed_at"]:
                    secs[r["kind"]].append(sla.business_seconds(r["created_at"], r["completed_at"]))
        for i, k in enumerate(seen):
            order[k] += i
    used = [k for k, _ in sorted(order.items(), key=lambda kv: kv[1] / max(1, sum(1 for _ in roots)))]
    for k in used or ["investigation"]:
        steps.append({"key": STEP_KEY.get(k, k), "name": STEP_NAME.get(k, k), "who": "agent", "kinds": [k],
                      "gate": "result validates", "next": "result lands", "sla": _sla_for(secs.get(k, []))})
    steps.append({"key": "ben", "name": "Ben reads", "who": "ben", "kinds": [], "gate": "Ben",
                  "next": "case done", "sla": {"business_days": 2}})
    first = re.escape(words.split()[0]) if words.split() else "."
    rest = r"\W+".join(re.escape(w) for w in words.split()[1:])
    tag = {"title_re": rf"\b{first}\b" + (rf"\W+{rest}\b" if rest else "")}
    if kinds:
        tag["kinds"] = sorted(kinds)
    if f["sources"]:
        tag["sources"] = sorted(f["sources"])
    return {"code": code, "name": wname, "group": group, "engine": False,
            "trigger": f"untagged work matching '{f['pattern']}'",
            "notes": f"Drafted by the workflow miner on {time.strftime('%Y-%m-%d')}; rename and edit before adopting.",
            "tag": tag, "steps": steps}


def _draft_change(code: str, fs: list[dict[str, Any]]) -> dict[str, Any]:
    d = json.loads(json.dumps(defs.get(code)))
    # A spawn without `after` waits on the step just before it. Pin each one
    # to that step now, so a step inserted below cannot become what it waits
    # on (an optional step most cases skip would stop the engine silently).
    for j, st in enumerate(d["steps"]):
        if st.get("spawn") and not st["spawn"].get("after") and j > 0:
            st["spawn"]["after"] = d["steps"][j - 1]["key"]
    notes = []
    for f in fs:
        keys = [s["key"] for s in d["steps"]]
        if f["type"] == "lacks":
            key = STEP_KEY.get(f["kind"], f["kind"])
            while key in keys:
                key += "_2"
            if f["after"] in keys:
                at = keys.index(f["after"]) + 1
            else:  # before the trailing Ben steps
                at = len(d["steps"])
                while at > 0 and d["steps"][at - 1]["who"] == "ben":
                    at -= 1
            d["steps"].insert(at, {"key": key, "name": STEP_NAME.get(f["kind"], f["kind"]), "who": "agent",
                                   "kinds": [f["kind"]], "gate": "result validates", "next": "result lands",
                                   "optional": True, "sla": _sla_for(f["secs"])})
            notes.append(f"added step {key} ({f['kind']})")
        elif f["type"] == "skips":
            st = d["steps"][keys.index(f["step"])]
            st["optional"] = True
            notes.append(f"step {f['step']} optional")
        elif f["type"] == "stalls":
            st = d["steps"][keys.index(f["step"])]
            new = _sla_for(f["secs"])
            new["why"] = f"refit to the median observed time ({time.strftime('%Y-%m-%d')})"
            st["sla"] = new
            notes.append(f"step {f['step']} limit refit")
    d["notes"] = ((d.get("notes") or "") + f" Miner {time.strftime('%Y-%m-%d')}: " + "; ".join(notes) + ".").strip()
    return d


def draft(conn: sqlite3.Connection, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One proposal per drifting workflow and per new cluster, most cases first."""
    out = []
    by_wf: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in findings:
        if f["type"] != "new":
            by_wf[f["workflow"]].append(f)
    for code, fs in sorted(by_wf.items()):
        sig = sorted(f"{f['type']}:{f.get('kind') or f.get('step')}" for f in fs)
        pid = f"{code}-{hashlib.sha1('|'.join(sig).encode()).hexdigest()[:6]}"
        cases = sorted({c for f in fs for c in f["cases"]})
        out.append({"id": pid, "code": code, "kind": "change", "name": name(code), "cases": len(cases),
                    "base_sha": _sha(defs.get(code)), "signature": sig,
                    "examples": _examples([e for f in fs for e in f["examples"]]),
                    "why": [f["why"] for f in fs], "definition": _draft_change(code, fs)})
    taken = set(CATALOG)
    for f in findings:
        if f["type"] != "new":
            continue
        d = _draft_new(f, conn, taken)
        taken.add(d["code"])
        pid = f"{d['code']}-new-{hashlib.sha1(f['pattern'].encode()).hexdigest()[:6]}"
        out.append({"id": pid, "code": d["code"], "kind": "new", "name": d["name"], "cases": len(f["cases"]),
                    "base_sha": None, "signature": [f"new:{f['pattern']}"], "examples": f["examples"],
                    "why": [f["why"]], "titles": f["titles"], "definition": d})
    for p in out:
        cat = {**CATALOG, p["code"]: (p["definition"]["name"], p["definition"].get("group", ""))}
        p["errors"] = defs.validate(p["definition"], catalog=cat)
    out.sort(key=lambda p: (-p["cases"], p["id"]))
    return out


def write(proposals: list[dict[str, Any]], t: int | None = None, folder: Path | None = None) -> list[Path]:
    """proposed/<id>.json per sound proposal. Rewrites a file only when its
    content changed, so a weekly run over the same board is quiet in git."""
    folder = folder or PROPOSED_DIR
    folder.mkdir(exist_ok=True)
    paths = []
    for p in proposals:
        if p["errors"]:
            continue
        f = folder / f"{p['id']}.json"
        body = {**p, "generated_at": t or int(time.time())}
        if f.exists():
            try:
                old = json.loads(f.read_text(encoding="utf-8"))
                if {**old, "generated_at": 0} == {**body, "generated_at": 0}:
                    paths.append(f)
                    continue
            except ValueError:
                pass
        f.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8", newline="\n")
        paths.append(f)
    return paths


def load_proposal(pid: str, folder: Path | None = None) -> dict[str, Any] | None:
    if not re.fullmatch(r"[A-Za-z0-9+_-]{1,60}", pid or ""):
        return None
    f = (folder or PROPOSED_DIR) / f"{pid}.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_proposals(folder: Path | None = None) -> list[dict[str, Any]]:
    out = []
    for f in sorted((folder or PROPOSED_DIR).glob("*.json")):
        p = load_proposal(f.stem, folder)
        if p:
            out.append(p)
    return sorted(out, key=lambda p: (-p["cases"], p["id"]))


# ------------------------------------------------------------------ filing

def record(conn: sqlite3.Connection, pid: str) -> sqlite3.Row | None:
    ensure(conn)
    return conn.execute("SELECT * FROM workflow_proposals WHERE id=?", (pid,)).fetchone()


def _blocked(conn: sqlite3.Connection, pid: str) -> str | None:
    """Why a proposal must not be filed again, or None."""
    r = record(conn, pid)
    if r is None:
        return None
    if r["state"] in ("adopted", "rejected", "deferred"):
        return r["state"]
    if r["follow_up_id"]:
        fu = conn.execute("SELECT state FROM follow_ups WHERE id=?", (r["follow_up_id"],)).fetchone()
        if fu is not None:
            return "open in Ben's queue" if fu["state"] == "open" else fu["state"]
    return None


def url(pid: str) -> str:
    return f"{BASE_URL}/workflows/proposed/{pid}"


def item_text(p: dict[str, Any]) -> str:
    return f"Approve workflow {p['id']} {p['name']} ({p['cases']} cases): {url(p['id'])}"


def file(conn: sqlite3.Connection, proposals: list[dict[str, Any]], t: int | None = None,
         dry_run: bool = False) -> dict[str, Any]:
    """Put the top proposals in Ben's queue, at most WEEKLY_CAP a rolling week."""
    from .. import store
    from ..db import now
    t = t or now()
    ensure(conn)
    recent = conn.execute("SELECT COUNT(*) FROM workflow_proposals WHERE filed_at >= ?",
                          (t - 7 * 86400,)).fetchone()[0]
    room = max(0, WEEKLY_CAP - recent)
    filed, skipped = [], []
    for p in proposals:
        if p.get("errors"):
            skipped.append({"id": p["id"], "why": "does not validate: " + "; ".join(p["errors"][:2])})
            continue
        why = _blocked(conn, p["id"])
        if why:
            skipped.append({"id": p["id"], "why": why})
            continue
        if room <= 0:
            skipped.append({"id": p["id"], "why": f"over the {WEEKLY_CAP} a week cap"})
            continue
        room -= 1
        if dry_run:
            filed.append({"id": p["id"], "what": item_text(p)})
            continue
        it = store.propose_intent(conn, f"Workflow proposal {p['id']} {p['name']} ({p['cases']} cases)"[:200],
                                  SOURCE, context=f"proposal {p['id']}; {url(p['id'])}", origin="dashboard",
                                  trust="trusted")
        bi = store.ben_item(conn, it["intent_id"], item_text(p)[:300], CLIENT, ref=url(p["id"]))
        store.resolve_intent(conn, it["intent_id"], note=f"Ben item {bi['follow_up_id']}; workflow proposal {p['id']}")
        conn.execute("""INSERT INTO workflow_proposals(id, code, kind, name, cases, state, follow_up_id, intent_id,
                        created_at, updated_at, filed_at) VALUES (?,?,?,?,?, 'filed', ?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET state='filed', follow_up_id=excluded.follow_up_id,
                        intent_id=excluded.intent_id, cases=excluded.cases, updated_at=excluded.updated_at,
                        filed_at=excluded.filed_at""",
                     (p["id"], p["code"], p["kind"], p["name"], p["cases"], bi["follow_up_id"], it["intent_id"],
                      t, t, t))
        filed.append({"id": p["id"], "follow_up_id": bi["follow_up_id"], "what": item_text(p)})
    if not dry_run:
        conn.commit()
    return {"filed": filed, "skipped": skipped}


# ------------------------------------------------------------------ Ben's answer

def _close_item(conn: sqlite3.Connection, r: sqlite3.Row | None, decision_id: str, defer: bool = False) -> None:
    from .. import store
    if r is None or not r["follow_up_id"]:
        return
    fu = conn.execute("SELECT state FROM follow_ups WHERE id=?", (r["follow_up_id"],)).fetchone()
    if fu is not None and fu["state"] in ("open", "deferred") and not (defer and fu["state"] == "deferred"):
        store.resolve_follow_up(conn, r["follow_up_id"], decision_id=decision_id, defer=defer)


def _upsert(conn: sqlite3.Connection, p: dict[str, Any], state: str, decision_id: str | None) -> None:
    from ..db import now
    t = now()
    conn.execute("""INSERT INTO workflow_proposals(id, code, kind, name, cases, state, decision_id, created_at,
                    updated_at) VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET state=excluded.state, decision_id=excluded.decision_id,
                    updated_at=excluded.updated_at""",
                 (p["id"], p["code"], p["kind"], p["name"], p["cases"], state, decision_id, t, t))


def find_id(conn: sqlite3.Connection, ref: str) -> str:
    """A proposal id from a proposal id or its follow-up id."""
    ensure(conn)
    r = conn.execute("SELECT id FROM workflow_proposals WHERE follow_up_id=?", (ref,)).fetchone()
    return r["id"] if r else ref


def adopt(conn: sqlite3.Connection, ref: str, folder: Path | None = None,
          defs_dir: Path | None = None) -> dict[str, Any]:
    """Move an approved proposal into defs/, record the decision, resolve
    Ben's item and backfill its cases. A new workflow lands with engine
    false; a change keeps the workflow's engine setting (added steps never
    spawn)."""
    from .. import store
    from . import cases as cs
    pid = find_id(conn, ref)
    r = record(conn, pid)
    if r is not None and r["state"] not in ("drafted", "filed"):
        # A rejected or deferred proposal's file may still be on disk; its
        # recorded answer stands until it is drafted and filed again.
        raise ValueError(f"proposal {pid} is {r['state']}, not open: re-run propose to raise it again")
    p = load_proposal(pid, folder)
    if p is None:
        raise KeyError(f"no proposal {ref!r} under {folder or PROPOSED_DIR}")
    d = dict(p["definition"])
    code = d["code"]
    ddir = defs_dir or defs.DEFS_DIR
    target = ddir / f"{code}.json"
    # The file itself, not the loader's cache: a hand edit in place does not
    # move the directory mtime the cache keys on.
    cur = json.loads(target.read_text(encoding="utf-8")) if target.exists() else None
    if p["kind"] == "change":
        if _sha(cur) != p["base_sha"]:
            raise ValueError(f"{code} changed since proposal {pid} was drafted; re-run propose")
        d["engine"] = bool((cur or {}).get("engine"))
    else:
        if target.exists() or code in CATALOG:
            raise ValueError(f"{code} already exists; re-run propose to draft it under a free code")
        d["engine"] = False
    cat = {**CATALOG, code: (d["name"], d.get("group", ""))}
    errs = defs.validate(d, catalog=cat)
    if errs:
        raise ValueError("does not validate: " + "; ".join(errs[:3]))
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, target)  # a rename moves the directory mtime, which every loader watches
    if ddir == defs.DEFS_DIR:
        CATALOG.setdefault(code, (d["name"], d.get("group", "")))
    dec = store.record_decision(conn, f"Adopted workflow {pid} {d['name']} ({p['cases']} cases) into defs/{code}.json"[:200],
                                "Ben approved the miner's proposal; engine " + ("on" if d["engine"] else "off"))
    _close_item(conn, record(conn, pid), dec["decision_id"])
    _upsert(conn, p, "adopted", dec["decision_id"])
    conn.commit()
    back = backfill(conn, code, p["kind"], ddir)
    (folder or PROPOSED_DIR).joinpath(f"{pid}.json").unlink(missing_ok=True)
    return {"ok": True, "id": pid, "code": code, "path": str(target), "decision_id": dec["decision_id"],
            "engine": d["engine"], "backfill": back,
            "commit": commit_note([target.name]) if ddir == defs.DEFS_DIR else ""}


def backfill(conn: sqlite3.Connection, code: str, kind: str, ddir: Path | None = None) -> dict[str, int]:
    """Re-place the cases an adopted proposal touches: a new workflow's tag
    rule over untagged cases, a change's steps over its own cases."""
    from . import cases as cs
    from .tag import tag_root
    cs.ensure_workflows(conn)
    retagged = 0
    if kind == "new":
        for c in conn.execute("""SELECT c.id, t.kind, t.title, t.spec, t.assigned_to, i.source FROM workflow_cases c
                                 JOIN tasks t ON t.id=c.root_task_id LEFT JOIN intents i ON i.id=t.from_intent
                                 WHERE c.workflow IS NULL""").fetchall():
            wf, rule = tag_root(c["kind"], c["title"], c["spec"], c["assigned_to"], c["source"])
            if wf == code:
                conn.execute("UPDATE workflow_cases SET workflow=?, rule=? WHERE id=?", (code, rule, c["id"]))
                retagged += 1
    ids = [r["id"] for r in conn.execute("SELECT id FROM workflow_cases WHERE workflow=?", (code,))]
    for cid in ids:
        conn.execute("UPDATE tasks SET case_step=NULL WHERE case_id=?", (cid,))
        cs.recompute(conn, cid)
    conn.commit()
    return {"retagged": retagged, "recomputed": len(ids)}


def reject(conn: sqlite3.Connection, ref: str, defer: bool = False) -> dict[str, Any]:
    from .. import store
    pid = find_id(conn, ref)
    p = load_proposal(pid) or {}
    r = record(conn, pid)
    if not p and r is None:
        raise KeyError(f"no proposal {ref!r}")
    p = p or {"id": pid, "code": r["code"], "kind": r["kind"], "name": r["name"], "cases": r["cases"]}
    verb = "deferred" if defer else "rejected"
    dec = store.record_decision(conn, f"Ben {verb} workflow proposal {pid} {p['name']}"[:200],
                                "not filed again under this id" if not defer else "waits until Ben raises it")
    _close_item(conn, r, dec["decision_id"], defer=defer)
    _upsert(conn, p, verb, dec["decision_id"])
    conn.commit()
    return {"ok": True, "id": pid, "state": verb, "decision_id": dec["decision_id"]}


ANSWERS = {"approve": "adopt", "approved": "adopt", "yes": "adopt", "adopt": "adopt",
           "reject": "reject", "drop": "reject", "no": "reject", "defer": "defer", "later": "defer"}


# Politeness that may ride along with a bare answer ("Approve, thanks").
FILLER = frozenset("thanks thank you please ok okay sure ty".split())


def answer_action(conn: sqlite3.Connection, follow_up_id: str, text: str) -> tuple[str, str] | None:
    """(proposal id, 'adopt' | 'reject' | 'defer') when `text` is a bare
    answer on a proposal's item, else None. Nothing is applied. Bare: every
    word is an answer word for the same action, or politeness. "Approve, but
    rename step X" or "Approve once X is fixed" carries a condition: it goes
    to master as a typed answer instead of adopting the proposal unchanged."""
    ensure(conn)
    r = conn.execute("SELECT id FROM workflow_proposals WHERE follow_up_id=?", (follow_up_id,)).fetchone()
    if r is None:
        return None
    words = [w for w in re.findall(r"[a-z']+", (text or "").lower()) if w not in FILLER]
    acts = {ANSWERS.get(w) for w in words}
    if not words or None in acts or len(acts) != 1:
        return None
    return r["id"], acts.pop()


def apply_action(conn: sqlite3.Connection, pid: str, act: str) -> dict[str, Any]:
    return adopt(conn, pid) if act == "adopt" else reject(conn, pid, defer=act == "defer")


def answer(conn: sqlite3.Connection, follow_up_id: str, text: str) -> dict[str, Any] | None:
    """Ben's typed answer on a proposal's item: a bare approve, reject or
    defer is applied. None when the item is not a proposal or the answer is
    anything else (it then goes to master like any answer)."""
    hit = answer_action(conn, follow_up_id, text)
    return apply_action(conn, *hit) if hit else None


# ------------------------------------------------------------------ weekly

def switch_on(conn: sqlite3.Connection) -> bool:
    """The `workflow_propose` switch (on by default), stored like every
    queue switch (meta switch:<name>) and set with
    `python -m coord_mcp.queue switch workflow_propose on|off`."""
    r = conn.execute("SELECT value FROM meta WHERE key='switch:workflow_propose'").fetchone()
    return r is None or r["value"] == "on"


def due(conn: sqlite3.Connection, t: float | None = None) -> str | None:
    """This ISO week's key when the weekly run is owed now, else None."""
    lt = datetime.fromtimestamp(t or time.time())
    y, w, _ = lt.isocalendar()
    week = f"{y}-W{w:02d}"
    if (lt.weekday(), lt.hour, lt.minute) < RUN_AT:
        return None
    r = conn.execute("SELECT value FROM meta WHERE key=?", (META_WEEK,)).fetchone()
    return None if r is not None and r["value"] == week else week


def weekly(conn: sqlite3.Connection, t: float | None = None) -> dict[str, Any] | None:
    """The dashboard loop's call: once a week, from Monday 06:30 local, when
    the workflow_propose switch is on. Marks the week first, so a failure
    does not retry every pass (the loop records the error)."""
    if not switch_on(conn):
        return None
    week = due(conn, t)
    if week is None:
        return None
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (META_WEEK, week))
    conn.commit()
    if _has(conn, "workflow_cases"):
        from . import cases as cs
        cs.sync(conn)
    props = draft(conn, find(conn, int(t or time.time())))
    write(props, int(t or time.time()))
    out = file(conn, props, int(t or time.time()))
    out["week"] = week
    return out


# ------------------------------------------------------------------ page

def uncommitted_defs(ddir: Path | None = None) -> list[str]:
    """Definition files git does not have as they stand: adopt() writes into
    defs/ and goes live at once, but nothing commits it (Ben, 2026-11-14:
    remind instead). Empty when git is missing or the folder is clean."""
    import subprocess
    d = Path(ddir or defs.DEFS_DIR)
    try:
        out = subprocess.run(["git", "status", "--porcelain", "--", "."], cwd=d, capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return sorted(Path(ln[3:].strip().strip('"')).name for ln in out.stdout.splitlines()
                  if ln[3:].strip().strip('"').endswith(".json"))


def commit_note(files: list[str]) -> str:
    """The reminder /workflows/proposed shows while adopted definitions are uncommitted."""
    if not files:
        return ""
    return ("Adopted definitions not yet in git: " + ", ".join(files) + ". They are live already; commit them "
            "from agent-tracking: git add coord_mcp/workflows/defs and commit, then pull --rebase and push.")


def render(conn: sqlite3.Connection, pid: str | None) -> str | None:
    """/workflows/proposed and /workflows/proposed/<id>."""
    from ..report import _task_link, esc, page
    ensure(conn)
    if not pid:
        note = commit_note(uncommitted_defs())
        note = f'<div class="card note"><b>Commit needed.</b> {esc(note)}</div>' if note else ""
        rows = list_proposals()
        items = "".join(
            f'<li><a href="/workflows/proposed/{esc(p["id"])}"><b>{esc(p["id"])}</b> {esc(p["name"])}</a> · '
            f'{p["cases"]} cases · {"new workflow" if p["kind"] == "new" else "change to " + esc(p["code"])}'
            f' · <span class="muted">{esc((record(conn, p["id"]) or {"state": "drafted"})["state"])}</span></li>'
            for p in rows) or '<p class="clear">No proposals. The miner runs Mondays at 06:30.</p>'
        return page("Workflows", "Workflow proposals", "Workflow proposals",
                    "Drafted by the weekly miner (python -m coord_mcp.workflows propose).",
                    f'{note}<div class="card"><ul class="queue">{items}</ul></div>')
    p = load_proposal(pid)
    r = record(conn, pid)
    if p is None and r is None:
        return None
    if p is None:
        return page("Workflows", f"Proposal {pid}", f"Proposal {esc(pid)}",
                    f"{esc(r['name'])}: {esc(r['state'])}. The draft file is gone (adopted or superseded).", "")
    state = r["state"] if r else "drafted"
    d = p["definition"]
    cur = defs.get(p["code"]) if p["kind"] == "change" else None
    cur_keys = {s["key"]: s for s in (cur or {}).get("steps") or []}
    steps = "".join(
        f'<tr><td><code>{esc(s["key"])}</code></td><td>{esc(s["name"])}</td><td>{esc(s["who"])}</td>'
        f'<td>{esc(", ".join(s.get("kinds") or []))}</td><td>{esc(json.dumps(s.get("sla") or {}))}</td>'
        f'<td>{"optional" if s.get("optional") else ""}</td><td>'
        + ("<b>new</b>" if cur is not None and s["key"] not in cur_keys
           else "<b>changed</b>" if cur is not None and cur_keys.get(s["key"]) != s else "")
        + "</td></tr>" for s in d["steps"])
    ex = "".join(f"<li>{_task_link(e)}</li>" if re.fullmatch(r"[0-9a-f]{12}", e) else f"<li><code>{esc(e)}</code></li>"
                 for e in p["examples"])
    why = "".join(f"<li>{esc(w)}</li>" for w in p["why"])
    buttons = ""
    if state in ("drafted", "filed"):
        buttons = "".join(
            f'<form method="post" action="/api/workflow-proposal" style="display:inline">'
            f'<input type="hidden" name="id" value="{esc(pid)}"><input type="hidden" name="action" value="{a}">'
            f'<button>{label}</button></form> '
            for a, label in (("approve", "Approve (adopt)"), ("defer", "Defer"), ("reject", "Reject")))
    body = (f'<div class="card"><h2>Why it was flagged</h2><ul>{why}</ul>'
            f'<p class="muted">Window: last {WINDOW_DAYS} days; threshold {MIN_CASES} cases. '
            + ("A new workflow lands with the engine off." if p["kind"] == "new"
               else f"Changes {esc(p['code'])}; its engine setting is kept and added steps never spawn.")
            + '</p></div>'
            f'<div class="card"><h2>Steps</h2><div class="wrap"><table><tr><th>Key</th><th>Name</th><th>Who</th>'
            f'<th>Kinds</th><th>Limit</th><th></th><th></th></tr>{steps}</table></div></div>'
            f'<div class="card"><h2>Examples</h2><ul>{ex}</ul></div>'
            f'<div class="card"><h2>Definition</h2><pre style="white-space:pre-wrap;font-size:12px">'
            f'{esc(json.dumps(d, indent=2))}</pre></div>')
    sub = (f'{"New workflow" if p["kind"] == "new" else "Change to " + esc(p["code"])} · {p["cases"]} cases · '
           f'state <b>{esc(state)}</b>')
    return page("Workflows", f"Proposal {pid}", f"{esc(pid)} {esc(p['name'])}", sub, body, actions=buttons)
