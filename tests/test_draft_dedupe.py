"""One email, one draft_reply; one Outlook draft per covered task (2026-11-17)."""
from __future__ import annotations

import pytest

from coord_mcp import store
from coord_mcp.db import connect


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


SPEC = {"instructions": "reply", "reply_from": "Raquel Brown <rbrown@example.com>",
        "reply_date": "2026-11-17T08:37"}


def _mk(conn, spec=SPEC, parent=None, refuse=True):
    return store.create_task(conn, "draft_reply", "DEV-124 reply", dict(spec), "Royal",
                             parent_id=parent, pipeline_task_id="1", refuse_duplicate_draft=refuse)


def test_second_draft_for_same_email_refused(conn):
    first = _mk(conn)["task_id"]
    with pytest.raises(ValueError, match=first):
        _mk(conn, {**SPEC, "reply_from": "rbrown@example.com"})


def test_internal_callers_get_a_warning(conn):
    first = _mk(conn)["task_id"]
    out = _mk(conn, refuse=False)
    assert any(first in w for w in out.get("warnings", []))


def test_revision_and_opt_out_allowed(conn):
    first = _mk(conn)["task_id"]
    _mk(conn, parent=first)
    _mk(conn, {**SPEC, "duplicate_ok": True})


def test_other_email_allowed(conn):
    _mk(conn)
    _mk(conn, {**SPEC, "reply_date": "2026-11-17T09:00"})


def test_covered_by_counts_as_drafted(conn):
    a = _mk(conn)["task_id"]
    store.record_decision(conn, f"{a} covered by 236776596826 Outlook draft (same body, one draft only).",
                          because="test", task_id=a)
    assert store.outlook_marks(conn)[a]["mark"] == "drafted"
