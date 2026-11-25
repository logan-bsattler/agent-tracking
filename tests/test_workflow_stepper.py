"""The workflow stepper on a /board/task page: done, current and future
steps, Ben's steps, and each step's plain-words meaning."""

from __future__ import annotations

import re

from coord_mcp import report
from coord_mcp.workflows import definitions as defs


def _w(at: str, step: str | None = None, state: str = "open", waits: str | None = "agent") -> dict:
    d = defs.get("C3")
    steps = [{"key": s["key"], "name": s["name"], "who": s["who"], "means": defs.means(s),
              "optional": bool(s.get("optional"))} for s in d["steps"]]
    step = step or at
    names = {s["key"]: s["name"] for s in steps}
    return {"workflow": "C3", "workflow_name": d["name"], "case_id": "c", "root_task_id": "t",
            "case_state": state, "step": step, "step_name": names[step],
            "n": defs.step_index(d, step) + 1, "of": len(steps), "steps": steps,
            "case_at": at, "case_at_name": names[at], "waits_on": waits}


def _nodes(html: str) -> list[tuple[str, str]]:
    """[(class, name)] for each node, in order."""
    return re.findall(r'<li class="(wf-node[^"]*)"[^>]*>.*?<span class="wf-name">([^<]*)</span>', html)


def test_done_current_and_future_steps():
    html = report.workflow_stepper(_w("regen"))
    nodes = _nodes(html)
    assert [n for _, n in nodes][:3] == ["New input", "Ben rules on the input", "Regenerate workbook"]
    states = [c.split()[1] for c, _ in nodes]
    assert states == ["done", "done", "cur", "todo", "todo", "todo", "todo"]
    cur = html.split('class="wf-node cur')[1].split("</li>")[0]
    assert "waiting on agent" in cur
    assert "<b>Now:</b> The client agent rebuilds the workbook" in html


def test_ben_steps_are_marked_and_optional_ones_dashed():
    nodes = _nodes(report.workflow_stepper(_w("regen")))
    ben = [n for c, n in nodes if " ben" in c]
    assert ben == ["Ben rules on the input", "Ben rules", "Ben uses the result"]
    assert " opt" in nodes[1][0] and " opt" not in nodes[2][0]
    html = report.workflow_stepper(_w("rulings", waits="ben"))
    cur = html.split('class="wf-node cur')[1].split("</li>")[0]
    assert cur.startswith(' ben') and "waiting on Ben" in cur and ">Ben<" in cur


def test_every_step_carries_its_meaning():
    html = report.workflow_stepper(_w("regen"))
    for s in defs.get("C3")["steps"]:
        assert report.esc(s["means"]) in html  # tooltip and the "what each step means" list
    assert html.count('title="') == 7


def test_task_behind_the_case_and_a_closed_case():
    html = report.workflow_stepper(_w("review", step="input"))
    assert "this task" in html.split("New input")[1].split("</li>")[0]
    assert "case now at Review:" in html
    done = report.workflow_stepper(_w("use", state="done", waits=None))
    assert {c.split()[1] for c, _ in _nodes(done)} == {"done"} and "case done" in done


def test_every_def_step_has_a_means_line_and_validates():
    for code, d in defs.load().items():
        assert not defs.validate(d), code
        for s in d["steps"]:
            assert s.get("means"), f"{code}.{s['key']}"
    assert defs.means({"who": "ben", "gate": "Ben pastes", "next": "case done"}) == \
        "Ben acts: Ben pastes. Moves on when: case done."


# The same picture elsewhere: /workflows rows, deliverables, drafts, proposals.

def _states(html: str) -> list[str]:
    return [c.split()[1] for c, _ in _nodes(html)]


def test_compact_stepper_names_the_current_step_once():
    html = report.workflow_stepper(_w("regen"), compact=True)
    assert 'class="wf-steps compact"' in html
    assert '<span class="wf-at"><b>Regenerate workbook</b> · waiting on agent</span>' in html
    assert "What each step means" not in html


def test_deliverable_lifecycle():
    d = {"kind": "deliverable", "stage": "changes", "reviews": 1}
    html = report.deliverable_stepper(d)
    assert _states(html) == ["done", "cur", "todo", "todo"] and "changes requested · 1 review" in html
    assert " warn" in _nodes(html)[1][0]
    html = report.deliverable_stepper({"kind": "deliverable", "stage": "approved", "reviews": 1})
    assert _states(html) == ["done", "done", "cur", "todo"] and "ready to promote" in html
    html = report.deliverable_stepper({"kind": "deliverable", "stage": "final", "version": 3})
    assert _states(html)[-1] == "cur" and "final v3" in html
    assert report.deliverable_stepper({"kind": "data_pull", "stage": "draft"}) == ""
    code = report.deliverable_stepper({"kind": "code_change", "stage": "approved"})
    assert [n for _, n in _nodes(code)] == ["Written", "Review", "Check in"]


def test_draft_stepper_shows_review_only_when_gated():
    plain = report.draft_stepper("waiting", None)
    assert [n for _, n in _nodes(plain)] == ["Written", "Into Outlook", "Ben sends"]
    assert _states(plain) == ["done", "cur", "todo"] and "to go through" in plain
    gated = report.draft_stepper("review", {"stage": "escalated"})
    assert _states(gated) == ["done", "cur", "todo", "todo"] and " fail" in _nodes(gated)[1][0]
    sent = report.draft_stepper("drafted", {"stage": "approved"})
    assert _states(sent) == ["done", "done", "done", "cur"]
    assert " ben" in _nodes(sent)[-1][0]


def test_proposal_diff_marks_new_changed_and_removed():
    from coord_mcp.workflows import propose
    cur = {"steps": [{"key": "a", "name": "A", "who": "agent"}, {"key": "b", "name": "B", "who": "ben"},
                     {"key": "c", "name": "C", "who": "code"}]}
    new = {"steps": [{"key": "a", "name": "A", "who": "agent", "means": "reworded only"},
                     {"key": "b", "name": "B2", "who": "ben"}, {"key": "d", "name": "D", "who": "agent"}]}
    html = propose._diff_steppers(cur, new)
    today, proposed = html.split("Proposed")
    assert [c.split()[-1] for c, n in _nodes(today) if n == "C"] == ["del"]
    marks = {n: c for c, n in _nodes(proposed)}
    assert "chg" not in marks["A"] and marks["B2"].endswith("chg") and marks["D"].endswith("add")
    assert "Today" not in propose._diff_steppers(None, new)
