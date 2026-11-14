"""python -m coord_mcp.workflows <command>

  mine [--db PATH] [--out FILE] [--json]   tag every task and intent, write the HTML report (read-only)
  backfill [--db PATH]                     put every past task in a case (writes the case tables only)
  cases [--db PATH] [--open] [--workflow X] [--json]   list cases and where each stands (read-only)
  case <id> [--db PATH]                    one case with its tasks, decisions and events, as JSON
  engine --dry-run [--db PATH]             what the engine would create now (writes nothing)
  propose [--db PATH] [--file] [--json]    find new or drifting workflows and draft definitions under
                                           proposed/ (dry run by default: prints, writes no file, files
                                           nothing); --file writes the drafts and puts up to 3 a week in
                                           Ben's queue
  adopt <id> [--db PATH]                   move an approved proposal into defs/, record it, backfill
  reject <id> [--defer] [--db PATH]        close (or defer) a proposal's item in Ben's queue
  precedent [--db PATH] [--out FILE] [--json] [--apply]
                                           open Ben loose ends a past ruling would settle (read-only;
                                           --apply needs COORD_WORKFLOW_PRECEDENT=1)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

DOCS = Path(__file__).resolve().parents[2] / "docs"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m coord_mcp.workflows")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mine", help="tag past tasks and intents to workflows; read-only")
    m.add_argument("--db")
    m.add_argument("--out", help="HTML report path (default docs/workflow-mining-<date>.html)")
    m.add_argument("--json", action="store_true", help="print the numbers as JSON instead")
    b = sub.add_parser("backfill", help="every past task into a case; safe to re-run")
    b.add_argument("--db")
    c = sub.add_parser("cases", help="list cases and where each stands")
    c.add_argument("--db")
    c.add_argument("--open", action="store_true", help="open cases only")
    c.add_argument("--workflow")
    c.add_argument("--json", action="store_true")
    one = sub.add_parser("case", help="one case as JSON")
    one.add_argument("id")
    one.add_argument("--db")
    e = sub.add_parser("engine", help="what the workflow engine would do now")
    e.add_argument("--db")
    e.add_argument("--dry-run", action="store_true", required=True,
                   help="required: the engine itself runs only in the dashboard, behind COORD_WORKFLOW_ENGINE=1")
    pr = sub.add_parser("precedent", help="open Ben loose ends a past ruling would settle")
    pr.add_argument("--db")
    pr.add_argument("--out", help="HTML report path (default docs/workflow-precedent-<date>.html)")
    pr.add_argument("--json", action="store_true")
    pr.add_argument("--apply", action="store_true", help="resolve the 'decided, no work' rows; needs the switch")
    at = sub.add_parser("attach", help="move a task into a case (fix for a 'no case' warning)")
    at.add_argument("task_id")
    at.add_argument("case_id")
    at.add_argument("--db")
    po = sub.add_parser("propose", help="weekly miner: new or drifting workflows")
    po.add_argument("--db")
    po.add_argument("--file", action="store_true", help="write drafts and file Ben items (default: dry run)")
    po.add_argument("--json", action="store_true")
    ad = sub.add_parser("adopt", help="adopt a proposal (its id or its Ben item's follow-up id)")
    ad.add_argument("id")
    ad.add_argument("--db")
    rj = sub.add_parser("reject", help="reject or defer a proposal")
    rj.add_argument("id")
    rj.add_argument("--defer", action="store_true")
    rj.add_argument("--db")
    a = ap.parse_args(argv)

    if a.cmd == "propose":
        from . import propose as pp
        if a.file:
            from ..db import connect
            from . import cases as cs
            conn = connect(a.db) if a.db else connect()
            cs.sync(conn)
        else:
            from . import mine as mn
            conn = mn.connect_ro(a.db)
        try:
            props = pp.draft(conn, pp.find(conn))
            if a.file:
                pp.write(props)
                filing = pp.file(conn, props)
            else:
                filing = pp.file(conn, props, dry_run=True) if pp._has(conn, "workflow_proposals") else                     {"filed": [{"id": p["id"], "what": pp.item_text(p)} for p in props
                               if not p["errors"]][:pp.WEEKLY_CAP], "skipped": []}
        finally:
            conn.close()
        if a.json:
            print(json.dumps({"proposals": props, "filing": filing}, indent=1, default=str))
            return 0
        for p in props:
            bad = f"  INVALID: {'; '.join(p['errors'][:2])}" if p["errors"] else ""
            print(f"{p['id']:16} {p['kind']:6} {p['cases']:4} cases  {p['name']}{bad}")
            for w in p["why"]:
                print(f"{'':18}- {w}")
            print(f"{'':18}examples: {', '.join(p['examples'])}")
        verb = "filed" if a.file else "would file"
        for f in filing["filed"]:
            print(f"{verb}: {f['what']}")
        for s in filing["skipped"]:
            print(f"not filed: {s['id']} ({s['why']})")
        if not props:
            print("no findings")
        return 0

    if a.cmd in ("adopt", "reject"):
        from ..db import connect
        from . import propose as pp
        conn = connect(a.db) if a.db else connect()
        try:
            out = pp.adopt(conn, a.id) if a.cmd == "adopt" else pp.reject(conn, a.id, defer=a.defer)
        except (KeyError, ValueError) as e:
            print(f"refused: {e}", file=sys.stderr)
            return 1
        finally:
            conn.close()
        print(json.dumps(out, indent=1))
        return 0

    if a.cmd == "attach":
        from ..db import connect
        from .checks import attach
        conn = connect(a.db) if a.db else connect()
        try:
            out = attach(conn, a.task_id, a.case_id)
            conn.commit()
        finally:
            conn.close()
        print(json.dumps(out, indent=1, default=str))
        return 0

    if a.cmd == "precedent":
        from . import precedent as pc
        if a.apply:
            if not pc.enabled():
                print("refused: COORD_WORKFLOW_PRECEDENT is not 1 (off until Ben has read the report)", file=sys.stderr)
                return 1
            from ..db import connect
            conn = connect(a.db) if a.db else connect()
            try:
                print(json.dumps(pc.apply(conn), indent=1))
            finally:
                conn.close()
            return 0
        from . import mine as mn
        conn = mn.connect_ro(a.db)
        try:
            rep = pc.report(conn)
        finally:
            conn.close()
        if a.json:
            print(json.dumps(rep, indent=1, default=list))
            return 0
        out = Path(a.out) if a.out else DOCS / f"workflow-precedent-{time.strftime('%Y-%m-%d')}.html"
        pc.write_report(rep, out)
        print(f"{out}  {rep['open']} open Ben loose ends: {rep['tally']}")
        return 0

    if a.cmd == "engine":
        from ..db import connect
        from . import cases as cs, engine
        conn = connect(a.db) if a.db else connect()
        try:
            cs.sync(conn)
            print(json.dumps(engine.run(conn, dry_run=True), indent=1))
        finally:
            conn.close()
        return 0

    if a.cmd in ("backfill", "cases", "case"):
        from ..db import connect
        from . import cases as cs
        conn = connect(a.db) if a.db else connect()
        try:
            if a.cmd == "backfill":
                print(json.dumps(cs.backfill(conn)))
                return 0
            if a.cmd == "case":
                d = cs.case_detail(conn, a.id)
                if d is None:
                    print(f"no case {a.id}", file=sys.stderr)
                    return 1
                print(json.dumps(d, indent=1))
                return 0
            rows = cs.list_cases(conn, open_only=a.open, workflow=a.workflow)
            if a.json:
                print(json.dumps(rows, indent=1))
                return 0
            from . import name
            for r in rows:
                print(f"{r['id']}  {r['workflow'] or '-':4} {r['state']:5} {r['step'] or '-':14} "
                      f"{r['waits_on'] or '-':7} {r['client'] or '-':9} {r['title'][:70]}")
            print(f"{len(rows)} case(s)")
        finally:
            conn.close()
        return 0

    if a.cmd == "mine":
        from . import mine as mn
        conn = mn.connect_ro(a.db)
        try:
            if a.json:
                print(json.dumps(mn.mine(conn), indent=1))
                return 0
            from .revisions import REVISIONS
            out = Path(a.out) if a.out else DOCS / f"workflow-mining-{time.strftime('%Y-%m-%d')}.html"
            r = mn.write_report(conn, out, REVISIONS)
            t = r["totals"]
            print(f"{out}  tasks {t['tagged_tasks']}/{t['tasks']} tagged, intents "
                  f"{t['tagged_intents']}/{t['intents']}, {len(r['leftovers'])} leftover patterns")
        finally:
            conn.close()
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
