# Session upkeep in code (2026-11-24)

`coord_mcp/upkeep.py` does four chores Master and Ben used to do by hand.
Each has its own switch (`python -m coord_mcp.queue switch <name> on|off`),
all on by default. `/upkeep` on the dashboard and `python -m coord_mcp.upkeep
status` show the state. The mod part runs only where `COORD_AUTO_DISPATCH=1`.

| Switch | What it does |
| --- | --- |
| `vpn_watch` | Every `vpn_interval_minutes` (3), probe TCP 22 on Moog, Royal and Cascade's servers (no login). A task failed with a retryable VPN reason is reopened once the server has answered since the failure; auto-dispatch then sends it. A park saying the VPN is down waits the same way. Furlani/Yamamoto are not watched. 3 reopens, then master. |
| `auto_redispatch` | A parked task (under 3 parks, no decision since the park) is re-sent: get_session until idle, then the same `Do task` line. |
| `clear_check` | After every delivered send, get_usage: a Messages category over `clear_max_messages_k` (40k) means the clear did not fire. The session is told to `clear_session "self"`, and once idle the task is sent again. Two clears that do not help: flagged to master. |
| `code_restart` | Each coord server records the hash of its code. A registered client session on old code, free and quiet for `restart_idle_minutes` (2), gets its claude.exe ended once get_session reads not running; the app restarts it on the new code at its next message. Never Master, the desk, or unregistered sessions. It gets no new dispatch until then. |
| `auto_review_dispatch` | A `dod_review` (created by the board when a deliverable, or gated CVC code or email, lands done) goes to the client's side session by plain auto-dispatch. With no side session, or when the side session wrote the work, upkeep waits for the primary to be free and idle, asks it to `clear_session "self"`, waits for idle again, sends the review and checks the clear with get_usage. |

What is left to master shows in the minder: `parked` (3+ parks, or a
decision since the park) and the new `upkeep` (a flag: clear failed twice,
sends failing, VPN reopen loop). `python -m coord_mcp.upkeep drop <key>`
forgets a row once handled.

## Master manual changes (applied to `C:\development\agents\CLAUDE.md` on 2026-11-24; take effect once this is merged and the dashboard and mod run it)

**When a client parks**: add at the top:

> Automatic (switch `auto_redispatch`, since 2026-11-24): the board waits
> for the session to go idle, re-sends the same dispatch line and checks the
> clear with get_usage. Do nothing for a park unless the minder lists it:
> then it has 3+ parks (split it) or a decision since the park, and the
> manual steps below apply.

**Dispatching**, the list "Master still owns": replace "parked tasks and
their re-dispatch" with "parked tasks the minder lists (3+ parks, or held by
a decision); the rest re-dispatch in code (docs/session-upkeep.md)". Add:
"A clear that did not fire is fixed in code (switch `clear_check`); the minder
lists it under `upkeep` after two failed clears." Add: "Client sessions on
old coord code restart in code (switch `code_restart`): do not ask Ben to
restart them."

**Delivery changes what a result means**: the Moog/Royal/Cascade bullet
becomes:

> Moog, Royal, Cascade can legitimately return `{"done": false, "reason":
> "VPN down", "retryable": true}`. That is waiting, not failed. The VPN
> watch (switch `vpn_watch`) reopens the task once the server answers, and
> auto-dispatch sends it: no decision or re-dispatch needed. The minder
> lists it only after 3 reopens (under `upkeep`).

## Dispatch automation (2026-11-24)

Switches `auto_revise`, `auto_queue` (setting `queue_max_context_pct`),
`auto_task_loose` (now on) and `auto_checkin_task` finish the automation of
master's rule-following dispatch work. The parent `C:/development/agents/CLAUDE.md`
"Dispatching" section lists them and what master still owns (judgement only).
