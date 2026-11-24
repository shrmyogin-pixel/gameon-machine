"""Windows toast notifications for strong clips, with a safe fallback.

A toast is sent through Windows PowerShell's built-in WinRT access (no extra packages). If that is not
possible (not Windows, no PowerShell, notifications blocked, any error) we fall back to a console beep
plus a line in the log, and the tool carries on. Nothing here can raise into the pipeline.

The clip title and streamer reach PowerShell through environment variables, never pasted into the script,
so odd characters in a title cannot change what the script does.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading

from .config import Config
from .proc import CREATE_NO_WINDOW

log = logging.getLogger("clipper")

# Windows PowerShell's own registered app id: toasts from an unregistered id are silently dropped.
APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$t = [Security.SecurityElement]::Escape($env:CLIPPER_TOAST_TITLE)
$m = [Security.SecurityElement]::Escape($env:CLIPPER_TOAST_BODY)
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastText02'><text id='1'>$t</text><text id='2'>$m</text></binding></visual></toast>")
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:CLIPPER_TOAST_APP).Show($toast)
"""


def _powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


def send_toast(title: str, body: str) -> bool:
    """True if Windows accepted the toast. Never raises."""
    exe = _powershell()
    if sys.platform != "win32" or not exe:
        return False
    env = {**os.environ, "CLIPPER_TOAST_TITLE": title, "CLIPPER_TOAST_BODY": body, "CLIPPER_TOAST_APP": APP_ID}
    try:
        p = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _SCRIPT],
                           env=env, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW)
        if p.returncode != 0:
            log.info("toast not shown (powershell exit %s): %s", p.returncode, (p.stderr or "").strip()[:150])
        return p.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("toast not shown: %s", exc)
        return False


def notify(title: str, body: str) -> str:
    """Toast if possible, else console beep + log line. Returns 'toast' or 'fallback'."""
    try:
        if send_toast(title, body):
            return "toast"
    except Exception:  # pragma: no cover - belt and braces
        log.exception("toast failed")
    try:
        sys.stderr.write("\a")
        sys.stderr.flush()
    except Exception:
        pass
    log.info("NOTIFY %s - %s", title, body)
    return "fallback"


def should_notify(cfg: Config, clip: dict) -> bool:
    n = cfg.notify
    if not n.get("enabled", True):
        return False
    score = clip.get("ai_score")
    if n.get("banger", True) and clip.get("banger"):
        return True
    return score is not None and score >= float(n.get("min_score", 8))


def describe(clip: dict) -> tuple[str, str]:
    """(toast title, toast body). Titles are already profanity-masked by the editor."""
    score = clip.get("ai_score")
    s = f"{score:g}" if score is not None else "?"
    head = f"Banger clip ready ({s}/10)" if clip.get("banger") else f"High-score clip ready ({s}/10)"
    body = f"{clip['handle']} ({clip['platform']}): {clip.get('title') or clip['folder']}"
    if clip.get("sensitive"):
        body += "  [SENSITIVE - review carefully]"
    return head, body


def notify_rendered(cfg: Config, folder_name: str) -> None:
    """Call after a clip finishes rendering. Fire-and-forget on a thread so rendering never waits on it."""
    try:
        with_row = _row_by_folder(cfg, folder_name)
        if not with_row or not should_notify(cfg, with_row):
            return
        title, body = describe(with_row)
        threading.Thread(target=notify, args=(title, body), name="toast", daemon=True).start()
    except Exception:
        log.exception("[%s] notification failed (ignored)", folder_name)


def _row_by_folder(cfg: Config, folder_name: str) -> dict | None:
    from .db import connect, row_to_dict
    with connect(cfg.database) as conn:
        row = conn.execute("SELECT * FROM clips WHERE folder = ?", (folder_name,)).fetchone()
    return row_to_dict(row) if row else None
