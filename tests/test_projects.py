"""Client projects: workstreams above the task (docs/client-projects.md)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from coord_mcp import client_pages, projects, queue, report, store
from coord_mcp.db import connect, ensure_projects


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _task(conn, title, kind="investigation", client="MAG", parent=None, spec=None, pipeline=None):
    return store.create_task(conn, kind, title, spec or {"q": "x"}, assigned_to=client, parent_id=parent,
                             pipeline_task_id=pipeline)["task_id"]


def test_ensure_is_idempotent_and_keeps_rows(conn):
    t = _task(conn, "root")
    ensure_projects(conn)
    ensure_projects(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "project_id" in cols
    assert conn.execute("SELECT title FROM tasks WHERE id=?", (t,)).fetchone()[0] == "root"


def test_add_validates_and_refuses_live_duplicates(conn):
    p = projects.add(conn, "MAG", "32soft uploads", goal="loaders move to 32soft", pipeline_ids="SP-1, SP-2")
    assert p["state"] == "active" and p["pipeline_ids"] == ["SP-1", "SP-2"]
    with pytest.raises(ValueError, match="already has"):
        projects.add(conn, "MAG", "32SOFT uploads")
    projects.add(conn, "LNK", "32soft uploads")  # another client may use the name
    with pytest.raises(ValueError):
        projects.add(conn, "MAG", "  ")
    with pytest.raises(ValueError, match="state"):
        projects.add(conn, "MAG", "x", state="paused")


def test_membership_inherits_through_parents_and_checkins(conn):
    p = projects.add(conn, "MAG", "FDD 430")
    root = _task(conn, "design")
    child = _task(conn, "code", "code_change", parent=root)
    grandchild = _task(conn, "review", "dod_review", parent=child)
    checkin = _task(conn, "check in", "code_change")
    conn.execute("UPDATE tasks SET spec=? WHERE id=?", (json.dumps({"checkin_of": child}), checkin))  # past the gate
    other = _task(conn, "unrelated")
    projects.attach(conn, p["id"], [root])
    for t in (root, child, grandchild, checkin):
        assert projects.project_of(conn, t) == p["id"]
    assert projects.project_of(conn, other) is None
    rows = {r["id"]: r for r in conn.execute("SELECT id, parent_id, spec, project_id FROM tasks")}
    assert projects._resolve_all(rows) == {t: projects.project_of(conn, t) for t in rows}


def test_attach_refuses_other_clients_tasks(conn):
    p = projects.add(conn, "MAG", "x")
    t = _task(conn, "lnk work", client="LNK")
    with pytest.raises(ValueError, match="LNK"):
        projects.attach(conn, p["id"], [t])
    with pytest.raises(ValueError, match="no task"):
        projects.attach(conn, p["id"], ["000000000000"])


def test_pipeline_falls_back_to_a_single_project_item(conn):
    one = projects.add(conn, "MAG", "one item", pipeline_ids="SP-9")
    two = projects.add(conn, "MAG", "two items", pipeline_ids="SP-1,SP-2")
    a, b, own = _task(conn, "a"), _task(conn, "b"), _task(conn, "own", pipeline="SP-own")
    projects.attach(conn, one["id"], [a, own])
    projects.attach(conn, two["id"], [b])
    assert queue.pipeline_id(conn, a) == "SP-9"
    assert queue.pipeline_id(conn, b) is None  # several: the task must say which
    assert queue.pipeline_id(conn, own) == "SP-own"  # its own link wins
    # A deliverable under the project no longer warns about a missing link.
    warn = queue.create_warnings(conn, "deliverable", {"format": "html"}, None, a)
    assert not any("pipeline" in w for w in warn)


def test_update_stamps_summary_only_when_it_changes(conn):
    p = projects.add(conn, "MAG", "x")
    p1 = projects.update(conn, p["id"], by="master", summary="In review.")
    assert p1["summary_by"] == "master" and p1["summary_at"]
    conn.execute("UPDATE projects SET summary_at=1 WHERE id=?", (p["id"],))
    p2 = projects.update(conn, p["id"], by="ben", summary="In review.", next_step="Promote r5")
    assert p2["summary_at"] == 1 and p2["summary_by"] == "master" and p2["next_step"] == "Promote r5"
    p3 = projects.update(conn, p["id"], state="done")
    assert p3["closed_at"]
    assert projects.update(conn, p["id"], state="active")["closed_at"] is None


def test_client_view_rolls_up_tasks_loose_ends_and_unfiled(conn):
    p = projects.add(conn, "MAG", "FDD-O2S-1396")
    root = _task(conn, "write FDD", "deliverable",
                 spec={"deliverable_type": "fdd", "dod_path": "d.md", "format": "docx"})
    store.get_task(conn, root, reader="MAG")
    store.complete_task(conn, root, {"done": True, "summary": "s", "path": "C:/x/fdd.docx", "deliverable_type": "fdd",
                                     "dod_path": "d.md", "self_check": [{"item": "a", "met": True, "note": "ok"}],
                                     "follow_ups": [{"who": "MAG", "what": "ask Rajesh about loaders"}]})
    # Completion created the deliverable's dod_review under it.
    review = conn.execute("SELECT id FROM tasks WHERE parent_id=? AND kind='dod_review'", (root,)).fetchone()["id"]
    store.record_decision(conn, "Ship r5 as is", "Ben said so", task_id=root)
    child = _task(conn, "fix review notes", parent=root)
    _task(conn, "stray question")
    projects.attach(conn, p["id"], [root])
    v = projects.client_view(conn, "MAG")
    [pv] = v["projects"]
    assert sorted(d["id"] for d in pv["open"]) == sorted([review, child])
    assert [d["stage"] for d in pv["deliverables"]] == ["written"]
    assert pv["loose"][0]["what"] == "ask Rajesh about loaders"
    assert pv["decisions"][0]["statement"] == "Ship r5 as is"
    assert pv["summary_stale"]  # open work, no summary
    assert [d["title"] for d in v["unfiled"]] == ["stray question"]
    assert v["filed"] == 3 and v["task_count"] == 4
    html = client_pages.client_page(v)
    assert "FDD-O2S-1396" in html and "stray question" in html and "Attach checked" in html
    over = projects.clients_overview(conn, ["MAG", "LNK"])
    assert over[0]["active"] == 1 and over[0]["unfiled"] == 1 and over[0]["stale"] == 1
    assert over[1]["tasks"] == 0
    assert "Clients" in client_pages.clients_page(over, v["as_of"])


def test_task_page_names_the_project(conn):
    p = projects.add(conn, "MAG", "Kiro tracker gap")
    root = _task(conn, "root")
    child = _task(conn, "child", parent=root)
    projects.attach(conn, p["id"], [root])
    html = report.task_page(store.task_detail(conn, child), child)
    assert "Kiro tracker gap" in html and "/client/MAG#p-" in html and "inherited" in html


def test_cli_add_attach_show(conn, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COORD_DB", str(tmp_path / "t.db"))
    t = _task(conn, "root")
    assert projects.main(["add", "MAG", "Salesforce", "APIs", "--pipeline", "SP-3"]) == 0
    pid = json.loads(capsys.readouterr().out)["id"]
    assert projects.main(["attach", pid, t]) == 0
    assert projects.main(["set", pid, "--next", "map KHS QDocs"]) == 0
    capsys.readouterr()
    assert projects.main(["show", "MAG"]) == 0
    v = json.loads(capsys.readouterr().out)
    assert v["projects"][0]["title"] == "Salesforce APIs" and v["projects"][0]["next_step"] == "map KHS QDocs"
    assert projects.main(["attach", pid, "000000000000"]) == 1


def test_dashboard_routes(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    c = connect(db)
    t = _task(c, "root", client="P&C")
    c.close()
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setattr(report, "build_home", lambda conn: b"home", raising=False)
    th = threading.Thread(target=report.serve, args=(lambda: connect(db),), kwargs={"port": port}, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}"
    import time
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/clients", timeout=2)
            break
        except OSError:
            time.sleep(0.1)
    assert "P&amp;C" in urllib.request.urlopen(base + "/clients").read().decode()
    key = "/client/" + urllib.parse.quote("P&C", safe="")
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(base + "/client/Nobody")

    def post(form):
        req = urllib.request.Request(base + "/api/project", data=urllib.parse.urlencode(form, doseq=True).encode(),
                                     method="POST")
        return urllib.request.urlopen(req)

    post({"action": "add", "client": "P&C", "title": "Azure VPN cutover", "back": key})
    c = connect(db)
    [p] = projects.list_projects(c, "P&C")
    post({"action": "attach", "project": p["id"], "task": [t], "back": key})
    post({"action": "set", "project": p["id"], "title": "Azure VPN cutover", "goal": "", "state": "waiting",
          "summary": "Waiting on P&C IT.", "next_step": "", "pipeline": "", "back": key})
    assert projects.project_of(c, t) == p["id"]
    assert projects.get(c, p["id"])["state"] == "waiting"
    c.close()
    page = urllib.request.urlopen(base + key).read().decode()
    assert "Azure VPN cutover" in page and "Waiting on P&amp;C IT." in page
    with pytest.raises(urllib.error.HTTPError) as e:
        post({"action": "add", "client": "Nobody", "title": "x"})
    assert e.value.code == 400
