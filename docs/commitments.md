# Commitments

"I'll send that Friday." Ben makes promises like this in Teams and email every
day, and the board has no record of them. This doc adds a **commitment**: who
was promised what, by when, and whether it was kept. It does two jobs:

- **Prioritization.** Work under a commitment that is coming due runs first.
- **Follow-up.** The minder and Ben's queue flag a commitment coming due or
  overdue.

Status: **design, for Ben's approval.** Nothing below is built yet. It
replaces the `teams-commitment` → task rule in the master `CLAUDE.md` (added
2026-11-10, commit cf8bbe2). That text is rewritten once this is approved.

## Why not an intent, a task, a loose end or a decision

Each of the existing records holds part of a commitment, but none holds all
of it:

| Record | What it lacks |
| --- | --- |
| intent | Closed the moment master converts it, so the promise is gone by the time it comes due. |
| task | Has no due date and no recipient. Its `done` means the work is ready, not that the promise was kept: Ben still has to send it. Many promises have no agent work in them ("I'll call them Tuesday"). |
| loose end | Exists only under a finished task, and has no date. |
| decision | Records the promise, but nothing is owed afterwards and nothing comes due. |

So a commitment is its own row, and it **links to** the task or loose end
that covers the work.

## Shape

```sql
CREATE TABLE IF NOT EXISTS commitments (
  id            TEXT PRIMARY KEY,        -- 12 hex, like tasks
  client        TEXT NOT NULL,           -- the assigned_to key, or 'internal'
  project_id    TEXT REFERENCES projects(id),
  by_who        TEXT NOT NULL DEFAULT 'Ben',  -- who promised; see "Owed to Ben"
  to_who        TEXT NOT NULL,           -- "David Roe", "Moog AP team"
  to_email      TEXT,
  what          TEXT NOT NULL,           -- <= 200 chars, in our words
  due_at        INTEGER,                 -- epoch; NULL = no date at all
  due_basis     TEXT NOT NULL CHECK (due_basis IN ('stated','inferred','none')),
  due_text      TEXT,                    -- the words used: "Friday", "end of week"
  source        TEXT NOT NULL,           -- 'teams' | 'email' | 'meeting' | 'manual'
  source_ref    TEXT,                    -- teams:/// uri, Outlook message id, meeting id
  source_at     INTEGER,                 -- when it was said
  intent_id     TEXT REFERENCES intents(id),
  task_id       TEXT REFERENCES tasks(id),       -- work that covers it (chain root or leaf)
  follow_up_id  TEXT REFERENCES follow_ups(id),  -- or the loose end that does
  state         TEXT NOT NULL CHECK (state IN ('open','done','slipped','dropped')),
  slipped_at    INTEGER,                 -- first time due_at passed while open; kept after done
  due_history   TEXT,                    -- JSON [{due_at, decision_id, at}] of earlier dates
  close_decision_id TEXT REFERENCES decisions(id),
  created_at    INTEGER NOT NULL,
  closed_at     INTEGER
);
CREATE INDEX IF NOT EXISTS commitments_open ON commitments(state, due_at);
CREATE INDEX IF NOT EXISTS commitments_task ON commitments(task_id);
```

**States.**

- `open`: the promise stands and its date has not passed.
- `slipped`: `due_at` passed while the promise was open. A sweep sets this
  and stamps `slipped_at`. Nothing else changes, and nothing is sent.
- `done`: the promise was kept. This is set only by a decision (see
  **Closing**), never because the linked task finished. If the commitment
  slipped first, `slipped_at` stays, so an on-time rate can be read later.
- `dropped`: released or moot (the client withdrew the ask, or it was folded
  into another commitment). Set by a decision.

A new date (Ben re-agrees Friday → Tuesday) moves the old `due_at` into
`due_history`, together with the decision that moved it, and returns the
state to `open`.

**Due dates.** Each commitment stores the date with its basis (`due_basis`)
and the words used (`due_text`).

- `stated`: the date is resolved against `source_at` in Ben's time zone.
  Day names mean the next such day ("Friday" said on a Friday afternoon is
  next Friday). "Tomorrow", "Monday" and similar mean 17:00 local that day.
  "End of week" means Friday 17:00. "Next week" means the following Friday
  17:00.
- `inferred`: there is a promise but no date ("I'll get back to you",
  "let me check"). Default: two working days after `source_at`, flagged as
  inferred everywhere it is shown. The default is an open question below.
- `none`: only for open-ended standing promises ("I'll keep you posted").
  These never slip. They show on the client page, but never in the minder.

**Client and project.** Client is mapped the way triage maps a message
(conversation or thread, then domain, then content). `project_id` is set
when the linked task's project resolves (`projects.project_of`), or by
master directly. A commitment with neither a client nor a project is
`internal`: it is listed, but drives nothing client-facing.

## Where commitments come from

1. **Teams.** The existing `teams-commitment` intent source (cf8bbe2)
   already carries `to`, `to_email`, `due`, `due_text`, `what` and `msg`.
   Master converts it into a commitment instead of only a task.
2. **Email (new source `email-commitment`).** Today triage skips Ben's sent
   mail. It would now read his sent mail for promises only, using the same
   rules and fields as Teams, with `msg` set to the Outlook message id.
   Inbound mail never creates a Ben commitment.
3. **Meetings.** Later, from caption notes (`coord_meeting_notes`) once the
   listener is verified. Out of scope here.
4. **Manual.** Ben or master adds one from the CLI or the dashboard.

Triage still proposes only intents: an intent is how external content gets
reviewed. The commitment row is created by master, from the intent, so the
`what` and the linked work are master's wording, not the message's.

**Dedupe.** One commitment per promise, not per message that repeats it.
Before creating one, master checks open commitments for the same client,
recipient and `what`. If one matches, a later message that re-dates it
updates the date (`due_history`) instead of adding a row.

## Linking to work

At creation, master links the commitment to whatever covers it:

- **Agent work in it** (send a doc, an estimate, a fix): a task to that
  client. The spec names the commitment and its due date. The commitment
  takes the `task_id`. When that task finishes, its result's loose end for
  Ben ("send X to David") takes the `follow_up_id` too, so the promise shows
  in his queue once the work is ready.
- **Already covered** by an open task or loose end: link to it. No new task.
- **No agent work** (a call, joining a meeting): link nothing. The
  commitment itself is the reminder.

The links are pointers. A task finishing never closes a commitment.

## Closing

A commitment closes only by a decision (`close_decision_id`). The evidence
for one comes from:

- **Triage.** `email-closes` and `teams-status` gain an optional
  `commitments <ids>` field, filled when Ben's sent mail or a Teams message
  of his visibly delivers the thing to the recipient. Master checks the
  match and records `Commitment <id> kept: <evidence>`.
- **Ben.** He closes it on /commitments or from his queue ("done", "drop").
- **Master.** It closes one when a linked `draft_reply` Ben drafted is in
  his Sent folder (the existing closes path, which matches it by thread).

## Prioritization

"Work under a commitment due soonest runs first" works by giving each task
and loose end an **effective due date**: the earliest `due_at` among the
open or slipped commitments linked to it, its parent chain, or its project.
That date then orders three things:

- **Ben's queue** (`queue.ben_queue`, /queue, the master's one-question
  order). Items with an effective due date come first, overdue before due,
  soonest first, ahead of the current oldest-wait order. Items without one
  keep today's order.
- **Master dispatch.** When a session has several owed tasks,
  `coord_board` puts the one with the earliest effective due date first
  (phase two). The master `CLAUDE.md` rule becomes: dispatch in due order,
  then as before.
- **The client page and /board.** These show a due chip on the task, with
  the date's basis.

## Follow-ups

This is a new minder condition, `commitments`, listed after `pipeline`:

- **Coming due.** Due within one working day, and the linked task is not
  done (or there is no link). The minder names the commitment id. Master
  checks the work is moving, and dispatches or escalates it.
- **Overdue.** The state is `slipped` and there is no decision since
  `slipped_at`. Master puts it to Ben with `coord_present_question`, with
  three options: deliver now (what is ready), re-date it (Ben tells the
  recipient, which is outward and his), or drop it. Master never contacts
  the recipient.
- **Untracked.** The commitment has been open more than 30 minutes with no
  `task_id`, no `follow_up_id` and agent work implied. Master tasks it or
  records why not.

The **morning recap** gains a "Due today / overdue" block. The
**dashboard** gains /commitments: open first, sorted by due date, with
filters by client and state, and Done, Drop and Re-date buttons applied in
Ben's name (the same pattern as /queue).

## Owed to Ben

`by_who` lets a commitment record a promise **to** Ben ("I'll send the test
data Monday", from a client). Those often block work, and the table holds
them at no extra cost. Phase one stores only Ben's own (`by_who = 'Ben'`).
Whether to triage promises others make to Ben is an open question below.

## Phase one: dashboard and CLI (no schema bump)

This follows the same pattern as `task_deps`, `promotions` and `projects`,
so the live coord servers are untouched.

1. **Storage.** `db.COMMITMENTS_DDL` plus `db.ensure_commitments(conn)`.
   `SCHEMA_VERSION` stays at its current value.
2. **Module** `coord_mcp/commitments.py`: `add`, `set` (re-date, re-link,
   to, what), `link`, `close` (done or dropped, with a decision id),
   `sweep` (open → slipped), `list`, `due(conn, now)` and
   `effective_due(conn, task_id)`. It also gets a CLI:
   `python -m coord_mcp.commitments add|set|link|close|list|sweep`. Master
   uses the CLI until phase two, as it does for projects.
3. **Intent source `email-commitment` (additive).** It takes the same
   required fields as `teams-commitment`, except that `msg` is an Outlook
   id. Both take an optional `commitment <id>` field, which triage sets
   when a message re-dates a commitment it found open.
4. **Minder.** `minder_owed` gains a `commitments` key and
   `_MINDER_LABELS` gains a label. `sweep` runs inside `minder_check`, so
   slips are stamped without a new scheduled job.
5. **Queue order.** `queue.ben_queue` sorts by effective due date first.
6. **Dashboard.** /commitments, a due chip on /board, /client/<key> and
   /queue, and the morning recap block.
7. **Triage routine.** Read Ben's sent mail for `email-commitment`. Add the
   `commitments <ids>` field to the closes sources.
8. **Master `CLAUDE.md`.** Commitment intents become commitments, created
   with the CLI and linked as in **Linking to work**. Add the minder
   condition. Dispatch in due order.

Nothing an agent sees changes in this phase.

## Next restart: the agent side

Ship this with the projects restart, since both bump the schema together.

1. **Schema (breaking for running servers).** Fold `COMMITMENTS_DDL` into
   `DDL` together with the projects fold, and keep `ensure_commitments` as a
   no-op guard for one release.
2. **New tool `coord_commitment` (lead only, additive).** `action`:
   `create`, `update`, `link`, `close`. It returns the row and replaces
   the CLI for master.
3. **`coord_board` (lead-facing, additive).** A top-level `commitments`
   list: open and slipped items due within three working days, plus any
   that are overdue, each with id, client, to, what, due, basis and linked
   ids. Tasks carry `due` (effective due date) when they have one, and
   owed tasks sort by it.
4. **`coord_create_task` gets `commitment_id` (additive).** It links the
   new task in the same call. It refuses an unknown id, or a commitment of
   another client.
5. **Teammate side: `coord_get_task` (additive).** It returns the linked
   commitment's `to`, `what` and `due`, so the client agent knows the date
   it is working to.

**Breaking, stated plainly:** only item 1, and only for servers still
running the old code when the schema version moves. That is the same risk,
and the same remedy (everyone restarts together), as projects. No result
contract changes, no tool loses a field, and every new field is optional.
Phase one is not breaking at all. The minder prompt gains one more
condition in its text, which the master reads as data. The master
`CLAUDE.md` is updated in the same change.

## Backfill

1. Open and converted `teams-commitment` intents since cf8bbe2.
2. One triage backfill run over the last 14 days of Ben's sent mail and
   Teams messages, proposing `email-commitment` / `teams-commitment` intents
   only. Master converts them, and Ben drops the stale ones on
   /commitments.
3. `email-reply` intents whose summary names a commitment are left alone.
   They are asks to Ben, not promises.

## Tests

- DDL on first use: an old board file gains the table, and a second call
  is a no-op.
- Due date resolution: the day-name, "end of week" and "next week" rules
  across a week boundary and across a time-zone (DST) change. An inferred
  date defaults to two working days, skipping weekends.
- Sweep: open → slipped exactly once, `slipped_at` is kept through done,
  and a re-date returns to open with a `due_history` entry.
- Effective due date: inherited through the parent chain and the project.
  The earliest wins, and closed commitments are ignored.
- `ben_queue` order: overdue, then due soonest, then today's order.
- Minder: coming due, overdue and untracked each fire once, at the right
  thresholds, and stay quiet after a decision.
- A task finishing does not close its commitment.
- `email-commitment` validation mirrors `teams-commitment`.
- Smoke: unchanged in phase one. In phase two, `coord_commitment` round
  trip, and `coord_board` carrying `commitments`.

## Open questions for Ben

1. **Inferred due date.** When he gives no date ("I'll get back to you"),
   default to two working days? *Recommended:* yes. It is shown as
   inferred, and he can re-date it in one click.
2. **What counts as kept.** Must there be evidence that the thing reached
   the recipient (his sent mail or Teams message), or is his "done" enough?
   *Recommended:* either. Triage proposes the evidence, and his button
   closes it without any.
3. **Promises to Ben.** Track what clients and colleagues promise him (test
   data, access, sign-off) as `by_who` commitments? *Recommended:* yes, in
   a second step after his own are working. They are the usual blockers.
4. **Coming-due window.** One working day before `due_at` for the minder
   flag? Or a morning-of flag only?
