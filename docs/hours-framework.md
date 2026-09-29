# Billable hours framework

Status: designed, not built. Decision `8f4ed8b96bf9`. Build all of it; ship the
SharePoint push **disabled**.

## Goal

Daily billable hours per client per task for a date range, drawn from what the
board and the usage ingest already record, plus Ben's own time. Later pushed to
SharePoint (`sharepoint-pipeline`), which stays the system of record.

## What exists

- `requests` (usage ingest): one row per API request with `ts`, `session_id`,
  `cwd`/`project`. Each client works in its own folder
  (`~/OneDrive - Logan Consulting/Documents/<client key>`), so cwd → client.
- `tasks.picked_up_at` (last pickup only, overwritten), `created_at`,
  `completed_at`; `task_parks.created_at`.
- `server.READER` = client key from the server's cwd (or `COORD_CLIENT`).

## Build

1. **`task_events` table** (schema bump + `_migrate`): `id, task_id, client,
   event ('picked_up'|'parked'|'completed'|'failed'), ts`. Written by
   `store.get_task` (assignee read only, same rule as `picked_up_at`),
   `park_task`, `complete_task`. Keep `picked_up_at` as is.
2. **Active time** (`hours.py`): for a client's requests, sum gaps between
   consecutive requests, capping each gap at `IDLE_GAP` (default 300s; a longer
   gap counts as `IDLE_GAP`, not zero, so a request's own work is not lost).
   Split by local calendar day.
3. **Attribution**: a task's intervals are pickup → next park/complete/fail for
   that task. Client active time inside an interval goes to the task; outside
   any interval goes to an `unassigned` row for that client. Overlapping
   intervals on one client: split evenly (rare: one session per client).
   Tasks before `task_events` existed: estimate with created_at → completed_at
   and flag `estimated: true`.
4. **Operator time**: `time_entries` table (`id, task_id nullable, client,
   day, minutes, note, created_at`) and a `coord_log_time` tool (lead only by
   convention) so Ben's review/Kiro time is billable too. Agent and operator
   minutes stay separate columns; total = both.
5. **SharePoint link**: optional `pipeline_task_id` on tasks (new column,
   settable at create and via a `coord_set_pipeline_task` tool). Hours roll up
   by it; rows without one roll up under `unassigned`.
6. **Output**: `hours.breakdown(conn, start, end, client=None)` →
   `[{day, client, task_id, title, pipeline_task_id, agent_min, operator_min,
   estimated}]`. Exposed as `coord_hours` tool (named fields, capped) and an
   `/hours` dashboard page (day × client table, per-task drill-down, CSV
   download). Round to 0.25h only at display, never in storage.
7. **SharePoint push, off**: `hours.push_to_pipeline(rows, dry_run=True)`
   builds the per-task payload (task id, date, hours, note) and returns it.
   Gated by meta key `hours_push_enabled` (absent = off); with it off, the
   function never calls out and the page shows "push disabled". No scheduled
   job. Turning it on is a later decision.

## Tests

Idle-gap capping, day split at midnight, attribution inside/outside
intervals, park → re-pickup continuity, estimated fallback, operator entries
summed separately, push stays dry-run when the flag is absent.

## Caveats to show on the page

Agent time is not Ben's time; the numbers are a draft for Ben to edit before
billing. Sessions outside client folders (master, Posey) are excluded.
