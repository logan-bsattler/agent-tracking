"""/clients and /client/<key>: each client's projects (docs/client-projects.md).

Kept out of report.py, which imports this lazily from its request handler.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from . import projects
from .report import _age, esc, page

PROJECT_CSS = """<style>
table.clients { width:100%; border-collapse:collapse; font-size:13px; }
table.clients th, table.clients td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--grid); vertical-align:top; }
table.clients th { color:var(--ink2); font-weight:600; font-size:12px; }
table.clients td.n { font-variant-numeric:tabular-nums; text-align:right; } table.clients th.n { text-align:right; }
table.clients .zero { color:var(--muted); } table.clients .bad { color:var(--critical); font-weight:600; }
table.clients .titles { color:var(--ink2); font-size:12px; }
.proj { border-left:5px solid var(--s1); } .proj.st-waiting { border-left-color:var(--s4); }
.proj h2 { display:flex; flex-wrap:wrap; gap:6px 10px; align-items:baseline; }
.proj .chip { font-size:11px; font-weight:600; padding:1px 8px; border-radius:10px; border:1px solid var(--axis); color:var(--ink2); }
.proj .goal { color:var(--ink2); margin:-6px 0 10px; }
.proj .summary { white-space:pre-wrap; margin:0 0 6px; }
.proj .stale { color:var(--critical); font-weight:600; font-size:12px; }
.proj .meta { font-size:12px; color:var(--muted); margin-bottom:10px; }
.proj .next { font-weight:600; border:none; }
.pgrid { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:4px 22px; margin-top:8px; }
.pgrid h3 { font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--ink2); margin:8px 0 4px; }
.pgrid ul { margin:0; padding-left:18px; } .pgrid li { margin:2px 0; overflow-wrap:anywhere; }
.pgrid .muted, .proj .muted { color:var(--muted); font-size:12px; }
.waits { color:var(--s4); font-size:12px; }
form.pform { display:grid; gap:6px; max-width:720px; margin-top:8px; font-size:13px; }
form.pform input, form.pform textarea, form.pform select, form.attach select {
  font:inherit; font-size:13px; padding:5px 7px; border-radius:6px; border:1px solid var(--axis);
  background:var(--surface); color:var(--ink); }
form.pform button, form.attach button, form.newp button { font:inherit; font-size:13px; padding:5px 12px; border-radius:6px;
  cursor:pointer; border:1px solid var(--axis); background:var(--surface); color:var(--ink); justify-self:start; }
form.newp { display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin:0; font-size:13px; }
form.newp input { font:inherit; font-size:13px; padding:4px 7px; border-radius:6px; border:1px solid var(--axis);
  background:var(--surface); color:var(--ink); }
details.edit summary { cursor:pointer; color:var(--s1); font-size:13px; margin-top:10px; }
.unfiled li { list-style:none; margin-left:-18px; }
form.attach .bar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:8px; }
.closed li { color:var(--ink2); }
</style>"""


def client_href(client: str) -> str:
    return "/client/" + quote(client, safe="")


def _task(d: dict[str, Any], t: int, extra: str = "") -> str:
    waits = (f' <span class="waits">waits on {", ".join(esc(w[:8]) for w in d["waits_on"])}</span>'
             if d.get("waits_on") else "")
    picked = "" if d.get("state") != "open" or d.get("picked_up") else ' <span class="muted">not picked up</span>'
    return (f'<li><a class="drill" href="/board/task/{esc(d["id"])}">{esc(d["title"])}</a> '
            f'<span class="muted">{esc(d["kind"])} · {esc(_age(d["since"], t))}{extra}</span>{picked}{waits}</li>')


def _list(title: str, items: list[str], empty: str, cap: int = 6) -> str:
    """A labelled list that shows `cap` rows and folds the rest behind a count."""
    more = (f'<li><details><summary class="muted">{len(items) - cap} more</summary><ul>{"".join(items[cap:])}</ul>'
            f'</details></li>' if len(items) > cap else "")
    body = f"<ul>{''.join(items[:cap])}{more}</ul>" if items else f'<p class="muted">{esc(empty)}</p>'
    return f"<div><h3>{esc(title)} <span class=\"muted\">{len(items)}</span></h3>{body}</div>"


def _project_card(p: dict[str, Any], t: int, back: str) -> str:
    pid = esc(p["id"])
    head = (f'<h2>{esc(p["title"])}<span class="chip">{esc(p["state"])}</span>'
            f'<span class="muted">{p["task_count"]} tasks · last activity {esc(_age(p["last_ts"], t))}</span></h2>')
    goal = f'<p class="goal">{esc(p["goal"])}</p>' if p["goal"] else ""
    if p["summary"]:
        by = f' by {esc(p["summary_by"])}' if p["summary_by"] else ""
        summary = (f'<p class="summary">{esc(p["summary"])}</p>'
                   f'<div class="meta">summary {esc(_age(p["summary_at"], t))}{by}'
                   + (' · <span class="stale">stale</span>' if p["summary_stale"] else "") + "</div>")
    else:
        summary = ('<p class="muted">No summary yet.'
                   + (' <span class="stale">Open work with no summary.</span>' if p["summary_stale"] else "") + "</p>")
    nxt = f'<p class="next">Next: {esc(p["next_step"])}</p>' if p["next_step"] else ""
    pipe = ""
    if p["pipeline_ids"]:
        pipe = '<div class="meta">SharePoint ' + ", ".join(f"<code>{esc(x)}</code>" for x in p["pipeline_ids"])
        if p["sp_snapshot"]:
            pipe += " · " + esc("; ".join(f"{k}: {v}" for k, v in p["sp_snapshot"].items()
                                          if not isinstance(v, (dict, list)))[:200])
        pipe += "</div>"
    grid = '<div class="pgrid">' + "".join((
        _list("Open tasks", [_task(d, t) for d in p["open"]], "Nothing open."),
        _list("Loose ends", [f'<li><b>{esc(f["who"])}</b>: {esc(f["what"][:200])} '
                             f'<a class="drill muted" href="/board/task/{esc(f["task_id"])}">{esc(f["task_id"][:8])}</a></li>'
                             for f in p["loose"]], "None open."),
        _list("Deliverables", [_task(d, t, f' · <b>{esc(d["stage"])}</b>') for d in p["deliverables"]], "None yet."),
        _list("Recent decisions", [f'<li>{esc(x["statement"][:220])} <span class="muted">{esc(_age(x["created_at"], t))}</span></li>'
                                   for x in p["decisions"]], "None recorded."),
        _list("Recently finished", [_task(d, t, f' · {esc(d["state"])}') for d in p["recent"]], "Nothing finished."),
    )) + "</div>"
    states = "".join(f'<option{" selected" if s == p["state"] else ""}>{s}</option>' for s in projects.STATES)
    edit = (f'<details class="edit"><summary>Edit project</summary>'
            f'<form class="pform" method="post" action="/api/project">'
            f'<input type="hidden" name="action" value="set"><input type="hidden" name="project" value="{pid}">'
            f'<input type="hidden" name="back" value="{esc(back)}">'
            f'<label>Title <input name="title" value="{esc(p["title"])}" maxlength="{projects.LINE_MAX}" size="60"></label>'
            f'<label>Goal <input name="goal" value="{esc(p["goal"] or "")}" maxlength="{projects.LINE_MAX}" size="60"></label>'
            f'<label>State <select name="state">{states}</select></label>'
            f'<label>Summary (where it stands)<br><textarea name="summary" rows="3" cols="80" '
            f'maxlength="{projects.SUMMARY_MAX}">{esc(p["summary"] or "")}</textarea></label>'
            f'<label>Next step <input name="next_step" value="{esc(p["next_step"] or "")}" maxlength="{projects.LINE_MAX}" size="60"></label>'
            f'<label>SharePoint ids <input name="pipeline" value="{esc(",".join(p["pipeline_ids"]))}" size="40" '
            f'placeholder="comma separated"></label>'
            f'<button>Save</button><span class="muted">id <code>{pid}</code></span></form></details>')
    return (f'<div class="card proj st-{esc(p["state"])}" id="p-{pid}">{head}{goal}{summary}{nxt}{pipe}{grid}{edit}</div>')


def client_page(v: dict[str, Any]) -> str:
    t, client = v["as_of"], v["client"]
    back = client_href(client)
    pct = round(100 * v["filed"] / v["task_count"]) if v["task_count"] else 0
    sub = (f'{len(v["projects"])} live project{"s" if len(v["projects"]) != 1 else ""} · '
           f'{v["filed"]} of {v["task_count"]} tasks filed ({pct}%) · '
           f'<a href="/board?client={quote(client)}">board</a> · '
           f'<a href="/hours?client={quote(client)}">hours</a> · '
           f'<a href="/deliverables?client={quote(client)}">deliverables</a> · '
           f'<a href="/clients">all clients</a>')
    new = (f'<form class="newp" method="post" action="/api/project">'
           f'<input type="hidden" name="action" value="add"><input type="hidden" name="client" value="{esc(client)}">'
           f'<input type="hidden" name="back" value="{esc(back)}">'
           f'<input name="title" required maxlength="{projects.LINE_MAX}" placeholder="New project title" size="28">'
           f'<input name="goal" maxlength="{projects.LINE_MAX}" placeholder="Goal (optional)" size="28">'
           f'<input name="pipeline" placeholder="SharePoint ids" size="14">'
           f'<button>Add project</button></form>')
    cards = [_project_card(p, t, back) for p in v["projects"]]
    if not cards:
        cards.append('<div class="card"><p class="empty">No projects yet. Add one above, then file the tasks '
                     'below under it. Children follow their root.</p></div>')
    live = [p for p in v["projects"]]
    if v["unfiled"]:
        opts = "".join(f'<option value="{esc(p["id"])}">{esc(p["title"])}</option>' for p in live)
        rows = "".join(
            f'<li><label><input type="checkbox" name="task" value="{esc(d["id"])}"> '
            f'<a class="drill" href="/board/task/{esc(d["id"])}">{esc(d["title"])}</a></label> '
            f'<span class="muted">{esc(d["kind"])} · {esc(d["state"])} · {esc(_age(d["since"], t))}</span></li>'
            for d in v["unfiled"])
        bar = (f'<div class="bar"><select name="project" required><option value="">File under…</option>{opts}</select>'
               f'<button>Attach checked</button></div>' if live
               else '<p class="muted">Add a project first.</p>')
        cards.append(
            f'<div class="card unfiled"><h2>Unfiled<span class="count">{len(v["unfiled"])}</span></h2>'
            f'<p class="muted">Root tasks from the last {projects.UNFILED_DAYS} days with no project. '
            f'Their follow-up tasks come along.</p>'
            f'<form class="attach" method="post" action="/api/project">'
            f'<input type="hidden" name="action" value="attach"><input type="hidden" name="back" value="{esc(back)}">'
            f'{bar}<ul>{rows}</ul></form></div>')
    if v["closed"]:
        items = "".join(f'<li>{esc(p["title"])} <span class="muted">{esc(p["state"])}'
                        + (f' {esc(_age(p["closed_at"], t))}' if p["closed_at"] else "")
                        + f' · {p["task_count"]} tasks</span></li>' for p in v["closed"])
        cards.append(f'<div class="card closed" data-fold="closed"><h2>Closed<span class="count">{len(v["closed"])}</span>'
                     f'</h2><ul>{items}</ul></div>')
    return page("Clients", f"{client} projects", esc(client), sub, PROJECT_CSS + "".join(cards), actions=new)


def clients_page(rows: list[dict[str, Any]], t: int) -> str:
    def n(x: int, bad: bool = False) -> str:
        cls = "zero" if not x else "bad" if bad else ""
        return f'<td class="n {cls}">{x}</td>'
    body = []
    for r in rows:
        oldest = (_age(t - r["oldest_summary"], t) if r["oldest_summary"] is not None else "—")
        filed = f'{round(100 * r["filed"] / r["tasks"])}%' if r["tasks"] else "—"
        body.append(
            f'<tr><td><a class="drill" href="{client_href(r["client"])}"><b>{esc(r["client"])}</b></a></td>'
            f'{n(r["active"])}{n(r["waiting"])}{n(r["open_tasks"])}{n(r["loose"])}'
            f'{n(r["unfiled"], bad=r["unfiled"] > 5)}<td class="n">{filed}</td>{n(r["stale"], bad=True)}'
            f'<td class="n">{esc(oldest)}</td>'
            f'<td class="titles">{esc(" · ".join(r["titles"]))}</td></tr>')
    table = ('<div class="card"><table class="clients"><thead><tr><th>Client</th><th class="n">Active</th>'
             '<th class="n">Waiting</th><th class="n">Open tasks</th><th class="n">Loose ends</th>'
             f'<th class="n">Unfiled ({projects.UNFILED_DAYS}d)</th><th class="n">Filed</th>'
             f'<th class="n">Stale summaries</th><th class="n">Oldest summary</th><th>Live projects</th></tr></thead>'
             f'<tbody>{"".join(body)}</tbody></table></div>')
    sub = ("Each client's workstreams above the task. A project's tasks are its filed roots and everything "
           f"under them. A summary is stale after {projects.STALE_SUMMARY_DAYS} days while work is open.")
    return page("Clients", "Clients", "Clients", sub, PROJECT_CSS + table)
