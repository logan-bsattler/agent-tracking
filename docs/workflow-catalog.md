# Workflow catalog (revised from mining, 2026-11-14)

Internal. Source: master `CLAUDE.md`, board counts (about 2,500 tasks), the
last 100 converted intents, live and failed task titles, `process/`, `dod/`.
Checked against every task and intent by `python -m coord_mcp.workflows mine`
(build step 1; report in `docs/workflow-mining-2026-11-14.html`). All 1,674
tasks and 406 of 418 intents now tag to a workflow; revisions are marked
**(mined)**.

"Ben" marks the steps only he does today. "In code" means the board or
dashboard advances the step without master reasoning about it.

## A. Triggered by something arriving

| # | Workflow | Steps | In code today | Ben |
|---|---|---|---|---|
| A1 | Status request (email, Teams) | ingest > client > status read > draft > review > Outlook draft > send | ingest, review creation, Outlook listing | review, send |
| A2 | Target-date ask | ingest > propose date from board/plan > Outlook draft > send (1 business day) | ingest | confirm date, send |
| A3 | Defect or change report on delivered work ("not in TEST", "export wrong") | ingest > investigation > code_change > review > check-in > pipeline note > reply | review creation, pipeline note | check-in approval, send |
| A4 | Promotion ask (push to TEST, weekend CVC list, QSM) | ingest > what is ready > CVC ticket moves > QSM ticket > confirm install > reply | none (one process file, one skill) | most of it |
| A5 | FDD review assigned to Ben | ingest > read FDD and code > findings and questions > reply or tracker comment | none; no defined workflow | review, send |
| A6 | Decision ask (scope, price, staffing) | ingest > draft around the options > Ben decides > send | ingest | decide, send |
| A7 | Evidence that closes open items (email, Teams) | ingest > match loose ends > decision > resolve | all, at id-match | none |
| A8 | Commitment Ben made (sent mail, Teams, meeting) | ingest > commitments row > link work > due chase > close on evidence | row, due ordering, minder | deliver, re-date |
| A9 | Meeting action item | transcript > intent > task(s) > result > reply or deliverable | import, intent | varies |
| A10 | Ben's note to himself | ingest > task | ingest | none |
| A11 | Technical question **(mined)** (how does X work, can QAD do Y, where is Z) | ingest > investigation > answer or reply | ingest | reads answer |

## B. Pipeline development

| # | Workflow | Steps | In code today | Ben |
|---|---|---|---|---|
| B1 | FDD or requirement to production | spec in > sizing > approve estimate > architect handoff > build > dev compile > review > check-in > pipeline note > test evidence > promote DEVL/TEST/PROD > UAT sign-off | review creation, check-in gate, pipeline note | estimate approval, status/dates, promotion asks, sign-off |
| B2 | Repair loop (pipeline item back "In Repair"); **(mined)** shows as a loop inside a B1 case, not its own root | pipeline change > investigation > fix > B1 from review on | intent | as B1 |
| B3 | Environment refresh recovery (DEVL refreshed from PROD) | notice > list in-flight work > confirm to QAD > restore setup > re-verify | none | confirm to QAD |

## C. Documents and analysis

| # | Workflow | Steps | In code today | Ben |
|---|---|---|---|---|
| C1 | Client document (sizing, FDD plan, status report, meeting material) | work/ draft > review > up to 2 rounds > Promote > covering email | review creation, Promote gate | Promote, send |
| C2 | Tracker text (RAID log, Jira, ServiceNow) | draft text > review > paste | review creation | paste |
| C3 | Running analysis (MAG breakdown, wave plan, cancel list) | new input > regen workbook > rulings workbook > Ben rules > regen | grouping of rulings | every ruling |
| C4 | Meeting preparation | calendar > per-meeting brief (open items, commitments due, drafts waiting) | none | reads it |

## D. Upkeep

| # | Workflow | Steps | In code today | Ben |
|---|---|---|---|---|
| D1 | Board hygiene | minder > park and re-dispatch > cleanup > 14-day expiry > self-clear | most | none |
| D2 | Hours | transcripts and meetings > draft minutes > edit > bill | draft | edit, bill |
| D3 | Hourly triage and morning recap | sweep mail/Teams > intents > recap | all | reads it |
| D4 | Client onboarding | folder > handoff scripts > key > session > characterize | skill | key install |
| D5 | Definition-of-done checklist **(mined)** | draft dod/<type>.md > review > Ben approves | review creation | approve |
| D6 | Tooling and method **(mined)** (Kiro/Lynx trials, agent-tracking) | investigation or build > result | none | reads it |

## What the mining says (2026-11-14)

- 407 cases (a root task and everything under it). Largest by tasks: C3
  Running analysis 510, B1 FDD to production 163, A1 Status request 164,
  D1 Board hygiene 143, D5 DoD checklists 139, A5 FDD review 112.
- Open Ben loose ends by workflow: C3 206, A1 78, A11 51, D5 51, D1 48,
  A5 43, A9 40, B1 39. C3 is where precedent rulings (build step 6) pay.
- Median cycle, finished cases: A1 and A4 under a day; A3 defects 12.5 h;
  B1 10.3 h per case (a case is one root chain, not the whole FDD).
- A7, A8, A10, D2, D3 have no task cases: their cases are intents,
  commitments and code. A9 is mostly a source: 101 intents, 10 cases.

## What the evidence says (first pass)

- Board work by kind: investigation 570, code_change 546, deliverable 226,
  dod_review 204, draft_reply 110, data_pull 15.
- Ben's backlog is the constraint: 758 open loose ends, 447 deferred. Most are
  C3 rulings for MAG.
- Task kind is not workflow. C3 regen work was filed as `code_change`, and
  A4 and A5 have no shape at all, so nothing can measure or advance them.
- Of the last 100 intents, 60 came from email and 38 from meetings.

## Proposal

Make the workflow a board object, not prose in the manual.

1. **Definition**: one file per workflow: ordered steps, each with task kind,
   who does it (agent, code, Ben), its gate, and what starts the next step.
2. **Case**: one instance per trigger, linking the intent, its tasks,
   decisions, draft and commitment. Every task carries a case id and step.
3. **Engine**: the dashboard loop advances a case when a step's result lands
   (extends the four procedures moved to code on 2026-11-13). Master handles
   exceptions only.
4. **/workflows page**: every open case at its step, how long it has sat
   there, and who it waits on. Per workflow: cycle time, Ben touches, where
   cases stall.

Build order: (1) read-only `python -m coord_mcp.workflows mine` that tags
every past task to a workflow and reports the leftovers; (2) definitions and
case id; (3) the page; (4) engine for A1, A3, B1; (5) A4, A5, C3, C4.
