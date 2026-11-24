"""Trend discovery: `python -m clipper trends` (also run at the end of `python -m clipper news`).

Finds what gamers are watching and grabbing right now WITHOUT touching anybody's footage:

* YouTube search metadata through yt-dlp (`--flat-playlist -J`: titles, channels, view counts, URLs - nothing is
  downloaded; skipped quietly when yt-dlp is not installed). Short-length, well-viewed videos from the last week.
* GamerPower's public giveaways API (live free games / DLC: Steam, Epic, GOG ...).

Each item becomes a Story marked with `trend`; headless Claude then finds the core gaming fact, VERIFIES it (unverified
items are dropped), and rewrites an original 35-second script in Game On's voice (see news.TREND_PROMPT). The Short is
rendered like any news Short (Tokyo Neon look, real article / topic pictures, neutral voice, CC-BY music, watermark) and its
`Sources & Credits:` line carries the original video / offer URL. No third-party video, audio or thumbnail is ever used
for video trends (autopost = false: a human still reviews and uploads).
"""
from __future__ import annotations

import json
import logging
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, topics
from .config import Config
from .lifecycle import story_keys, url_key
from .news import HARD_SKIP, USER_AGENT, NewsResult, Story, make_short, pick_stories
from .proc import run

log = logging.getLogger("clipper")

SEARCH_URL = "https://www.youtube.com/results?search_query={q}&sp=EgQIAxAB"      # filter: this week, videos
DEFAULTS = {
    "enabled": True,
    "per_run": 1,                          # trend Shorts made at the end of each `news` run
    "min_views": 20000,
    "max_short_seconds": 180,
    "per_query": 15,
    "max_candidates": 20,
    "giveaways": True,
    "queries": ["gaming easter egg shorts", "video game secret shorts", "gaming news shorts",
                "free steam games shorts", "patch notes surprise gaming shorts", "gaming facts shorts"],
}


def settings(cfg: Config) -> dict:
    return {**DEFAULTS, **cfg.raw.get("trends", {})}


def _json_from(cmd: list, timeout: int = 90):
    code, out = run(cmd, timeout=timeout, low_priority=True)
    start = out.find("{")
    if code != 0 or start < 0:
        return None
    try:
        return json.loads(out[start:])
    except ValueError:
        return None


def youtube_trends(cfg: Config, now: datetime) -> list[Story]:
    """Popular short gaming videos (metadata only) as trend pointers, best view counts first."""
    st = settings(cfg)
    exe = [sys.executable, "-m", "yt_dlp"]
    found: dict[str, Story] = {}
    for q in st["queries"]:
        data = _json_from([*exe, "--flat-playlist", "-J", "--no-warnings", "--playlist-end", str(st["per_query"]),
                           SEARCH_URL.format(q=urllib.request.quote(str(q)))])
        if not data:
            log.info("trends: no YouTube metadata for %r (yt-dlp missing or blocked)", q)
            continue
        for e in data.get("entries") or []:
            views, dur, url = e.get("view_count") or 0, e.get("duration") or 0, e.get("url") or ""
            title = " ".join(str(e.get("title") or "").split())
            if not url.startswith("http") or not title or views < st["min_views"] or not 0 < dur <= st["max_short_seconds"]:
                continue
            text = title + " " + str(e.get("description") or "")
            if HARD_SKIP.search(text):
                continue
            channel = str(e.get("channel") or e.get("uploader") or "a gaming channel")
            found[url] = Story(0, f"YouTube - {channel}", title, " ".join(str(e.get("description") or "").split())[:240],
                               url, now, None, [], {"source": "youtube", "views": views, "signal": f"{views:,} views, {dur}s"})
    return sorted(found.values(), key=lambda s: -s.trend["views"])


def giveaway_trends(cfg: Config, now: datetime) -> list[Story]:
    """Live free-game giveaways from GamerPower's public API (full games and DLC, most claimed first)."""
    try:
        req = urllib.request.Request("https://www.gamerpower.com/api/giveaways?sort-by=popularity",
                                     headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            items = json.loads(resp.read(3_000_000).decode("utf-8", "replace"))
    except Exception as exc:
        log.info("trends: giveaways unavailable (%s)", str(exc)[:80])
        return []
    out = []
    for g in items if isinstance(items, list) else []:
        if g.get("status") != "Active" or g.get("type") not in ("Game", "DLC"):
            continue
        link = str(g.get("gamerpower_url") or "")
        title = " ".join(str(g.get("title") or "").split())
        if not link.startswith("http") or not title:
            continue
        info = f"{g.get('worth', 'N/A')} value; platforms: {g.get('platforms')}; ends: {g.get('end_date')}. " \
               + " ".join(str(g.get("description") or "").split())[:200]
        pics = [u for u in (g.get("image"), g.get("thumbnail")) if u]
        out.append(Story(0, "GamerPower", title, info, link, now, pics[0] if pics else None, pics,
                         {"source": "giveaway", "signal": f"{g.get('users', '?')} claims, {g.get('worth', 'N/A')} value"}))
    return out[:8]


def fetch_trends(cfg: Config, now: datetime | None = None) -> list[Story]:
    """Candidate trend items (not already made / rejected / uploaded), YouTube pointers first, ids 1..n."""
    now = now or datetime.now(timezone.utc)
    st = settings(cfg)
    items = youtube_trends(cfg, now) + (giveaway_trends(cfg, now) if st["giveaways"] else [])
    seen = db.seen_story_keys(cfg.database) | {url_key(u) for u in db.news_urls_seen(cfg.database)}
    fresh = []
    for s in items:
        if any(k in seen for k in story_keys(s.link, s.title)):
            continue
        fresh.append(s)
    fresh = topics.cap_hot_topics(fresh)[:int(st["max_candidates"])]
    for i, s in enumerate(fresh, start=1):
        s.id = i
    return fresh


def run_trends(cfg: Config, count: int | None = None, dry_run: bool = False, blocked: set[str] | None = None) -> NewsResult:
    """Discover -> Claude verifies + paraphrases -> voice -> render. Never raises."""
    result = NewsResult()
    try:
        db.init_db(cfg.database)
        want = int(count if count is not None else settings(cfg)["per_run"])
        if want <= 0 or not settings(cfg)["enabled"]:
            return result
        stories = fetch_trends(cfg)
        if not stories:
            result.note = "no trending gaming topics found (or all already used)"
            log.info("trends: %s", result.note)
            return result
        log.info("trends: %d candidate topics", len(stories))
        cfg_news = dict(cfg.raw.get("news", {}))
        cfg.raw["news"] = {**cfg_news, "stories": want}
        try:
            result.picked = pick_stories(cfg, stories, want, cfg.data_dir / "news_work", trend=True, blocked=blocked)
        finally:
            cfg.raw["news"] = cfg_news
        if dry_run:
            return result
        for pick in result.picked:
            try:
                result.made.append((pick, make_short(cfg, pick)))
            except Exception as exc:
                log.exception("trends: %s failed", pick.story.link)
                result.failed.append((pick, str(exc)))
    except Exception as exc:
        log.exception("trend run failed")
        result.note = f"trend run failed: {exc}"
    return result
