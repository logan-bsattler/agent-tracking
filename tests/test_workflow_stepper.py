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
