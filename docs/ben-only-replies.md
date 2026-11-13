# Ben-only replies: a durable home on the board

Status: design, not built. **Breaking**: schema change to `follow_ups`, and a
contract change to `coord_resolve_intent`. Source: the email intake audit
(`docs/email-intake-audit-2026-11-13.html`, gap 2).

## Problem

An email or Teams ask that only Ben can answer is lost when it does not become
a task. Three examples from 11-09 to 11-11:

| Intent | Ask | What happened |
| --- | --- | --- |
| `d5e065d052cf` | Homero Rendon: hours and target date for Essex-Brownell FDD-005 | Dismissed ("not a wired client"). Presented as q642 and closed 85 min later by q643. No answer recorded. |
| `19b4abfda2ee` | Colin Gray: 1–2 technical names for Piston VPN | Dismissed. Decision `23aaa66299a7` said "queue for Ben", but it was never presented. |
| `29dee5176ab9` | Jyoti Kotti (Rani): confirm Ben as primary contact | Dismissed. Decision `9de53da13db7` said "Ben answers directly". Nothing tracks whether he did. |

Each link in the chain looks correct on its own:

- Ben's queue is "open loose ends whose `who` is Ben".
- A loose end needs a `task_id`.
- An unwired client gets no task.
- So Master dismisses the intent with a free-text note.

The only place the ask then lives is a board-wide question
(`questions.task_id = ''`). `present_question` closes that question when the
next one goes up, whether or not Ben answered it.

The FDD-005 ask is also a target-date request. Decision `d1c3d5216d4f` (Ben,
11-13) wants those answered within one business day, which makes a silent
drop the worst outcome.

## Design

### 1. A loose end that needs no task

The schema change is to `follow_ups`:

- `task_id` becomes nullable.
- New nullable columns:
  - `client`, the client key, or the client's name when it is not wired (`Essex-Brownell`);
  - `intent_id`;
  - `due_at`;
  - `ref`, the source pointer: `msg <outlook id>` or a `teams:///` uri.

A row with `task_id NULL` must have an `intent_id`, and its `who` must be
`Ben`. SQLite cannot alter a column's nullability, so the migration rebuilds
`follow_ups` with the usual create-copy-rename, guarded by a `meta` version
row. It runs on first use, like `commitments` does.

A new store call, `ben_item(conn, intent_id, what, client, due_at=None)`,
inserts one row with `idx` 0. It is exposed as part of
`coord_resolve_intent` (below), not as a new tool.

Everything that reads `follow_ups` must tolerate `task_id NULL`:

- `/queue`: group by `client` when there is no task. Send to agent is
  disabled for an unwired client; Defer, Drop and typed answers work as today.
- The board's `loose_ends`, `list_follow_ups` and the minder's `loose` query.
  `who = Ben` rows are already excluded there, so that query needs a check,
  not a change.
- `present_question(follow_up_id=...)` currently looks up `fu.task_id` and
  raises on an unknown task. Allow `NULL`, so a question about a Ben item has
  a loose end behind it and is never board-wide.
- `/intents` (`triage_emails`) shows the row's `what` as owed. It replaces the
  heuristic added in 1c6ec83.

### 2. `coord_resolve_intent` refuses a bare dismissal of an only-Ben ask

This applies to the sources `email-reply`, `teams-ask` with `ask decision`,
and `email-status` when the client is `unknown`. Dismissing one of them must
carry exactly one of the following:

- `answered_by`: a decision id whose statement cites Ben's reply (his sent
  mail or Teams message).
- `ben_item`: `{what, client, due?}`, which creates the task-less loose end
  above and resolves the intent to it (`task_ids` stays empty, `note` names
  the follow-up id).
- `duplicate_of`: an open intent or follow-up id.

A plain `note` alone is refused, with an error that names the three options.

This is the contract change. The live Master `CLAUDE.md` tells Master to
dismiss with a note, so the error text has to teach the new call.

`due` defaults to one business day after `reply_date` when the summary or
context reads as a target-date ask (decision `d1c3d5216d4f`), and to two
business days otherwise. The default follows the inferred-due rule
`commitments` already uses.

### 3. Questions are never closed silently

`questions` gets a nullable `superseded_at`:

- `present_question` sets `superseded_at`, not `closed_at`, on the question
  it replaces.
- An answer (a button press, `/api/question/close`, or a decision citing
  `q<id>`) sets `closed_at`.

The minder gains an item: "questions superseded unanswered". It lists
board-wide questions with `superseded_at` set, no `closed_at`, and no
decision citing them. Item 1 makes such questions rare, because every Ben
item gets a loose end, but it keeps questions with no loose end behind them
honest.

The minder prompt text gains a label. The Master `CLAUDE.md` section **Ben's
buttons → Minder** needs one line for it: "re-present it, or record why not".

## Rollout

1. Merge the schema with readers that tolerate `NULL` first (steps 1 and 3,
   no refusal yet). This is safe for live sessions: nothing writes the new
   shape until Master calls `ben_item`.
2. Backfill: one `ben_item` each for `d5e065d052cf`, `19b4abfda2ee` and
   `29dee5176ab9`, unless Ben says he has answered them. That is a Master
   action, not a migration.
3. Update the Master and desk `CLAUDE.md` (**Routine wake-ups → email-reply**,
   **Teams sources → teams-ask**, and the minder line).
4. Turn on the refusal in `coord_resolve_intent`.

Rollback: turn the refusal off (one flag). The nullable columns can stay.

## Not in scope

- Wiring Essex-Brownell, Piston or Rani as clients. That is Ben's call, and
  wiring Essex would remove most of the volume (22 triage lines in 7 days).
- Drafting replies for unwired clients. There is no client agent to do it.
  The Ben item only keeps the ask alive.
- Changes to the hourly-triage routine. Those are listed in the audit report.

## Tests

- Migration: an old board with tasks and follow-ups upgrades, and existing
  rows keep their `task_id`.
- `ben_item` round trip: it appears in `/queue` under its client and in the
  board's Ben queue, and is presentable with `follow_up_id`.
- `resolve_intent` on an `email-reply`:
  - a note alone is refused;
  - each of `answered_by`, `ben_item` and `duplicate_of` is accepted.
- `present_question` on a second question sets `superseded_at` on the first,
  and the minder lists it until a decision cites `q<id>`.
- Smoke: `coord_resolve_intent` over stdio with `ben_item`.
