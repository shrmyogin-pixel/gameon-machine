"""Post-render QA: 1080x1920, a video stream that decodes cleanly, and a 'Sources & Credits:' line.

Only warns (shown on the review dashboard): it never deletes, re-renders or blocks anything. Exporting still refuses a
clip without its credit line, as before.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from . import db
from .config import Config
from .export import ExportRefused, check_credit
from .render import H, W, _ffmpeg_out, _slot

log = logging.getLogger("clipper")


def check(cfg: Config, clip: Path, description: str) -> list[str]:
    """The warnings for one rendered clip ([] = passed)."""
    warnings = []
    if not clip.is_file() or clip.stat().st_size == 0:
        warnings.append("clip.mp4 is missing or empty")
    else:
        _, info = _ffmpeg_out(cfg, ["-i", clip], timeout=60)             # exits non-zero (no output file): only parsed
        size = re.search(r"Stream #\d+:\d+.*?Video:.*?\b(\d{3,5})x(\d{3,5})\b", info)
        if not size:
            warnings.append("no readable video stream")
        else:
            if (int(size[1]), int(size[2])) != (W, H):
                warnings.append(f"resolution is {size[1]}x{size[2]}, not {W}x{H}")
            with _slot(cfg):                                             # a full decode is as heavy as work gets here
                code, out = _ffmpeg_out(cfg, ["-v", "error", "-i", clip, "-map", "0:v:0", "-f", "null", "-"])
            errors = [ln for ln in out.splitlines() if ln.strip()]
            if code != 0 or errors:
                warnings.append("video stream has decode errors: " + (errors[0][:160] if errors else f"exit {code}"))
    try:
        check_credit(description)
    except ExportRefused as exc:
        warnings.append(str(exc))
    return warnings


def record(cfg: Config, folder: Path, description: str, notes: list[str] | None = None) -> None:
    """Run the checks and save the result on the clip's row. Never raises."""
    try:
        warnings = [*(notes or []), *check(cfg, folder / "clip.mp4", description)]
        db.set_qa(cfg.database, folder.name, warnings)
        for w in warnings:
            log.warning("[%s] QA: %s", folder.name, w)
    except Exception:
        log.exception("[%s] post-render QA could not run (ignored)", folder.name)
