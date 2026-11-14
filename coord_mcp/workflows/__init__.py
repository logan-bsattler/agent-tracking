"""Workflows as board objects (docs/workflow-catalog.md, docs/workflow-build-brief.md).

A workflow is a repeatable shape of Ben's work (a status request, a defect
report, an FDD on its way to production). This package holds:

- CATALOG: the workflow codes and names, from the catalog.
- tag.py: rules that put a past task or intent in a workflow (read-only).
- mine.py: `python -m coord_mcp.workflows mine`, the read-only report.

Everything here reads the board unless a command says it writes.
"""

from __future__ import annotations

# code -> (name, group). Groups follow the catalog: A triggered, B pipeline
# development, C documents and analysis, D upkeep. A11, D5 and D6 were added from
# the step-1 mining (docs/workflow-mining-2026-11-14.html).
CATALOG: dict[str, tuple[str, str]] = {
    "A1": ("Status request", "A"),
    "A2": ("Target-date ask", "A"),
    "A3": ("Defect or change report", "A"),
    "A4": ("Promotion ask", "A"),
    "A5": ("FDD review assigned to Ben", "A"),
    "A6": ("Decision ask", "A"),
    "A7": ("Evidence that closes open items", "A"),
    "A8": ("Commitment Ben made", "A"),
    "A9": ("Meeting action item", "A"),
    "A10": ("Ben's note to himself", "A"),
    "A11": ("Technical question", "A"),
    "B1": ("FDD or requirement to production", "B"),
    "B2": ("Repair loop", "B"),
    "B3": ("Environment refresh recovery", "B"),
    "C1": ("Client document", "C"),
    "C2": ("Tracker text", "C"),
    "C3": ("Running analysis", "C"),
    "C4": ("Meeting preparation", "C"),
    "D1": ("Board hygiene", "D"),
    "D2": ("Hours", "D"),
    "D3": ("Hourly triage and morning recap", "D"),
    "D4": ("Client onboarding", "D"),
    "D5": ("Definition-of-done checklist", "D"),
    "D6": ("Tooling and method", "D"),
}

GROUPS = {"A": "Triggered by something arriving", "B": "Pipeline development",
          "C": "Documents and analysis", "D": "Upkeep"}


def name(code: str | None) -> str:
    return CATALOG.get(code or "", ("untagged", ""))[0]
