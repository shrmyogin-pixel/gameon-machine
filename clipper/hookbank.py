"""data/hook_bank.json: hook patterns and how the Shorts that used them did.

times_used goes up when a Short is rendered with a pattern; approved_count / rejected_count follow the review dashboard
(Approve or Mark Uploaded = approved, Reject = rejected, Undo takes it back), counted once per clip. performance_notes
keeps whatever you typed and ends with one automatic line about the last action. Nothing here uploads anything, and a
problem with this file never blocks a render or a review action.
"""
from __future__ import annotations

import json
import logging
import os
from collections import Counter
from datetime import date
from pathlib import Path
import threading

from . import db
from .config import Config
from .render import _machine_slot

log = logging.getLogger("clipper")

FILE_NAME = "hook_bank.json"
AUTO = "[auto]"
OUTCOMES = {"approve": "approved", "uploaded": "approved", "posted": "approved", "reject": "rejected", "undo": None}
WORDS = {"approve": "approved", "uploaded": "uploaded", "posted": "uploaded", "reject": "rejected", "undo": "undone"}
_lock = threading.Lock()


def bank_path(cfg: Config) -> Path:
    return cfg.data_dir / FILE_NAME


def load(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data.get("hooks"), list):
            raise ValueError("no 'hooks' list")
        return data
    except FileNotFoundError:
        return None
    except (OSError, ValueError, AttributeError) as exc:
        log.warning("hook bank: cannot read %s (%s); leaving it as it is", path.name, str(exc)[:100])
        return None


def choose(bank: dict | None, bucket: str, taken: Counter | None = None) -> dict | None:
    """The pattern for a bucket: not yet handed out in this run, then the best approval record, then the least used."""
    options = [h for h in (bank or {}).get("hooks", []) if h.get("bucket") == bucket and h.get("id") and h.get("pattern")]
    if not options:
        return None
    taken = taken or Counter()

    def rank(h):
        ok, no = int(h.get("approved_count", 0)), int(h.get("rejected_count", 0))
        return (taken[h["id"]], -(ok + 1) / (ok + no + 2), int(h.get("times_used", 0)))
    return min(options, key=rank)


def _update(cfg: Config, hook_id: str, change) -> bool:
    """Apply `change(entry)` to one pattern and save atomically, one writer at a time across processes."""
    path = bank_path(cfg)
    with _lock, _machine_slot(cfg.data_dir / "hook_bank_lock", 1):
        bank = load(path)
        entry = next((h for h in (bank or {}).get("hooks", []) if h.get("id") == hook_id), None)
        if entry is None:
            return False
        change(entry)
        bank["updated_at"] = date.today().isoformat()
        tmp = path.with_name(path.name + ".part")
        tmp.write_text(json.dumps(bank, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True


def record_use(cfg: Config, hook_id: str) -> bool:
    def change(entry):
        entry["times_used"] = int(entry.get("times_used", 0)) + 1
    return _update(cfg, hook_id, change)


def feedback(cfg: Config, clip: dict, action: str) -> None:
    """Count a dashboard action for the clip's pattern (clips without one are ignored). `clip` is the row after it."""
    hook_id = clip.get("hook_id")
    if not hook_id or action not in OUTCOMES:
        return
    old, new = clip.get("hook_feedback"), OUTCOMES[action]

    def change(entry):
        for outcome, step in ((old, -1), (new, 1)):
            if outcome and old != new:
                key = f"{outcome}_count"
                entry[key] = max(int(entry.get(key, 0)) + step, 0)
        mine = [ln for ln in str(entry.get("performance_notes") or "").splitlines() if not ln.startswith(AUTO)]
        mine.append(f"{AUTO} {date.today().isoformat()} {WORDS[action]}: {str(clip.get('title') or '')[:60]}")
        entry["performance_notes"] = "\n".join(mine)

    if _update(cfg, hook_id, change):
        db.set_hook(cfg.database, clip["folder"], hook_id, new)
