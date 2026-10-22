"""draft_reply: a reply an agent wrote, turned into an Outlook draft on Ben's go."""

from __future__ import annotations

import pytest

from coord_mcp import contracts, outlook, report, store
from coord_mcp.db import connect


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


BODY = "# DRAFT — reply to Corwin (not sent)\n\nTo: Corwin · Cc: Kelly\n\n---\n\nHi Corwin,\n\nSame error as last fall.\n\nBen\n"


def _draft_task(conn, tmp_path, body=BODY, done=True):
    f = tmp_path / "DRAFT-reply-Corwin.md"
    f.write_text(body, encoding="utf-8")
    tid = store.create_task(conn, "draft_reply", "Reply to Corwin re QSM", {"q": "x"}, assigned_to="Moog")["task_id"]
    store.get_task(conn, tid, reader="Moog")
    if done:
        store.complete_task(conn, tid, {
            "done": True, "to": ["corwin@moog.com"], "cc": ["kelly@moog.com", "jade@moog.com"],
            "subject": "RE: QSM-1050438 SMTP failure", "body_path": str(f),
            "in_reply_to": "Corwin, 2026-10-21 09:12", "summary": "Point QAD at the email-definition fix.",
            "follow_ups": [{"who": "Ben", "what": "Review and draft to Corwin."}],
        })
    return tid, f


def test_contract_envelope_only():
    shape = contracts.expected_shape("draft_reply")
    assert shape["to"] and shape["subject"] and shape["body_path"] and "cc?" in shape
    with pytest.raises(contracts.ValidationError) as ei:
        contracts.validate("draft_reply", {"done": True, "to": "corwin@moog.com", "subject": "s", "summary": "x",
                                           "body_path": "p", "body": "the whole email"})
    errs = ei.value.errors
    assert any(e.startswith("to:") for e in errs)
    assert any("body" in e and not e.startswith("body_path") for e in errs)  # a body field is off-contract


def test_compose_url_carries_envelope_not_body():
    u = outlook.compose_url(["a@x.com", " b@x.com "], ["c@x.com"], "RE: hello & goodbye")
    assert u.startswith(outlook.COMPOSE + "?to=a%40x.com%3Bb%40x.com&cc=c%40x.com&subject=RE%3A+hello+%26+goodbye")
    assert "body" not in u


def test_body_drops_draft_header_only():
    assert outlook.body_text(BODY) == "Hi Corwin,\n\nSame error as last fall.\n\nBen\n"
    plain = "Hi,\n\nno header here\n"
    assert outlook.body_text(plain) == plain
    assert outlook.body_text("# Title\nno rule follows\n") == "# Title\nno rule follows\n"


def test_draft_from_task(conn, tmp_path):
    tid, f = _draft_task(conn, tmp_path)
    d = outlook.draft(conn, tid)
    assert d["to"] == ["corwin@moog.com"] and d["cc"] == ["kelly@moog.com", "jade@moog.com"]
    assert d["exists"] and d["body"].startswith("Hi Corwin,") and d["body_path"] == str(f)
    assert "subject=RE%3A+QSM-1050438" in d["url"]


def test_draft_refuses_other_kinds_and_unfinished(conn, tmp_path):
    other = store.create_task(conn, "investigation", "t", {"q": "x"}, assigned_to="Moog")["task_id"]
    with pytest.raises(ValueError):
        outlook.draft(conn, other)
    tid, _ = _draft_task(conn, tmp_path, done=False)
    with pytest.raises(ValueError):
        outlook.draft(conn, tid)
    with pytest.raises(ValueError):
        store.queue_operator_prompt(conn, "draft", tid)


def test_button_queues_the_draft_prompt(conn, tmp_path):
    tid, _ = _draft_task(conn, tmp_path)
    assert store.queue_operator_prompt(conn, "draft", tid)["ok"]
    text = store.take_operator_prompt(conn)["text"]
    assert text.startswith(f"Draft in Outlook: task {tid} (Moog: RE: QSM-1050438")
    assert "never press Send" in text and f"coord_mcp.outlook {tid}" in text


def test_task_page_shows_draft_card(conn, tmp_path):
    tid, f = _draft_task(conn, tmp_path)
    page = report.task_page(store.task_detail(conn, tid), tid)
    assert "Draft reply" in page and 'value="draft"' in page and "Hi Corwin," in page
    assert "corwin@moog.com" in page and "Open compose in my browser" in page
    assert "# DRAFT" not in page.split('class="draft-body"')[1].split("</pre>")[0]
    assert "Master drafts it when idle" in report.task_page(store.task_detail(conn, tid), tid, queued=True)


def test_task_page_without_body_file_has_no_button(conn, tmp_path):
    tid, f = _draft_task(conn, tmp_path)
    f.unlink()
    page = report.task_page(store.task_detail(conn, tid), tid)
    assert "Draft reply" in page and 'value="draft"' not in page and "not on disk" in page


def test_cli_prints_url_then_body(conn, tmp_path, monkeypatch, capsys):
    tid, _ = _draft_task(conn, tmp_path)
    monkeypatch.setenv("COORD_DB", str(tmp_path / "t.db"))
    assert outlook.main([tid]) == 0
    out = capsys.readouterr().out
    assert out.startswith(outlook.COMPOSE) and "Subject: RE: QSM-1050438" in out and out.rstrip().endswith("Ben")
    assert outlook.main(["nope"]) == 1
