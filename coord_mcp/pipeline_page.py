"""/pipeline: the board's work ranked by SharePoint pipeline item, the line
Ben bills to (docs/pipeline-lines.md). Proposals wait for his Create, Use
existing or Drop; root tasks with no line show in red with a set form.

Kept out of report.py, which imports this lazily from its request handler.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

from . import pipeline
from .report import _age, esc, page

PIPE_CSS = """<style>
table.lines { width:100%; border-collapse:collapse; font-size:13px; }
table.lines th, table.lines td { text-align:left; padding:6px 9px; border-bottom:1px solid var(--grid); vertical-align:top; }
table.lines th { color:var(--ink2); font-weight:600; font-size:12px; }
table.lines td.n, table.lines th.n { text-align:right; font-variant-numeric:tabular-nums; }
table.lines .zero { color:var(--muted); }
table.lines tr.overdue td.end { color:var(--critical); font-weight:600; }
table.lines tr.unknown td.lbl { color:var(--muted); }
table.lines tr.proposed td.lbl { font-style:italic; }
table.lines .titles { color:var(--ink2); font-size:12px; margin-top:2px; }
table.lines .titles a { color:inherit; }
.pri { font-size:11px; font-weight:600; padding:1px 7px; border-radius:10px; border:1px solid var(--axis); color:var(--ink2); }
.pri.High { border-color:var(--critical); color:var(--critical); }
.muted { color:var(--muted); font-size:12px; }
.card.unfiled { border-left:5px solid var(--critical); }
.card.unfiled h2 { color:var(--critical); }
.card.props { border-left:5px solid var(--s4); }
ul.plist { list-style:none; margin:0; padding:0; }
ul.plist li { padding:7px 0; border-bottom:1px solid var(--grid); display:flex; flex-wrap:wrap; gap:6px 12px; align-items:center; }
ul.plist li .what { flex:1 1 320px; overflow-wrap:anywhere; }
ul.plist .err { color:var(--critical); font-size:12px; flex-basis:100%; }
form.inl { display:inline-flex; gap:5px; align-items:center; margin:0; }
form.inl input { font:inherit; font-size:13px; padding:3px 6px; border-radius:6px; border:1px solid var(--axis);
  background:var(--surface); color:var(--ink); width:9em; }
form.inl button { font:inherit; font-size:12px; padding:3px 10px; border-radius:6px; cursor:pointer;
  border:1px solid var(--axis); background:var(--surface); color:var(--ink); }
form.inl button.go { border-color:var(--s1); color:var(--s1); font-weight:600; }
form.filter { display:flex; gap:6px; align-items:center; margin:0; font-size:13px; }
form.filter select { font:inherit; font-size:13px; padding:4px 6px; border-radius:6px; border:1px solid var(--axis);
  background:var(--surface); color:var(--ink); }
.flash { padding:8px 12px; border-radius:8px; margin-bottom:14px; font-size:13px;
  background:color-mix(in srgb, var(--s3) 10%, transparent); border:1px solid var(--s3); }
.flash.bad { background:color-mix(in srgb, var(--critical) 8%, transparent); border-color:var(--critical); }
</style>"""


def pipeline_href(client: str | None = None) -> str:
    return "/pipeline" + (f"?client={quote(client)}" if client else "")


def _hidden(**kv: str | None) -> str:
    return "".join(f'<input type="hidden" name="{k}" value="{esc(v)}">' for k, v in kv.items() if v is not None)


def _proposal_row(p: dict[str, Any], back: str, t: int) -> str:
    pid = p["id"]
    what = (f'<span class="what"><b>{esc(pipeline.label(p, ""))}</b> '
            f'<span class="muted">{esc(p["client"])} · {p.get("tasks", 0)} task(s) · '
            f'proposed {esc(_age(p["created_at"], t))}'
            + (f' · first task <a class="drill" href="/board/task/{esc(p["task_id"])}">{esc(p["task_id"][:8])}</a>'
               if p.get("task_id") else "")
            + "</span>"
            + (f' <span class="pri {esc(p["priority"])}">{esc(p["priority"])}</span>' if p.get("priority") else "")
            + (f'<br><span class="muted">{esc(p["note"][:300])}</span>' if p.get("note") else "")
            + "</span>")
    base = _hidden(proposal=pid, back=back)
    create = (f'<form class="inl" method="post" action="/api/pipeline/proposal">{base}{_hidden(action="approve")}'
              f'<button class="go" title="The worker creates the SharePoint item on its next tick">Create</button></form>')
    use = (f'<form class="inl" method="post" action="/api/pipeline/proposal">{base}{_hidden(action="link")}'
           f'<input name="item" required pattern="#?[0-9]+" placeholder="item #" list="items-{esc(p["client"])}">'
           f'<button>Use existing</button></form>')
    drop = (f'<form class="inl" method="post" action="/api/pipeline/proposal">{base}{_hidden(action="drop")}'
            f'<button>Drop</button></form>')
    return f"<li>{what}{create}{use}{drop}</li>"


def _approved_row(p: dict[str, Any], back: str, t: int) -> str:
    when = f'approved {esc(_age(p["decided_at"], t))}' if p.get("decided_at") else "approved"
    err = f'<span class="err">Last try failed: {esc(p["detail"])}</span>' if p.get("detail") else ""
    use = (f'<form class="inl" method="post" action="/api/pipeline/proposal">{_hidden(proposal=p["id"], back=back)}'
           f'{_hidden(action="link")}<input name="item" required pattern="#?[0-9]+" placeholder="item #">'
           f'<button title="It was made by hand: point its tasks at it">Use existing</button></form>')
    return (f'<li><span class="what"><b>{esc(pipeline.label(p, ""))}</b> <span class="muted">{esc(p["client"])} · '
            f'{p.get("tasks", 0)} task(s) · {when}</span></span>{use}{err}</li>')


def _num(v: Any) -> str:
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v)


def _line_row(x: dict[str, Any], t: int) -> str:
    cls = " ".join(c for c, on in (("overdue", x["overdue"]), ("unknown", not x["known"]),
                                   ("proposed", bool(x["proposal"]))) if on)
    pid = x["pipeline_id"]
    ident = f"<code>{esc(pid)}</code>" if not pipeline.is_proposal(pid) else '<span class="muted">proposal</span>'
    pri = f'<span class="pri {esc(x["priority"])}">{esc(x["priority"])}</span>' if x["priority"] else ""
    titles = ""
    if x["open_titles"]:
        titles = '<div class="titles">' + " · ".join(
            f'<a class="drill" href="/board/task/{esc(tid)}">{esc(title[:60])}</a>' for tid, title in x["open_titles"]
        ) + "</div>"
    est = "-".join(_num(e) for e in (x["est_low"], x["est_high"]) if e not in (None, ""))

    def n(v: int) -> str:
        return f'<td class="n{" zero" if not v else ""}">{v}</td>'

    end = x["end_date"] or ""
    if end and x["overdue"]:
        end += " overdue"
    return (f'<tr class="{cls}"><td>{pri}</td><td>{ident}</td>'
            f'<td class="lbl">{esc(x["label"]) if x["known"] else "Not in the cached pipeline"}{titles}</td>'
            f'<td><a href="{pipeline_href(x["client"])}">{esc(x["client"] or "?")}</a></td>'
            f'<td>{esc(x["status"])}</td><td class="end">{esc(end)}</td><td class="n">{esc(est)}</td>'
            f'<td class="n">{"" if x["pct"] is None else esc(x["pct"])}</td>'
            f'{n(x["open"])}{n(x["done"])}{n(x["ben"])}'
            f'<td class="n">{esc(_age(x["last"], t)) if x["last"] else ""}</td></tr>')


def _unfiled_row(d: dict[str, Any], back: str, t: int) -> str:
    return (f'<li><span class="what"><a class="drill" href="/board/task/{esc(d["task_id"])}">{esc(d["title"])}</a> '
            f'<span class="muted">{esc(d["client"])} · {esc(d["kind"])} · {esc(d["state"])} · '
            f'{esc(_age(d["created_at"], t))}</span></span>'
            f'<form class="inl" method="post" action="/api/pipeline/set">{_hidden(task=d["task_id"], back=back)}'
            f'<input name="item" required placeholder="item # or p:id" list="items-{esc(d["client"])}">'
            f'<button>Set</button></form></li>')


def _datalists(v: dict[str, Any]) -> str:
    """Per-client suggestions for the item inputs: the cached items and open
    proposals of that client."""
    by: dict[str, list[tuple[str, str]]] = {}
    for x in v["lines"]:
        if x["client"] and x["known"]:
            by.setdefault(x["client"], []).append((x["pipeline_id"], x["label"]))
    return "".join(f'<datalist id="items-{esc(c)}">'
                   + "".join(f'<option value="{esc(pid)}">{esc(lbl[:80])}</option>' for pid, lbl in rows)
                   + "</datalist>" for c, rows in by.items())


def pipeline_page(v: dict[str, Any], client: str | None, keys: list[str], flash: str | None = None,
                  bad: bool = False) -> str:
    """`v` is pipeline.lines(conn, client)."""
    t = v["now"]
    back = pipeline_href(client)
    synced = v.get("synced_at")
    age = (f'cache {esc(_age(synced, t))} ({datetime.fromtimestamp(synced).strftime("%m-%d %H:%M")})'
           if synced else '<span style="color:var(--critical)">pipeline never synced</span>')
    last = v.get("sync_last")
    if last and not last.get("ok"):  # the worker's last try failed: say how, not just "never synced"
        age += (f' · <span style="color:var(--critical)">last sync {esc(_age(int(last.get("at") or t), t))}: '
                f'{esc(last.get("stage"))}, {esc(str(last.get("detail"))[:160])}</span>')
    rule = "rule on: root tasks need a line" if v.get("require") else "rule off"
    sub = (f'{len(v["lines"])} line(s) · {len(v["proposals"])} proposal(s) await you · '
           f'{len(v["unfiled"])} root task(s) with no line · {age} · {rule}')
    opts = "".join(f'<option{" selected" if k == client else ""}>{esc(k)}</option>' for k in keys)
    actions = (f'<form class="filter" method="get" action="/pipeline"><select name="client" onchange="this.form.submit()">'
               f'<option value="">All clients</option>{opts}</select><noscript><button>Show</button></noscript></form>')
    cards: list[str] = []
    if flash:
        cards.append(f'<div class="flash{" bad" if bad else ""}">{esc(flash)}</div>')
    if v["proposals"]:
        cards.append(f'<div class="card props" id="proposals"><h2>Proposed items<span class="count">'
                     f'{len(v["proposals"])}</span></h2><p class="muted">Work with no SharePoint item yet. Create '
                     f'makes the item (title as shown) on the worker\'s next tick; Use existing points its tasks at an '
                     f'item you name; Drop leaves them with no line.</p><ul class="plist">'
                     + "".join(_proposal_row(p, back, t) for p in v["proposals"]) + "</ul></div>")
    if v["approved"]:
        cards.append(f'<div class="card" id="approved"><h2>Approved, awaiting creation<span class="count">'
                     f'{len(v["approved"])}</span></h2><ul class="plist">'
                     + "".join(_approved_row(p, back, t) for p in v["approved"]) + "</ul></div>")
    if v["unfiled"]:
        cards.append(f'<div class="card unfiled" id="unfiled"><h2>No pipeline item<span class="count">'
                     f'{len(v["unfiled"])}</span></h2><p class="muted">Root tasks from the last {pipeline.UNFILED_DAYS} '
                     f'days (or still open) whose chain names no line. Set an item number, or <code>p:&lt;id&gt;</code> '
                     f'for a proposal; their follow-up tasks come along.</p><ul class="plist">'
                     + "".join(_unfiled_row(d, back, t) for d in v["unfiled"]) + "</ul></div>")
    if v["lines"]:
        table = ('<table class="lines"><thead><tr><th>Pri</th><th>Item</th><th>Line</th><th>Client</th>'
                 '<th>Status</th><th>End</th><th class="n">Est h</th><th class="n">%</th><th class="n">Open</th>'
                 '<th class="n">Done</th><th class="n">Ben</th><th class="n">Last</th></tr></thead><tbody>'
                 + "".join(_line_row(x, t) for x in v["lines"]) + "</tbody></table>")
    else:
        table = '<p class="empty">No lines yet. The worker fills the cache from SharePoint every 30 minutes.</p>'
    cards.append(f'<div class="card" id="lines"><h2>Lines<span class="count">{len(v["lines"])}</span></h2>'
                 f'<p class="muted">Ranked by priority, then overdue, end date and latest activity. Open, Done and Ben '
                 f'count board tasks (last {pipeline.WINDOW_DAYS} days or open) and Ben\'s open items.</p>{table}</div>')
    return page("Pipeline", "Pipeline lines", "Pipeline" + (f" · {esc(client)}" if client else ""), sub,
                PIPE_CSS + _datalists(v) + "".join(cards), actions=actions)


def board_strip(conn) -> str:
    """One line on /board: proposals waiting and unfiled roots, linking to
    /pipeline. Never costs the board: any error is an empty strip."""
    try:
        props = len(pipeline.proposals(conn, states=("proposed",)))
        unf = len(pipeline.unfiled(conn))
    except Exception:
        return ""
    if not props and not unf:
        return ('<p class="sub" id="pipeline-strip">Pipeline: every recent root task has a line. '
                '<a href="/pipeline">Lines</a></p>')
    bits = []
    if props:
        bits.append(f"{props} proposed item{'s' if props != 1 else ''} await you")
    if unf:
        bits.append(f'<span style="color:var(--critical)">{unf} root task{"s" if unf != 1 else ""} with no line</span>')
    return f'<p class="sub" id="pipeline-strip">Pipeline: {" · ".join(bits)}. <a href="/pipeline">Open /pipeline</a></p>'
