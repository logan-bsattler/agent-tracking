"""Desktop alerts. Fired from the statusLine hook when a limit crosses a band,
once per band per reset window, so a long session doesn't nag.

Windows: native toast via PowerShell, no modules needed.
macOS: osascript. Linux: notify-send. All fire-and-forget, never block.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from typing import Any

from .db import now

_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$n = $t.GetElementsByTagName('text')
$n.Item(0).AppendChild($t.CreateTextNode($env:COORD_TOAST_TITLE)) | Out-Null
$n.Item(1).AppendChild($t.CreateTextNode($env:COORD_TOAST_BODY)) | Out-Null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($t))
"""


def notify(title: str, body: str) -> bool:
    """Show a desktop notification. Returns False if no notifier is available."""
    try:
        if sys.platform == "win32":
            import os
            env = dict(os.environ, COORD_TOAST_TITLE=title, COORD_TOAST_BODY=body)
            subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", _PS],
                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        elif sys.platform == "darwin":
            script = f'display notification "{body}" with title "{title}"'
            subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["notify-send", title, body], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def band(pct: float | None, warn: float, hard: float) -> str:
    if pct is None:
        return "unknown"
    return "hard" if pct >= hard else "warn" if pct >= warn else "ok"


def _key(window: str, reset: int | None) -> str:
    return f"alert:{window}:{reset or 0}"


def check_and_alert(conn: sqlite3.Connection, snap: dict[str, Any], burn: dict[str, Any] | None = None) -> list[str]:
    """Compare this snapshot's bands with the last alerted band for the same
    reset window; notify on any upward crossing. Returns the alerts sent."""
    from .guard import HARD_5H, HARD_7D, WARN_5H, WARN_7D

    sent: list[str] = []
    order = {"ok": 0, "warn": 1, "hard": 2, "unknown": -1}
    for window, label, warn, hard in (("five_hour", "5-hour", WARN_5H, HARD_5H),
                                      ("seven_day", "Weekly", WARN_7D, HARD_7D)):
        pct = snap.get(f"{window}_pct")
        reset = snap.get(f"{window}_reset")
        b = band(pct, warn, hard)
        key = _key(window, reset)
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        prev = row["value"] if row else "ok"
        if order[b] > order.get(prev, 0):
            from .usage import when
            body = f"{label} limit at {pct:.0f}%"
            if reset:
                body += f", resets {when(reset)}"
            if b == "hard":
                body += ". Guard is blocking tool calls."
            else:
                body += ". Prefer cheap work; guard blocks at " + f"{hard:.0f}%."
            notify("Claude usage", body)
            sent.append(body)
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, b))
    if burn and burn.get("hits_limit_before_reset"):
        key = _key("projection", snap.get("five_hour_reset"))
        if conn.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone() is None:
            body = (f"At this pace the 5-hour window hits 100% around {burn['hit_at_text']}, "
                    f"before it resets at {burn['reset_text']}.")
            notify("Claude usage", body)
            sent.append(body)
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, str(now())))
    return sent
