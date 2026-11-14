# Workflow build brief (approved by Ben 2026-11-14, decision 66bbfd41ad66)

For a fresh maintainer session in `C:\development\agents\agent-tracking`.
Read `docs/workflow-catalog.md` first, then
`docs/master-procedures-in-code-review-2026-11-13.html`.

Goal: agents handle as much of Ben's daily workload as possible so he can be
present in meetings. Each workflow becomes a first-class board object.

Constraints: schema and contract changes are additive, with a migration, and
you say what needs a restart. Nothing ever sends email, posts to Teams or
changes client-visible SharePoint status. Client deliverables are never
Markdown. Commit before pull, message via `-F`, run `python -m pytest -q
tests/` and `python tests/smoke_stdio.py` before pushing. Say "Devoir", never
"offshore".

Build in this order, committing each step once both suites pass:

1. `python -m coord_mcp.workflows mine` (read-only): tag every past task and
   intent to a catalog workflow from kind, title, spec fields, parent chain
   and intent source. Report counts per workflow, untagged leftovers grouped
   by pattern, median cycle time and Ben loose ends per workflow. Revise the
   catalog from the findings. Results to `docs/workflow-mining-<date>.html`.
2. Definitions: one file per workflow (ordered steps; per step the task kind,
   who does it: agent, code or Ben; its gate; what starts the next step).
   Cases: one instance per trigger linking intent, tasks, decisions, draft
   and commitment; case id and step on each task. Backfill from step 1.
3. `/workflows` page: every open case at its step, time there, who it waits
   on; per workflow, cycle time, Ben touches, stall points. Link from /board.
4. Engine: the dashboard loop advances a case when a step's result lands,
   behind an env switch like `COORD_AUTO_DISPATCH`. Start with status
   request, defect report, FDD-to-production. Then define promotion ask (see
   `..\process\cvc-reopen-ticket.md`), FDD review assigned to Ben, and DEVL
   refresh recovery.
5. Per-meeting briefs: for each meeting on Ben's calendar (see
   `coord_mcp/meetings.py`, `docs/meeting-listener-architecture.html`), a
   `/brief/<meeting>` page built shortly before it starts: that client's open
   cases and where each stands, commitments due, drafts waiting on him,
   questions he owes, and what changed since the last meeting with those
   people. List today's briefs on the dashboard.
6. Fewer rulings: when a result hands Ben per-row rulings (workbooks under a
   client's `work/`, mostly MAG), match each row against his past rulings
   recorded as decisions. Covered rows are applied as "precedent; Ben may
   override" citing the decision id; only rows with no precedent reach his
   queue. First a read-only report of how many open Ben loose ends precedent
   would settle; the apply step stays behind a switch that is off until Ben
   has seen that report.

Done: all six merged and pushed with tests; switches off by default except
read-only pages; a short HTML summary in `docs/` saying what is live, what
each switch does, what needs a restart, and what Ben must decide. If a step
needs a design call that changes what Ben sees or what is done in his name,
stop that step and ask; carry on with the others.
