# Pipeline lines

Every task rolls up to one SharePoint pipeline item: the same line Ben enters
on his timesheet (Ben, 2026-11-15). Ideally the item title carries its origin
id, e.g. `DEV-39 Sales Order Change Audit` or `L04102-86 4.23 / 4.24 Sales
Order Alloc - No Update`. MAG's work needs real items, not just "Design" (#234).

## Why

Measured 2026-11-15: 52 of 1,694 tasks carry `pipeline_task_id` (3%); the
`projects` table (docs/client-projects.md) has 0 rows. Linking was advice
(`queue.create_warnings`) and the missing item was the blocker: master could
not link work to an item that did not exist, and creating items is Ben's.
Pipeline lines supersede the project layer as the roll-up.

## Built (commit 57a0f7e, `coord_mcp/pipeline.py`)

- **Cache** `pipeline_items`: get_pipeline's answer, refreshed by the mod.
  `map_client` maps SharePoint names to board keys ("Royal Technologies" ->
  Royal); overrides via `pipeline client-map <key> <name>`.
- **Proposals** `pipeline_proposals`: a task links as `p:<id>` until the item
  exists. One Ben item per client ("Approve N proposed pipeline items for
  <client> on /pipeline"). `decide` approve / link / drop; `claim_create` +
  `create_done` for the mod; `link` relinks every `p:<id>` task to the item.
- **Rule** `check_create` (switch `require_pipeline`, on): a root task for a
  client must pass `pipeline_task_id` or `pipeline_proposal {title,
  origin_id?, priority?, note?}`. Children and check-ins inherit. Breaking for
  master once servers restart. Tests enforce it only under marker
  `pipeline_rule`. Keys in `NO_LINE` (`Internal`, Logan's own work) bill to
  no item: exempt from the rule and never listed as unfiled.
- **View data** `lines()` ranked by priority, overdue, end date, activity;
  `unfiled()` roots with no line.
- **Backfill** `pipeline backfill dump <client> --out f.csv` -> an
  investigation fills `pipeline_id` or `proposal_title`/`origin_id` -> Ben
  reviews -> `pipeline backfill apply f.csv --yes`. MAG first.

## Built (steps 1-4, 2026-11-15)

1. `coord_create_task` takes `pipeline_proposal` (server.py, README).
2. `/pipeline` (`coord_mcp/pipeline_page.py`): proposals (Create / Use
   existing / Drop -> `POST /api/pipeline/proposal`), approved awaiting
   creation with their errors, the ranked lines, a red No pipeline item list
   with a set form (`POST /api/pipeline/set`), cache age, `?client=`. NAV
   after Board; a strip on `/board`.
3. `/api/pipeline/sync` (form `payload`), `/api/pipeline/create/claim`,
   `/api/pipeline/create/done`. master-console (worker only) syncs
   get_pipeline every 30 minutes and creates one approved proposal a tick.
   An ok answer with no parsable item id is held, never retried (a retry
   could make a duplicate item); Ben links it with Use existing.
4. `tests/test_pipeline.py`; smoke passes `pipeline_task_id` and one
   `pipeline_proposal`.

## Remaining

5. Ben approves the master `CLAUDE.md` paragraph (every root task names
   `pipeline_task_id` or `pipeline_proposal`, titled as the timesheet line).
6. Ben restarts the coord servers and master sessions (breaking: root
   client tasks with no line are refused), reloads master-console, and runs
   the MAG backfill.
