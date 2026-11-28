"""Watchlist research: config/watchlist.json -> outlier videos -> which news stories to favour and which hook pattern fits.

Metadata only (yt-dlp flat extraction: titles, view counts, ids): no video or audio is ever downloaded here, and results
are cached in data/watchlist_cache.json for [scan_settings] cache_ttl_minutes.

An outlier title is a SIGNAL of what viewers are watching, never a source of facts: it only moves a matching story from
the news feeds up the candidate list and pairs it with a pattern from data/hook_bank.json. The title itself is not shown
to the script writer. Sponsored / deal / affiliate items are dropped, and only clean watch URLs (no parameters from the
source) are kept. Promo runs never use the watchlist. Everything is best effort: a failure never stops a news run.
"""
from __future__ import annotations

import json
import logging
import re
import statistics
import time
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import creators, db, gta, hookbank, promo, tech, topics
from .config import PROJECT_ROOT, Config

log = logging.getLogger("clipper")

WATCHLIST_FILE = PROJECT_ROOT / "config" / "watchlist.json"
CACHE_NAME = "watchlist_cache.json"
BUCKETS = ("upcoming_games", "trailers", "upcoming_tech", "gta_6", "creators")
MODE_BUCKETS = {None: BUCKETS, "gta": ("gta_6",), "creators": ("creators",), "tech": ("upcoming_tech",), "promo": ()}
SEARCH_URL = "https://www.youtube.com/results?search_query={q}&sp=EgQIAxAB"      # filter: this week, videos
DEFAULTS = {"mode": "metadata_only", "max_items_per_source": 12, "cache_ttl_minutes": 360, "request_delay_seconds": 1.5,
            "outlier_view_multiplier": 2.5, "min_views_for_outlier": 25000}
RECENT_HOURS = 72                       # an outlier about something the channel made this recently is a duplicate
PROMOTIONAL = re.compile(r"(#ad\b|\bsponsor(?:ed|ship)?\b|\baffiliate\b|\bpromo ?code\b|\bdiscount code\b|\buse code\b|"
                         r"\bpaid promotion\b|\bgiveaway\b)", re.I)
GTA6 = re.compile(r"\b(?:gta|grand theft auto)[ :-]*(?:6|vi)\b", re.I)
TRAILER = re.compile(r"\b(trailers?|teaser|gameplay (?:reveal|showcase|deep[- ]dive)|first look)\b", re.I)
RELEASE = re.compile(r"\b(release date|release window|launch(?:es|ing)? (?:date|on|in)|delay(?:s|ed)?|coming (?:to|in|on)|"
                     r"arrives? (?:on|in))\b", re.I)
# words that do not say which game or event a title is about
MATCH_COMMON = set(gta.COMMON) | set(topics.GENERIC) | set(
    "official gameplay trailer reveal upcoming announcement details breakdown world first look".split())


def load(path: Path = WATCHLIST_FILE) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data.get("buckets"), dict):
            raise ValueError("no 'buckets' object")
    except FileNotFoundError:
        return None
    except (OSError, ValueError, AttributeError) as exc:
        log.warning("watchlist: cannot read %s (%s)", path.name, str(exc)[:100])
        return None
    settings = {**DEFAULTS, **(data.get("scan_settings") or {})}
    if settings["mode"] != "metadata_only":
        log.warning("watchlist: mode %r is not supported (only metadata_only); not scanning", settings["mode"])
        return None
    return {"settings": settings, "buckets": data["buckets"]}


def source_url(source: dict) -> str | None:
    """The page to read for one source: a YouTube channel / playlist URL, or a this-week search."""
    if source.get("type") == "search_query":
        q = str(source.get("query") or "").strip()
        return SEARCH_URL.format(q=urllib.parse.quote(q)) if q else None
    url = str(source.get("url") or "")
    parts = urllib.parse.urlsplit(url)
    return url if parts.scheme == "https" and parts.hostname in ("www.youtube.com", "youtube.com") else None


def fetch_metadata(url: str, limit: int) -> list[dict]:
    """yt-dlp flat listing of one page. download=False: nothing but the listing is requested."""
    from .videosearch import _ydl
    with _ydl({"extract_flat": "in_playlist", "skip_download": True, "playlistend": limit, "noplaylist": False}) as ydl:
        info = ydl.extract_info(url, download=False)
    return [e for e in (info or {}).get("entries") or [] if e]


def clean_items(entries: list[dict], limit: int) -> list[dict]:
    """[{title, views, url, channel}] without promotional items; the URL is rebuilt from the video id."""
    out = []
    for e in entries[:limit]:
        vid, title = str(e.get("id") or ""), " ".join(str(e.get("title") or "").split())
        text = f"{title} {e.get('description') or ''}"
        if not re.fullmatch(r"[\w-]{11}", vid) or not title or PROMOTIONAL.search(text) or promo.is_deal(title):
            continue
        out.append({"title": title, "views": int(e.get("view_count") or 0), "url": f"https://www.youtube.com/watch?v={vid}",
                    "channel": str(e.get("channel") or e.get("uploader") or "")})
    return out


def _read_cache(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def outliers(items: list[dict], settings: dict) -> list[dict]:
    """Items well above their own source's median views (and above the floor), each with its `ratio`."""
    views = [i["views"] for i in items if i["views"] > 0]
    if len(views) < 3:                                      # no meaningful median to beat
        return []
    median = statistics.median(views)
    need = float(settings["outlier_view_multiplier"]) * median
    return [{**i, "ratio": round(i["views"] / median, 1)} for i in items
            if i["views"] >= need and i["views"] >= int(settings["min_views_for_outlier"])]


def matches(a: str, b: str) -> bool:
    return gta.same_angle(a, b, MATCH_COMMON)


def recent_titles(cfg: Config, now: datetime) -> list[str]:
    if not cfg.database.exists():
        return []
    since = (now - timedelta(hours=RECENT_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        return db.recent_topic_headlines(cfg.database, since) + db.recent_news_titles(cfg.database, since)
    except Exception:
        log.exception("watchlist: could not read recent titles (ignored)")
        return []


def scan(cfg: Config, only: str | None = None, refresh: bool = False, now: datetime | None = None,
         path: Path | None = None) -> list[dict]:
    """Outliers for this kind of run, strongest first, without recent duplicates or repeats of each other."""
    now = now or datetime.now(timezone.utc)
    wl = load(path or WATCHLIST_FILE)
    if not wl:
        return []
    settings = wl["settings"]
    limit, ttl = int(settings["max_items_per_source"]), float(settings["cache_ttl_minutes"]) * 60
    cache_file = cfg.data_dir / CACHE_NAME
    cache, changed, asked = _read_cache(cache_file), False, False
    found = []
    for bucket in MODE_BUCKETS.get(only, BUCKETS):
        for source in wl["buckets"].get(bucket) or []:
            url = source_url(source)
            if not url:
                log.warning("watchlist: source %s has no usable YouTube URL or query", source.get("id"))
                continue
            hit = cache.get(url)
            if refresh or not hit or now.timestamp() - float(hit.get("at", 0)) > ttl:
                if asked:
                    time.sleep(float(settings["request_delay_seconds"]))
                asked = True
                try:
                    hit = {"at": now.timestamp(), "items": clean_items(fetch_metadata(url, limit), limit)}
                except Exception as exc:                    # not cached: the next run tries again
                    log.info("watchlist: no metadata for %s (%s: %s)", source.get("id"), type(exc).__name__, str(exc)[:100])
                    continue
                cache[url], changed = hit, True
            for item in outliers(hit["items"], settings):
                if bucket == "creators" and creators.UNSUITABLE.search(item["title"]):
                    continue
                found.append({**item, "bucket": bucket, "source": str(source.get("name") or source.get("id"))})
    if changed:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            log.warning("watchlist: could not write the cache (%s)", exc)
    from .news import HARD_SKIP
    covered = recent_titles(cfg, now)
    kept: list[dict] = []
    for item in sorted(found, key=lambda i: -i["ratio"]):
        if HARD_SKIP.search(item["title"]):
            continue
        if any(matches(item["title"], t) for t in covered) or any(matches(item["title"], k["title"]) for k in kept):
            continue
        kept.append(item)
    return kept


# ------------------------------------------------------------------ pairing with stories
def bucket_of(title: str, summary: str = "", only: str | None = None) -> str | None:
    """The watchlist bucket (hook pattern family) a news story belongs to, or None."""
    if GTA6.search(title):
        return "gta_6"
    if only == "gta":
        return None                                         # older GTA games: no GTA 6 pattern
    if only == "creators":
        return "creators"
    if only == "tech":
        return "upcoming_tech"
    if TRAILER.search(title):
        return "trailers"
    if RELEASE.search(title):
        return "upcoming_games"
    if creators.is_story(title, summary):
        return "creators"
    if tech.is_story(title, summary):
        return "upcoming_tech"
    return None


def best_match(story, found: list[dict]) -> dict | None:
    return next((o for o in found if matches(story.title, o["title"])), None)


def annotate(cfg: Config, stories: list, found: list[dict], only: str | None = None,
             niche: str | None = None) -> None:
    """Set story.watch = {bucket, hook_id, pattern, ratio} for the stories a hook pattern fits."""
    if only == "promo":
        return
    bank = hookbank.load(hookbank.bank_path(cfg))
    taken: Counter = Counter()
    for story in stories:
        hit = best_match(story, found)
        bucket = niche or bucket_of(story.title, story.summary, only) or (hit and hit["bucket"])
        pattern = hookbank.choose(bank, bucket, taken) if bucket else None
        if pattern:
            taken[pattern["id"]] += 1
        if pattern or hit:
            story.watch = {"bucket": bucket, "hook_id": pattern["id"] if pattern else "",
                           "pattern": pattern["pattern"] if pattern else "", "ratio": hit["ratio"] if hit else None}


def prompt_block(stories: list) -> str:
    """What Claude is told about the annotated stories ('' when there are none)."""
    rows = []
    for s in stories:
        w = s.watch
        if not w:
            continue
        parts = [f"id {s.id}"]
        if w["ratio"]:
            parts.append(f"viewers are watching this topic now ({w['ratio']}x its source's usual views)")
        if w["hook_id"]:
            parts.append(f"pattern {w['hook_id']}: {w['pattern']}")
        rows.append(" | ".join(parts))
    if not rows:
        return ""
    return ("\n\nWATCHLIST AND HOOK PATTERNS (the voice, fact and hook rules above always win):\n" + "\n".join(rows) +
            "\nBetween equally strong stories prefer one viewers are watching. This is not a source of facts: use only what "
            "the story's own row states.\nEvery story you pick from an id listed with a pattern needs one more JSON key, "
            '"hook_pattern". The FIRST of its 5 hooks takes the pattern\'s angle and shape, not its exact words: fill the '
            "[placeholders] from the row, keep it to at most 12 words (cut any trailing 'here is ...' clause), name the game "
            'early and keep it concrete. Set "hook_pattern" to the pattern id, or to "" only when the row cannot support '
            "that angle.")
