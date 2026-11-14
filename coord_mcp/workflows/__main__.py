"""python -m coord_mcp.workflows <command>

  mine [--db PATH] [--out FILE] [--json]   tag every task and intent, write the HTML report (read-only)
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
    a = ap.parse_args(argv)

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
