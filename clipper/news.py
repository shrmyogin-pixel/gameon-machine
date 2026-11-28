"""Breaking News Engine: `python -m clipper news`.

    RSS feeds (last N hours)  ->  headless Claude (+ voice.md) picks the top stories and writes the scripts
    -> 5 hooks per story scored by hookscore.py  ->  edge-tts voiceover  ->  word timestamps from Groq
    (whisper-large-v3-turbo)  ->  1080x1920 Short rendered with the normal hardware FFmpeg path
    (the article's real picture with Ken Burns zoom/pan + dark scrim, watermark, 3-word bold captions)
    ->  data/clips.sqlite with platform = "news".

Policy: every Short credits its source ("Sources & Credits: <outlet>, <exact article URL>"), profanity is
masked, real-money-gambling stories are skipped, and nothing is ever uploaded (autopost = false).
One story failing never stops the next. The GROQ_API_KEY is only read, never printed or logged.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from calendar import timegm
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import creators, db, factcheck, gta, hookbank, promo, qa, tech, watchlist
from .config import PROJECT_ROOT, Config, get_env_key
from .buckets import BUCKETS, classify, enforce_mix
from .hookscore import HookScore, banned_in, closer_banned, names_subject, opener_of, rank_hooks, restates_hook, words_of
from .lifecycle import clean_link, story_keys, url_key
from .newsimage import fetch_news_images, images_from_entry, set_wikimedia_contact
from .profanity import mask

log = logging.getLogger("clipper")

USER_AGENT = "gameon-clipper/1.0"
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
FALLBACK_VOICES = ("en-US-BrianNeural", "en-US-GuyNeural", "en-US-AriaNeural")   # all neutral, tried in order
TTS_DEADLINE_SECONDS = 150
PATTERN_SLACK = 2.5           # hook points a hook-bank pattern hook may trail the best one and still be used
# stories that are never made: gambling / betting spam, piracy, cheat tools and "free currency" scams
HARD_SKIP = re.compile(
    r"\b(casinos?|sportsbooks?|bookmakers?|gambling|bet365|1xbet|betway|draftkings|fanduel|online slots|poker site|"
    r"stake\.com|parlays?|wagering|thunderpick|betboom|ggbet|rollbit|csgoroll|"
    # "betting" / "gamble" / "bet" alone are everyday idioms ("a big gamble"): they count only in a wagering context
    r"(?:sports?|online|esports|skin|crypto|match|live|in-game)[- ]betting|"
    r"betting (?:sites?|apps?|odds|markets?|sponsor\w*|partner\w*|compan(?:y|ies)|firms?|brands?|platforms?|operators?|"
    r"promos?|bonus(?:es)?|tips|scandals?|ring)|"
    r"betting on (?:(?:his|her|their) own )?(?:matches|esports|sports)|(?:place|placed|placing) bets|"
    r"gamble (?:online|with (?:real )?money|your skins)|"
    r"fitgirl|repacks?|keygen|key generator|torrent (?:download|link|site)s?|download torrent|"
    r"pirated (?:copy|copies|download|games?)|cracked (?:apk|copy|download|version)|mod apk|apk mod|"
    r"mod menu|aimbots?|wallhacks?|hack tool|cheat (?:download|tool|software)s?|"
    r"free (?:v-?bucks|robux)|(?:v-?bucks|robux|money|gems?) generator|no (?:human )?verification|crypto airdrop)\b", re.I)
# news --topic <name>: which stories qualify, what Claude is told, and which words do not tell two stories apart
MODES = {"gta": (gta.is_gta_story, gta.PROMPT_NOTE, gta.COMMON, "GTA"),
         "creators": (creators.is_story, creators.PROMPT_NOTE, creators.COMMON, "creator"),
         "tech": (tech.is_story, tech.PROMPT_NOTE, tech.COMMON, "gaming tech"),
         "promo": (promo.is_story, promo.PROMPT_NOTE, promo.COMMON, "deal or gear")}
KINDS = {"news": "BREAKING NEWS", "free_offer": "FREE GAME OFFER", "trend": "TRENDING NOW"}


class _Skip(Exception):
    """Not an error: a bookkeeping step that does not apply to this run."""


class NewsError(Exception):
    pass


@dataclass
class Story:
    id: int
    outlet: str
    title: str
    summary: str
    link: str
    published: datetime
    image_url: str | None = None      # the first picture the RSS feed itself carries (if any)
    image_urls: list = field(default_factory=list)   # every picture the feed entry carries
    trend: dict | None = None         # set for trend-discovery items: {'source': 'youtube'|'giveaway', 'signal': ...}
    watch: dict | None = None         # watchlist.annotate: hook pattern and viewer signal for this story


@dataclass
class Pick:
    story: Story
    kind: str
    score: float
    subjects: list
    hooks: list                    # list[HookScore], best first
    hook: HookScore
    body: str
    script: str
    headline: str
    title: str
    description: str
    tags: list
    hashtags: list
    sensitive: bool
    sensitive_reason: str
    reason: str
    bucket: str = ""               # one of buckets.BUCKETS
    major_new: bool = False        # Claude says: a completely new major announcement (may repeat a topic covered < 24 h ago)
    core_fact: str = ""            # trend items: the one verified fact the Short is built on
    hook_pattern: str = ""         # hook_bank.json id, set only when the chosen hook follows that pattern


@dataclass
class NewsResult:
    picked: list = field(default_factory=list)
    made: list = field(default_factory=list)       # (Pick, folder)
    failed: list = field(default_factory=list)     # (Pick, reason)
    note: str = ""


# ------------------------------------------------------------------ feeds
def _clean(text: str, limit: int = 300) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text[:limit].rsplit(" ", 1)[0] + "..." if len(text) > limit else text


def parse_feed(name: str, parsed, cutoff: datetime) -> list[Story]:
    """Stories from one parsed feed that are newer than `cutoff` (UTC). Entries without a link or date are ignored."""
    out = []
    for e in getattr(parsed, "entries", []):
        when = e.get("published_parsed") or e.get("updated_parsed")
        link = (e.get("link") or "").strip()
        title = _clean(e.get("title", ""), 200)
        if not when or not link.startswith(("http://", "https://")) or not title:
            continue
        published = datetime.fromtimestamp(timegm(when), timezone.utc)
        if published < cutoff:
            continue
        urls = images_from_entry(e)
        out.append(Story(0, name, title, _clean(e.get("summary") or e.get("description") or ""), link, published,
                         urls[0] if urls else None, urls))
    return out


def fetch_stories(cfg: Config, hours: float | None = None, now: datetime | None = None,
                  outliers: list | None = None) -> list[Story]:
    """Fresh, de-duplicated, not-yet-used stories from every feed, newest first, with ids 1..n."""
    import feedparser
    now = now or datetime.now(timezone.utc)
    hours = float(hours if hours is not None else cfg.news["hours"])
    cutoff = now - timedelta(hours=hours)
    in_promo = cfg.news.get("only") == "promo"
    seen_urls = db.news_urls_seen(cfg.database, in_promo) if cfg.database.exists() else set()
    seen_keys = db.seen_story_keys(cfg.database) if cfg.database.exists() else set()   # rejected / uploaded / cleaned
    if in_promo:                                     # a promo run also honours what was rejected / uploaded from promo
        seen_keys |= {k[2:] for k in seen_keys if k.startswith("p:")}
    seen_keys |= {url_key(u) for u in seen_urls}
    found: list[Story] = []
    for feed in cfg.news_feeds:
        try:
            parsed = feedparser.parse(feed["url"], agent=USER_AGENT)
            items = parse_feed(feed["name"], parsed, cutoff)
            if not items and not getattr(parsed, "entries", None):
                log.warning("news: feed %s gave no entries (%s)", feed["name"], getattr(parsed, "bozo_exception", "empty"))
            found += items
        except Exception:
            log.exception("news: feed %s failed; continuing", feed.get("name"))
    unique, links, titles = [], set(seen_urls), set()
    for s in sorted(found, key=lambda s: s.published, reverse=True):
        key = re.sub(r"\W+", " ", s.title.lower()).strip()
        if s.link in links or key in titles:
            continue
        if any(k in seen_keys for k in story_keys(s.link, s.title)):
            log.info("news: skipped (story already made, rejected or uploaded): %s", s.title[:80])
            continue
        if cfg.news.get("only") and not MODES[cfg.news["only"]][0](s.title, s.summary):
            continue
        if cfg.news.get("no_promo") and (watchlist.PROMOTIONAL.search(f"{s.title} {s.summary}") or promo.is_deal(s.title)):
            log.info("news: skipped (sponsored, deal or promotion): %s", s.title[:80])
            continue
        if HARD_SKIP.search(f"{s.title} {s.summary} {s.link}"):
            log.info("news: skipped (gambling, piracy, cheat or scam): %s", s.title[:80])
            continue
        links.add(s.link)
        titles.add(key)
        unique.append(s)
    from .buckets import balance
    from .topics import cap_hot_topics                            # before balancing, so a flooding topic wastes no slots
    if outliers:                                                  # stories viewers are watching keep their place
        unique.sort(key=lambda s: watchlist.best_match(s, outliers) is None)
    if not (cfg.news.get("only") or cfg.news.get("all_niches")):  # one theme, or niche by niche: nothing to cap
        unique = cap_hot_topics(unique)
    unique = balance(unique, int(cfg.news["max_candidates"]))     # a mix of the six buckets
    for i, s in enumerate(unique, start=1):
        s.id = i
    return unique


# ------------------------------------------------------------------ Claude: pick + write
PROMPT = """You are the news editor and scriptwriter of Game On, a gaming news YouTube Shorts channel.

VOICE AND SCRIPT RULES (voice.md - follow them exactly):
{voice}

CANDIDATE STORIES (all published in the last {hours} hours): id | outlet | age | headline | summary
{rows}
{covered}
TASK
Choose the {n} best stories to turn into 35-second Shorts. BALANCE ACROSS SIX BUCKETS so a night is never just generic news: (flagship) GTA 6 and other flagship games: gameplay secrets, easter eggs, Twitch Drops, verified leaks, bugs and glitches in them; (freebie) free games and rewards: Epic / Steam / console freebies, Twitch Drops, limited-time rewards; (update) major announcements and updates: trailers, release dates, major patches, server shutdowns; (glitch) wild bugs and viral glitches: funny physics, exploits, community discoveries; (creator) creator and esports news: streamer milestones, roster moves, tournament upsets; (tech) gaming tech and AI: GPUs, handhelds, DLSS / FSR updates, AI in game development. Take a DIFFERENT bucket for each story whenever the candidates allow, and never more than 2 from one bucket. Pick the most clickable story in each bucket, but never force a weak or unsupported one just to fill a bucket. NEVER pick or mention piracy, cracks, cheat tools, hacks, free-currency generators or other scams, and no real-money gambling, betting or skin-gambling promotion; never tell viewers to visit a third-party link. A leak counts only when a named outlet reports it: label it as a leak and never present it as fact. STRICT TOPIC DIVERSITY: pick stories about DIFFERENT games / franchises / platforms - never more than ONE story about the same game or franchise (e.g. at most one GTA 6 story, however many are listed). Never repeat an event the channel already covered (listed above, if any), even from a different outlet, and avoid any game / franchise already covered in the last 24 hours - unless the story is a completely NEW major announcement (new reveal, release date, delay), in which case set "major_new": true. Prefer variety across PC, PlayStation, Xbox, Nintendo and handhelds. Skip real-money gambling or betting, hate or harassment, anything about minors, graphic violence, and pure opinion / guide / review pieces.
Use ONLY facts found in the headline and summary. Never invent dates, prices, names or details; if a detail (such as an end date) is not given, leave it out. Say "reportedly", "leak" or "rumour" for anything unconfirmed.

FOR EACH STORY write:
- script_body: the spoken script AFTER the hook, {bmin}-{bmax} words, written like a real gaming creator talking to a friend: concrete details FIRST (price, date, platform, number, name), punchy sentences of 8-12 words, one fact each, contractions allowed, no robotic filler openers or closers. Then one line on why it matters, then ONE closing line that follows the CLOSING rules below. NEVER repeat the headline or the hook: do not reuse 5 or more words in a row from the story's headline, say it in your own words. Plain text only (no emoji, no hashtags, no stage directions, no markdown). No profanity.
- hooks: exactly 5 different hooks, each 5-12 words (never more than 12). Each must carry urgency, a curiosity gap or the news itself in its first words (free, today, now, leak, confirmed, delayed, "you have 48 hours", "your save could" ...) and name the game, platform, store or creator inside its FIRST 6 WORDS (put a creator in subjects too) - EXCEPT that up to 2 of the 5 may be pure curiosity-gap hooks that withhold the name ("One streamer just broke a record nobody thought possible."); if you use one, the FIRST sentence of script_body MUST name the game / creator / store. Use 5 different angles, and at least 3 of the 5 must open on a curiosity gap, a bold claim or a stake for the viewer ("You have 48 hours to ...", "Your ... could disappear", a question) instead of a plain "X just did Y" headline. No banned filler.
- subjects: the game / platform / store names exactly as they appear in your hooks (1-4 short strings). Put the MAIN GAME'S proper title first (e.g. "Alan Wake 2"): it is used to search for matching screenshots.
- headline: the on-screen headline card, max 70 characters, no emoji, no profanity.
- title: video title, 25-60 characters, game or platform within its first 4 words, no emoji.
- description: one line, max 90 characters.
- tags: 8-15 search tags. hashtags: 3-6 hashtags that start with #.
- kind: "news" or "free_offer".  bucket: "flagship", "freebie", "update", "glitch", "creator" or "tech".  score: 0-10, how strong this story is for the channel today.
- sensitive: true only when a human should look carefully (add sensitive_reason), else false.
- answers_hook: true only when the row itself gives every fact the headline and your hooks turn on (WHICH platform, game or person, HOW MUCH, WHEN). If the headline teases a detail the row never states ("skipping one platform", "a surprise guest"), do not pick that story; if you are unsure, set false.
- reason: one sentence on why you chose it.

HOOK AND CLOSING RULES (strict, they decide whether people stay):
- OPENING (spoken in the first 1-2.5 seconds): NEVER a greeting ("hey gamers", "welcome back"), never filler ("breaking news today") and never a dry headline read. Open on a sharp curiosity gap (name what the viewer does not know yet) or concrete stakes (what they get, lose or miss, and by when), then lead straight into the story. Examples of the energy: "Your Exoprimal progress has an expiry date.", "One streamer just broke a record nobody thought was possible.", "You have 48 hours to claim this free game before it's gone." Weak, do not write: "X is shutting down in 2027." (plain headline), "Stop scrolling if you play X." (nothing at stake). Only claim stakes the facts support. No vague teasers ("Nobody expected this ...", "a surprising call"): say the concrete thing that is odd, lost or won and withhold only how or why. Never open with "Nobody expected" or "Stop scrolling", and open every story in this reply with different words.
- The FIRST sentence of script_body must pay off or deepen the hook with a NEW fact. It must never restate the hook, not even with different wording or the same date, number or verb.
- CLOSING (the last 2-3 seconds, the last sentence of script_body, at most 12 words): NEVER "thanks for watching", "follow for more", "see you next time", "watch this space" or any slow generic outro. End on a punchy final twist, a debate question the viewer wants to answer, or a loop line that flows naturally back into the opening hook. It must make sense if the video starts over.

Reply with STRICT JSON only (no prose, no code fences):
{"stories": [{"id": 3, "kind": "news", "bucket": "creator", "score": 8, "subjects": ["..."], "hooks": ["", "", "", "", ""], "script_body": "...", "headline": "...", "title": "...", "description": "...", "tags": ["..."], "hashtags": ["#..."], "sensitive": false, "sensitive_reason": "", "major_new": false, "answers_hook": true, "reason": "..."}]}"""

TREND_PROMPT = """You are the news editor and scriptwriter of Game On, a gaming news YouTube Shorts channel.

VOICE AND SCRIPT RULES (voice.md - follow them exactly):
{voice}

TRENDING GAMING TOPICS: id | source | signal | title / text. These are public pointers to what gamers are watching and grabbing right now (a popular gaming video's title with its view count, or a live free-game giveaway). You do NOT have the video or its footage and must not describe it.
{rows}
{covered}
TASK
Choose up to {n} topics worth an ORIGINAL 35-second Game On Short (viral gaming facts, easter eggs, free games, patch surprises, release details). For each one:
1. Identify the core gaming fact behind the topic (core_fact: one plain sentence).
2. VERIFY it: set "verified": true ONLY if the fact is stated in the row itself or is well-established gaming knowledge you are confident is true. If you are not sure, or it is clickbait with no concrete fact, do not pick it.
3. Rewrite it COMPLETELY in Game On's voice. Do not copy or closely echo the original title or its wording, do not name or quote the creator, do not describe the original video. Add context or why it matters so the Short is original commentary.
Use ONLY facts you can stand behind; never invent dates, prices or names; say "reportedly" for anything unconfirmed. STRICT TOPIC DIVERSITY: different games each, never two on the same game or franchise, and avoid games already covered in the last 24 hours (unless a completely new major announcement: "major_new": true). Skip real-money gambling or betting, hate or harassment, anything about minors, graphic violence.

FOR EACH TOPIC write:
- script_body: the spoken script AFTER the hook, {bmin}-{bmax} words, written like a real gaming creator talking to a friend: the concrete fact FIRST, punchy sentences of 8-12 words, one fact each, contractions allowed, no robotic filler. Then why it matters, then ONE closing line that follows the CLOSING rules below. Never reuse 5 or more words in a row from the topic's title. Plain text only (no emoji, no hashtags, no stage directions, no markdown). No profanity.
- hooks: exactly 5 different hooks, each 5-12 words (never more than 12). Each must carry urgency, a curiosity gap or the news itself in its first words (free, today, now, leak, confirmed, delayed, "you have 48 hours", "your save could" ...) and name the game, platform, store or creator inside its FIRST 6 WORDS (put a creator in subjects too) - EXCEPT that up to 2 of the 5 may be pure curiosity-gap hooks that withhold the name ("One streamer just broke a record nobody thought possible."); if you use one, the FIRST sentence of script_body MUST name the game / creator / store. 5 different angles, and at least 3 of the 5 must open on a curiosity gap, a bold claim or a stake for the viewer instead of a plain "X just did Y" headline. No banned filler.
- subjects: the game / platform / store names exactly as they appear in your hooks (1-4 short strings). Put the MAIN GAME'S proper title first (e.g. "Alan Wake 2"): it is used to search for matching screenshots.
- headline: max 70 characters, no emoji. title: 25-60 characters, game or platform within its first 4 words, no emoji. description: one line, max 90 characters.
- tags: 8-15 search tags. hashtags: 3-6 hashtags that start with #.
- kind: "trend" or "free_offer".  bucket: "flagship", "freebie", "update", "glitch", "creator" or "tech".  score: 0-10.  verified: true/false.  core_fact: the fact.  major_new: true/false.
- sensitive: true only when a human should look carefully (add sensitive_reason), else false.
- answers_hook: true only when the row itself gives every fact the headline and your hooks turn on (WHICH platform, game or person, HOW MUCH, WHEN). If the headline teases a detail the row never states ("skipping one platform", "a surprise guest"), do not pick that story; if you are unsure, set false.
- reason: one sentence on why you chose it.

HOOK AND CLOSING RULES (strict, they decide whether people stay):
- OPENING (spoken in the first 1-2.5 seconds): NEVER a greeting ("hey gamers", "welcome back"), never filler ("breaking news today") and never a dry headline read. Open on a sharp curiosity gap (name what the viewer does not know yet) or concrete stakes (what they get, lose or miss, and by when), then lead straight into the story. Examples of the energy: "Your Exoprimal progress has an expiry date.", "One streamer just broke a record nobody thought was possible.", "You have 48 hours to claim this free game before it's gone." Weak, do not write: "X is shutting down in 2027." (plain headline), "Stop scrolling if you play X." (nothing at stake). Only claim stakes the facts support. No vague teasers ("Nobody expected this ...", "a surprising call"): say the concrete thing that is odd, lost or won and withhold only how or why. Never open with "Nobody expected" or "Stop scrolling", and open every story in this reply with different words.
- The FIRST sentence of script_body must pay off or deepen the hook with a NEW fact. It must never restate the hook, not even with different wording or the same date, number or verb.
- CLOSING (the last 2-3 seconds, the last sentence of script_body, at most 12 words): NEVER "thanks for watching", "follow for more", "see you next time", "watch this space" or any slow generic outro. End on a punchy final twist, a debate question the viewer wants to answer, or a loop line that flows naturally back into the opening hook. It must make sense if the video starts over.

Reply with STRICT JSON only (no prose, no code fences):
{"stories": [{"id": 3, "kind": "trend", "bucket": "update", "score": 8, "verified": true, "core_fact": "...", "subjects": ["..."], "hooks": ["", "", "", "", ""], "script_body": "...", "headline": "...", "title": "...", "description": "...", "tags": ["..."], "hashtags": ["#..."], "sensitive": false, "sensitive_reason": "", "major_new": false, "answers_hook": true, "reason": "..."}]}"""

RETRY_NOTE = ("\n\nYour previous reply was rejected: {why}. Reply again with ONLY the JSON object "
              "- no prose, no markdown, no code fences.")


def age_text(published: datetime, now: datetime) -> str:
    mins = max(int((now - published).total_seconds() // 60), 0)
    return f"{mins}m" if mins < 90 else f"{mins // 60}h"


def covered_block(cfg: Config, now: datetime) -> str:
    """The news Shorts made in the last 24 hours, so the same event is not made twice from another outlet."""
    try:
        titles = db.recent_news_titles(cfg.database, (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        return ""
    if not titles:
        return ""
    return ("\nALREADY MADE IN THE LAST 24 HOURS (do not pick these events again, nor other stories about the same game "
            "unless it is a completely new major announcement):\n" + "\n".join(f"- {t}" for t in titles[:30]) + "\n")


def build_prompt(cfg: Config, stories: list[Story], n: int, now: datetime | None = None, trend: bool = False) -> str:
    now = now or datetime.now(timezone.utc)
    voice = (PROJECT_ROOT / "voice.md").read_text(encoding="utf-8")
    if trend:
        rows = "\n".join(f"{s.id} | {s.outlet} | {(s.trend or {}).get('signal', '')} | {s.title} | {s.summary or '(no text)'}"
                         for s in stories)
    else:
        rows = "\n".join(f"{s.id} | {s.outlet} | {age_text(s.published, now)} ago | {s.title} | {s.summary or '(no summary)'}"
                         for s in stories)
    lo, hi = int(cfg.news["script_min_words"]), int(cfg.news["script_max_words"])
    covered = covered_block(cfg, now)
    text = TREND_PROMPT if trend else PROMPT
    for key in ("voice", "rows", "covered"):                    # their text is inserted last, never re-expanded
        text = text.replace("{" + key + "}", "\0" + key + "\0")
    for key, val in {"hours": cfg.news["hours"], "n": n, "bmin": lo - 4, "bmax": hi - 12}.items():
        text = text.replace("{" + key + "}", str(val))
    text = text.replace("\0voice\0", voice).replace("\0rows\0", rows).replace("\0covered\0", covered)
    only = cfg.news.get("only")
    if only != "promo":
        text += watchlist.prompt_block(stories) + cfg.news.get("niche_note", "")
    return text + MODES[only][1] if only else text


def _text(d: dict, key: str, lo: int, hi: int, what: str | None = None) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v.strip():
        raise NewsError(f"'{key}' must be a non-empty string")
    v = mask(v.strip())
    if not lo <= len(v) <= hi:
        raise NewsError(f"'{what or key}' must be {lo}-{hi} characters, got {len(v)}")
    return v


def _list(d: dict, key: str, lo: int, hi: int) -> list[str]:
    v = d.get(key)
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise NewsError(f"'{key}' must be a list of non-empty strings")
    if not lo <= len(v) <= hi:
        raise NewsError(f"'{key}' needs {lo}-{hi} items, got {len(v)}")
    return [mask(x.strip()) for x in v]


def repeated_run(headline: str, body: str, n: int = 5) -> str:
    """The first run of `n` words in a row that the script copies from the source headline ('' = none)."""
    h, b = words_of(headline), words_of(body)
    h, b = [w.lower() for w in h], [w.lower() for w in b]
    grams = {tuple(h[i:i + n]) for i in range(len(h) - n + 1)}
    for i in range(len(b) - n + 1):
        if tuple(b[i:i + n]) in grams:
            return " ".join(b[i:i + n])
    return ""


def copies_headline(headline: str, hook: str, subjects: list[str] | tuple = ()) -> bool:
    """True when the hook lifts 4 or more words in a row from the source headline (a long game name does not count)."""
    run = repeated_run(headline, hook, 4)
    return bool(run) and not any(run in " ".join(words_of(s)).lower() for s in subjects)


def final_script(hook: str, body: str) -> str:
    hook = hook.strip()
    if hook[-1] not in ".!?":
        hook += "."
    return f"{hook} {body.strip()}"


def validate_reply(reply: str, stories: list[Story], cfg: Config) -> list[Pick]:
    """Strict checks on Claude's reply; returns the picks with their best hook chosen. A story that fails its checks is
    dropped (and logged) and the others are kept. Raises NewsError for a malformed reply or when no story passes."""
    start, end = reply.find("{"), reply.rfind("}")
    try:
        data = json.loads(reply[start:end + 1]) if 0 <= start < end else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("stories"), list) or not data["stories"]:
        raise NewsError("reply was not a JSON object with a non-empty 'stories' list")
    by_id = {s.id: s for s in stories}
    lo, hi = int(cfg.news["script_min_words"]), int(cfg.news["script_max_words"])
    limit = int(cfg.news["stories"])
    picks, used, rejected = [], set(), []
    openers: set[str] = set()                  # hook openings already used in this batch
    for d in data["stories"][:limit]:
        if not isinstance(d, dict):
            raise NewsError("each story must be a JSON object")
        sid = d.get("id")
        if isinstance(sid, bool) or sid not in by_id:
            raise NewsError(f"story id {sid!r} is not one of the candidates")
        if sid in used:
            raise NewsError(f"story id {sid} was picked twice")
        used.add(sid)
        tag = f"story {sid}: "
        try:
            kind = d.get("kind")
            if kind not in KINDS:
                raise NewsError("'kind' must be \"news\" or \"free_offer\"")
            score = d.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 10:
                raise NewsError("'score' must be a number 0-10")
            if not isinstance(d.get("sensitive"), bool):
                raise NewsError("'sensitive' must be true or false")
            if by_id[sid].trend and d.get("verified") is not True:
                log.info("news: trend skipped (Claude could not verify the fact): %s", by_id[sid].title[:80])
                used.discard(sid)
                continue
            if d.get("answers_hook") is False:
                raise NewsError("the row does not give the fact the story turns on, so the Short cannot answer its own hook; "
                                "pick a different story")
            subjects = _list(d, "subjects", 1, 4)
            hooks_raw = _list(d, "hooks", 5, 5)
            body = _text(d, "script_body", 200, 900)
            if bad := banned_in(body):
                raise NewsError("script_body uses banned filler: " + ", ".join(bad))
            if echo := repeated_run(by_id[sid].title, body):
                raise NewsError(f"script_body repeats the source headline ('{echo}'); say it in your own words")
            bucket = d.get("bucket")
            if bucket not in BUCKETS:
                bucket = classify(by_id[sid].title + " " + by_id[sid].summary) or ("freebie" if kind == "free_offer" else "update")
            if bad := closer_banned(body):
                raise NewsError("the closing line is a generic outro (" + ", ".join(bad) + "); end on a twist, a debate question or a loop line")
            if len(words_of(re.split(r"(?<=[.!?])\s+", body.strip())[-1])) > 14:
                raise NewsError("the closing line must be short and punchy (at most 12 words)")
            ranked = rank_hooks(hooks_raw, subjects)
            watch = by_id[sid].watch or {}
            patterned = (hooks_raw[0] if watch.get("hook_id") and d.get("hook_pattern") == watch["hook_id"] else None)
            body_words = len(body.split())
            fitting = [h for h in ranked if h.valid and lo <= h.words + body_words <= hi]
            # best hook that sentence 2 does not simply repeat; if all are repeated, the check below rejects the reply
            usable = [h for h in fitting if not restates_hook(h.hook, body, subjects=subjects)
                      and (names_subject(h.hook, subjects) or names_subject(body.split(".")[0], subjects))]
            chosen = fitting[0] if fitting else None
            if usable:                       # never the headline again when there is a choice, then a fresh opening
                usable = [h for h in usable if not copies_headline(by_id[sid].title, h.hook, subjects)] or usable
                chosen = next((h for h in usable if h.total >= usable[0].total - 2.0 and opener_of(h.hook) not in openers),
                              usable[0])
                # the hook written after the hook-bank pattern wins unless it scores clearly worse than the best one
                chosen = next((h for h in usable if patterned and h.hook.strip() == patterned.strip()
                               and h.total >= usable[0].total - PATTERN_SLACK and opener_of(h.hook) not in openers), chosen)
            if chosen is None:
                if not any(h.valid for h in ranked):
                    raise NewsError("no valid hook: " + "; ".join(f"'{h.hook[:30]}' {', '.join(h.reasons)}" for h in ranked))
                raise NewsError(f"hook + script_body must total {lo}-{hi} words; script_body has {body_words}, "
                                f"hooks have {min(h.words for h in ranked)}-{max(h.words for h in ranked)}")
            if not names_subject(chosen.hook, subjects) and not names_subject(body.split(".")[0], subjects):
                raise NewsError("the hook withholds the game / creator name, so the first sentence of script_body must name it")
            if restates_hook(chosen.hook, body, subjects=subjects):
                raise NewsError("script_body starts by restating the hook; continue with NEW facts instead")
            pick = Pick(
                story=by_id[sid], kind=kind, score=float(score), subjects=subjects, hooks=ranked, hook=chosen,
                body=body, script=final_script(chosen.hook, body), headline=_text(d, "headline", 8, 70),
                title=_text(d, "title", 10, 60), description=mask(_text(d, "description", 5, 200)).splitlines()[0],
                tags=_list(d, "tags", 8, 15),
                hashtags=["#" + h.lstrip("#").replace(" ", "") for h in _list(d, "hashtags", 0, 30)],
                sensitive=d["sensitive"], sensitive_reason=mask(str(d.get("sensitive_reason") or "")),
                reason=mask(str(d.get("reason") or "")), bucket=bucket, major_new=d.get("major_new") is True,
                core_fact=mask(str(d.get("core_fact") or "")),
                hook_pattern=watch["hook_id"] if patterned and chosen.hook.strip() == patterned.strip() else "")
            if len(pick.description) > 90:
                raise NewsError("'description' is longer than 90 characters")
            if pick.sensitive and not pick.sensitive_reason:
                raise NewsError("sensitive=true needs a sensitive_reason")
        except NewsError as exc:
            rejected.append(tag + str(exc))
            continue
        picks.append(pick)
        openers.add(opener_of(pick.hook.hook))
    if rejected and not picks:
        raise NewsError("; ".join(rejected))            # nothing usable: the caller retries with these reasons
    for why in rejected:
        log.warning("news: dropped (failed checks): %s", why)
    return picks


def pick_stories(cfg: Config, stories: list[Story], n: int, work_dir: Path, trend: bool = False,
                 blocked: set[str] | None = None) -> list[Pick]:
    """One headless Claude call (with retries on a bad reply), then the strict topic-diversity rule:
    one story per game / franchise per run, none on a game covered in the last 24 h (unless a major new announcement).
    `blocked` = topics already taken earlier in this run. Raises NewsError."""
    from . import topics
    from .editor import EditorError, call_claude
    work_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    covered = topics.covered_topics(cfg, (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"))
    covered += [",".join(topics.topic_words(t)) for t in _recent_titles(cfg, now)]
    base = build_prompt(cfg, stories, n, now, trend)
    prompt, last = base, "unknown"
    for attempt in range(1, int(cfg.ai["retries"]) + 2):
        try:
            reply = call_claude(cfg, prompt, work_dir)
            if re.search(r'"stories"\s*:\s*\[\s*\]', reply):
                log.info("news: Claude found no story worth making")
                return []                    # a clean "nothing today", not a broken reply
            picks = validate_reply(reply, stories, cfg)
            only = cfg.news.get("only")
            if only == "promo":
                for p in picks:                          # GAME DEAL or TECH PICK, from the headline itself
                    p.bucket = promo.bucket_for(p.story.title)
            if only in ("creators", "tech"):
                for p in picks:                          # the pill reads CREATOR NEWS / GAMING TECH whatever the game
                    p.bucket = "creator" if only == "creators" else "tech"
            if only:
                kept, dropped = gta.enforce_angles(picks, _covered_headlines(cfg, now), MODES[only][2])
            else:
                kept, dropped = topics.enforce(picks, [c for c in covered if c], blocked)
            for why in dropped:
                log.info("news: dropped (topic diversity): %s", why)
            if not kept:
                raise NewsError("every pick repeated a topic: " + "; ".join(dropped))
            kept, over = enforce_mix(kept, len(kept) if only else int(cfg.news.get("bucket_max", 2)))
            for why in over:
                log.info("news: dropped (bucket mix): %s", why)
            return kept
        except (NewsError, EditorError) as exc:
            last = str(exc)
            log.warning("news: Claude attempt %d failed: %s", attempt, last)
            prompt = base + RETRY_NOTE.format(why=last)
    raise NewsError(last)


def _covered_headlines(cfg: Config, now: datetime) -> list[str]:
    """Source headlines and titles of the Shorts of the last 24 hours (headlines survive Reject / Mark Uploaded)."""
    since = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        return (db.recent_topic_headlines(cfg.database, since)
                + db.recent_news_titles(cfg.database, since, cfg.news.get("only") == "promo"))
    except Exception:
        return []


def _recent_titles(cfg: Config, now: datetime) -> list[str]:
    try:
        return db.recent_news_titles(cfg.database, (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"),
                                     cfg.news.get("only") == "promo")
    except Exception:
        return []


# ------------------------------------------------------------------ voiceover + timestamps
def _rate_value(rate: str) -> int:
    m = re.fullmatch(r"([+-]\d+)%", rate.strip())
    return int(m.group(1)) if m else 0


def _rate_text(value: int) -> str:
    return f"{value:+d}%"


async def _tts_once(text: str, voice: str, rate: str, out: Path, pitch: str = "+0Hz") -> None:
    import edge_tts
    await asyncio.wait_for(edge_tts.Communicate(text, voice, rate=rate, pitch=pitch).save(str(out)), timeout=30)


def synthesize(cfg: Config, text: str, out: Path, rate: str | None = None) -> str:
    """edge-tts voiceover -> out (mp3). The service is flaky (it sometimes returns no audio), so retry, then try
    other voices. Returns the voice used. Raises NewsError."""
    rate = rate or str(cfg.news["rate"])
    voices = [str(cfg.news["voice"]), *[v for v in FALLBACK_VOICES if v != cfg.news["voice"]]]
    last = "no attempt"
    deadline = time.monotonic() + TTS_DEADLINE_SECONDS
    for voice in voices:
        for attempt in range(1, 4):
            if time.monotonic() > deadline:
                raise NewsError(f"edge-tts gave up after {TTS_DEADLINE_SECONDS}s ({last})")
            try:
                out.unlink(missing_ok=True)
                asyncio.run(_tts_once(text, voice, rate, out, str(cfg.news["pitch"])))
                if out.is_file() and out.stat().st_size > 2000:
                    return voice
                last = "audio file was empty"
            except Exception as exc:
                last = f"{type(exc).__name__}: {str(exc)[:100]}"
            log.info("news: edge-tts %s attempt %d failed (%s)", voice, attempt, last)
            time.sleep(1.5 * attempt)
    raise NewsError(f"edge-tts could not produce audio ({last})")


def _multipart(fields: dict, file_field: str, filename: str, content: bytes, ctype: str) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items()]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                 f"Content-Type: {ctype}\r\n\r\n".encode() + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def groq_words(cfg: Config, audio: Path, key: str) -> list[list]:
    """[[start_s, end_s, word], ...] from Groq whisper-large-v3-turbo. Raises NewsError (never includes the key)."""
    body, ctype = _multipart({"model": cfg.news["groq_model"], "response_format": "verbose_json",
                              "timestamp_granularities[]": "word", "language": "en", "temperature": "0"},
                             "file", audio.name, audio.read_bytes(), "audio/mpeg")
    req = urllib.request.Request(GROQ_URL, data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": ctype, "User-Agent": USER_AGENT})
    last = ""
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read())
            words = [[float(w["start"]), float(w["end"]), str(w["word"]).strip()]
                     for w in data.get("words", []) if str(w.get("word", "")).strip()]
            if not words:
                raise NewsError("Groq returned no word timestamps")
            return words
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code in (400, 401, 403, 404):
                break
        except NewsError:
            raise
        except Exception as exc:
            last = type(exc).__name__
        time.sleep(2)
    raise NewsError(f"Groq transcription failed ({last})")


def estimate_words(script: str, duration: float) -> list[list]:
    """Fallback timestamps: words spread over the audio in proportion to their length."""
    toks = script.split()
    if not toks:
        return []
    start, end = 0.12, max(duration - 0.15, 0.5)
    weights = [len(t) + 2 for t in toks]
    total, t, out = sum(weights), start, []
    for tok, w in zip(toks, weights):
        step = (end - start) * w / total
        out.append([round(t, 3), round(t + step, 3), tok])
        t += step
    return out


def get_words(cfg: Config, audio: Path, script: str, duration: float) -> tuple[list[list], str]:
    """-> (words, 'groq' | 'estimated'). Falls back to estimates when there is no key or Groq fails."""
    key = get_env_key("GROQ_API_KEY")
    if not key:
        log.warning("news: GROQ_API_KEY not set; using estimated word timing")
        return estimate_words(script, duration), "estimated"
    try:
        words = groq_words(cfg, audio, key)
        spoken, scripted = len(words), len(script.split())
        if spoken < scripted * 0.7:
            log.warning("news: Groq heard %d words but the script has %d; using estimated timing", spoken, scripted)
            return estimate_words(script, duration), "estimated"
        return words, "groq"
    except NewsError as exc:
        log.warning("news: %s; using estimated word timing", exc)
        return estimate_words(script, duration), "estimated"


# ------------------------------------------------------------------ one Short
def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30] or "news"


def _new_folder(cfg: Config, outlet: str) -> Path:
    base = f"news-{_slug(outlet)}-{datetime.now():%Y%m%d-%H%M%S}"
    folder, n = cfg.clips_dir / base, 1
    while folder.exists():
        n += 1
        folder = cfg.clips_dir / f"{base}-{n}"
    folder.mkdir(parents=True)
    return folder


def credit_for_story(story: Story) -> str:
    return f"Sources & Credits: {story.outlet}, {clean_link(story.link)}"


def collect_visuals(cfg: Config, story: Story, folder: Path, topic: str | None = None) -> list[dict]:
    """8-10 REAL pictures for the story's cuts: the article's own, topped up with topic-matched game screenshots.

    Article pictures come first (og:image, feed, body). When fewer than images_min unique high-res ones were found,
    `topic` (the game / platform name) is searched ("<topic> screenshot", "<topic> gameplay") and the validated hits are
    mixed in. There are no generated cards: when even that gives fewer than images_floor pictures a NewsError is
    raised and the story is skipped.

    -> [{'file', 'url', 'source', 'kind': 'article' | 'topic'}]."""
    from . import imagesearch
    set_wikimedia_contact(str(cfg.news.get("wikimedia_contact", "")))
    try:
        if story.trend and story.trend.get("source") == "youtube":
            found = []                       # never reuse a third-party video's thumbnail: topic screenshots only
        else:
            found = fetch_news_images(story, folder, float(cfg.news["image_timeout"]), int(cfg.news["images_max"]))
    except Exception:
        log.exception("news: picture hunt failed (ignored)")
        found = []
    visuals = [{**f, "kind": "article"} for f in found]
    want, floor = int(cfg.news["images_min"]), int(cfg.news["images_floor"])
    if len(visuals) < want:
        topic = (topic or story.title or "").strip()

        def search(need: int, known: list, start_n: int) -> list[dict]:
            return imagesearch.fetch_topic_images(topic, folder, cfg.data_dir / "image_cache", need, known, start_n,
                                                  float(cfg.news["image_timeout"]))
        room = int(cfg.news["images_max"]) - len(visuals)
        have = imagesearch.hashes_of([v["file"] for v in visuals])
        used = _recently_used_pictures(cfg)
        extra = search(room, have + used, len(visuals) + 1)           # pictures recent Shorts have not shown ...
        if used and len(visuals) + len(extra) < want:                 # ... and only then repeats, rather than too few
            extra += search(room - len(extra), have + imagesearch.hashes_of([e["file"] for e in extra]),
                            len(visuals) + len(extra) + 1)
        topical = [{**f, "kind": "topic"} for f in extra]
        mixed, a, t = [], list(visuals), topical      # article, topic, article, topic ... so the cuts alternate
        while a or t:
            if a:
                mixed.append(a.pop(0))
            if t:
                mixed.append(t.pop(0))
        visuals = mixed
    log.info("[%s] news visuals: %s", folder.name,
             ", ".join(f"{v['kind']}:{v['source']}" for v in visuals) or "none")
    if len(visuals) < floor:
        raise NewsError(f"only {len(visuals)} real picture(s) found for '{topic or story.title}' (need {floor}); "
                        "skipping rather than showing placeholders")
    return visuals


def collect_clips(cfg: Config, pick: Pick, folder: Path, seconds: float, need: int = 0) -> list[dict]:
    """As many video clips as a Short of `seconds` needs, at most [news] videos_max (videosearch.py, every command);
    [] when off or nothing was found."""
    if not cfg.news.get("video", True):
        return []
    from . import videosearch
    from .news_render import clips_needed
    try:
        return videosearch.fetch_clips(cfg, list(pick.subjects), folder, PROJECT_ROOT / "assets" / "gameplay",
                                       need or clips_needed(seconds, int(cfg.news.get("videos_max", 15))),
                                       float(cfg.news["image_timeout"]),
                                       kind=pick.bucket, exclude=pick.story.link,
                                       youtube=bool(cfg.news.get("youtube", True)), seen=_recently_used_clips(cfg))
    except Exception:
        log.exception("news: video hunt failed (using pictures)")
        return []


def clip_frames(clips: list[dict], folder: Path, known: list, need: int) -> list[dict]:
    """Up to `need` distinct still frames from the fetched clips (same size, focus and duplicate rules as any picture)."""
    import cv2
    from .imagesearch import hashes_of
    from .newsimage import save_picture
    hashes, out = hashes_of(known), []
    for second in (0.4, 2.0, 3.6, 1.2, 2.8):
        for clip in clips:
            if len(out) >= need:
                return out
            cap = cv2.VideoCapture(str(clip["file"]))
            try:
                cap.set(cv2.CAP_PROP_POS_MSEC, second * 1000)
                ok, img = cap.read()
            finally:
                cap.release()
            if not ok:
                continue
            dest = folder / f"news_frame_{len(out) + 1}.jpg"
            info = save_picture(cv2.imencode(".jpg", img)[1].tobytes(), dest, hashes)
            if info:
                hashes.append(info["hash"])
                out.append({"file": dest, "url": clip["url"], "source": clip["source"], "kind": "frame"})
    return out


def collect_dense(cfg: Config, pick: Pick, folder: Path, seconds: float) -> tuple[str, list[dict], list[dict]]:
    """Visuals for a dense Short: DENSE_VIDEOS+ video cuts, else DENSE_PHOTOS+ distinct pictures and clip frames.

    cfg.news['dense'] = {'videos', 'photos'} is how many to try for. -> ('video' | 'photo', clips whose source must be
    credited, pictures). Raises NewsError when neither floor can be reached."""
    from .news_render import (DENSE_MAX_PHOTOS, DENSE_MAX_VIDEOS, DENSE_PHOTOS, DENSE_VIDEOS, TAIL_SECONDS, dense_count)
    want = cfg.news["dense"]
    videos = min(max(int(want.get("videos", 0)), DENSE_VIDEOS), DENSE_MAX_VIDEOS)
    photos = min(max(int(want.get("photos", 0)), DENSE_PHOTOS), DENSE_MAX_PHOTOS)
    total = seconds + TAIL_SECONDS
    clips = collect_clips(cfg, pick, folder, seconds, videos)
    n = dense_count(total, len(clips), "video")
    if n >= DENSE_VIDEOS:
        return "video", clips[:n], []
    before = cfg.raw["news"]
    cfg.raw["news"] = {**before, "images_min": photos, "images_max": photos, "images_floor": 0}
    try:
        visuals = collect_visuals(cfg, pick.story, folder, pick.subjects[0] if pick.subjects else None)
    finally:
        cfg.raw["news"] = before
    frames = clip_frames(clips, folder, [v["file"] for v in visuals], photos - len(visuals))
    have = dense_count(total, len(visuals) + len(frames), "photo")
    if have < DENSE_PHOTOS:
        raise NewsError(f"only {len(clips)} video cut(s) and {have} distinct picture(s)/frame(s) "
                        f"(need {DENSE_VIDEOS} cuts or {DENSE_PHOTOS} pictures); skipping")
    shown = (visuals + frames)[:have]
    used = {f["url"] for f in shown if f["kind"] == "frame"}
    return "photo", [c for c in clips if c["url"] in used], [v for v in shown]


def _recently_used_clips(cfg: Config) -> list:
    """Fingerprints of the footage shown in the last [news] video_repeat_hours, so two Shorts never share a shot."""
    hours = float(cfg.news.get("video_repeat_hours", 48))
    if hours <= 0:
        return []
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        return db.recent_used_clips(cfg.database, since)
    except Exception:
        log.exception("news: could not read the used footage (ignored)")
        return []


def _recently_used_pictures(cfg: Config) -> list:
    """Fingerprints of the pictures shown in the last [news] picture_repeat_hours, so two Shorts about one game differ."""
    hours = float(cfg.news.get("picture_repeat_hours", 48))
    if hours <= 0:
        return []
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        return db.recent_used_pictures(cfg.database, since)
    except Exception:
        log.exception("news: could not read the used pictures (ignored)")
        return []


def choose_music(cfg: Config, seed: str) -> dict | None:
    """A background track for this Short (Creative Commons or synthesized), or None when music is off / unavailable."""
    from . import music
    if not music.settings(cfg)["enabled"]:
        return None
    return music.pick_track(music.ensure_music(cfg), seed)


def make_short(cfg: Config, pick: Pick) -> Path:
    """Build, render and register one news Short. Raises on failure (the caller records it)."""
    from .news_render import render_news_video
    from .worker import probe_duration

    story = pick.story
    problems, source_notes = factcheck.check(cfg, pick)      # before any voiceover, B-roll download or render
    if problems:
        raise NewsError("source check failed: " + "; ".join(problems))
    folder = _new_folder(cfg, story.outlet)
    in_promo = cfg.news.get("only") == "promo"
    db.add_clip(cfg.database, "news", story.outlet, folder.name, stream=story.link, lang="en",
                moment_kind="promo" if in_promo else "news",
                moment_at=datetime.now().isoformat(timespec="seconds"), pre_score=pick.hook.total,
                source_url=story.link)
    try:
        (folder / "script.txt").write_text(pick.script + "\n", encoding="utf-8")
        track = choose_music(cfg, folder.name)                        # None = no music
        mp3 = folder / "voiceover.mp3"
        voice = synthesize(cfg, pick.script, mp3)
        dur = probe_duration(cfg, mp3)
        lo, hi = float(cfg.clip["min_seconds"]), float(cfg.clip["max_seconds"]) - 0.6
        if not lo <= dur <= hi:                          # nudge the speaking rate once to land in 30-40 s
            base = _rate_value(str(cfg.news["rate"]))
            new = min(base + 14, 40) if dur > hi else max(base - 8, -20)
            try:
                voice = synthesize(cfg, pick.script, mp3, _rate_text(new))
                dur = probe_duration(cfg, mp3)
            except NewsError:
                log.warning("[%s] could not retime the voiceover; keeping the first take", folder.name)
        if dur > float(cfg.clip["max_seconds"]) - 0.4:
            raise NewsError(f"voiceover is {dur:.1f}s, longer than the {cfg.clip['max_seconds']}s Short limit")
        words, how = get_words(cfg, mp3, pick.script, dur)
        from .pacing import tighten
        words, dur, _saved = tighten(cfg, mp3, words, dur)          # dead-air pauses over 0.7 s are cut down
        (folder / "words.json").write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
        dense = None
        if cfg.news.get("dense"):
            dense, clips, visuals = collect_dense(cfg, pick, folder, dur)
        else:
            clips = collect_clips(cfg, pick, folder, dur)            # now that the length is known
            try:
                visuals = collect_visuals(cfg, story, folder, pick.subjects[0] if pick.subjects else None)
            except NewsError:
                if len(clips) < 3:                        # too few pictures only matters without enough video
                    raise
                visuals = []
        # a dense photo Short shows pictures and frames only (its clips are listed for their credit)
        shown = ([] if dense == "photo" else [c["file"] for c in clips]) + [v["file"] for v in visuals]
        log.info("[%s] news visuals: %d video clip(s) (%s), %d picture(s)", folder.name, len(clips),
                 ", ".join(sorted({c["source"] for c in clips})) or "none", len(visuals))

        info = {"hook": pick.hook.hook, "headline": pick.headline, "kind": pick.kind, "outlet": story.outlet,
                "title": pick.title, "images": shown, "music": track, "lang": "en",
                "bucket": pick.bucket, "subjects": pick.subjects, "dense": dense}
        render = render_news_video(cfg, folder, info, words, dur)

        credit = credit_for_story(story)
        used_music = render.get("music")                              # what the renderer REALLY used (it may have dropped it)
        trend = bool(story.trend)
        link = clean_link(story.link)
        origin = (f"Inspired by a trending topic (rewritten from scratch): {link}"
                  if trend else f"Read the original story: {link}")
        credit_lines = [pick.description, origin]
        video_credits = list(dict.fromkeys(c["credit"] for c in clips))
        credit = "; ".join([credit, *video_credits])                 # every video source is on the credit line itself
        if any(v["kind"] == "article" for v in visuals):
            credit_lines.append(f"Images: {story.outlet} (article pictures)")
        if any(v["kind"] == "topic" for v in visuals):
            credit_lines.append("Game screenshots: web image search and Wikimedia Commons; artwork belongs to its "
                                "developers and publishers")
        if used_music and used_music.get("attribution"):
            credit_lines.append(used_music["attribution"])          # the music license's required credit, automatically
        analysis = {
            "post": True, "status": "edited", "autopost": False, "needs_human_review": pick.sensitive,
            "score": pick.score, "banger": pick.score >= 9, "sensitive": pick.sensitive,
            "sensitive_reason": pick.sensitive_reason, "reason": pick.reason, "start": 0.0, "end": round(dur, 2),
            "title": pick.title, "hook": pick.hook.hook.upper(), "meme_caption": [], "tags": pick.tags,
            "hashtags": pick.hashtags, "description": pick.description,
            "description_full": "\n".join([*credit_lines, credit]),
            "attribution": origin, "credit_line": credit,
            "trend": ({"source": story.trend.get("source"), "signal": story.trend.get("signal"), "core_fact": pick.core_fact,
                       "origin_url": story.link} if trend else None),
            "news": {"url": story.link, "outlet": story.outlet, "headline": story.title, "kind": pick.kind,
                     "published": story.published.isoformat(timespec="minutes"), "script": pick.script, "bucket": pick.bucket,
                     "hooks": [{"hook": h.hook, "score": h.total, "valid": h.valid, "parts": h.parts,
                                "reasons": h.reasons} for h in pick.hooks],
                     "voice": voice, "word_timestamps": how,
                     "videos": [{"url": c["url"], "source": c["source"]} for c in clips],
                     "images": [{"url": v["url"], "source": v["source"], "kind": v["kind"]}
                                for v in visuals]},
            "music": ({"title": used_music["title"], "file": used_music["file"], "attribution": used_music.get("attribution"),
                       "artist": (track or {}).get("artist"), "license": (track or {}).get("license")}
                      if used_music else None),
            "render": render,
            "watchlist": ({**story.watch, "hook_pattern_used": pick.hook_pattern} if story.watch else None)}
        (folder / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
        db.record_analysis(cfg.database, folder.name, analysis, folder / "analysis.json")
        db.record_files(cfg.database, folder.name, folder)
        db.set_clip_status(cfg.database, folder.name, "rendered")
        qa.record(cfg, folder, analysis["description_full"], source_notes)
        try:                                                  # a warning tag for the reviewer; never skips a story
            from .videosearch import mature_flag
            flag = mature_flag(list(pick.subjects), float(cfg.news["image_timeout"]))
            db.set_mature_flag(cfg.database, folder.name, flag)
            if flag and flag != "unchecked":
                log.warning("[%s] Steam flags gore or sexual content: %s (check before approving)", folder.name, flag)
        except Exception:
            log.exception("[%s] could not check the Steam content flags (ignored)", folder.name)
        if pick.hook_pattern:
            try:
                db.set_hook(cfg.database, folder.name, pick.hook_pattern)
                hookbank.record_use(cfg, pick.hook_pattern)
            except Exception:
                log.exception("[%s] could not count the hook pattern (ignored)", folder.name)
        try:
            if in_promo:
                raise _Skip
            from .imagesearch import hashes_of
            db.add_used_pictures(cfg.database, hashes_of([v["file"] for v in visuals]))
        except _Skip:
            pass
        except Exception:
            log.exception("[%s] could not remember its pictures (ignored)", folder.name)
        try:
            if not in_promo and clips:
                from .videosearch import window_prints
                db.add_used_clips(cfg.database, [p for c in clips for p in window_prints(c["file"])])
        except Exception:
            log.exception("[%s] could not remember its footage (ignored)", folder.name)
        try:
            from . import topics
            if not in_promo:                                 # promo never blocks a game for the other commands
                topics.remember(cfg, pick)                                   # so this game is not repeated for 24 h
        except Exception:
            log.exception("[%s] could not remember the topic (ignored)", folder.name)
        log.info("[%s] NEWS Short ready: %s | hook score %.1f | %.1fs | timestamps: %s | source: %s",
                 folder.name, pick.title, pick.hook.total, dur, how, story.link)
        from .notify import notify_rendered
        from .routing import assign_routes
        notify_rendered(cfg, folder.name)
        assign_routes(cfg)
        return folder
    except Exception as exc:
        try:
            db.set_clip_status(cfg.database, folder.name, "failed", f"news: {exc}"[:300])
        except Exception:
            pass
        raise


# ------------------------------------------------------------------ the whole run
def news_left_today(cfg: Config, now: datetime | None = None) -> int:
    """How many more news Shorts today's quota ([volume] news_per_day, Melbourne day) allows; 0 = full."""
    from .routing import day_start_utc
    cap = int(cfg.volume["news_per_day"])
    if cap <= 0:
        return 10 ** 6
    made = db.count_accepted_since(cfg.database, day_start_utc(cfg, now), news=True)
    return max(cap - made, 0)


def run_news(cfg: Config, hours: float | None = None, count: int | None = None, dry_run: bool = False,
             quota: bool = False, trends: bool = True, only: str | None = None, research: bool = False,
             no_promo: bool = False, dense: dict | None = None) -> NewsResult:
    """The RSS news Shorts (see _run_stories), then - when [trends] is enabled - the trend-discovery Shorts, which are
    paraphrased from scratch and may not repeat a game already picked in this run. Never raises.

    only="gta" / "creators": that theme's stories from the RSS feeds only (see MODES), looking back [news]
    gta_hours / creators_hours, without trend Shorts.

    research=True (the command line) first reads config/watchlist.json (metadata only; never for promo runs)."""
    if no_promo or dense:                                 # for this run only, trend Shorts included
        saved = dict(cfg.raw.get("news", {}))
        cfg.raw["news"] = {**saved, "no_promo": no_promo, "dense": dense}
        try:
            return run_news(cfg, hours, count, dry_run, quota, trends and not no_promo, only, research)
        finally:
            cfg.raw["news"] = saved
    if only:
        before = dict(cfg.raw.get("news", {}))
        cfg.raw["news"] = {**before, "only": only}
        hours = hours if hours is not None else float(cfg.news.get(f"{only}_hours", 48))
        trends = False
        try:
            return _run_stories(cfg, hours, count, dry_run, quota, research)
        finally:
            cfg.raw["news"] = before
    result = _run_stories(cfg, hours, count, dry_run, quota, research)
    try:
        from . import topics
        from .trends import run_trends, settings as trend_settings
        if trends and trend_settings(cfg)["enabled"] and not result.note.startswith("run failed"):
            per = int(trend_settings(cfg)["per_run"])
            if quota:
                per = min(per, news_left_today(cfg))
            if per > 0:
                taken = {k for p in result.picked for k in topics.pick_topics(p.subjects)}
                extra = run_trends(cfg, per, dry_run, blocked=taken)
                result.picked += extra.picked
                result.made += extra.made
                result.failed += extra.failed
                result.note = result.note or extra.note
    except Exception:
        log.exception("trend discovery failed (ignored)")
    return result


def _run_stories(cfg: Config, hours: float | None = None, count: int | None = None, dry_run: bool = False,
                 quota: bool = False, research: bool = False) -> NewsResult:
    """Fetch -> pick -> script -> voice -> render for the top stories. Never raises.

    quota=True (the scheduled runs) also stops at today's [volume] news_per_day; a run you start by hand does not."""
    result = NewsResult()
    try:
        if cfg.raw.get("general", {}).get("autopost", False):
            log.warning("config has autopost=true; ignored - this tool never uploads")
        db.init_db(cfg.database)
        cfg_news = dict(cfg.raw.get("news", {}))
        want = int(count if count is not None else cfg.news["stories"])
        if quota:
            left = news_left_today(cfg)
            if left <= 0:
                result.note = f"today's news quota ({cfg.volume['news_per_day']}) is already made"
                log.info("news: %s", result.note)
                return result
            want = min(want, left)
        cfg.raw["news"] = {**cfg_news, "stories": want}
        only, found = cfg.news.get("only"), []
        research = research and only != "promo"
        if research:
            try:
                found = watchlist.scan(cfg, only)
                log.info("watchlist: %d outlier topic(s)", len(found))
            except Exception:
                log.exception("watchlist scan failed (ignored)")
        stories = fetch_stories(cfg, hours, outliers=found) if found else fetch_stories(cfg, hours)
        if not stories:
            result.note = f"no fresh stories in the last {hours or cfg.news['hours']:g}h (or all already used)"
            log.info("news: %s", result.note)
            return result
        from .topics import cap_hot_topics
        if not cfg.news.get("only"):
            stories = cap_hot_topics(stories)                          # a topic flooding the feeds (GTA 6) shows at most 2 stories
        for i, s in enumerate(stories, start=1):
            s.id = i
        if research:
            try:
                watchlist.annotate(cfg, stories, found, only)
            except Exception:
                log.exception("watchlist pairing failed (ignored)")
        log.info("news: %d candidate stories", len(stories))
        picks = pick_stories(cfg, stories, int(cfg.news["stories"]), cfg.data_dir / "news_work")
        result.picked = picks
        if not picks:
            result.note = "no story worth making this time"
        if dry_run:
            return result
        for pick in picks:
            try:
                result.made.append((pick, make_short(cfg, pick)))
            except Exception as exc:
                log.exception("news: story %s failed", pick.story.link)
                result.failed.append((pick, str(exc)))
    except Exception as exc:
        log.exception("news run failed")
        result.note = f"run failed: {exc}"
    return result

