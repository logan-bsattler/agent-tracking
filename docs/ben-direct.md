# Ben's direct asks (Ben, 2026-11-16)

When Ben asks the Desk or a client session for something himself, it is top
priority, and the board hands it back to him finished without his chasing it.
Code: `coord_mcp/direct.py`. Tests: `tests/test_ben_direct.py`.

## The flag

A task is *direct* when its spec has `"from": "ben-direct"`, or any task above
it does (parent chain, and the code a check-in checks in). Reviews, revisions
and follow-ups of a direct ask are direct too.

| Who | How |
| --- | --- |
| Desk, from Ben's own prompt | `coord_create_task(..., ben_direct=true)` (sets the spec flag) |
| Client session, a reply Ben asked for | `coord_file_draft` (already stamped ben-direct) |
| Client session, any other finished work | `coord_file_work(kind, title, result, asked?, spec?, parent_id?)` |
| Master, relaying an ask Ben typed to it | `coord_create_task(..., ben_direct=true)` |

`coord_file_work` takes investigation, data_pull, code_change, deliverable
(spec needs `deliverable_type`, `dod_path`, `format`) and draft_reply (routed
to `coord_file_draft`). Never a review, never a check-in.

## Priority

- Auto-dispatch (`sessions._candidates`, and `python -m coord_mcp.sessions
  dry-run`) sends direct tasks first, ahead of commitments and oldest-first,
  with no `DISPATCH_AFTER` wait. Resend and retry timers still apply.
- The minder's first list, `ben_direct`, names direct work with no threshold:
  `<id> undelivered`, `<id> unread`, `<follow-up id> untasked`. Those ids are
  left out of the generic lists. A direct id the last minder prompt did not
  carry fires past the rate limit.

## Hand-back

When a direct task lands (`coord_complete_task`, `coord_file_work`,
`coord_file_draft`):

1. Its client loose ends become child tasks at once and are resolved with
   them, behind a `Ben-direct hand-back:` decision. Reply-shaped →
   `draft_reply`; otherwise the source's kind (investigation for a review). A
   `dod_review` that requests changes opens one revision of the reviewed task
   (its kind, spec copied, `findings`). Tasks in a workflow-engine case are
   left to the engine. Delivery is auto-dispatch's, first in line.
2. When the whole chain is finished, it becomes one **ready item**: a
   `Ready for Ben:` decision on the root, a toast from the master-console mod
   (the desk while live, else the worker), and a line on /board's **Ready for
   you** card with a Seen button. Ben's "review and draft" items in the chain
   close behind it. No question is presented.

### Completed state, per kind

| Kind | Finished when |
| --- | --- |
| draft_reply | done, review gate clear (when gated), and in Outlook Drafts (`/drafts` reads `drafted`) |
| deliverable | done and promoted to `<client>/deliverables` (approving review → auto-promote) |
| code_change | done, and its newest dod_review approves when the CVC gate covers it (check-in stays its own step) |
| investigation, data_pull, review | done |

The chain: no open task, no open client loose end, no failed task nothing
replaced, and each standing piece of work (no child of its own kind) finished
as above. A chain waiting on Ben (an escalation, a refused promote) is not
ready: it is already in his queue. The minder tick re-checks every chain, so
a draft master types into Outlook becomes ready on the next tick.

## Interface changes

Additive, no schema bump:

- `coord_create_task` gains optional `ben_direct`.
- New tool `coord_file_work` (team role: 23 tools).
- `coord_complete_task` / `coord_file_draft` results may add `ben_direct`,
  `hand_back_tasks`, `ready_for_ben`, `hand_back_error`.
- Minder owed dict gains `ben_direct`; its prompt text gains a leading
  `N Ben's direct asks` clause. Anything parsing that text by position moves.
- New table `ready_items` (made on first use). Dashboard: `GET /api/ready`,
  `POST /api/ready/claim`, `POST /api/ready/seen`.

Breaking in practice: a direct task's result no longer shows under the
minder's `unread` / `undelivered` / `loose` lists, only under `ben_direct`.
Running master sessions on the old `CLAUDE.md` still act on it (Board check
covers listed ids), but should be told the list exists.

## Proposed instruction text (not applied)

Edits to `C:\development\agents\CLAUDE.md`, `desk\CLAUDE.md` and the client
`CLAUDE.md` files are Ben's to approve: they steer live sessions.

**Master (parent CLAUDE.md), new subsection under Dispatching:**

> **Ben's direct asks come first** (Ben, 2026-11-16; docs/ben-direct.md in
> `agent-tracking/`). A task flagged ben-direct (spec `from`, or any ancestor's)
> is dispatched, read and followed up before anything else. The minder lists it
> under "Ben's direct asks" the moment it is owed. Its client loose ends are
> tasked by the board; dispatch them. Do not present a finished direct ask as a
> question: the board hands it back as a ready item. Draft a direct draft to
> Outlook the moment the minder lists it.

**Desk (desk\CLAUDE.md), under "When Ben's answer calls for agent work":**

> When Ben asks you for work himself (not an answer to a queue item), create the
> task with `ben_direct=true`. It goes out first and comes back to him finished.

**Client CLAUDE.md, after the coord_file_draft section:**

> Other work Ben asks you for in chat (an investigation, a data pull, a
> deliverable, code) goes on the board when it is finished:
> `coord_file_work(kind, title, result={done: true, ...}, asked="<what he asked>")`.
> A deliverable also passes `spec={deliverable_type, dod_path, format}`; CVC code
> passes `spec={cvc: true}`. The board reviews, promotes and hands it back.
> Loose ends for you become tasks; do not message master.
