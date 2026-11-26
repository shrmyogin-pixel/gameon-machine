"""SQLite database (data/clips.sqlite): one row per detected moment / clip.

Status flow:  new -> skipped | failed | edited -> rendered -> approved | rejected -> posted
('edited' is the short step between the AI editor and the renderer.) Nothing here uploads anything;
'posted' (the dashboard calls it "uploaded") only records that a human uploaded the clip by hand (autopost = false).
Rejecting or uploading a clip deletes its files at once (lifecycle.py) and, for news, remembers the story in
`seen_stories` so it is never made again.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from .profanity import mask

STATUSES = ("new", "skipped", "edited", "rendered", "approved", "rejected", "posted", "failed")
REVIEW_STATUSES = ("rendered", "approved", "rejected", "posted")      # clips a human can act on
CREDIT_PREFIX = "Sources & Credits:"

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    platform   TEXT NOT NULL,
    handle     TEXT NOT NULL,
    folder     TEXT NOT NULL UNIQUE,
    status     TEXT NOT NULL DEFAULT 'new',   -- new, skipped, edited, rendered, approved, rejected, posted, failed
    skip_reason TEXT,
    error      TEXT,
    slot       TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS seen_stories (
    key        TEXT PRIMARY KEY,              -- 'u:<hash of the normalized article URL>' or 't:<hash of the headline>'
    url        TEXT,
    title      TEXT,
    reason     TEXT,                          -- rejected | uploaded | cleaned
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS used_pictures (
    ahash      TEXT NOT NULL,                 -- the two picture fingerprints (newsimage.fingerprint), as hex
    dhash      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS news_topics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    topics     TEXT NOT NULL,                 -- comma-separated topic keys of one made Short ('gta', 'elden ring')
    title      TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Columns added after the first version. init_db() adds whichever are missing, so old databases keep working.
EXTRA_COLUMNS = {
    "stream": "TEXT",             # stream URL
    "lang": "TEXT",
    "moment_kind": "TEXT",        # chat | chat+audio | audio
    "moment_at": "TEXT",          # when the spike happened (local ISO time)
    "clip_start_s": "REAL",       # cut start/end inside source.mp4, from the AI editor
    "clip_end_s": "REAL",
    "pre_score": "REAL",
    "ai_score": "REAL",
    "banger": "INTEGER",
    "sensitive": "INTEGER",
    "sensitive_reason": "TEXT",
    "title": "TEXT",
    "hook": "TEXT",
    "meme_caption": "TEXT",       # JSON list
    "description": "TEXT",        # full text, always ends with the 'Sources & Credits:' line
    "credit_line": "TEXT",
    "tags": "TEXT",               # JSON list
    "clip_path": "TEXT",
    "thumb_path": "TEXT",
    "frames_path": "TEXT",
    "analysis_path": "TEXT",
    "reviewed_at": "TEXT",
    "posted_at": "TEXT",
    "updated_at": "TEXT",
    "exported_at": "TEXT",        # when `export` last packaged it (status stays 'approved' until you post it)
    "export_path": "TEXT",        # the exported .mp4
    "source_url": "TEXT",         # news Shorts: the exact article URL
    "route_platform": "TEXT",     # where this clip should be posted: youtube | tiktok | instagram | ...
    "route_slot": "TEXT",         # YouTube best-of-best clips: the AEDT peak slot name
    "route_at": "TEXT",           # next occurrence of that slot, Melbourne time (ISO)
    "route_rank": "INTEGER",      # 1 = best of the pending queue
    "assign_slot": "TEXT",        # HUMAN choice: posting slot name from config [[slots]] (beats the automatic routing)
    "files_deleted_at": "TEXT",   # reject / mark-uploaded delete the clip folder; set when that happened
    "assign_target": "TEXT",      # HUMAN choice: 'youtube' (top 2-3) or 'other' platforms (beats the automatic routing)
}
ASSIGN_TARGETS = ("youtube", "other")


@contextmanager
def connect(db_path: Path):
    """sqlite3 connection that commits on success and really closes (Windows keeps the file locked otherwise)."""
    with closing(sqlite3.connect(db_path, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        with conn:
            yield conn


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        have = {r["name"] for r in conn.execute("PRAGMA table_info(clips)")}
        for name, kind in EXTRA_COLUMNS.items():
            if name not in have:
                conn.execute(f"ALTER TABLE clips ADD COLUMN {name} {kind}")
        conn.execute("CREATE INDEX IF NOT EXISTS clips_status ON clips (status)")


def add_clip(db_path: Path, platform: str, handle: str, folder: str, *, stream: str | None = None,
             lang: str | None = None, moment_kind: str | None = None, moment_at: str | None = None,
             pre_score: float | None = None, source_url: str | None = None) -> None:
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute("INSERT OR IGNORE INTO clips (platform, handle, folder, stream, lang, moment_kind, moment_at,"
                     " pre_score, source_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (platform, handle, folder, stream, lang, moment_kind, moment_at, pre_score, source_url))


def set_clip_status(db_path: Path, folder: str, status: str, error: str | None = None,
                    skip_reason: str | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute("UPDATE clips SET status = ?, error = ?, skip_reason = ?, updated_at = datetime('now')"
                     " WHERE folder = ?", (status, error, skip_reason, folder))


def record_analysis(db_path: Path, folder: str, analysis: dict, analysis_path: Path | None = None) -> None:
    """Copy the AI editor's result (analysis.json) into the row."""
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE clips SET ai_score = ?, banger = ?, sensitive = ?, sensitive_reason = ?, title = ?, hook = ?,"
            " meme_caption = ?, description = ?, credit_line = ?, tags = ?, clip_start_s = ?, clip_end_s = ?,"
            " analysis_path = COALESCE(?, analysis_path), updated_at = datetime('now') WHERE folder = ?",
            (analysis.get("score"), int(bool(analysis.get("banger"))), int(bool(analysis.get("sensitive"))),
             analysis.get("sensitive_reason") or "", analysis.get("title"), analysis.get("hook"),
             json.dumps(analysis.get("meme_caption") or [], ensure_ascii=False),
             analysis.get("description_full"), analysis.get("credit_line"),
             json.dumps(analysis.get("tags") or [], ensure_ascii=False),
             analysis.get("start"), analysis.get("end"),
             str(analysis_path) if analysis_path else None, folder))


def record_files(db_path: Path, folder: str, clip_dir: Path) -> None:
    """Remember where the rendered files are (only the ones that exist)."""
    def found(name: str) -> str | None:
        p = clip_dir / name
        return str(p) if p.is_file() else None
    with connect(db_path) as conn:
        conn.execute("UPDATE clips SET clip_path = ?, thumb_path = ?, frames_path = ?, analysis_path = ?,"
                     " updated_at = datetime('now') WHERE folder = ?",
                     (found("clip.mp4"), found("thumb.jpg"), found("frames.jpg"), found("analysis.json"), folder))


def backfill_from_folders(db_path: Path, clips_dir: Path) -> int:
    """Fill the new columns for clips made before they existed (reads analysis.json / payload.json)."""
    init_db(db_path)
    done = 0
    with connect(db_path) as conn:
        rows = conn.execute("SELECT folder FROM clips WHERE ai_score IS NULL AND title IS NULL").fetchall()
    for r in rows:
        folder = clips_dir / r["folder"]
        try:
            analysis = json.loads((folder / "analysis.json").read_text(encoding="utf-8"))
            if "score" not in analysis:
                continue
            record_analysis(db_path, r["folder"], analysis, folder / "analysis.json")
            record_files(db_path, r["folder"], folder)
            payload_file = folder / "payload.json"
            if payload_file.is_file():
                p = json.loads(payload_file.read_text(encoding="utf-8"))
                with connect(db_path) as conn:
                    conn.execute("UPDATE clips SET stream = COALESCE(stream, ?), lang = COALESCE(lang, ?),"
                                 " moment_kind = COALESCE(moment_kind, ?), pre_score = COALESCE(pre_score, ?),"
                                 " moment_at = COALESCE(moment_at, ?) WHERE folder = ?",
                                 (p.get("stream", {}).get("url"), p.get("stream", {}).get("lang"),
                                  p.get("moment", {}).get("kind"), p.get("moment", {}).get("prescore"),
                                  p.get("clip", {}).get("starts_at"), r["folder"]))
            done += 1
        except (OSError, ValueError):
            continue
    return done


# ------------------------------------------------------------------ review queries
def _loads(text: str | None) -> list:
    try:
        v = json.loads(text) if text else []
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["tags"] = _loads(d.get("tags"))
    d["meme_caption"] = _loads(d.get("meme_caption"))
    d["banger"] = bool(d.get("banger"))
    d["sensitive"] = bool(d.get("sensitive"))
    return d


def list_clips(db_path: Path, status: str | None = None, handle: str | None = None, banger: bool | None = None,
               sensitive: bool | None = None, sort: str = "newest", limit: int = 500) -> list[dict]:
    where, args = [], []
    if status == "active":                       # the dashboard inbox: to review + approved
        where.append("status IN ('rendered', 'approved')")
    elif status:
        where.append("status = ?")
        args.append(status)
    if handle:
        where.append("handle = ? COLLATE NOCASE")
        args.append(handle)
    if banger is not None:
        where.append("COALESCE(banger, 0) = ?")
        args.append(int(banger))
    if sensitive is not None:
        where.append("COALESCE(sensitive, 0) = ?")
        args.append(int(sensitive))
    order = {"score": "ai_score IS NULL, ai_score DESC, id DESC",
             "prescore": "pre_score IS NULL, pre_score DESC, id DESC"}.get(sort, "created_at DESC, id DESC")
    sql = "SELECT * FROM clips" + (" WHERE " + " AND ".join(where) if where else "") + f" ORDER BY {order} LIMIT ?"
    with connect(db_path) as conn:
        return [row_to_dict(r) for r in conn.execute(sql, (*args, limit))]


def get_clip(db_path: Path, clip_id: int) -> dict | None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
    return row_to_dict(row) if row else None


def status_counts(db_path: Path) -> dict:
    with connect(db_path) as conn:
        return {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM clips GROUP BY status")}


def streamers(db_path: Path) -> list[str]:
    with connect(db_path) as conn:
        return [r["handle"] for r in conn.execute("SELECT DISTINCT handle FROM clips ORDER BY handle COLLATE NOCASE")]


class ReviewError(Exception):
    """A review action that is not allowed (shown to the user as-is). http: 400 bad input, 409 wrong state."""

    def __init__(self, message: str, http: int = 400):
        super().__init__(message)
        self.http = http


# action -> (new status, statuses it may start from)
ACTIONS = {
    "approve": ("approved", ("rendered", "rejected")),
    "reject": ("rejected", ("rendered", "approved")),
    "posted": ("posted", ("rendered", "approved")),
    "uploaded": ("posted", ("rendered", "approved")),     # the dashboard's "Mark Uploaded" (same as "posted")
    "undo": ("rendered", ("approved", "rejected", "posted")),
}


def review_action(db_path: Path, clip_id: int, action: str) -> dict:
    if action not in ACTIONS:
        raise ReviewError(f"unknown action '{action}'")
    new, allowed = ACTIONS[action]
    clip = get_clip(db_path, clip_id)
    if not clip:
        raise ReviewError("clip not found", 404)
    if clip["status"] not in allowed:
        raise ReviewError(f"cannot {action} a clip that is '{clip['status']}'", 409)
    if clip.get("files_deleted_at") and new in ("approved", "rendered"):
        raise ReviewError("this clip's files were deleted when it was rejected / uploaded", 409)
    with connect(db_path) as conn:
        conn.execute("UPDATE clips SET status = ?, reviewed_at = datetime('now'), updated_at = datetime('now'),"
                     " posted_at = CASE WHEN ? = 'posted' THEN datetime('now') WHEN ? = 'rendered' THEN NULL"
                     " ELSE posted_at END WHERE id = ?", (new, new, new, clip_id))
    return get_clip(db_path, clip_id)


def credit_for(clip: dict) -> str:
    """The locked 'Sources & Credits:' line for a clip (policy rule 2)."""
    line = (clip.get("credit_line") or "").strip()
    if line.startswith(CREDIT_PREFIX):
        return line
    return f"{CREDIT_PREFIX} {clip['platform']} - {clip['handle']} ({clip.get('stream') or 'n/a'})"


def split_description(clip: dict) -> tuple[str, str]:
    """-> (editable body, locked credit line)."""
    credit = credit_for(clip)
    lines = (clip.get("description") or "").rstrip().splitlines()
    while lines and lines[-1].startswith(CREDIT_PREFIX):
        lines.pop()
    return "\n".join(lines).rstrip(), credit


def edit_clip_text(db_path: Path, clip_id: int, title: str, description: str, tags: list) -> dict:
    """Human edit of title / description / tags. Profanity is masked and the credit line is re-attached."""
    clip = get_clip(db_path, clip_id)
    if not clip:
        raise ReviewError("clip not found", 404)
    title = mask(str(title)).strip()
    if not title:
        raise ReviewError("title cannot be empty")
    if len(title) > 100:
        raise ReviewError("title is longer than 100 characters")
    credit = credit_for(clip)
    body = [ln for ln in mask(str(description)).splitlines() if not ln.startswith(CREDIT_PREFIX)]
    full = ("\n".join(body).strip() + "\n" + credit).lstrip("\n")
    if len(full) > 5000:
        raise ReviewError("description is longer than 5000 characters")
    if not isinstance(tags, list):
        raise ReviewError("tags must be a list")
    clean: list[str] = []
    for t in tags:
        t = mask(str(t)).strip().lstrip("#").strip()
        if t and t.lower() not in {c.lower() for c in clean}:
            clean.append(t[:40])
    if len(clean) > 30:
        raise ReviewError("at most 30 tags")
    with connect(db_path) as conn:
        conn.execute("UPDATE clips SET title = ?, description = ?, credit_line = ?, tags = ?,"
                     " updated_at = datetime('now') WHERE id = ?",
                     (title, full, credit, json.dumps(clean, ensure_ascii=False), clip_id))
    _sync_analysis_json(clip, title, full, clean)
    return get_clip(db_path, clip_id)


def _sync_analysis_json(clip: dict, title: str, description: str, tags: list[str]) -> None:
    """Keep analysis.json in step with the edit (best effort; the database is the source of truth)."""
    path = clip.get("analysis_path")
    if not path:
        return
    try:
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        data.update(title=title, description_full=description, tags=tags, human_edited=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError):
        pass


# ------------------------------------------------------------------ export
def clips_to_export(db_path: Path, include_exported: bool = False, ids: list[int] | None = None) -> list[dict]:
    """Approved clips not exported yet (oldest first); all approved with include_exported; or exactly `ids`."""
    with connect(db_path) as conn:
        if ids:
            marks = ",".join("?" * len(ids))
            rows = conn.execute(f"SELECT * FROM clips WHERE id IN ({marks}) ORDER BY id", ids).fetchall()
        else:
            rows = conn.execute("SELECT * FROM clips WHERE status = 'approved'"
                                + ("" if include_exported else " AND exported_at IS NULL") + " ORDER BY id").fetchall()
    return [row_to_dict(r) for r in rows]


def mark_exported(db_path: Path, clip_id: int, export_path: Path) -> None:
    with connect(db_path) as conn:
        conn.execute("UPDATE clips SET exported_at = datetime('now'), export_path = ?, updated_at = datetime('now')"
                     " WHERE id = ?", (str(export_path), clip_id))


# ------------------------------------------------------------------ volume + routing
def count_accepted_since(db_path: Path, since_utc: str, news: bool | None = None) -> int:
    """Clips made since `since_utc` ('YYYY-MM-DD HH:MM:SS', UTC) that were accepted (not skipped / failed / new).

    news=True counts only platform 'news', False only live-stream clips, None both."""
    sql = ("SELECT COUNT(*) AS n FROM clips WHERE created_at >= ? AND status IN "
           "('edited', 'rendered', 'approved', 'rejected', 'posted')")
    if news is True:
        sql += " AND platform = 'news'"
    elif news is False:
        sql += " AND platform != 'news'"
    with connect(db_path) as conn:
        return conn.execute(sql, (since_utc,)).fetchone()["n"]


def pending_for_routing(db_path: Path, since_utc: str) -> list[dict]:
    """Clips that can still be routed: rendered or approved, made since `since_utc`."""
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM clips WHERE status IN ('rendered', 'approved') AND created_at >= ?"
                            " ORDER BY id", (since_utc,)).fetchall()
    return [row_to_dict(r) for r in rows]


def set_route(db_path: Path, clip_id: int, platform: str | None, slot: str | None, at: str | None,
              rank: int | None) -> None:
    with connect(db_path) as conn:
        conn.execute("UPDATE clips SET route_platform = ?, route_slot = ?, route_at = ?, route_rank = ? WHERE id = ?",
                     (platform, slot, at, rank, clip_id))


def set_assignment(db_path: Path, clip_id: int, slot: str | None, target: str | None) -> dict:
    """Save the human's slot / target choice for a rendered or approved clip (None = leave it to the automatic routing)."""
    if target not in (None, *ASSIGN_TARGETS):
        raise ReviewError("target must be 'youtube' or 'other'")
    clip = get_clip(db_path, clip_id)
    if not clip:
        raise ReviewError("clip not found", 404)
    if clip["status"] not in ("rendered", "approved"):
        raise ReviewError(f"cannot route a clip that is '{clip['status']}'", 409)
    with connect(db_path) as conn:
        conn.execute("UPDATE clips SET assign_slot = ?, assign_target = ?, updated_at = datetime('now') WHERE id = ?",
                     (slot, target, clip_id))
    return get_clip(db_path, clip_id)


def clip_by_folder(db_path: Path, folder: str) -> dict | None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM clips WHERE folder = ?", (folder,)).fetchone()
    return row_to_dict(row) if row else None


def recent_news_titles(db_path: Path, since_utc: str) -> list[str]:
    """Titles of news Shorts made since `since_utc` (failed ones excluded): what the channel just covered."""
    with connect(db_path) as conn:
        return [r["title"] for r in conn.execute(
            "SELECT title FROM clips WHERE platform = 'news' AND title IS NOT NULL AND status != 'failed'"
            " AND created_at >= ? ORDER BY id DESC", (since_utc,))]


def add_topics(db_path: Path, topics: list[str], title: str | None) -> None:
    """Remember the game / franchise topics of a news Short that was just made (kept even after Reject / Uploaded)."""
    if not topics:
        return
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute("INSERT INTO news_topics (topics, title) VALUES (?, ?)", (",".join(topics), title))


def recent_topics(db_path: Path, since_utc: str) -> list[str]:
    """Comma-joined topic keys of the news Shorts made since `since_utc`."""
    init_db(db_path)
    with connect(db_path) as conn:
        return [r["topics"] for r in conn.execute("SELECT topics FROM news_topics WHERE created_at >= ?", (since_utc,))]


def recent_topic_headlines(db_path: Path, since_utc: str) -> list[str]:
    """Source headlines of the news Shorts made since `since_utc` (kept even after the clip is deleted)."""
    init_db(db_path)
    with connect(db_path) as conn:
        return [r["title"] for r in conn.execute(
            "SELECT title FROM news_topics WHERE title IS NOT NULL AND created_at >= ?", (since_utc,))]


def add_used_pictures(db_path: Path, prints: list[tuple[int, int]]) -> None:
    """Remember the pictures a finished news Short showed (kept even after the clip is deleted)."""
    init_db(db_path)
    with connect(db_path) as conn:
        conn.executemany("INSERT INTO used_pictures (ahash, dhash) VALUES (?, ?)", [(f"{a:x}", f"{d:x}") for a, d in prints])


def recent_used_pictures(db_path: Path, since_utc: str) -> list[tuple[int, int]]:
    init_db(db_path)
    with connect(db_path) as conn:
        return [(int(r["ahash"], 16), int(r["dhash"], 16)) for r in conn.execute(
            "SELECT ahash, dhash FROM used_pictures WHERE created_at >= ?", (since_utc,))]


def news_urls_seen(db_path: Path) -> set[str]:
    """Article URLs that already became a news Short (so the same story is never made twice).
    A story whose Short failed is not counted, so the next run can try it again."""
    with connect(db_path) as conn:
        return {r["source_url"] for r in conn.execute(
            "SELECT source_url FROM clips WHERE platform = 'news' AND source_url IS NOT NULL AND status != 'failed'")}


# ------------------------------------------------------------------ clean
# status -> the column that says when it reached that status (all UTC 'YYYY-MM-DD HH:MM:SS')
CLEAN_AGE_COLUMN = {"skipped": "updated_at", "rejected": "reviewed_at", "posted": "posted_at"}


def clips_older_than(db_path: Path, status: str, cutoff_utc: str) -> list[dict]:
    """Clips with this status that reached it before `cutoff_utc` (falls back to updated_at / created_at)."""
    col = CLEAN_AGE_COLUMN[status]
    with connect(db_path) as conn:
        rows = conn.execute(f"SELECT * FROM clips WHERE status = ? AND COALESCE({col}, updated_at, created_at) < ?"
                            " ORDER BY id", (status, cutoff_utc)).fetchall()
    return [row_to_dict(r) for r in rows]


def delete_clip_row(db_path: Path, clip_id: int) -> None:
    with connect(db_path) as conn:
        conn.execute("DELETE FROM clips WHERE id = ?", (clip_id,))


# ------------------------------------------------------------------ seen stories + deleted files
def add_seen(db_path: Path, keys: list[str], url: str | None, title: str | None, reason: str) -> None:
    """Remember a story so `news` never makes it again (even after its clip row and folder are gone)."""
    init_db(db_path)
    with connect(db_path) as conn:
        for key in keys:
            conn.execute("INSERT OR IGNORE INTO seen_stories (key, url, title, reason) VALUES (?, ?, ?, ?)",
                         (key, url, title, reason))


def seen_story_keys(db_path: Path) -> set[str]:
    init_db(db_path)
    with connect(db_path) as conn:
        return {r["key"] for r in conn.execute("SELECT key FROM seen_stories")}


def mark_files_deleted(db_path: Path, clip_id: int) -> None:
    with connect(db_path) as conn:
        conn.execute("UPDATE clips SET files_deleted_at = datetime('now'), clip_path = NULL, thumb_path = NULL,"
                     " frames_path = NULL, updated_at = datetime('now') WHERE id = ?", (clip_id,))
