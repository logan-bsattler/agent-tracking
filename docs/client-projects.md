# Client projects

"What is going on with MAG?" The board answers that one task at a time.
This adds a **project** above the task, so each client has a short list of
workstreams, each with a goal, a state and the tasks under it.

## Why

Measured on the live board, 2026-11-09:

- **Tasks do not roll up.** `pipeline_task_id` is the only upward link, and
  49 of 1,467 tasks carry it (3%). MAG has 939 tasks and 4 links, all to one
  item. Yamamoto, JBSS, GHSP, P&C, TS Tech and Acument have none.
- **Parent chains are lineage, not membership.** 341 root tasks. One
  workstream (MAG 32soft uploads, the Kiro tracker gap, FDD-O2S-1396)
  restarts as a new root every time master tasks it fresh.
- **Decisions float.** 910 of 4,016 have no `task_id`.
- **Narrative lives nowhere.** SharePoint holds status, dates and percent
  complete, but coord never reads it back, and nobody writes a "where this
  stands" line that survives a `/clear`.

## Shape

A project is a client workstream: one development item (an FDD, a CVC
change), one engagement thread (32soft migration, go-live cutover), or a
standing bucket (`MAG: support`). It is **not** a SharePoint pipeline item:
many workstreams have none, and some span several. It links to zero or more.

```sql
CREATE TABLE IF NOT EXISTS projects (
  id          TEXT PRIMARY KEY,           -- 12 hex, like tasks
  client      TEXT NOT NULL,              -- the assigned_to key
  title       TEXT NOT NULL,              -- "32soft upload migration"
  goal        TEXT,                       -- one line: what done looks like
  state       TEXT NOT NULL CHECK (state IN ('active','waiting','done','dropped')),
  pipeline_ids TEXT,                      -- JSON list of SharePoint item ids
  summary     TEXT,                       -- where it stands, <= 600 chars
  summary_at  INTEGER,                    -- when summary was last written
  summary_by  TEXT,                       -- task id of the project_status that wrote it, or 'master'/'ben'
  next_step   TEXT,                       -- one line
  sp_snapshot TEXT,                       -- JSON: SharePoint fields as last read (status, dates, %); display only
  created_at  INTEGER NOT NULL,
  closed_at   INTEGER
);
CREATE INDEX IF NOT EXISTS projects_client ON projects(client, state);
-- tasks gains: project_id TEXT REFERENCES projects(id)
-- decisions gains: project_id TEXT REFERENCES projects(id)
```

**Membership is inherited.** A task's project is its own `project_id`, else
its parent chain's, else (for a check-in) the code it checks in: the same
walk as `queue.pipeline_id`, factored into one helper. Most tasks never set
it directly; only roots do.

**Pipeline links come from the project.** `pipeline_id()` falls back to the
project's single pipeline id when the chain has none. A project with several
pipeline ids gives no fallback (the task must say which). This is what lifts
the 3%: link the project once and every task under it bills correctly.

**Decisions** take `project_id` explicitly or inherit from `task_id`. The
910 orphans stay orphans unless the backfill places them.

**SharePoint stays the system of record.** `sp_snapshot` is a read copy,
shown with its age, never edited on the board and never written back. Status,
dates, estimates and percent complete remain Ben's.

## Now: dashboard and CLI (no schema bump) — shipped

Same pattern as `task_deps` and `promotions`: created on first use, so live
coord servers are untouched.

1. **Storage.** `db.PROJECTS_DDL` plus `db.ensure_projects(conn)`, which
   creates `projects` and adds `tasks.project_id` (indexed) when missing.
   `SCHEMA_VERSION` stays 11. `decisions.project_id` waits for the restart:
   until then a project's decisions are the decisions on its tasks.
2. **CLI** (master or Ben): `python -m coord_mcp.projects`
   `add`, `set`, `attach`, `detach`, `list`, `show <client>`, and
   `dump <client>` (root tasks, for the backfill).
3. **Pipeline fallback.** `queue.pipeline_id` falls back to the project's
   pipeline item when the project names exactly one, so `create_warnings`
   and the minder's pipeline-note check see it.
4. **`/client/<key>`.** One card per live project, active first, then by
   latest activity: goal, summary with its age (stale after 7 days while
   work is open), next step, SharePoint ids and snapshot, then open tasks
   (with waits), loose ends, deliverables with stage (written, review
   verdict, promoted vN), recent decisions, recently finished. Each list
   shows six and folds the rest. An Edit form per card (Ben's edits stamp
   `summary_by=ben`). An Add project form in the topbar. **Unfiled**: the
   client's root tasks from the last 30 days with no project, with
   checkboxes and one Attach control. Closed projects fold into one card.
   Writes go through `POST /api/project` (`add`, `set`, `attach`).
5. **`/clients`** (in the nav). One row per client: active, waiting, open
   tasks, loose ends, unfiled roots, percent filed, stale summaries, oldest
   summary, live project titles.
6. **Links in.** The task page names the project (own or inherited). The
   board's client scope line links to `/client/<key>`.
7. **Not in phase one:** per-project hours. `hours.breakdown` takes about
   8s for one client over 70 days, too slow for a page load; the client
   page links to `/hours?client=` instead. Board rows do not show the
   project yet.

Nothing an agent sees changes in this phase.

## Next restart: the agent side

Breaking where marked. Ship when every session restarts together.

1. **Schema (breaking for running servers).** Fold `PROJECTS_DDL` and the
   two columns into `DDL`, bump `SCHEMA_VERSION` to 12 (together with the
   `task_deps` move if that has not shipped). Keep `ensure_projects` as a
   no-op guard for one release.
2. **`coord_create_task` gets `project_id` (additive).** Unknown id or a
   project of another client refuses the create. A new warning joins
   `create_warnings`: a root task (no parent) with no project gets "no
   project_id: it will be Unfiled on /client/<key>". Advice, never a refusal.
3. **New tool `coord_project` (lead only).** `action`: `create`, `update`,
   `attach`, `close`. Returns the project row. Master uses it instead of the
   CLI.
4. **`coord_board` (lead-facing, additive).** Each task carries
   `project_id`. A new top-level `projects` list per client: id, title,
   state, open task count, summary age. Lets master see "MAG has three
   active projects" without a second call.
5. **`coord_record_decision` gets `project_id` (additive).**
6. **New task kind `project_status` (contract change: new kind).** Spec
   names one project. The client agent reads the project's tasks, results
   and decisions through `coord_get_task` / its own files, reads the linked
   SharePoint items with `sharepoint-pipeline`, and returns:
   ```json
   {"project_id": "..", "summary": "<= 600 chars", "next_step": "..",
    "state": "active|waiting|done", "sp_snapshot": {"<pipeline id>": {..}},
    "gaps": ["SharePoint says 80%, board shows the code still in review"],
    "follow_ups": [..]}
   ```
   `coord_complete_task` writes `summary`, `next_step`, `sp_snapshot` and
   `summary_by` onto the project. `state` is advice: master applies it.
   `gaps` become loose ends for Ben, since SharePoint fields are his.
   It is an internal read, so not a deliverable and no `dod_review`.
7. **Teammate side: `coord_get_task` (additive).** Returns the project's
   title, goal and summary, so a fresh session knows the workstream it is
   working in without a longer spec.
8. **The minder.** Two new "master owes" items: a project summary older
   than 7 days while the project has open tasks (dispatch a
   `project_status`), and more than 5 unfiled roots for one client.

## Backfill

One `investigation` per client, side session where one exists, run after
the "Now" phase ships:

- Input: the client's tasks (titles, kinds, parents, pipeline links) via a
  CLI dump `python -m coord_mcp.projects dump <client>`, and its pipeline
  items from `sharepoint-pipeline`.
- Output: a proposed project list, each with title, goal, pipeline ids and
  the root task ids it claims, plus the roots it could not place.
- Ben approves on `/client/<key>` (an Accept proposal button applies it via
  the CLI). Nothing is attached without that click: the grouping is how he
  thinks about the client, so it is his.

MAG goes first: 939 tasks, and its titles already cluster.

## Master `CLAUDE.md` changes (with the restart)

- **Dispatching:** every root task gets a `project_id`. If the work fits no
  project, create one; `<Client>: support` is the catch-all, not "none".
- **Each turn:** a result that changes where a project stands
  (a deliverable promoted, code checked in, a client reply) gets a one-line
  `coord_project update` to `next_step`. The full summary is the
  `project_status` task's job, not master's.
- **Weekly:** each active project with open tasks gets a `project_status`,
  batched per client on its side session.

## Tests

- `ensure_projects` on a v11 board: creates, is idempotent, leaves
  existing rows alone.
- Inheritance: project via parent, via `checkin_of`, and the pipeline
  fallback (one id gives it, several give none).
- `/client/<key>` and `/clients` render against a fixture board with
  unfiled roots.
- After the restart: stdio round trip of `coord_project`, `project_id` on
  create, the `project_status` contract, and one project created and
  attached in `smoke_stdio.py` lead role.

## Open questions

1. **Granularity.** One project per FDD, or one per engagement thread with
   FDDs as tasks under it? Proposal: per deliverable stream (an FDD that
   ships its own code is a project), plus a `support` bucket per client.
   The backfill will show whether that is too fine for MAG.
2. **Cross-client work** (Logan tooling, a QAD pattern reused at two
   clients). Proposal: out of scope; it stays task-only under `Master`.
3. **Should `/client/<key>` be shareable with the client?** Proposal: no.
   SharePoint is what clients see. If a status report is wanted, it is a
   `deliverable` built from this page, through the DoD gate.
