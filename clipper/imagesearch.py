"""Topic-matched game pictures for news Shorts whose article has fewer than 4 usable pictures.

Searches for "<game> screenshot" / "<game> gameplay" with the `ddgs` package (DuckDuckGo Images, no API key) and the
free Wikimedia Commons API, downloads the hits through the same safe downloader as the article pictures
(newsimage.download_retry), and keeps only pictures that pass validation: PIL can open AND fully decode them, they are
at least 1000x560 (either way up), shaped like a photo (no banners / slivers), not flat placeholders and not duplicates of a picture
the Short already has.

Every kept picture is cached in data/image_cache/<topic_slug>/ so the same game is never downloaded twice (the cache is
re-validated on every use). Everything is best-effort: any failure returns fewer pictures, never raises.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import shutil
import time
import urllib.parse
from pathlib import Path

from . import newsimage
from .newsimage import MAX_ASPECT, MIN_ASPECT, big_enough

log = logging.getLogger("clipper")

SEARCH_DEADLINE_SECONDS = 150        # whole topic hunt per story
MAX_DOWNLOADS = 40                   # downloads tried per story
PER_QUERY = 3                        # pictures kept from one query on the first pass, so the Short gets varied views
SUFFIXES = ("screenshot", "gameplay", "official art", "key art", "in-game screenshot", "screenshot 4k",
            "press kit screenshot", "streaming")
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
COMMONS_SUFFIXES = SUFFIXES[:2]      # Commons is a small, shared, free service: ask it twice per game, not for every suffix
STOP = {"the", "a", "an", "of", "and", "for", "to", "in", "on", "game", "games", "gaming", "new", "free", "pc", "ps5", "ps4",
        "xbox", "steam", "epic", "nintendo", "switch", "playstation"}


def topic_slug(topic: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (topic or "").lower()).strip("-")
    return slug[:60] or "gaming"


def topic_tokens(topic: str) -> list[str]:
    """The distinctive words of a game name ("Alan Wake 2" -> alan, wake, 2); platform / filler words are dropped
    unless they are all there is ("Xbox" -> xbox)."""
    words = re.findall(r"[a-z0-9]+", (topic or "").lower())
    core = [w for w in words if w not in STOP]
    return core or words


def relevant(topic: str, *blobs: str) -> bool:
    """A search hit counts only when its title / page / file name mentions the topic (every distinctive word for names
    of up to two words, else at least two thirds) - a wrong game is worse than no picture."""
    tokens = topic_tokens(topic)
    if not tokens:
        return True
    text = " ".join(re.findall(r"[a-z0-9]+", " ".join(blobs).lower()))
    have = sum(1 for t in tokens if re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", text))
    need = len(tokens) if len(tokens) <= 2 else -(-len(tokens) * 2 // 3)
    return have >= need


# Hits whose title / page / file name says they are a web page, store screen, price list or UI, not game art.
UI_HIT = re.compile(r"(subscription|pricing|price[- ]?list|membership|sign[- ]?up|log[- ]?in|checkout|settings|dashboard|"
                    r"interface|menu|homepage|website|web ?page|ui|hud|logo|icon|banner|wallpaper|infographic|"
                    r"comparison chart|plans?|store page|eshop)", re.I)


def looks_like_ui(*blobs: str) -> bool:
    return bool(UI_HIT.search(" ".join(b for b in blobs if b)))


# ------------------------------------------------------------------ validation
def validate_image(data: bytes) -> tuple[int, int] | None:
    """-> (width, height) when PIL can fully decode `data` as a picture of a sensible size and shape, else None."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            im.verify()                                  # structural check ...
        with Image.open(io.BytesIO(data)) as im:
            im.load()                                    # ... and a full decode (catches truncated downloads)
            w, h = im.size
    except Exception:
        return None
    if not big_enough(w, h) or not MIN_ASPECT <= w / h <= MAX_ASPECT:
        return None
    return w, h


def hash_of(path: Path) -> tuple[int, int] | None:
    try:
        import cv2
        img = cv2.imread(str(path))
        return newsimage.fingerprint(img) if img is not None else None
    except Exception:
        return None


def hashes_of(files: list) -> list[tuple[int, int]]:
    return [h for h in (hash_of(Path(f)) for f in files) if h is not None]


# ------------------------------------------------------------------ searching
def search_ddgs(query: str, limit: int = 12) -> list[dict]:
    """[{'url', 'title', 'page', 'width', 'height', 'source'}] from DuckDuckGo Images (large, safe-search on).
    [] on any failure."""
    try:
        try:
            from ddgs import DDGS
        except ImportError:                               # the package's old name
            from duckduckgo_search import DDGS
        rows = DDGS().images(query, safesearch="on", size="Large", max_results=limit) or []
    except Exception as exc:
        log.info("image search: ddgs failed for %r (%s: %s)", query, type(exc).__name__, str(exc)[:100])
        return []
    return [{"url": r.get("image"), "title": r.get("title") or "", "page": r.get("url") or "",
             "width": int(r.get("width") or 0), "height": int(r.get("height") or 0), "source": "ddgs"}
            for r in rows if r.get("image")]


def search_commons(query: str, limit: int = 10, timeout: float = 15.0) -> list[dict]:
    """The same from Wikimedia Commons (free, no key). [] on any failure."""
    params = {"action": "query", "format": "json", "generator": "search", "gsrnamespace": "6", "gsrlimit": str(limit),
              "gsrsearch": query, "prop": "imageinfo", "iiprop": "url|size|mime", "iiurlwidth": "1920"}      # must be one of Wikimedia's standard thumbnail widths
    try:
        raw, _ = newsimage.download_retry(COMMONS_API + "?" + urllib.parse.urlencode(params), 2_000_000, timeout, tries=2)
        pages = (json.loads(raw.decode("utf-8", "replace")).get("query") or {}).get("pages") or {}
    except Exception as exc:
        log.info("image search: commons failed for %r (%s: %s)", query, type(exc).__name__, str(exc)[:100])
        return []
    out = []
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        if str(info.get("mime", "")).startswith("image/") and info.get("mime") != "image/svg+xml":
            out.append({"url": info.get("thumburl") or info.get("url"), "title": page.get("title", ""),
                        "page": info.get("descriptionurl", ""), "width": int(info.get("width") or 0),
                        "height": int(info.get("height") or 0), "source": "commons"})
    return [o for o in out if o["url"]]


# ------------------------------------------------------------------ the whole hunt
def fetch_topic_images(topic: str, folder: Path, cache_root: Path, need: int, known_hashes: list,
                       start_n: int, timeout: float = 15.0) -> list[dict]:
    """Up to `need` validated, distinct, topic-matched pictures saved as folder/news_image_<start_n..>.jpg.

    -> [{'file', 'url', 'source': 'cache' | 'ddgs' | 'commons', 'width', 'height'}]. Never raises."""
    kept: list[dict] = []
    hashes = list(known_hashes)
    if need <= 0 or not (topic or "").strip():
        return kept
    cache = cache_root / topic_slug(topic)
    deadline = time.monotonic() + SEARCH_DEADLINE_SECONDS

    def dest() -> Path:
        return folder / newsimage.IMAGE_NAME.format(n=start_n + len(kept))

    try:
        try:
            cache.mkdir(parents=True, exist_ok=True)
            cached = sorted(cache.glob("*.jpg"), key=lambda p: hashlib.sha1((topic + p.name).encode()).hexdigest())
        except OSError:
            cached = []
        for f in cached:                                   # 1. what an earlier run already downloaded
            if len(kept) >= need:
                return kept
            target = dest()
            try:
                info = newsimage.save_picture(f.read_bytes(), target, hashes)
            except Exception:
                info = None
            if info:
                hashes.append(info.pop("hash"))
                kept.append({"file": target, "url": None, "source": "cache", **info})
            elif hash_of(f) is None:
                f.unlink(missing_ok=True)                  # corrupt cache entry: drop it

        downloads = 0
        tried: set[str] = set()

        def full() -> bool:
            return len(kept) >= need or downloads >= MAX_DOWNLOADS or time.monotonic() > deadline

        def take(hit: dict) -> bool:
            """Download and keep one search hit; True when it became one of the pictures."""
            nonlocal downloads
            url = newsimage._http(hit["url"])
            if not url or url in tried or not newsimage._usable_url(url):
                return False
            tried.add(url)
            too_small = hit["width"] and hit["height"] and not big_enough(hit["width"], hit["height"])
            if too_small or not relevant(topic, hit["title"], hit["page"], url):
                return False
            if looks_like_ui(hit["title"], url):                 # store / price / menu screenshots are not game art
                return False
            if newsimage.GAMBLING_SOURCE.search(f"{hit['title']} {hit['page']} {url}"):
                return False
            downloads += 1
            probe = folder / "topic_probe.jpg"
            try:
                data, ctype = newsimage.download_retry(url, newsimage.MAX_IMAGE_BYTES, timeout,
                                                       referer=hit["page"] or None, tries=2)
                if ctype.startswith("text/") or not validate_image(data):
                    return False
                info = newsimage.save_picture(data, probe, hashes)   # shrinks, drops blurry, flat and duplicate pictures
                if not info:
                    return False
                try:
                    shutil.copyfile(probe, cache / (hashlib.sha1(url.encode()).hexdigest()[:12] + ".jpg"))
                except OSError:
                    pass                                             # the cache is a bonus
                final = dest()
                probe.replace(final)
                hashes.append(info.pop("hash"))
                kept.append({"file": final, "url": url, "source": hit["source"], **info})
                return True
            except Exception as exc:
                log.info("image search: %s failed (%s: %s)", url[:100], type(exc).__name__, str(exc)[:80])
                return False
            finally:
                probe.unlink(missing_ok=True)

        reserve: list[dict] = []
        for suffix in SUFFIXES:                            # 2. search, best query first, a few pictures from each
            if full():
                break
            query = f"{topic} {suffix}"
            from_query = 0
            commons = search_commons(query, timeout=timeout) if suffix in COMMONS_SUFFIXES else []
            for hit in search_ddgs(query) + commons:
                if full():
                    break
                if from_query >= PER_QUERY:
                    reserve.append(hit)
                else:
                    from_query += take(hit)
        for hit in reserve:                                # 3. still short: use the rest of what the queries returned
            if full():
                break
            take(hit)
    except Exception:
        log.exception("image search: unexpected error (ignored)")
    log.info("image search: %r -> %d picture(s) (%s)", topic, len(kept), ", ".join(k["source"] for k in kept) or "none")
    return kept

