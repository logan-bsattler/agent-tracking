"""hourly-triage helpers: the 2026-11-16 Teams and overnight-email gaps."""
import json

import pytest

from coord_mcp import triage


# --- email_kind: the six hand-raised emails were all "signal" -------------

@pytest.mark.parametrize(
    "subject,sender",
    [
        ("QAD ticket:  QSM-1048881 - Promote CVC0534-63 to TEST", "QAD Support"),
        ("Re: LNK CVC Deployment: Checklist : 13th November", "Rahul Singh"),
        ("QAD ticket:  QSM-1069690 - DEV-137 Pre-Shipper Print - Pack Codes", ""),
        ("[JIRA] Updates for CVC0423-195: DEV-137 Pre-Shipper Reprint Update", "Arista"),
        ("A RAID item assigned to you is overdue", "RaidLogAdmin"),
        ("RE: LNK IT Maintenance Notification: QAD AUX 11/17/2026", "John Polich"),
    ],
)
def test_ticket_and_outage_mail_is_signal(subject, sender):
    assert triage.email_kind(subject, sender) == "signal"


def test_overdue_notice_is_signal_by_sender_alone():
    assert triage.email_kind("Reminder", "raidlogadmin@loganconsulting.com") == "signal"


@pytest.mark.parametrize(
    "subject,sender",
    [
        ("Please rate your experience with QSM-1069690: DEV-137", "QAD"),
        ("Accepted: MAG stand-up", "Stephen Ludwig"),
        ("Your weekly digest", "Barracuda Email Protection"),
        ("Changes to sheet", "Smartsheet"),
    ],
)
def test_noise(subject, sender):
    assert triage.email_kind(subject, sender) == "noise"


def test_plain_mail_is_left_to_judgement():
    assert triage.email_kind("Lunch Thursday?", "Shelton King") == "mail"


# --- teams_window: afterDateTime-only sweep, cut in code -----------------

def _msg(uri, created):
    return {"uri": uri, "createdDateTime": created, "summary": "x"}


SWEEP = [
    _msg("u-later", "2026-11-16T21:29:48.000Z"),
    _msg("u-shelton", "2026-11-16T21:00:41.000Z"),
    _msg("u-setty", "2026-11-16T19:27:17.000Z"),
    _msg("u-old", "2026-11-16T18:30:00.000Z"),
    {"moreResults": True, "nextOffset": 4},
]


def test_window_keeps_the_message_the_bounded_search_lost():
    # The 20:09Z run: last_check 19:00Z, window_end 20:10Z.
    out = triage.teams_window(SWEEP, "2026-11-16T19:00:00Z", "2026-11-16T20:10:00Z")
    assert [m["uri"] for m in out["new"]] == ["u-setty"]
    assert out["search_after"] == "2026-11-16T18:45:00Z"
    assert out["after_window"] == 2  # left for the next run, not marked seen


def test_window_skips_seen_and_orders_oldest_first():
    out = triage.teams_window(
        SWEEP, "2026-11-16T19:00:00Z", "2026-11-16T22:00:00Z", seen=["u-shelton"]
    )
    assert [m["uri"] for m in out["new"]] == ["u-setty", "u-later"]
    assert out["already_seen"] == 1


def test_overlap_reaches_back_15_minutes():
    out = triage.teams_window(SWEEP, "2026-11-16T18:40:00Z", "2026-11-16T19:00:00Z")
    assert [m["uri"] for m in out["new"]] == ["u-old"]


def test_naive_times_are_utc():
    out = triage.teams_window(SWEEP, "2026-11-16T19:00:00", "2026-11-16T20:10:00")
    assert [m["uri"] for m in out["new"]] == ["u-setty"]


# --- teams_next_check: a silent empty answer never advances the mark -----

def test_empty_sweep_without_probe_keeps_last_check():
    assert (
        triage.teams_next_check("2026-11-16T19:09:42Z", "2026-11-16T20:10:00Z", 0, False)
        == "2026-11-16T19:09:42Z"
    )


def test_empty_sweep_confirmed_by_probe_advances():
    assert (
        triage.teams_next_check("2026-11-16T19:09:42Z", "2026-11-16T20:10:00Z", 0, True)
        == "2026-11-16T20:10:00Z"
    )


def test_nonempty_sweep_advances():
    assert (
        triage.teams_next_check("2026-11-16T19:09:42Z", "2026-11-16T20:10:00Z", 3, False)
        == "2026-11-16T20:10:00Z"
    )


# --- CLI ------------------------------------------------------------------

def test_cli_teams_window(tmp_path, capsys):
    res = tmp_path / "r.json"
    res.write_text(json.dumps(SWEEP))
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"teams": {"seen_uris": ["u-setty"]}}))
    rc = triage._cli([
        "teams-window", str(res), "--after", "2026-11-16T19:00:00Z",
        "--before", "2026-11-16T22:00:00Z", "--state", str(state),
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert [m["uri"] for m in out["new"]] == ["u-shelton", "u-later"]


def test_cli_email_kind(capsys):
    assert triage._cli(["email-kind", "--subject", "[JIRA] Updates for CVC0707-79"]) == 0
    assert capsys.readouterr().out.strip() == "signal"
