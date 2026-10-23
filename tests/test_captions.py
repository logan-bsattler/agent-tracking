"""Live captions: which snapshot text is new, and when the watcher hands a batch over."""

from __future__ import annotations

import io

import pytest

from coord_mcp import captions
from coord_mcp.captions import Batcher, LiveDiffer, TeamsDiffer


def it(speaker, text):
    return {"speaker": speaker, "text": text}


def texts(lines):
    return [(l.speaker, l.text, l.cont) for l in lines]


# ------------------------------------------------------------ Teams


def test_entry_above_the_last_is_written_at_once():
    d = TeamsDiffer(settle=4)
    assert d.feed([it("Ana", "Hello all")], 0) == []
    out = d.feed([it("Ana", "Hello all"), it("Ben", "Hi")], 1)
    assert texts(out) == [("Ana", "Hello all", False)]


def test_last_entry_waits_until_it_holds_still():
    d = TeamsDiffer(settle=4)
    d.feed([it("Ana", "Can you")], 0)
    d.feed([it("Ana", "Can you send the spec")], 1)
    assert d.feed([it("Ana", "Can you send the spec")], 4) == []
    assert texts(d.feed([it("Ana", "Can you send the spec")], 5)) == [("Ana", "Can you send the spec", False)]
    assert d.feed([it("Ana", "Can you send the spec")], 9) == []


def test_entry_that_grows_after_writing_is_written_as_its_new_words():
    d = TeamsDiffer(settle=1)
    d.feed([it("Ana", "We need the BOM report")], 0)
    d.feed([it("Ana", "We need the BOM report")], 2)
    d.feed([it("Ana", "We need the BOM report by Friday please")], 3)
    out = d.feed([it("Ana", "We need the BOM report by Friday please")], 5)
    assert texts(out) == [("Ana", "by Friday please", True)]


def test_entries_scrolling_off_the_top_are_not_rewritten():
    d = TeamsDiffer(settle=1)
    d.feed([it("A", "one two three"), it("B", "four five six"), it("A", "seven")], 0)
    out = d.feed([it("B", "four five six"), it("A", "seven"), it("B", "eight")], 1)
    assert texts(out) == [("A", "seven", False)]


def test_same_words_from_another_speaker_are_new():
    d = TeamsDiffer(settle=1)
    d.feed([it("A", "sounds good to me"), it("B", "x")], 0)
    out = d.feed([it("A", "sounds good to me"), it("B", "sounds good to me"), it("A", "y")], 1)
    assert texts(out) == [("B", "sounds good to me", False)]


def test_teams_entries_found_below_wrappers_and_beside_the_toolbar():
    # The shape the live pane had on 2026-10-23, names shortened.
    def leaf(n, button=False):
        return (n, button, [])

    def entry(spk, text):
        return ("", False, [leaf(spk), leaf(text)])

    pane = ("Live Captions", False, [
        ("", False, [("", False, [("", False, [
            ("", False, [leaf("Captions will be shown in: English (US)")]),
            ("", False, []),
            entry("Ana", "Can you send the spec"),
            ("", False, []),
            entry("Ben", "By Friday"),
        ])])]),
        ("", False, [
            ("", False, [leaf("Give live captions feedback Thumb up", True),
                         leaf("Give live captions feedback Thumb down", True)]),
            leaf("Hide live captions", True),
        ]),
    ])
    assert captions.teams_entries(pane) == [it("Ana", "Can you send the spec"), it("Ben", "By Friday")]


def test_render():
    from datetime import datetime

    t = datetime(2026, 10, 22, 9, 5, 7)
    assert captions.Line("Ana", "hi").render(t) == "09:05:07 Ana: hi"
    assert captions.Line("Ana", "more", cont=True).render(t) == "09:05:07 Ana (cont.): more"
    assert captions.Line("", "no name").render(t) == "09:05:07 no name"


# ------------------------------------------------------------ Live Captions


def test_live_sentence_written_once_followed():
    d = LiveDiffer(settle=4)
    assert d.feed("Let's start. Ben can you", 0) == [captions.Line("", "Let's start.")]
    assert d.feed("Let's start. Ben can you check the report.", 1) == []
    out = d.feed("Let's start. Ben can you check the report. Next item", 2)
    assert [l.text for l in out] == ["Ben can you check the report."]


def test_live_tail_written_when_the_block_holds_still():
    d = LiveDiffer(settle=4)
    d.feed("All done here.", 0)
    assert [l.text for l in d.feed("All done here.", 5)] == ["All done here."]


def test_live_block_cut_at_the_front_is_not_new_speech():
    d = LiveDiffer(settle=4)
    d.feed("First we review the open tickets. Then", 0)
    out = d.feed("review the open tickets. Then pricing.", 1)
    assert out == []


# ------------------------------------------------------------ batching


def test_batch_due_after_a_pause():
    b = Batcher(quiet=20, max_wait=90)
    b.add(["a"], 0)
    b.add(["b"], 10)
    assert not b.due(29)
    assert b.due(30)
    assert b.take() == ["a", "b"] and not b.due(100)


def test_batch_due_after_max_wait_during_a_monologue():
    b = Batcher(quiet=20, max_wait=90)
    for t in range(0, 100, 10):
        b.add([str(t)], t)
        if t < 90:
            assert not b.due(t)
    assert b.due(90)


# ------------------------------------------------------------ transcript and watch


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORD_DB", str(tmp_path / "coord.db"))
    return tmp_path


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def run_watch(path, clock, steps):
    """Run watch with a fake clock; `steps` maps a poll index to lines to
    append before that poll."""
    out = io.StringIO()
    n = {"i": 0}

    def sleep(_):
        clock.t += 5
        n["i"] += 1
        captions.append(path, steps.get(n["i"], []))
        if n["i"] > 200:
            raise RuntimeError("watch never ended")

    captions.watch(path, quiet=10, max_wait=60, out=out, clock=clock, sleep=sleep)
    return out.getvalue()


def test_watch_batches_with_line_numbers_and_ends(home):
    p = captions.new_transcript("Weekly sync", "teams")
    assert p.parent == home / "meetings" and p.name.endswith("-Weekly-sync.txt")
    out = run_watch(p, Clock(), {1: ["09:00:01 Ana: hi"], 2: ["09:00:05 Ben: hello"], 8: [captions.END]})
    blocks = out.split("== ")
    assert f"{p} lines 1-5 ==" in out  # 3 header lines + 2 speech
    assert "09:00:05 Ben: hello" in out
    assert out.rstrip().endswith(f"== meeting ended: {p} ==")
    assert len([b for b in blocks if b.startswith(str(p))]) == 1  # the end marker alone is no batch


def test_watch_resumes_where_a_re_armed_monitor_stopped(home):
    p = captions.new_transcript("m", "teams")
    captions.append(p, ["09:00:01 Ana: first"])
    out = io.StringIO()

    class Stop(Exception):
        pass

    clock = Clock()

    def sleep(_):
        clock.t += 15
        if clock.t > 30:
            raise Stop

    with pytest.raises(Stop):
        captions.watch(p, quiet=10, max_wait=60, out=out, clock=clock, sleep=sleep)
    assert "first" in out.getvalue()

    captions.append(p, ["09:01:00 Ana: second", captions.END])
    again = run_watch(p, Clock(), {})
    assert "first" not in again and "second" in again
    assert f"{p} lines 5-6 ==" in again


def test_end_is_idempotent_and_latest_finds_newest(home):
    a = captions.new_transcript("a", "teams")
    captions.end(a)
    captions.end(a)
    assert a.read_text().count(captions.END) == 1 and captions.ended(a)
    assert captions.latest() == a


# ------------------------------------------------------------ auto


from datetime import datetime as _dt

NOW = _dt(2026, 10, 22, 10, 0, 0)


def auto(log=None, patterns=("Lunch",)):
    return captions.Auto(settle=1, grace=60, log=(log.append if log is not None else lambda s: None),
                         patterns=lambda: list(patterns))


def test_clean_title():
    assert captions.clean_title("MAG weekly | Microsoft Teams") == "MAG weekly"
    assert captions.clean_title("") == "meeting"


def test_auto_starts_when_captions_show_and_ends_when_window_closes(home):
    a = auto()
    a.step("MAG weekly", "no-captions", 0, NOW)
    assert a.path is None  # no captions, no transcript
    a.step("MAG weekly", [it("Ana", "hi"), it("Ben", "x")], 1, NOW)
    p = a.path
    assert p and "MAG-weekly" in p.name and "Ana: hi" in p.read_text()
    a.step(None, "no-window", 30, NOW)
    assert a.path == p  # inside the grace period
    a.step(None, "no-window", 61, NOW)
    assert a.path is None and captions.ended(p)


def test_auto_rides_out_captions_flicker(home):
    a = auto()
    a.step("Sync", [it("A", "one")], 0, NOW)
    p = a.path
    a.step("Sync", "no-captions", 10, NOW)
    a.step("Sync", [it("A", "one"), it("B", "two")], 20, NOW)
    a.step("Sync", "no-captions", 70, NOW)
    assert a.path == p and not captions.ended(p)
    assert len(list(p.parent.glob("*.txt"))) == 1


def test_auto_new_title_ends_old_and_starts_new(home):
    a = auto()
    a.step("First", [it("A", "one")], 0, NOW)
    first = a.path
    a.step("Second", [it("B", "two")], 5, _dt(2026, 10, 22, 11, 0))
    assert captions.ended(first) and a.path != first and "Second" in a.path.name


def test_auto_generic_title_flip_does_not_split(home):
    a = auto()
    a.step("Staffing", [it("A", "one")], 0, NOW)
    p = a.path
    a.step("Microsoft Teams", [it("A", "one"), it("B", "two")], 5, NOW)
    a.step("Staffing", [it("A", "one"), it("B", "two"), it("A", "three")], 10, NOW)
    assert a.path == p and not captions.ended(p)
    assert len(list(p.parent.glob("*.txt"))) == 1


def test_auto_started_under_generic_title_takes_the_real_one(home):
    a = auto()
    a.step("Microsoft Teams", [it("A", "one")], 0, NOW)
    p = a.path
    a.step("Staffing", [it("A", "one"), it("B", "two")], 5, NOW)
    assert a.path == p and a.title == "Staffing"
    a.step("Other", [it("C", "x")], 10, NOW)
    assert captions.ended(p)


def test_auto_skip_list_is_logged_once(home):
    log = []
    a = auto(log)
    for t in range(3):
        a.step("Lunch with team", [it("A", "food")], t, NOW)
    assert a.path is None and len(log) == 1 and "skip 'Lunch'" in log[0]


def test_auto_end_by_hand_leaves_that_meeting_alone(home):
    a = auto()
    a.step("Sync", [it("A", "one")], 0, NOW)
    p = a.path
    captions.end(p)
    a.step("Sync", [it("A", "one"), it("B", "two")], 1, NOW)
    a.step("Sync", [it("A", "one"), it("B", "two")], 2, NOW)
    assert a.path is None and len(list(p.parent.glob("*.txt"))) == 1
    a.step(None, "no-window", 3, NOW)
    a.step("Sync", [it("A", "back")], 4, _dt(2026, 10, 22, 12, 0))
    assert a.path and a.path != p  # rejoined later: a new transcript


def test_skip_file_seeded_with_defaults(home):
    assert captions.skip_patterns() == list(captions.SKIP_DEFAULT)
    assert captions.skipped("Focus time block", captions.skip_patterns()) == "Focus time"


# ------------------------------------------------------------ follow


def test_silent_meeting_costs_nothing(home):
    p = captions.new_transcript("quiet", "teams")
    captions.end(p)
    assert run_watch(p, Clock(), {}) == ""
    assert captions.finished(p)


def test_follow_moves_to_the_next_meeting_and_drops_a_dead_one(home):
    import os as _os

    dead = captions.new_transcript("dead", "teams", _dt(2026, 10, 22, 9, 0))
    captions.append(dead, ["09:00:01 A: before the crash"])
    _os.utime(dead, (1, 1))
    out = io.StringIO()
    clock = Clock()
    state = {"n": 0, "live": None}

    def sleep(_):
        clock.t += 5
        state["n"] += 1
        if state["n"] == 3:
            state["live"] = captions.new_transcript("live", "teams", _dt(2026, 10, 22, 10, 0))
            captions.append(state["live"], ["10:00:01 B: hello", captions.END])

    captions.follow(quiet=10, max_wait=60, out=out, clock=clock, sleep=sleep,
                    until=lambda: state["live"] is not None and captions.finished(state["live"]))
    s = out.getvalue()
    assert "before the crash" in s and f"meeting ended: {dead}" in s
    assert "hello" in s and f"meeting ended: {state['live']}" in s
