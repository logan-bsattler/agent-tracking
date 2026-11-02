"""Name the real files behind a deliverable whose result path is a pattern.

A result like 'FDD-D2R-<1413|1414> <title> - DRAFT.docx' names nothing on
disk: /deliverables shows it missing and Promote has no file. This restates
it with the files themselves (store.restate_files), recorded as a decision:

    python -m coord_mcp.restate <task_id> <path> [<path> ...]
"""

from __future__ import annotations

import sys

from . import store


def main(argv: list[str] | None = None) -> int:
    from .db import connect
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        print(__doc__.strip())
        return 2
    conn = connect()
    try:
        r = store.restate_files(conn, argv[0], argv[1:])
        conn.commit()
        print(f"restated {r['task_id']}: {len(r['files'])} files; decision {r['decision_id']}")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
