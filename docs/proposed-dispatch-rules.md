# Proposed changes to Master's dispatch rules

Draft for Ben, 2026-10-31. Target: `C:\development\agents\CLAUDE.md`. Each
change names the section and gives replacement text. Nothing is applied yet.

Why: one task in flight per client serialises all of a client's work behind
one session (MAG had 16 undelivered tasks today), makes drafts wait behind
long code tasks, and forces a client clear before every DoD review. The thing
the rule protects is a park's clear, which is per session, not per client.

---

## 1. Parallel sessions per client

**Dispatching, replace the "One task in flight per client" paragraph with:**

> **One task in flight per session.** Send a session its next dispatch only
> after its current task has announced done or failed (exception: change 3).
> A dispatch queued behind a task that then parks cancels the clear and starts
> the next task at full context.
>
> A client may have more than one session. Its **primary** session (`<Client>
> - Local`) takes `code_change` and anything that uses the server handoff:
> those stay one at a time per client, because they share the server, the
> push/pull folder and the source tree. Its **side** session (`<Client> -
> Local 2`) takes independent work that touches neither: `draft_reply`,
> `investigation`, `data_pull` that reads only local files, and `dod_review`
> (change 2). When the primary is busy and the task fits the side session,
> dispatch it there instead of holding it. Parallelism comes from different
> clients and from side sessions, never from queuing on one session.

**Client agents, session table:** add a `Side session id` column. A blank
cell means the client has no side session yet: hold the task for the primary,
and tell Ben once which client would benefit from one being opened.

Prerequisites (not rules text): a side session needs the client's `.mcp.json`
and `CLAUDE.md` (same folder works), and the board needs to know which
session picked up a task. `coord_get_task` already records the reader; the
minder's "client went silent" check should key on session, not client.

## 2. DoD reviews in a session that did not produce

**Client deliverables, step 2 (Review), replace the clear procedure with:**

> The reviewer must not have written the file. Dispatch the `dod_review` to
> the client's side session, which never saw the producer's work. With no
> side session, fall back to the old route: the client session clears first
> (`run clear_session "self" and end the turn without starting work`, wait
> for `get_session` `isRunning: false`, dispatch, confirm with `get_usage`).

The side session is reused across reviews; it reads only the spec, the file
and the checklist, so earlier reviews in its context do not make it the
producer. If it ever produced the deliverable under review, use the primary
via the clear route instead.

## 3. Short tasks may queue

**Dispatching, add after the one-per-session paragraph:**

> **Exception: short tasks may queue.** A `draft_reply`, or an
> `investigation` scoped to a single question, may be sent to a busy session
> when that session's `get_usage` reads under 50% context. Such tasks rarely
> park, and at that level the running task is not about to park either. Never
> queue a `code_change`, a `deliverable`, or anything behind a task that has
> parked before.

## 4. Raise Master's auto-clear threshold

**Clearing this session, step 1:** change "default 120k" to the new default,
and set `COORD_MASTER_CLEAR=180000` in the worker's environment. (The mod
default in `mods/master-console/hooks/register.tsx` is 120000; change it too
so the doc and the code agree.)

Why: a fresh worker starts at about 85k, so a 120k threshold cleared it after
roughly one board turn: 77 of 117 worker sessions cleared in a week, often 2
to 5 minutes apart, and each clear is a window where queued prompts stall.
At 180k a worker gets several turns and the hard stop (300k) still leaves
120k of headroom for the handoff turn. Trimming the 85k baseline is the other
lever and is worth measuring separately.
