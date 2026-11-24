"""SQLite database (data/clips.sqlite)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    platform   TEXT NOT NULL,
    handle     TEXT NOT NULL,
    folder     TEXT NOT NULL UNIQUE,
    status     TEXT NOT NULL DEFAULT 'new',   -- new, edited, skipped, failed, exported
    skip_reason TEXT,
    error      TEXT,
    slot       TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)


def add_clip(db_path: Path, platform: str, handle: str, folder: str) -> None:
    init_db(db_path)
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("INSERT OR IGNORE INTO clips (platform, handle, folder) VALUES (?, ?, ?)",
                     (platform, handle, folder))


def set_clip_status(db_path: Path, folder: str, status: str, error: str | None = None,
                    skip_reason: str | None = None) -> None:
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("UPDATE clips SET status = ?, error = ?, skip_reason = ? WHERE folder = ?",
                     (status, error, skip_reason, folder))
