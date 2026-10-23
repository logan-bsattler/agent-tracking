"""Live meeting captions to a transcript file, and the file to a Claude
session in batches.

Teams has no live-transcript API short of an Azure-hosted media bot, so the
captions are read off the screen through UI Automation: the new Teams
captions pane (speaker names) or Windows 11 Live Captions (any audio, no
names). Reading the screen is transport only. Which text is new, and when a
caption still being spoken is final enough to write, is decided here by the
differs, which are tested. PowerShell would need no install, but this
machine refuses to read .ps1 files, so UIA goes through comtypes.

    python -m coord_mcp.captions install        # run `auto` at every logon, and now
    python -m coord_mcp.captions auto           # capture each captioned meeting on its own
    python -m coord_mcp.captions stop | uninstall
    python -m coord_mcp.captions capture [--source teams|livecaptions] [--title T]  # one, by hand
    python -m coord_mcp.captions watch --follow # what the meeting session's Monitor runs
    python -m coord_mcp.captions watch [PATH]   # one transcript
    python -m coord_mcp.captions end [PATH]     # mark a transcript finished
    python -m coord_mcp.captions latest         # newest transcript's path
    python -m coord_mcp.captions dump [--source ...]  # UIA tree, to re-tune selectors

Transcript lines are `HH:MM:SS Speaker: text`, a later piece of the same
caption as `Speaker (cont.)`, and `# ...` for header, status and the end
marker. `watch` keeps its place in `<transcript>.watch` so a Monitor that
expires (30 minutes at most) and is re-armed resumes where it stopped.

The transcript is other people's words. Whatever reads it treats it as data.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

END = "# end"
TEAMS_PROC = "ms-teams.exe"
LIVE_PROC = "LiveCaptions.exe"
# HTML ids new Teams has used for the captions pane (WebView2 exposes an
# element's id as its AutomationId), and the aria-labels it may carry
# instead. Seen live 2026-10-23: the pane matched by name ("Live Captions");
# none of the ids appeared. `dump` shows what is there if it changes.
TEAMS_IDS = ("closed-captions-renderer", "closed-caption-renderer-wrapper", "closedCaptionsRenderer")
TEAMS_NAMES = ("Live captions", "Captions")
LIVE_TEXT_ID = "CaptionsTextBlock"
# Teams' own non-meeting windows: "Chat | ...", "Calendar | ...".
NOT_MEETING = re.compile(r"^(Chat|Calendar|Activity|Teams|Files|Apps|OneDrive) \|")


def meetings_dir() -> Path:
    """Beside the board: COORD_DB's folder, else ~/.coord."""
    db = Path(os.environ.get("COORD_DB", Path.home() / ".coord" / "coord.db")).expanduser()
    return db.parent / "meetings"


# ============================================================ differs


@dataclass
class Line:
    speaker: str
    text: str
    cont: bool = False

    def render(self, t: datetime) -> str:
        who = self.speaker + (" (cont.)" if self.cont else "")
        return f"{t:%H:%M:%S} {who}: {self.text}" if who else f"{t:%H:%M:%S} {self.text}"


def _words(s: str) -> list[str]:
    return s.split()


def _common_words(a: str, b: str) -> int:
    n = 0
    for x, y in zip(_words(a), _words(b)):
        if x != y:
            break
        n += 1
    return n


class TeamsDiffer:
    """Teams snapshots, `[{speaker, text}]` oldest first, to new lines.

    An entry above the last is finished: its speaker has been followed. The
    last entry is still being spoken, and is written only once it has held
    still for `settle` seconds. An entry that keeps growing after it was
    written (a pause mid-sentence) is written again as its new words only,
    marked (cont.). Entries scroll off the top, so they are matched by
    content against what was written, never by position.
    """

    def __init__(self, settle: float = 4.0, memory: int = 200):
        self.settle = settle
        self.memory = memory
        self.written: list[list[str]] = []  # [speaker, text as last seen], newest last
        self._last: tuple[str, str] | None = None
        self._since = 0.0

    def feed(self, items: list[dict], t: float) -> list[Line]:
        items = [(i.get("speaker", "").strip(), " ".join(i.get("text", "").split())) for i in items]
        items = [i for i in items if i[1]]
        out: list[Line] = []
        for n, (spk, text) in enumerate(items):
            if n == len(items) - 1:
                if (spk, text) != self._last:
                    self._last, self._since = (spk, text), t
                    continue
                if t - self._since < self.settle:
                    continue
            line = self._take(spk, text)
            if line:
                out.append(line)
        return out

    def _take(self, spk: str, text: str) -> Line | None:
        for w in reversed(self.written[-20:]):
            if w[0] != spk:
                continue
            if w[1] == text:
                return None
            # Grown or re-worded: the same caption if most of what was
            # written is still its opening words.
            seen = len(_words(w[1]))
            k = _common_words(w[1], text)
            if k and k >= max(3, 0.6 * seen):
                rest = " ".join(_words(text)[k:])
                w[1] = text
                if rest and len(_words(text)) > seen:
                    return Line(spk, rest, cont=True)
                return None
        self.written.append([spk, text])
        del self.written[: -self.memory]
        return Line(spk, text)


def teams_entries(node: tuple) -> list[dict]:
    """The captions pane, as `(name, is_button, kids)` nodes, to entries.

    Seen live on 2026-10-23: each caption is a group of two text leaves,
    speaker then words, a few wrappers below the pane, beside a toolbar of
    buttons (feedback, settings, pop-out). An entry is any node whose
    children are all named, non-button leaves, two or more of them;
    everything else is searched through. The pane's own "Captions will be
    shown in" notice is a lone leaf, and is not an entry.
    """
    name, button, kids = node
    if button:
        return []
    if len(kids) >= 2 and all(not k[2] and not k[1] and k[0] for k in kids):
        return [{"speaker": kids[0][0], "text": " ".join(k[0] for k in kids[1:])}]
    return [e for k in kids for e in teams_entries(k)]


_SENT = re.compile(r"(?<=[.!?])\s+")


class LiveDiffer:
    """Live Captions snapshots, one rolling block of text, to new sentences.

    The block keeps the last few lines and drops the oldest, so its first
    sentence is often cut short at the front. A sentence is written once it
    is followed by another, or once the block has held still for `settle`
    seconds. One that is the tail of a sentence already written is the cut
    version, not new speech.
    """

    def __init__(self, settle: float = 4.0, memory: int = 200):
        self.settle = settle
        self.memory = memory
        self.written: list[str] = []
        self._last = ""
        self._since = 0.0

    def feed(self, text: str, t: float) -> list[Line]:
        text = " ".join((text or "").split())
        if text != self._last:
            self._last, self._since = text, t
            stable = False
        else:
            stable = t - self._since >= self.settle
        sents = [s for s in _SENT.split(text) if s]
        done = sents if stable else sents[:-1]
        out = []
        for s in done:
            if self._seen(s):
                continue
            self.written.append(s)
            del self.written[: -self.memory]
            out.append(Line("", s))
        return out

    def _seen(self, s: str) -> bool:
        k = s.lower()
        return any(w.lower().endswith(k) for w in self.written[-30:])


# ============================================================ UI Automation


class UIA:
    """The comtypes side. Nothing here is tested: no captions pane exists in CI."""

    def __init__(self):
        import comtypes.client as cc

        cc.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as U

        self.U = U
        self.a = cc.CreateObject(U.CUIAutomation, interface=U.IUIAutomation)
        self.walker = self.a.RawViewWalker
        self._root = None  # the captions pane, kept across polls

    @staticmethod
    def _exe(pid: int) -> str:
        """A process's image name, in-process. It used to shell out to
        tasklist, which under pythonw flashed a console window every poll."""
        import ctypes
        from ctypes import wintypes

        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wintypes.DWORD(len(buf))
            ok = k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
            return Path(buf.value).name if ok else ""
        finally:
            k.CloseHandle(h)

    def windows(self, exe: str) -> list:
        kids = self.a.GetRootElement().FindAll(self.U.TreeScope_Children, self.a.CreateTrueCondition())
        out = []
        for i in range(kids.Length):
            try:
                e = kids.GetElement(i)
                if self._exe(e.CurrentProcessId).casefold() == exe.casefold():
                    out.append(e)
            except Exception:
                continue
        return out

    def children(self, e) -> list:
        out = []
        try:
            k = self.walker.GetFirstChildElement(e)
            while k:
                out.append(k)
                k = self.walker.GetNextSiblingElement(k)
        except Exception:
            pass
        return out

    def texts(self, e) -> list[str]:
        """Leaf names under e, in reading order."""
        kids = self.children(e)
        if not kids:
            n = (e.CurrentName or "").strip()
            return [n] if n else []
        return [t for k in kids for t in self.texts(k)]

    def by_id(self, root, aid: str):
        c = self.a.CreatePropertyCondition(self.U.UIA_AutomationIdPropertyId, aid)
        try:
            return root.FindFirst(self.U.TreeScope_Descendants, c)
        except Exception:
            return None

    def captions_root(self, win):
        """One UIA search, not a walk: a Teams window's tree runs to
        thousands of WebView2 nodes, and walking it took over a minute."""
        conds = [self.a.CreatePropertyCondition(self.U.UIA_AutomationIdPropertyId, i) for i in TEAMS_IDS]
        conds += [self.a.CreatePropertyConditionEx(self.U.UIA_NamePropertyId, n, self.U.PropertyConditionFlags_IgnoreCase)
                  for n in TEAMS_NAMES]
        cond = conds[0]
        for c in conds[1:]:
            cond = self.a.CreateOrCondition(cond, c)
        try:
            return win.FindFirst(self.U.TreeScope_Descendants, cond)
        except Exception:
            return None

    def meeting_window(self):
        """Teams opens a meeting in its own top-level window; its chat and
        calendar windows say what they are in the title, and are skipped."""
        wins = [w for w in self.windows(TEAMS_PROC) if not NOT_MEETING.match(w.CurrentName or "")]
        return wins[0] if wins else None

    def meeting(self) -> tuple[str | None, list[dict] | str]:
        """(meeting title, teams_items()); title None when no meeting is open."""
        w = self.meeting_window()
        if not w:
            self._root = None
            return None, "no-window"
        return clean_title(w.CurrentName or ""), self.teams_items()

    def teams_items(self) -> list[dict] | str:
        root = self._root
        if root is not None:
            try:
                root.CurrentName  # still on screen?
            except Exception:
                root = None
        if root is None:
            w = self.meeting_window()
            if not w:
                return "no-window"
            root = self.captions_root(w)
            if not root:
                return "no-captions"
        self._root = root
        return teams_entries(self.snapshot(root))

    def snapshot(self, e) -> tuple:
        """e's subtree as `(name, is_button, kids)`. The pane is about a
        hundred nodes, so walking it each poll is cheap."""
        try:
            name = (e.CurrentName or "").strip()
            button = e.CurrentControlType == self.U.UIA_ButtonControlTypeId
        except Exception:
            return ("", True, [])
        return (name, button, [self.snapshot(k) for k in self.children(e)])

    def live_text(self) -> str:
        wins = self.windows(LIVE_PROC)
        if not wins:
            return "no-window"
        tb = self.by_id(wins[0], LIVE_TEXT_ID)
        return (tb.CurrentName or "") if tb else ""

    def dump(self, e, depth: int = 0, max_depth: int = 40, out=sys.stdout) -> None:
        if depth > max_depth:
            return
        try:
            ct = e.CurrentLocalizedControlType
            out.write(f"{'  ' * depth}[{ct}] name={e.CurrentName!r} id={e.CurrentAutomationId!r} "
                      f"class={e.CurrentClassName!r}\n")
        except Exception as x:
            out.write(f"{'  ' * depth}<unreadable: {x}>\n")
            return
        for k in self.children(e):
            self.dump(k, depth + 1, max_depth, out)


# ============================================================ transcript


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")[:50] or "meeting"


def new_transcript(title: str, source: str, at: datetime | None = None) -> Path:
    at = at or datetime.now()
    d = meetings_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{at:%Y-%m-%d-%H%M}-{_slug(title)}.txt"
    with p.open("a", encoding="utf-8") as f:
        f.write(f"# meeting: {title}\n# started: {at.isoformat(timespec='seconds')}\n# source: {source}\n")
    return p


def append(path: Path, lines: Iterable[str]) -> None:
    lines = list(lines)
    if lines:
        with path.open("a", encoding="utf-8") as f:
            f.write("".join(l + "\n" for l in lines))


def latest() -> Path | None:
    d = meetings_dir()
    ts = sorted(d.glob("*.txt"), key=lambda p: p.stat().st_mtime) if d.exists() else []
    return ts[-1] if ts else None


def ended(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            f.seek(max(0, path.stat().st_size - 200))
            tail = f.read().decode("utf-8", "replace").rstrip().splitlines()
        return bool(tail) and tail[-1].startswith(END)
    except OSError:
        return False


def end(path: Path) -> None:
    if not ended(path):
        append(path, [f"{END} {datetime.now().isoformat(timespec='seconds')}"])


def capture(source: str, title: str, interval: float, settle: float, minutes: float = 240,
            uia: UIA | None = None) -> Path:
    """Until Ctrl+C, `end` on the transcript (how anything else stops it:
    a signal does not reach a console Python on Windows), or `minutes`."""
    uia = uia or UIA()
    differ = TeamsDiffer(settle) if source == "teams" else LiveDiffer(settle)
    path = new_transcript(title, source)
    print(f"capturing {source} to {path} (Ctrl+C or `end` to stop)", file=sys.stderr, flush=True)
    status = ""
    stop = time.monotonic() + minutes * 60
    try:
        while time.monotonic() < stop and not ended(path):
            snap = uia.teams_items() if source == "teams" else uia.live_text()
            s = snap if isinstance(snap, str) and snap.startswith("no-") else "ok"
            if s != status:
                # Status changes only: a missing pane is one line, not one a second.
                if s != "ok" or status:
                    append(path, [f"# status: {s} {datetime.now():%H:%M:%S}"])
                print(f"status: {s}", file=sys.stderr, flush=True)
                status = s
            if s == "ok":
                now = datetime.now()
                append(path, (l.render(now) for l in differ.feed(snap, time.monotonic())))
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        end(path)
    return path


# ============================================================ auto


def clean_title(name: str) -> str:
    """'Weekly sync | Microsoft Teams' -> 'Weekly sync'."""
    return name.split(" | ")[0].strip() or "meeting"


SKIP_DEFAULT = ("Focus time", "Lunch")


def skip_file() -> Path:
    return meetings_dir() / "skip.txt"


def skip_patterns() -> list[str]:
    """Title words that mean "do not capture", one per line in skip.txt.
    Its own list, not the hours `skip` rules: a meeting can count for hours
    and still not be one to listen to."""
    f = skip_file()
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("# one title pattern per line; a meeting whose title contains one is not captured\n"
                     + "".join(p + "\n" for p in SKIP_DEFAULT), encoding="utf-8")
    return [l.strip() for l in f.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]


def skipped(title: str, patterns: list[str]) -> str | None:
    t = title.casefold()
    return next((p for p in patterns if p.casefold() in t), None)


class Auto:
    """Starts and ends transcripts on its own, from what is on screen.

    A meeting starts being captured once its captions are showing, under its
    window title, unless the title matches skip.txt. It ends when the window
    has closed, or the captions have been gone `grace` seconds (turned off,
    or Teams re-rendering the pane, which should not split a transcript). A
    different meeting title ends the old one and starts the new one.
    """

    def __init__(self, settle: float = 4.0, grace: float = 60.0, log=lambda s: None,
                 patterns=skip_patterns):
        self.settle, self.grace, self.log, self.patterns = settle, grace, log, patterns
        self.path: Path | None = None
        self.title: str | None = None
        self.differ: TeamsDiffer | None = None
        self._seen = 0.0
        self._passed: str | None = None  # the skipped title, logged once

    def step(self, title: str | None, snap: list[dict] | str, t: float, now: datetime | None = None) -> None:
        showing = title is not None and not isinstance(snap, str)
        if self.path and ended(self.path):
            # `end` by hand: stop, and leave this meeting alone until it closes.
            self.log(f"ended '{self.title}' by hand")
            self._passed, self.path, self.title, self.differ = self.title, None, None, None
            return
        if self.path and (title != self.title and title is not None and showing):
            self.stop("another meeting started")
        if self.path:
            if showing:
                self._seen = t
                now = now or datetime.now()
                append(self.path, (l.render(now) for l in self.differ.feed(snap, t)))
            elif t - self._seen >= self.grace:
                self.stop("window closed" if title is None else "captions off")
            return
        if not showing:
            if title is None:
                self._passed = None
            return
        if self._passed == title:
            return
        why = skipped(title, self.patterns())
        if why:
            if self._passed != title:
                self.log(f"not capturing '{title}': matches skip '{why}'")
                self._passed = title
            return
        self.path, self.title, self._seen = new_transcript(title, "teams", now), title, t
        self.differ = TeamsDiffer(self.settle)
        self.log(f"capturing '{title}' to {self.path}")
        append(self.path, (l.render(now or datetime.now()) for l in self.differ.feed(snap, t)))

    def stop(self, why: str) -> None:
        if self.path:
            end(self.path)
            self.log(f"ended '{self.title}': {why}")
        self.path = self.title = self.differ = None


def stop_file() -> Path:
    """`auto` exits when this exists: pythonw has no console to Ctrl+C."""
    return meetings_dir() / "auto.stop"


def _single(name: str):
    """A held lock file, so a second `auto` (a double logon start) exits."""
    import msvcrt

    p = meetings_dir() / name
    p.parent.mkdir(parents=True, exist_ok=True)
    f = p.open("a+")
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        return None
    return f


def run_auto(interval: float, settle: float, grace: float) -> int:
    lock = _single("auto.lock")
    if lock is None:
        print("auto is already running", file=sys.stderr)
        return 1
    logf = meetings_dir() / "auto.log"

    def log(s: str) -> None:
        with logf.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {s}\n")

    uia = UIA()
    a = Auto(settle, grace, log)
    stop = stop_file()
    stop.unlink(missing_ok=True)
    log("auto started")
    try:
        while not stop.exists():
            try:
                title, snap = uia.meeting()
            except Exception as x:  # a window closing mid-read
                title, snap = None, f"error: {x}"
            a.step(title, snap, time.monotonic())
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        a.stop("auto stopped")
        log("auto stopped")
    return 0


# ============================================================ logon start


def startup_file() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "coord-captions.cmd"


def install(start_now: bool = True) -> Path:
    """A Startup-folder .cmd that runs `auto` windowless at logon, with this
    interpreter. Startup folder rather than Task Scheduler: per user, no
    admin, and deleting the file is the whole uninstall."""
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    exe = w if w.exists() else exe
    f = startup_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f'@start "" "{exe}" -m coord_mcp.captions auto\r\n', encoding="utf-8")
    if start_now:
        import subprocess

        subprocess.Popen([str(exe), "-m", "coord_mcp.captions", "auto"],
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0), close_fds=True)
    return f


def uninstall() -> bool:
    f = startup_file()
    if f.exists():
        f.unlink()
        return True
    return False


# ============================================================ watch


class Batcher:
    """When to hand pending transcript lines to the session: after `quiet`
    seconds with nothing new (a pause in the talk), or once the oldest has
    waited `max_wait`, so a monologue still arrives. Never per line: every
    batch is a model turn."""

    def __init__(self, quiet: float = 20.0, max_wait: float = 90.0):
        self.quiet, self.max_wait = quiet, max_wait
        self.lines: list[str] = []
        self._first = self._latest = 0.0

    def add(self, lines: list[str], t: float) -> None:
        if not lines:
            return
        if not self.lines:
            self._first = t
        self.lines.extend(lines)
        self._latest = t

    def due(self, t: float) -> bool:
        return bool(self.lines) and (t - self._latest >= self.quiet or t - self._first >= self.max_wait)

    def take(self) -> list[str]:
        out, self.lines = self.lines, []
        return out


def _state(path: Path) -> Path:
    return path.with_name(path.name + ".watch")


def _load(path: Path) -> dict:
    try:
        return json.loads(_state(path).read_text())
    except (OSError, ValueError):
        return {"offset": 0, "line": 0}


def read_new(path: Path, offset: int) -> tuple[list[str], int]:
    """Whole lines past offset; a line still being written waits."""
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read()
    cut = data.rfind(b"\n") + 1
    return data[:cut].decode("utf-8", "replace").splitlines(), offset + cut


def _speech(lines: list[str]) -> bool:
    return any(not l.startswith("#") for l in lines)


def watch(path: Path, quiet: float, max_wait: float, poll: float = 1.0, out=sys.stdout,
          clock=time.monotonic, sleep=time.sleep, superseded=lambda: False) -> None:
    """One block per batch on stdout, each a header naming the transcript
    and its line numbers (what an intent's context cites), then the lines.
    The block prints in one write so Monitor delivers it as one event.

    `#` lines (header, status) ride along with speech but never make a batch
    on their own, so a meeting where nobody spoke costs the session nothing.
    Returns at the end marker, or when `superseded()` says a newer meeting
    started without this one being ended (a capture that died)."""
    st = _load(path)
    b = Batcher(quiet, max_wait)
    first = st["line"] + 1
    while True:
        lines, off = read_new(path, st["offset"]) if path.exists() else ([], st["offset"])
        t = clock()
        b.add(lines, t)
        st["offset"] = off
        over = any(l.startswith(END) for l in lines) or superseded()
        if _speech(b.lines) and (b.due(t) or over):
            batch = b.take()
            last = first + len(batch) - 1
            out.write(f"== {path} lines {first}-{last} ==\n" + "".join(l + "\n" for l in batch))
            out.flush()
            st["line"], st["spoke"] = last, True
            first = last + 1
        if over:
            st["offset"] = path.stat().st_size if path.exists() else off
            st["done"] = True
            if st.get("spoke"):
                out.write(f"== meeting ended: {path} ==\n")
                out.flush()
        if b.lines == [] or over:
            _state(path).write_text(json.dumps(st))
        if over:
            return
        sleep(poll)


def finished(path: Path) -> bool:
    return bool(_load(path).get("done"))


def follow(quiet: float, max_wait: float, poll: float = 2.0, out=sys.stdout,
           clock=time.monotonic, sleep=time.sleep, until=lambda: False) -> None:
    """watch, forever, whichever transcript is newest: what the meeting
    session's Monitor runs when capture starts on its own. Between meetings
    it prints nothing."""
    while not until():
        p = latest()
        if p is None or finished(p):
            sleep(poll)
            continue
        watch(p, quiet, max_wait, poll=1.0, out=out, clock=clock, sleep=sleep,
              superseded=lambda: latest() != p)


# ============================================================ cli


def _resolve(p: str | None) -> Path:
    if p:
        return Path(p).expanduser()
    found = latest()
    if not found:
        sys.exit(f"no transcripts in {meetings_dir()}")
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m coord_mcp.captions")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--source", choices=("teams", "livecaptions"), default="teams")
    c.add_argument("--title", default="meeting")
    c.add_argument("--interval", type=float, default=1.0)
    c.add_argument("--settle", type=float, default=4.0)
    c.add_argument("--minutes", type=float, default=240)
    au = sub.add_parser("auto")
    au.add_argument("--interval", type=float, default=1.0)
    au.add_argument("--settle", type=float, default=4.0)
    au.add_argument("--grace", type=float, default=60.0)
    ins = sub.add_parser("install")
    ins.add_argument("--no-start", action="store_true")
    sub.add_parser("uninstall")
    sub.add_parser("stop")
    w = sub.add_parser("watch")
    w.add_argument("path", nargs="?")
    w.add_argument("--follow", action="store_true")
    w.add_argument("--quiet", type=float, default=20.0)
    w.add_argument("--max-wait", type=float, default=90.0)
    e = sub.add_parser("end")
    e.add_argument("path", nargs="?")
    sub.add_parser("latest")
    d = sub.add_parser("dump")
    d.add_argument("--source", choices=("teams", "livecaptions"), default="teams")
    d.add_argument("--depth", type=int, default=40)
    a = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if a.cmd == "capture":
        capture(a.source, a.title, a.interval, a.settle, a.minutes)
    elif a.cmd == "auto":
        return run_auto(a.interval, a.settle, a.grace)
    elif a.cmd == "install":
        print(f"logon start: {install(not a.no_start)}")
        skip_patterns()
        print(f"skip list: {skip_file()}")
    elif a.cmd == "uninstall":
        print("removed" if uninstall() else "was not installed")
    elif a.cmd == "stop":
        stop_file().parent.mkdir(parents=True, exist_ok=True)
        stop_file().touch()
        print("auto will stop within a second; it starts again at next logon unless you uninstall")
    elif a.cmd == "watch" and a.follow:
        follow(a.quiet, a.max_wait)
    elif a.cmd == "watch":
        watch(_resolve(a.path), a.quiet, a.max_wait)
    elif a.cmd == "end":
        end(_resolve(a.path))
    elif a.cmd == "latest":
        p = latest()
        if not p:
            return 1
        print(p)
    elif a.cmd == "dump":
        u = UIA()
        win = u.meeting_window() if a.source == "teams" else next(iter(u.windows(LIVE_PROC)), None)
        if not win:
            print(f"no {a.source} window", file=sys.stderr)
            return 2
        u.dump(win, max_depth=a.depth)
    return 0


if __name__ == "__main__":
    sys.exit(main())
