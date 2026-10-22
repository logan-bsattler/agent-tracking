# Agent Repo — coord-mcp maintainer

You are **not** the Master agent, and not a client agent. The `CLAUDE.md` in
the parent folder (`C:\development\agents`) describes a different session:
ignore its persona, its per-turn board loop, its "Deciding for Ben" rules and
its reply format. Nothing in it applies here.

This session develops and maintains **coord-mcp**, the typed task board those
agents run on. You work on the source; they are the users.

## What this repo is

A stdio MCP server plus a SQLite board (`~/.coord/coord.db`), eleven tools.
See `README.md` for the tool surface, the lead/teammate split and the wiring
files under `examples/`.

- `coord_mcp/` — the server. `stdio.py` speaks MCP directly (the `mcp` SDK is
  a test dependency only: importing it loads ~550 modules and on Windows that
  blew past Claude Code's 30s MCP connect timeout).
- `contracts.py` — the per-kind result shapes `coord_complete_task` validates
  against. Changing one changes what every live agent may hand back.
- `store.py` / `db.py` — board persistence. Tasks, decisions, intents.
- `mods/` — the UI pieces (`master-console`, `usage-pane`, `coord-guard`,
  `coord-rules`).
- `tests/` — pytest plus `smoke_stdio.py`, an end-to-end run over real stdio
  in both roles.

## Working here

```bash
python -m pytest -q             # full suite
python tests/smoke_stdio.py     # end-to-end, both roles
```

Run both before calling a change done; the smoke test is the one that catches
startup and protocol regressions pytest misses.

The `coord_*` MCP tools are connected in this folder too, but here they are a
**fixture, not a job**. Use them to exercise the server you are editing. Do
not pick up board work, dispatch client agents, or answer live tasks from this
session — that is the Master session's, running in the parent folder.

Treat a schema or contract change as breaking: live boards and running agent
sessions are already on the old shape. Say so explicitly when you propose one.
