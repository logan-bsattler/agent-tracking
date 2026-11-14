"""Put a task or intent in a catalog workflow. Read-only rules, first match wins.

A case is a root task and everything under it (children, reviews, check-ins),
so only roots are judged; children inherit their root's workflow. A root is
judged on its kind, title, spec text, client and the source of the intent it
came from. Every rule has a name, so the mining report can say which rule
tagged what and the leftovers can be grouped.

These rules were fitted to the board as it stood on 2026-11-14 (1,674 tasks,
418 intents). They are a classifier, not a contract: a wrong tag misplaces a
row on /workflows, it never changes what a task does.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Clients whose board work is mostly the running MAG analysis (C3): keeper
# lists, breakdowns, wave plans, cancel lists. Elsewhere the same words are
# rarer and mean a delivered change.
ANALYSIS_CLIENTS = {"MAG"}

_I = re.IGNORECASE


def _rx(p: str) -> re.Pattern[str]:
    return re.compile(p, _I)


# (rule name, workflow, pattern on the title, extra predicate or None)
# Order matters: the specific shapes come before the broad ones.
HYGIENE = _rx(r"board cleanup|loose ends are already done|evidence check")
DOD = _rx(r"\bdod/|\bdod checklist|definition-of-done checklist|\bDoD checklist|checklist \(dod/")
ONBOARD = _rx(r"characteri[sz]e spec location|\bonboard")
TOOLING = _rx(r"agent-tracking|Kiro test \d|\bKiro handoff\b|Power Automate|contact profiles|repo push state"
              r"|Lynx plan folder|effort rubric")
REFRESH = _rx(r"\brefresh(ed)?\b.*\b(DEVL|PROD|TEST)\b|\b(DEVL|PROD|TEST)\b.*\brefresh")
PROMOTE = _rx(r"\bpromot(e|ion|ed|ing)\b.*\b(TEST|PROD|DEVL)\b|\b(TEST|PROD)\b.*\bpromot"
              r"|weekend|\bdeploy|\bQSM[- ]?\d|\bpush(es)?\b.*\bTEST\b|\bTEST push|\bto TEST\b"
              r"|due to migrate|\bmigrated? to TEST|re-?promotion|in the .*TEST package|go to TEST")
TARGET = _rx(r"\bETA\b|target date|completion date|retest date|\bdev timeline for\b|\bdue date for\b")
STATUS = _rx(r"\bstatus\b|status read|what is left|what'?s left|where .* stands?|current state"
             r"|\bwhat is owed\b|state and next step|resume\b")
FDD_REVIEW = _rx(r"assigned (to )?Ben|assigned Ben|transferred to Ben|reassigned to Ben"
                 r"|\breview (of )?FDD|size review of FDD|\breview FDD|\bintake FDD"
                 r"|For Ben spec|For Ben' FDDs|Ben's \d+ outstanding FDDs")
DECISION = _rx(r"options for Ben|for Ben's ruling|pushback|billing|\bshare of dev cost")
TRACKER = _rx(r"RAID log text|\bJira\b|ServiceNow|tracker comment|\bRAID\b.*\btext\b")
PREP = _rx(r"\bprep\b|walkthrough|before .*\bcall\b|for .*\bcall\b|kick-?off|SteerCo|\bslide\b|for Mon \d|review$")
REPAIR = _rx(r"\brepair|In Repair|re-run .* tests|after .* check-in|round \d fixes")
DEFECT = _rx(r"\bwhy\b|defect|\bfix\b|\bbug\b|\berror|fail|times out|missing|lacks|wrong|stopped|\bblank\b"
             r"|does not|doesn'?t|not printing|bypass|reverting|\$0|\bissue\b|isolate|root-cause|\btrace\b"
             r"|prints .* twice|typo|still\b")
BUILD = _rx(r"\.p\b|\bFDD\b|FDD-|\bprogram|\bbrowse|\breport\b|\bbuild\b|\bcreate\b|compile|check(ed)? in"
            r"|\bCVC|layout|screen|\bBC\b|\bapp\b|\bQRF\b|handoff package|functional|dataload|\bAPI\b")
SIZING = _rx(r"\bsiz(e|ing)\b|\bestimate|\bscope\b|\bquote\b|\bhours\b|ballpark")
DOCUMENT = _rx(r"deliverable|explainer|summary (for|of)|one-page|write-up|documentation pack|workbook"
               r"|\bcopy\b|\bdeck\b|justification doc|\bsizing\b|timeline|\bplan\b|\blist (for|of)\b"
               r"|highlight|recast|playbook|cutover plan")
QUESTION = _rx(r"\?\s*$|^(can|does|do|is|are|how|where|what|which|will|confirm|find|compare|impact|test)\b"
               r"|\bfold .* answers|\banswers? change|\btriage\b|\bextract decisions|\bdocument Ben's")
ANALYSIS = _rx(r"keeper|breakdown|tracker|\bwave|cancel|\bplan\b|\br\d+|timeline|estimate|register|usage"
               r"|\bLynx\b|\bKiro\b|integration|doc(ument)? tracker|staffing|dependenc|crosswalk|rulings?"
               r"|Staged|Streamlined|eliminat|reconcile|\bdiff\b|inventory|\bmap\b|\blist\b|FDDs?\b|regen"
               r"|re-?grade|rationale|Customizations")

RULES: list[tuple[str, str, re.Pattern[str]]] = [
    ("hygiene", "D1", HYGIENE),
    ("dod-checklist", "D5", DOD),
    ("onboarding", "D4", ONBOARD),
    ("tooling", "D6", TOOLING),
    ("env-refresh", "B3", REFRESH),
    ("tracker-text", "C2", TRACKER),
    ("fdd-review", "A5", FDD_REVIEW),
    ("decision", "A6", DECISION),
    ("promotion", "A4", PROMOTE),
    ("target-date", "A2", TARGET),
    ("status", "A1", STATUS),
    ("repair", "B2", REPAIR),
]

# Intent sources that name the workflow on their own.
SOURCE_WORKFLOW = {
    "email-status": "A1",
    "teams-ask": "A1",
    "email-reply": "A6",
    "email-closes": "A7",
    "teams-status": "A7",
    "email-commitment": "A8",
    "teams-commitment": "A8",
    "pipeline": "B2",
    "operator": "A10",
    "meeting": "A9",
}


def spec_text(spec: str | dict[str, Any] | None) -> str:
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError:
            return spec
    if not isinstance(spec, dict):
        return ""
    return " ".join(str(spec.get(k) or "") for k in ("goal", "objective", "question", "context"))[:2000]


def strip_client(title: str) -> str:
    """'MAG: rebuild X' -> 'rebuild X'; 'LNK DEV 38: draft' -> 'draft'."""
    head, sep, rest = title.partition(":")
    return rest.strip() if sep and len(head) <= 40 else title


def tag_root(kind: str, title: str, spec: Any = None, client: str | None = None,
             source: str | None = None) -> tuple[str | None, str]:
    """(workflow code or None, rule name) for a root task."""
    t = title or ""
    for rule, code, rx in RULES:
        if rx.search(t):
            return code, rule
    if source == "email-reply":
        return "A6", "source:email-reply"
    if source in ("email-status", "teams-ask"):
        return "A1", f"source:{source}"
    if source == "pipeline":
        return "B2", "source:pipeline"
    analysis = (client or "") in ANALYSIS_CLIENTS
    if kind == "draft_reply":
        return ("A1", "reply") if source not in ("meeting",) else ("A9", "reply-from-meeting")
    if PREP.search(t) and kind != "code_change":
        return "C4", "meeting-prep"
    if analysis and (ANALYSIS.search(t) or kind in ("data_pull", "code_change")):
        if kind == "deliverable" and BUILD.search(t) and re.search(r"\bFDD-[A-Z0-9]+-\d+", t):
            return "B1", "fdd-authoring"
        return "C3", "analysis"
    if DEFECT.search(t):
        return "A3", "defect"
    if QUESTION.search(strip_client(t)) and kind in ("investigation", "review"):
        return "A11", "question"
    if kind == "review":
        return "A11", "question"
    if kind in ("deliverable", "data_pull") or DOCUMENT.search(t):
        if SIZING.search(t):
            return "B1", "sizing"
        return "C1", "document"
    if SIZING.search(t):
        return "B1", "sizing"
    if BUILD.search(t) or kind == "code_change":
        return "B1", "build"
    if source and source in SOURCE_WORKFLOW:
        return SOURCE_WORKFLOW[source], f"source:{source}"
    if kind == "investigation":
        return "A11", "question"
    return None, "none"


def tag_intent(source: str, task_codes: list[str | None]) -> tuple[str | None, str]:
    """An intent's workflow: the one its own source names when the source is
    the workflow (closes, commitments), else its first task's, else the
    source's default."""
    if source in ("email-closes", "teams-status", "email-commitment", "teams-commitment"):
        return SOURCE_WORKFLOW[source], f"source:{source}"
    for c in task_codes:
        if c:
            return c, "task"
    if source in SOURCE_WORKFLOW:
        return SOURCE_WORKFLOW[source], f"source:{source}"
    return None, "none"


def pattern(title: str, kind: str) -> str:
    """A leftover's group key: kind plus the first two words after the client."""
    words = re.findall(r"[A-Za-z][\w'-]*", strip_client(title).lower())
    return f"{kind}: {' '.join(words[:2]) or '?'}"
