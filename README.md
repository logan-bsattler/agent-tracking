# coord-mcp

A typed task board for Claude Code agent teams. One stdio MCP server per
session, one SQLite file between them, eleven tools.

Claude Code's agent teams already handle spawning teammates, messaging and
lifecycle. This adds the three things they don't:

- **Typed results.** Each task kind has a contract. `coord_complete_task`
  validates against it server-side and rejects anything off-shape, so a
  teammate cannot hand the lead a wall of prose.
- **Durable decisions.** A separate table from results. Results say what
  happened, decisions say why. This is what makes `/clear` on the lead safe.
- **Intents as a quarantine.** Anything authored outside the system (an email
  read in Claude Desktop) lands in `intents`, never in `tasks`. Teammates only
  read `tasks`, so external content cannot reach a worker until the lead has
  reviewed it and written the spec itself.

## Install

```bash
pip install -e ".[test]"
python -m pytest -q             # 30 tests
python tests/smoke_stdio.py     # end-to-end over real stdio, both roles
```

Requires Python 3.11+ and `mcp>=2.0`.

## Wire it up

| File | Goes to |
| --- | --- |
| `examples/team.mcp.json` | the project's `.mcp.json` |
| `examples/CLAUDE.md` | the project's `CLAUDE.md` (lead and teammate sections) |
| `examples/desktop.mcp.json` | Claude Desktop's MCP config |

The statusLine hook for usage tracking is wired by `python -m coord_mcp.usage setup`, see below.

Enable agent teams with `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`. Lead and
teammates share one project, so they share one `.mcp.json` and one tool
surface. The lead/teammate split is in `CLAUDE.md`, not in role gating.

| Var | Default | Meaning |
| --- | --- | --- |
| `COORD_ROLE` | `team` | `team` (all 11 tools) or `desktop` (2 tools) |
| `COORD_DB` | `~/.coord/coord.db` | **Local disk only.** WAL plus OneDrive corrupts. |

## Flow

```
lead:      coord_create_task(kind, title, spec)  ->  task_id
lead:      "Do task <id>. Start with coord_get_task."      (SendMessage)
teammate:  coord_get_task(id)  ->  spec + expected_result_shape
teammate:  ...work...
teammate:  coord_complete_task(id, result)   rejected? fix, resend
lead:      coord_get_task_result(id, fields=["verdict"])
lead:      coord_record_decision("...", because="...")
```

From Claude Desktop:

```
email -> coord_propose_intent -> intents (open, untrusted)
                                    |  lead reviews when planning
                             coord_create_task(from_intent=...)
                                    |
                             coord_resolve_intent(task_ids=[...])
```

## Tools

| Tool | Role | Purpose |
| --- | --- | --- |
| `coord_board` | team | counts by kind and state, open/failed task lines, open-intent count |
| `coord_create_task` | team | put a task on the board, get the contract echoed back |
| `coord_get_task` | team | teammate reads spec and expected result shape |
| `coord_complete_task` | team | close with a validated result, or a typed failure |
| `coord_get_task_result` | team | read a result projected to named fields |
| `coord_record_decision` | team | write a decision and why |
| `coord_get_decisions` | team | standing decisions, superseded ones hidden |
| `coord_list_intents` | team | review the external queue |
| `coord_resolve_intent` | team | convert to tasks or dismiss |
| `coord_propose_intent` | team, desktop | queue something that might warrant work |
| `coord_board_summary` | desktop | counts only, nothing else |

The desktop role never has the other nine registered. They cost no context
there and cannot be tried.

## Task kinds

`code_change`, `investigation`, `review`, `data_pull`. Contracts live in
`coord_mcp/contracts.py`; adding a kind is one dict entry. Every result has a
required `done: bool`, and any kind may instead return
`{"done": false, "reason": "<=200", "retryable": bool}`.

On rejection nothing is written. The error lists every offending field with
its path, then prints the full expected shape, so the teammate fixes and
resends in one turn.

## Usage tracking

Answers "what did I spend the week's limit on", per session, project, model
and hour, across every Claude Code session on the machine.

```bash
python -m coord_mcp.usage report --days 7 --open   # ~/.coord/usage.html
python -m coord_mcp.usage status                   # same numbers as text
python -m coord_mcp.usage ingest                   # just pull new data
```

**Source.** Claude Code writes every API request's usage block into its own
transcript files under `~/.claude/projects/`, with model, timestamp, session
and working directory. Ingestion reads those incrementally by byte offset, so
it covers history you already have and costs about a second per run. Subagent
transcripts are attributed to their parent session and flagged.

**Spend** is API-equivalent dollars at published rates, the same figure Claude
Code's `/cost` shows. Subscription limits are not billed in dollars, but this
is the best proxy for how heavily a request weighs. Cross-checked against
Claude Code's own per-session totals: within a few percent.

**Current limits** come from the statusLine hook:

```bash
python -m coord_mcp.usage setup
```

That merges a `statusLine` entry into `~/.claude/settings.json` (backing it up
first, refusing to replace an unrelated statusLine unless `--force`), using
the absolute path of the interpreter you ran it with so PATH doesn't matter.
Every prompt then prints `Opus | ctx 42% | 5h 61% (2h10m) | 7d 88% (3d)` in
the status bar and records a snapshot, which is what draws the
limits-over-time chart. The hook never raises and prints nothing on a bad
payload. If `rate_limits` is absent from your Claude Code version, the tile
says so and everything else still works.

### Right now: the live window, alerts, and the guard

```bash
python -m coord_mcp.usage live          # burn rate, projection, who is spending
python -m coord_mcp.usage serve --open  # live dashboard, refreshes every 60s
```

The status line itself shows the burn rate (`12%/h`) and, when the pace would
hit 100% before the window resets, `!! 100% by 14:20`.

**Alerts** are desktop notifications (Windows toast, macOS notification
centre, notify-send) fired from the statusLine hook when the 5-hour or weekly
limit crosses into the warn or hard band, and once when the projection first
says you will hit the wall before the reset. One alert per band per reset
window, so a long session doesn't nag.

**The guard** is the part that acts. `setup` wires two hooks into
`~/.claude/settings.json`, `PreToolUse` and `UserPromptSubmit`, which run in
every session on the machine: the lead, teammates, subagents, anything. On
each tool call and each prompt they read the session's own transcript tail
(the last request's token count is the session's context size) and the latest
limit snapshot, then:

| Condition | Default | Action |
| --- | --- | --- |
| context ≥ `COORD_CTX_WARN` | 150k tokens | inject a warning: keep the turn short, /compact soon |
| context ≥ `COORD_CTX_HARD` | 300k tokens | **block** the tool call or prompt until /compact, /clear, or hand-off |
| 5h ≥ `COORD_WARN_5H` / `COORD_HARD_5H` | 75% / 92% | warn / **block** |
| 7d ≥ `COORD_WARN_7D` / `COORD_HARD_7D` | 85% / 97% | warn / **block** |
| projected 100% before reset | | warn, with the time it hits |

Why context size: every tool call re-reads the whole context, so cost per
turn is context times tool calls. A 300k-token session on Opus pays about
15 cents of cache reads per tool call; one busy turn is $6. That is the
runaway case, and it is invisible in the percentage until it has already
happened. The block message tells the model exactly why and what to do, so a
teammate that hits it hands back cleanly instead of failing.

A block is exit code 2 from the hook: for a tool call the reason goes to the
model and the turn ends; for a prompt the reason goes to you and nothing is
spent. To get past it deliberately:

```bash
python -m coord_mcp.guard pause 30      # minutes; `resume` to end early
```

or `COORD_GUARD=off` in the environment. `python -m coord_mcp.guard check
<transcript.jsonl>` shows what the guard would decide for a session.

### Installing on another machine or account

Nothing here is tied to this machine. On the second machine:

```bash
git clone <this repo> coord-mcp && cd coord-mcp
python -m pip install -e .
python -m coord_mcp.usage setup
python -m coord_mcp.usage report --open
```

The report is built from whatever transcripts that machine has, so it is
correct for that account from the first run. Two knobs if the layout differs:
`CLAUDE_CONFIG_DIR` if Claude Code's data isn't under `~/.claude`, and
`COORD_DB` if you want the board file somewhere other than `~/.coord`. Two
accounts on one machine share `~/.claude`, so their usage lands in the same
tables; keep that in mind before comparing weeks.

The report reads top-down: limits now, limits over time, spend per day by
model, spend by hour of day, then models, projects and sessions with cache-hit
rate and subagent share. Light and dark, no JavaScript beyond tooltips.

## Deliberately not here

- No sessions, heartbeats or stale reaping. Agent teams own teammate lifecycle.
- No claim queue. The lead assigns by handing over a task id.
- No messaging. Agent teams have `SendMessage`.
- No pre-flight cost estimation. Output can't be counted before it exists, and
  the history that would predict it is now in the usage tables if you want it.
- No `.mcpb` bundle. Point Claude Desktop at `desktop.mcp.json` until the
  two-tool surface has earned packaging.
