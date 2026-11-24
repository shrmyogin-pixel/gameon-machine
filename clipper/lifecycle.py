"""What happens to a clip when you Reject it or press Mark Uploaded in the review dashboard.

1. A news Short's story is remembered in the `seen_stories` table (a hash of the normalized article URL and one of its
   headline), so `python -m clipper news` never makes that story again, even from another outlet's feed with the same
   headline or the same article under a tracking-parameter URL.
2. The clip folder under data/clips is deleted at once. Exported packages in data/exports are your own copies and
   are never touched.

The story is recorded BEFORE anything is deleted; if recording fails, the files are kept (nothing is lost) and the
clip row itself still blocks the story. A folder that cannot be deleted (file locked) is reported and retried by
`python -m clipper clean`. Nothing here uploads anything (autopost = false).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import urllib.parse
from pathlib import Path

from . import db
from .config import Config

log = logging.getLogger("clipper")

TRACKING_PARAM = re.compile(r"^(utm_.*|fbclid|gclid|dclid|msclkid|ocid|cmp|cmpid|ref|ref_src|source|spm|igshid|mc_.*|_ga|"
                            r"cid|xtor|at_.*|taid)$", re.I)


def normalize_url(url: str) -> str:
    """Host without 'www.', no scheme, fragment or tracking parameters, no trailing slash, other parameters sorted."""
    parts = urllib.parse.urlsplit((url or "").strip())
    host = (parts.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    path = re.sub(r"/{2,}", "/", parts.path or "/").rstrip("/") or "/"
    query = sorted((k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                   if not TRACKING_PARAM.match(k))
    return host + path.lower() + (("?" + urllib.parse.urlencode(query)) if query else "")


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]


def url_key(url: str) -> str:
    return "u:" + _hash(normalize_url(url))


def title_key(title: str) -> str | None:
    """Hash of the headline's words (None for a headline too short to identify a story)."""
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    return "t:" + _hash(" ".join(words)) if len(words) >= 3 else None


def story_keys(url: str | None, title: str | None) -> list[str]:
    keys = [url_key(url)] if url else []
    tk = title_key(title or "")
    if tk:
        keys.append(tk)
    return keys


def clips_story(cfg: Config, clip: dict) -> tuple[str, str] | None:
    """(article URL, RSS headline) of a news clip, else None."""
    if clip.get("platform") != "news" or not clip.get("source_url"):
        return None
    headline = ""
    for path in (clip.get("analysis_path"), str(cfg.clips_dir / clip["folder"] / "analysis.json")):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            headline = (data.get("news") or {}).get("headline") or ""
            if headline:
                break
        except (OSError, ValueError, TypeError):
            continue
    return clip["source_url"], headline


def record_seen(cfg: Config, clip: dict, reason: str) -> bool:
    """Remember a news clip's story. True when there was nothing to record or it was recorded."""
    story = clips_story(cfg, clip)
    if not story:
        return True
    url, headline = story
    db.add_seen(cfg.database, story_keys(url, headline), url, headline or clip.get("title"), reason)
    return True


def delete_folder(cfg: Config, clip: dict) -> bool:
    """Delete data/clips/<folder>. True when it is gone (or never existed); False when it could not be removed."""
    root = cfg.clips_dir.resolve()
    folder = (cfg.clips_dir / clip["folder"]).resolve()
    if folder == root or root not in folder.parents:
        log.error("[%s] refusing to delete a folder outside the clips directory", clip["folder"])
        return False
    if not folder.exists():
        return True
    try:
        shutil.rmtree(folder)
        return True
    except OSError as exc:
        log.error("[%s] could not delete the clip folder yet (%s); `clean` will retry", clip["folder"], exc)
        return False


def discard(cfg: Config, clip: dict, reason: str) -> dict:
    """Record the story, then delete the files. `clip` is the row AFTER its status changed.

    -> {'seen': bool, 'deleted': bool}"""
    out = {"seen": True, "deleted": False}
    try:
        record_seen(cfg, clip, reason)
    except Exception:
        log.exception("[%s] could not record the story; keeping the files", clip["folder"])
        out["seen"] = False
        return out
    if delete_folder(cfg, clip):
        db.mark_files_deleted(cfg.database, clip["id"])
        out["deleted"] = True
        log.info("[%s] %s: story remembered, files deleted", clip["folder"], reason)
    return out
