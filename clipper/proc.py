"""Safe subprocess helper for external tools (ffmpeg, streamlink, whisper, claude)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
CREATE_NO_WINDOW = 0x08000000


def run(cmd: list[str | Path], timeout: int = 30, low_priority: bool = False):
    """Run a command; returns (returncode, combined_output). Never raises."""
    flags = 0
    if sys.platform == "win32":
        flags = CREATE_NO_WINDOW
        if low_priority:
            flags |= BELOW_NORMAL_PRIORITY_CLASS
    try:
        p = subprocess.run(
            [str(c) for c in cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=flags,
            stdin=subprocess.DEVNULL,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout}s"
    except (OSError, ValueError) as exc:
        return -2, str(exc)
