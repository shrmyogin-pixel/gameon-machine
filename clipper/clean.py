"""`python -m clipper clean`: free disk space by deleting old clips that are no longer needed.

Deleted (clip folder under data/clips AND its database row):
    skipped / rejected clips older than `skipped_days` / `rejected_days`  (default 2 days)
    posted clips older than `posted_days`                                  (default 7 days)

Age counts from when the clip reached that status (skipped: updated_at, rejected: reviewed_at, posted: posted_at).
Never touched: rendered / approved / failed clips, recordings in data/sessions, and the upload packages in
data/exports (those are your own copies; delete old day folders there yourself when you are done with them).
One clip that cannot be deleted (a file is locked) is logged and kept for the next run; it never stops the rest.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .config import Config

log = logging.getLogger("clipper")

DEFAULTS = {"skipped_days": 2, "rejected_days": 2, "posted_days": 7}


@dataclass
class CleanResult:
    deleted: list[dict] = field(default_factory=list)
    failed: list[tuple[dict, str]] = field(default_factory=list)
    freed_bytes: int = 0


def settings(cfg: Config) -> dict:
    return {**DEFAULTS, **cfg.raw.get("clean", {})}


def folder_size(path: Path) -> int:
    try:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except OSError:
        return 0


def run_clean(cfg: Config, dry_run: bool = False, now: datetime | None = None) -> CleanResult:
    db.init_db(cfg.database)
    s = settings(cfg)
    now = now or datetime.now(timezone.utc)
    result = CleanResult()
    clips_root = cfg.clips_dir.resolve()
    for status, key in (("skipped", "skipped_days"), ("rejected", "rejected_days"), ("posted", "posted_days")):
        cutoff = (now - timedelta(days=float(s[key]))).strftime("%Y-%m-%d %H:%M:%S")
        for clip in db.clips_older_than(cfg.database, status, cutoff):
            try:
                folder = (cfg.clips_dir / clip["folder"]).resolve()
                if folder == clips_root or clips_root not in folder.parents:
                    raise OSError("clip folder is outside the clips directory")      # never delete outside data/clips
                size = folder_size(folder) if folder.is_dir() else 0
                if not dry_run:
                    if folder.is_dir():
                        shutil.rmtree(folder)
                    db.delete_clip_row(cfg.database, clip["id"])
                    log.info("[%s] CLEANED id=%s (%s)", clip["folder"], clip["id"], status)
                result.deleted.append(clip)
                result.freed_bytes += size
            except Exception as exc:
                result.failed.append((clip, str(exc)))
                log.error("[%s] clean failed id=%s: %s", clip["folder"], clip["id"], exc)
    return result
