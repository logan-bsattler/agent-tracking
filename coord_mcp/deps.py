"""Task order from the shell: which task waits on which.

The dashboard shows and edits the same rows (the task page's Order card, the
drafts walk). No MCP tool reads them yet, so an agent sees no difference;
master and Ben set them here or on the page.

    python -m coord_mcp.deps add <task> <waits_on> [note]   # task waits on waits_on
    python -m coord_mcp.deps remove <task> <waits_on>
    python -m coord_mcp.deps clear <blocker>                # blocker done in the world (a draft sent)
    python -m coord_mcp.deps show <task>
"""

from __future__ import annotations

import json
import sys

from . import store


def main(argv: list[str] | None = None) -> int:
    from .db import connect
    argv = sys.argv[1:] if argv is None else argv
    need = {"add": 2, "remove": 2, "clear": 1, "show": 1}
    if not argv or argv[0] not in need or len(argv) < need[argv[0]] + 1:
        print(__doc__.strip())
        return 2
    cmd, args = argv[0], argv[1:]
    conn = connect()
    try:
        if cmd == "add":
            r = store.add_dep(conn, args[0], args[1], " ".join(args[2:]))
        elif cmd == "remove":
            r = store.remove_dep(conn, args[0], args[1])
        elif cmd == "clear":
            r = store.clear_blocker(conn, args[0])
        else:
            r = store.task_deps(conn, args[0])
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(json.dumps(r, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
