# Task dependencies

"Do B before A." The board records which task waits on which, so Ben can see
the order on the dashboard without anyone remembering a decision.

## Now: dashboard and CLI only

- **Storage.** A `task_deps` table (`task_id` waits on `blocked_by`, an
  optional `note`, `cleared_at`). It is created on first use by
  `db.ensure_deps`, not in `DDL`, so `SCHEMA_VERSION` stays at 11 (see the
  comment on `DEPS_DDL` for why a bump would hurt running sessions).
- **When a wait clears.** It clears by itself when the blocker is `done` and
  is not a `draft_reply`. A draft is `done` when the agent wrote it, not when
  Ben sent it, so a draft blocker clears only when Ben presses **Mark sent**
  (or `deps clear <id>`).
- **Where it shows.**
  - The task page has an **Order** card: "waits on" and "holds up", each
    linked, with Remove, Mark sent and an Add-wait form.
  - Board rows carry a "waits on X" or "do first: holds up Y" line.
  - **Next up** never leads with a row that is waiting.
  - The drafts walk puts a waiting draft after its blocker and shows a
    Mark sent button on the blocker.
- **Who sets it.** Ben on the task page; master or Ben from the shell:
  `python -m coord_mcp.deps add <task> <waits_on> [note]`, plus `remove`,
  `clear` and `show`.
- **Agents see nothing.** No MCP tool or contract changed.

## Next restart: the agent side

Breaking where marked. Live sessions keep the old tool list until they
restart, so ship this when every session restarts together.

1. **Schema (breaking for running servers).** Move `DEPS_DDL` into `DDL` and
   bump `SCHEMA_VERSION` to 12. Keep `ensure_deps` as a no-op guard for one
   release.
2. **`coord_create_task` gets `blocked_by: list[str]` (additive).** It writes
   the waits after the task row, with the same checks as `add_dep` (unknown
   id, self, loop). A bad id refuses the whole create.
3. **`coord_board` (shape change: lead-facing, additive fields).**
   - Each open task carries `waits_on: [id]`, listing only the waits that
     still hold.
   - A task with `waits_on` set is reported as `blocked: true`, so master
     does not dispatch it.
   - Each blocker carries `holds_up: [id]`.
4. **New tool `coord_set_dependency`** (lead only): `task_id`, `blocked_by`,
   `action` (`add` | `remove` | `clear`), `note`. Master uses it when a
   result's follow-up says "send X before Y". Today such follow-ups get
   dropped with a decision, which leaves nothing on the board.
5. **Teammate side: `coord_get_task` (additive).** It returns `waits_on` with
   each blocker's title and state. A teammate handed a blocked task returns
   `{"done": false, "reason": "waits on <id>", "retryable": true}` rather
   than working it. Nothing enforces this; it is the backstop for a
   dispatch made in error.
6. **The minder (`minder_owed`).** It skips tasks that are waiting, so a
   blocked task never counts as "not picked up". A draft blocker held for
   more than a day joins Ben's queue as "was <id> sent?".
7. **The Draft in Outlook button.** It stays enabled for a waiting draft,
   since a draft in Outlook is harmless. The prompt adds "send after <id>"
   so master's Done line repeats the order.
8. **Master `CLAUDE.md`.** Under **Dispatching**, never dispatch a task
   whose `waits_on` is non-empty. Under **Each turn**, a loose end of the
   form "do X before Y" becomes `coord_set_dependency`, not a dropped
   decision.
9. **Tests.** Cover the stdio round trip of the new tool, `blocked_by` on
   create, and the board fields. Extend `smoke_stdio.py` with one wait set
   and cleared in the lead role.

Open question: should a wait whose blocker **failed** hold, or show as
"blocker failed" in Needs you? The proposal is to hold it and flag it.
