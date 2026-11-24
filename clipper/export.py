"""`python -m clipper export`: package approved clips for manual upload.

For every approved clip that has not been exported yet this writes, into data/exports/<YYYY-MM-DD>/:

    <platform>-<handle>-<title>.mp4   copy of clip.mp4
    <platform>-<handle>-<title>.jpg   copy of thumb.jpg
    <platform>-<handle>-<title>.txt   upload sheet: Title, Description, Hashtags, Tags (copy-paste ready)

Rules enforced here:
* A clip whose description has no final 'Sources & Credits:' line is REFUSED (policy rule 2).
* Profanity is masked again in everything written (policy rule 4).
* Nothing is uploaded; this only copies files (autopost = false, policy rule 5).
* One bad clip never stops the others: it is logged, counted, and skipped.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import db
from .config import Config
from .profanity import mask

log = logging.getLogger("clipper")

CREDIT_PREFIX = db.CREDIT_PREFIX
MAX_NAME = 90                  # characters of file name (without extension); keeps full paths well under 260
MAX_HASHTAGS = 15              # YouTube ignores every hashtag on a video that has more than 15
MAX_TAGS_CHARS = 500           # YouTube's limit for the whole tags field
SHEET_RULE = "=" * 60


class ExportRefused(Exception):
    """This clip must not be exported (reason is shown to the user)."""


@dataclass
class Result:
    exported: list[tuple[dict, Path]] = field(default_factory=list)
    refused: list[tuple[dict, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.refused


# ------------------------------------------------------------------ names
def slugify(text: str, limit: int = 60) -> str:
    """Lower-case ASCII letters/digits/hyphens only: safe on Windows and in any upload form."""
    ascii_text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:limit].strip("-")


def base_name(clip: dict) -> str:
    """<platform>-<handle>-<title>, e.g. twitch-lacy-insane-clutch-on-the-last-circle (never empty)."""
    parts = [slugify(clip["platform"], 15), slugify(clip["handle"], 25)]
    title = slugify(mask(clip.get("title") or ""))   # masked first: file names are text that leaves the tool
    parts.append(title or f"clip-{clip['id']}")      # e.g. a Hindi-only title has no ASCII letters
    name = "-".join(p for p in parts if p)[:MAX_NAME].strip("-")
    return name or f"clip-{clip['id']}"


def free_name(folder: Path, base: str) -> str:
    """base, or base-2, base-3 ... so two clips never share a file name."""
    name, n = base, 1
    while any((folder / (name + ext)).exists() for ext in (".mp4", ".jpg", ".txt")):
        n += 1
        name = f"{base}-{n}"
    return name


# ------------------------------------------------------------------ upload sheet
def hashtags_for(clip: dict, folder: Path) -> list[str]:
    """From the AI editor's analysis.json; falls back to the clip's tags. Masked, '#'-prefixed, no spaces."""
    raw: list = []
    try:
        raw = json.loads((folder / "analysis.json").read_text(encoding="utf-8")).get("hashtags") or []
    except (OSError, ValueError):
        pass
    if not raw:
        raw = clip.get("tags", [])[:5]
    out: list[str] = []
    for h in raw:
        h = "#" + mask(re.sub(r"[\s#,]+", "", str(h)))      # close the spaces first, or 'f u c k' slips past mask
        if len(h) > 1 and h.lower() not in {o.lower() for o in out}:
            out.append(h)
    return out[:MAX_HASHTAGS]


def fit_tags(tags: list[str], limit: int = MAX_TAGS_CHARS) -> list[str]:
    """Whole tags only, so that the comma-separated field stays under YouTube's character limit."""
    out, used = [], 0
    for t in tags:
        t = mask(str(t)).strip()
        if not t:
            continue
        cost = len(t) + (2 if out else 0) + (2 if " " in t else 0)   # YouTube counts quotes around multi-word tags
        if used + cost > limit:
            break
        out.append(t)
        used += cost
    return out


def check_credit(description: str) -> str:
    """The description's last non-empty line must be the credit line; returns that line or raises."""
    lines = [ln.strip() for ln in (description or "").splitlines() if ln.strip()]
    if not lines or not lines[-1].startswith(CREDIT_PREFIX) or len(lines[-1]) <= len(CREDIT_PREFIX) + 3:
        raise ExportRefused(f"description has no final '{CREDIT_PREFIX}' line")
    return lines[-1]


def build_sheet(clip: dict, folder: Path, mp4_name: str, thumb_name: str | None) -> str:
    """The .txt upload sheet. Raises ExportRefused when the credit line is missing."""
    description = mask(clip.get("description") or "")
    credit = check_credit(description)
    title = mask(clip.get("title") or "").strip()
    if not title:
        raise ExportRefused("clip has no title")
    body = "\n".join(description.rstrip().splitlines()[:-1]).rstrip()   # everything above the credit line
    tags = fit_tags(clip.get("tags", []))
    hashtags = hashtags_for(clip, folder)
    # hashtags go above the credit line so the description still ENDS with 'Sources & Credits:'
    full = "\n\n".join(x for x in (body, " ".join(hashtags), credit) if x)
    notes = [f"Video file:  {mp4_name}", f"Thumbnail:   {thumb_name or '(none)'}",
             f"Streamer:    {clip['handle']} ({clip['platform']})", f"Clip id:     {clip['id']}"]
    if clip.get("ai_score") is not None:
        notes.append(f"AI score:    {clip['ai_score']:g}/10" + ("  (banger)" if clip.get("banger") else ""))
    if clip.get("sensitive"):
        notes.append(f"REVIEW NOTE: flagged sensitive by the AI editor: {mask(clip.get('sensitive_reason') or '')}")
    return "\n".join([
        "UPLOAD SHEET - copy each section into YouTube Studio / Reels. Nothing was uploaded for you.", "",
        SHEET_RULE, "TITLE", SHEET_RULE, title, "",
        SHEET_RULE, "DESCRIPTION (paste as is; the last line is the required credit)", SHEET_RULE, full, "",
        SHEET_RULE, "HASHTAGS", SHEET_RULE, " ".join(hashtags) or "(none)", "",
        SHEET_RULE, f"TAGS (comma separated, {sum(len(t) for t in tags)} characters)", SHEET_RULE,
        ", ".join(tags) or "(none)", "",
        SHEET_RULE, "NOT FOR PASTING", SHEET_RULE, *notes, ""])


# ------------------------------------------------------------------ export
def _copy(src: Path, dst: Path) -> None:
    tmp = dst.with_name(dst.name + ".part")
    try:
        shutil.copyfile(src, tmp)
        if tmp.stat().st_size != src.stat().st_size:
            raise OSError(f"copy of {src.name} is incomplete")
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)


def _remove_previous(cfg: Config, clip: dict) -> None:
    """Re-exporting replaces this clip's earlier package instead of leaving '-2' duplicates."""
    prev = clip.get("export_path")
    if not prev:
        return
    prev = Path(prev)
    try:
        inside = cfg.exports_dir.resolve() in prev.resolve().parents
    except OSError:
        return
    if inside:
        for ext in (".mp4", ".jpg", ".txt"):
            prev.with_suffix(ext).unlink(missing_ok=True)


def export_one(cfg: Config, clip: dict, out_dir: Path) -> Path:
    """Package one clip into out_dir. Returns the .mp4 path; raises ExportRefused if it must not be exported."""
    if clip["status"] != "approved":
        raise ExportRefused(f"status is '{clip['status']}', only approved clips are exported")
    folder = cfg.clips_dir / clip["folder"]
    try:
        if cfg.clips_dir.resolve() not in folder.resolve().parents:
            raise ExportRefused("clip folder is outside the clips directory")
    except OSError:
        raise ExportRefused("clip folder cannot be resolved")
    src_mp4, src_jpg = folder / "clip.mp4", folder / "thumb.jpg"
    if not src_mp4.is_file():
        raise ExportRefused("clip.mp4 is missing")
    check_credit(mask(clip.get("description") or ""))       # refuse before touching the disk
    _remove_previous(cfg, clip)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = free_name(out_dir, base_name(clip))
    mp4, jpg, txt = (out_dir / (name + e) for e in (".mp4", ".jpg", ".txt"))
    sheet = build_sheet(clip, folder, mp4.name, jpg.name if src_jpg.is_file() else None)
    written: list[Path] = []
    try:
        _copy(src_mp4, mp4)
        written.append(mp4)
        if src_jpg.is_file():
            _copy(src_jpg, jpg)
            written.append(jpg)
        else:
            log.warning("[%s] no thumb.jpg; exporting without a thumbnail", clip["folder"])
        txt.write_text(sheet, encoding="utf-8-sig")        # BOM so Windows Notepad shows Hindi correctly
        written.append(txt)
    except Exception:
        for f in written:                                   # no half-finished packages
            f.unlink(missing_ok=True)
        raise
    return mp4


def run_export(cfg: Config, include_all: bool = False, ids: list[int] | None = None, dry_run: bool = False,
               today: date | None = None) -> Result:
    """Export approved clips. Never raises for a single clip."""
    if cfg.raw.get("general", {}).get("autopost", False):
        log.warning("config has autopost=true; ignored - this tool never uploads")
    db.init_db(cfg.database)
    clips = db.clips_to_export(cfg.database, include_all, ids)
    found = {c["id"] for c in clips}
    result = Result()
    for missing in (ids or []):
        if missing not in found:
            result.refused.append(({"id": missing, "folder": f"id {missing}", "title": ""}, "no clip with that id"))
    out_dir = cfg.exports_dir / (today or date.today()).isoformat()
    for clip in clips:
        label = clip["folder"]
        try:
            if dry_run:
                check_credit(mask(clip.get("description") or ""))
                if clip["status"] != "approved":
                    raise ExportRefused(f"status is '{clip['status']}', only approved clips are exported")
                result.exported.append((clip, out_dir / (base_name(clip) + ".mp4")))
                continue
            mp4 = export_one(cfg, clip, out_dir)
            db.mark_exported(cfg.database, clip["id"], mp4)
            result.exported.append((clip, mp4))
            log.info("[%s] EXPORTED id=%s -> %s  (title: %s)", label, clip["id"], mp4, mask(clip.get("title") or ""))
        except ExportRefused as exc:
            result.refused.append((clip, str(exc)))
            log.error("[%s] export REFUSED id=%s: %s", label, clip["id"], exc)
        except Exception as exc:
            result.refused.append((clip, f"error: {exc}"))
            log.exception("[%s] export failed id=%s", label, clip["id"])
    return result
