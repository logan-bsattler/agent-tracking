"""Catalog revisions the step-1 mining led to, printed in its report.
Kept beside the code so the report and docs/workflow-catalog.md agree."""

REVISIONS: list[str] = [
    "<b>Added A11 Technical question</b>: a client or Ben asks how something works, whether QAD can do X, "
    "or where X lives. 33 cases that fit nothing else; one investigation and an answer, usually no reply task.",
    "<b>Added D5 Definition-of-done checklist</b>: drafting <code>dod/*.md</code>. 16 cases but 139 tasks and "
    "206 Ben loose ends (each draft is reviewed and approved by Ben), so it is real recurring work, not upkeep noise.",
    "<b>Added D6 Tooling and method</b>: Kiro and Lynx trials, agent-tracking, repo state. 11 cases.",
    "<b>A7, A8, A10, D2, D3 have no task cases</b>: they live as intents, commitments and code, not tasks. "
    "Their cases are counted from intents (A7 30, A8 13) and need no task steps in their definitions.",
    "<b>B2 Repair loop is a step of B1, not its own root</b>: repairs show as children of a B1 case "
    "(re-run tests after a check-in, round-N fixes). Defined as a loop inside B1 with its own trigger.",
    "<b>A9 Meeting action item is mostly a source, not a workflow</b>: 101 intents but only 10 cases; "
    "most meeting intents become A11, C3 or C4 work. Its definition stops at routing.",
    "<b>A5 FDD review assigned to Ben is large</b>: 18 MAG cases, 112 tasks, 43 open Ben loose ends; "
    "it gets a definition (write, review, Ben, tracker) as the catalog proposed.",
    "<b>C3 Running analysis dominates Ben's queue</b>: 109 cases, 206 open Ben loose ends, all MAG. "
    "That is where step 6 (precedent rulings) pays.",
    "<b>C1 Client document is small as a root</b>: most documents sit inside B1, A5 or C3 cases; "
    "C1 is kept for documents that start on their own (status reports, explainers, documentation packs).",
]
