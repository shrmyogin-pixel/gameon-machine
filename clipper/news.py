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

from . import db
from .config import PROJECT_ROOT, Config, get_env_key
from .hookscore import HookScore, banned_in, rank_hooks, words_of
from .lifecycle import story_keys, url_key
from .newsimage import fetch_news_images, images_from_entry
from .profanity import mask

log = logging.getLogger("clipper")

USER_AGENT = "gameon-clipper/1.0"
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
FALLBACK_VOICES = ("en-US-BrianNeural", "en-US-GuyNeural", "en-US-AriaNeural")   # all neutral, tried in order
TTS_DEADLINE_SECONDS = 150
HARD_SKIP = re.compile(r"\b(casino|casinos|sportsbook|betting|gambling|gamble|bet365|online slots|poker site)\b", re.I)
KINDS = {"news": "BREAKING NEWS", "free_offer": "FREE GAME OFFER"}


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


def fetch_stories(cfg: Config, hours: float | None = None, now: datetime | None = None) -> list[Story]:
    """Fresh, de-duplicated, not-yet-used stories from every feed, newest first, with ids 1..n."""
    import feedparser
    now = now or datetime.now(timezone.utc)
    hours = float(hours if hours is not None else cfg.news["hours"])
    cutoff = now - timedelta(hours=hours)
    seen_urls = db.news_urls_seen(cfg.database) if cfg.database.exists() else set()
    seen_keys = db.seen_story_keys(cfg.database) if cfg.database.exists() else set()   # rejected / uploaded / cleaned
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
        if HARD_SKIP.search(s.title + " " + s.summary):
            log.info("news: skipped (gambling/betting): %s", s.title[:80])
            continue
        links.add(s.link)
        titles.add(key)
        unique.append(s)
    unique = unique[:int(cfg.news["max_candidates"])]
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
Choose the {n} best stories to turn into 35-second Shorts. Prefer, in order: breaking gaming news and official reveals; free game offers (Epic, Steam, PS Plus, Game Pass, Prime Gaming ...) that are live right now; confirmed leaks (labelled as leaks); big patch or update news. Pick different games, never two stories about the same thing, and never an event the channel already covered (listed above, if any), even when a different outlet reports it. Skip real-money gambling or betting, hate or harassment, anything about minors, graphic violence, and pure opinion / guide / review pieces.
Use ONLY facts found in the headline and summary. Never invent dates, prices, names or details; if a detail (such as an end date) is not given, leave it out. Say "reportedly", "leak" or "rumour" for anything unconfirmed.

FOR EACH STORY write:
- script_body: the spoken script AFTER the hook, {bmin}-{bmax} words. Short sentences, active voice, plain text only (no emoji, no hashtags, no stage directions, no markdown): core facts, then one line on why it matters, then one short closing line. Do not repeat the hook. No profanity.
- hooks: exactly 5 different hooks, each 5-12 words (never more than 12). Each must name the game, platform or store inside its FIRST 6 WORDS and carry urgency or the news itself in those same first words (free, today, now, leak, confirmed, officially, delayed ...). Use 5 different angles. No banned filler.
- subjects: the game / platform / store names exactly as they appear in your hooks (1-4 short strings).
- headline: the on-screen headline card, max 70 characters, no emoji, no profanity.
- title: video title, 25-60 characters, game or platform within its first 4 words, no emoji.
- description: one line, max 90 characters.
- tags: 8-15 search tags. hashtags: 3-6 hashtags that start with #.
- kind: "news" or "free_offer".  score: 0-10, how strong this story is for the channel today.
- sensitive: true only when a human should look carefully (add sensitive_reason), else false.
- reason: one sentence on why you chose it.

Reply with STRICT JSON only (no prose, no code fences):
{"stories": [{"id": 3, "kind": "news", "score": 8, "subjects": ["..."], "hooks": ["", "", "", "", ""], "script_body": "...", "headline": "...", "title": "...", "description": "...", "tags": ["..."], "hashtags": ["#..."], "sensitive": false, "sensitive_reason": "", "reason": "..."}]}"""

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
    return "\nALREADY MADE IN THE LAST 24 HOURS (do not pick these events again):\n" + "\n".join(f"- {t}" for t in titles[:30]) + "\n"


def build_prompt(cfg: Config, stories: list[Story], n: int, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    voice = (PROJECT_ROOT / "voice.md").read_text(encoding="utf-8")
    rows = "\n".join(f"{s.id} | {s.outlet} | {age_text(s.published, now)} ago | {s.title} | {s.summary or '(no summary)'}"
                     for s in stories)
    lo, hi = int(cfg.news["script_min_words"]), int(cfg.news["script_max_words"])
    covered = covered_block(cfg, now)
    text = PROMPT
    for key in ("voice", "rows", "covered"):                    # their text is inserted last, never re-expanded
        text = text.replace("{" + key + "}", "\0" + key + "\0")
    for key, val in {"hours": cfg.news["hours"], "n": n, "bmin": lo - 4, "bmax": hi - 12}.items():
        text = text.replace("{" + key + "}", str(val))
    return text.replace("\0voice\0", voice).replace("\0rows\0", rows).replace("\0covered\0", covered)


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


def final_script(hook: str, body: str) -> str:
    hook = hook.strip()
    if hook[-1] not in ".!?":
        hook += "."
    return f"{hook} {body.strip()}"


def validate_reply(reply: str, stories: list[Story], cfg: Config) -> list[Pick]:
    """Strict checks on Claude's reply; returns the picks with their best hook chosen. Raises NewsError."""
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
    picks, used = [], set()
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
            subjects = _list(d, "subjects", 1, 4)
            hooks_raw = _list(d, "hooks", 5, 5)
            body = _text(d, "script_body", 200, 900)
            if bad := banned_in(body):
                raise NewsError("script_body uses banned filler: " + ", ".join(bad))
            ranked = rank_hooks(hooks_raw, subjects)
            body_words = len(body.split())
            chosen = next((h for h in ranked if h.valid and lo <= h.words + body_words <= hi), None)
            if chosen is None:
                if not any(h.valid for h in ranked):
                    raise NewsError("no valid hook: " + "; ".join(f"'{h.hook[:30]}' {', '.join(h.reasons)}" for h in ranked))
                raise NewsError(f"hook + script_body must total {lo}-{hi} words; script_body has {body_words}, "
                                f"hooks have {min(h.words for h in ranked)}-{max(h.words for h in ranked)}")
            pick = Pick(
                story=by_id[sid], kind=kind, score=float(score), subjects=subjects, hooks=ranked, hook=chosen,
                body=body, script=final_script(chosen.hook, body), headline=_text(d, "headline", 8, 70),
                title=_text(d, "title", 10, 60), description=mask(_text(d, "description", 5, 200)).splitlines()[0],
                tags=_list(d, "tags", 8, 15),
                hashtags=["#" + h.lstrip("#").replace(" ", "") for h in _list(d, "hashtags", 0, 30)],
                sensitive=d["sensitive"], sensitive_reason=mask(str(d.get("sensitive_reason") or "")),
                reason=mask(str(d.get("reason") or "")))
            if len(pick.description) > 90:
                raise NewsError("'description' is longer than 90 characters")
            if pick.sensitive and not pick.sensitive_reason:
                raise NewsError("sensitive=true needs a sensitive_reason")
        except NewsError as exc:
            raise NewsError(tag + str(exc))
        picks.append(pick)
    return picks


def pick_stories(cfg: Config, stories: list[Story], n: int, work_dir: Path) -> list[Pick]:
    """One headless Claude call (with retries on a bad reply). Raises NewsError."""
    from .editor import EditorError, call_claude
    work_dir.mkdir(parents=True, exist_ok=True)
    base = build_prompt(cfg, stories, n)
    prompt, last = base, "unknown"
    for attempt in range(1, int(cfg.ai["retries"]) + 2):
        try:
            return validate_reply(call_claude(cfg, prompt, work_dir), stories, cfg)
        except (NewsError, EditorError) as exc:
            last = str(exc)
            log.warning("news: Claude attempt %d failed: %s", attempt, last)
            prompt = base + RETRY_NOTE.format(why=last)
    raise NewsError(last)


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
    return f"Sources & Credits: {story.outlet}, {story.link}"


def collect_visuals(cfg: Config, story: Story, folder: Path) -> list[dict]:
    """3-5 pictures for the story's cuts: the article's own, topped up to at least images_min with meme cards.

    -> [{'file', 'url', 'source', 'kind': 'article' | 'meme'}]. Never raises; [] means the gradient background."""
    if not cfg.news["images"]:
        return []
    try:
        found = fetch_news_images(story, folder, float(cfg.news["image_timeout"]), int(cfg.news["images_max"]))
    except Exception:
        log.exception("news: picture hunt failed (ignored)")
        found = []
    visuals = [{**f, "kind": "article"} for f in found]
    need = int(cfg.news["images_min"]) - len(visuals)
    if need > 0:
        from .memes import pick_memes
        memes = [{"file": m, "url": None, "source": "assets/memes", "kind": "meme"}
                 for m in pick_memes(cfg, need, story.link)]
        # interleave: article, meme, article, meme ... so the cuts alternate instead of ending on memes
        mixed, a, m = [], list(visuals), list(memes)
        while a or m:
            if a:
                mixed.append(a.pop(0))
            if m:
                mixed.append(m.pop(0))
        visuals = mixed
    log.info("[%s] news visuals: %s", folder.name,
             ", ".join(f"{v['kind']}:{v['source']}" for v in visuals) or "none (gradient)")
    return visuals


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
    folder = _new_folder(cfg, story.outlet)
    db.add_clip(cfg.database, "news", story.outlet, folder.name, stream=story.link, lang="en", moment_kind="news",
                moment_at=datetime.now().isoformat(timespec="seconds"), pre_score=pick.hook.total,
                source_url=story.link)
    try:
        (folder / "script.txt").write_text(pick.script + "\n", encoding="utf-8")
        visuals = collect_visuals(cfg, story, folder)                 # [] = gradient background
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
        (folder / "words.json").write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")

        info = {"hook": pick.hook.hook, "headline": pick.headline, "kind": pick.kind, "outlet": story.outlet,
                "title": pick.title, "images": [v["file"] for v in visuals], "music": track}
        render = render_news_video(cfg, folder, info, words, dur)

        credit = credit_for_story(story)
        used_picture = render.get("background") == "picture"
        used_music = render.get("music")                              # what the renderer REALLY used (it may have dropped it)
        credit_lines = [pick.description, f"Read the original story: {story.link}"]
        if used_picture and any(v["kind"] == "article" for v in visuals):
            credit_lines.append(f"Images: {story.outlet} (article pictures)")
        if used_music and used_music.get("attribution"):
            credit_lines.append(used_music["attribution"])          # the music license's required credit, automatically
        analysis = {
            "post": True, "status": "edited", "autopost": False, "needs_human_review": pick.sensitive,
            "score": pick.score, "banger": pick.score >= 9, "sensitive": pick.sensitive,
            "sensitive_reason": pick.sensitive_reason, "reason": pick.reason, "start": 0.0, "end": round(dur, 2),
            "title": pick.title, "hook": pick.hook.hook.upper(), "meme_caption": [], "tags": pick.tags,
            "hashtags": pick.hashtags, "description": pick.description,
            "description_full": "\n".join([*credit_lines, credit]),
            "attribution": f"Read the original story: {story.link}", "credit_line": credit,
            "news": {"url": story.link, "outlet": story.outlet, "headline": story.title, "kind": pick.kind,
                     "published": story.published.isoformat(timespec="minutes"), "script": pick.script,
                     "hooks": [{"hook": h.hook, "score": h.total, "valid": h.valid, "parts": h.parts,
                                "reasons": h.reasons} for h in pick.hooks],
                     "voice": voice, "word_timestamps": how,
                     "images": [{"url": v["url"], "source": v["source"], "kind": v["kind"], "used": used_picture}
                                for v in visuals]},
            "music": ({"title": used_music["title"], "file": used_music["file"], "attribution": used_music.get("attribution"),
                       "artist": (track or {}).get("artist"), "license": (track or {}).get("license")}
                      if used_music else None),
            "render": render}
        (folder / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
        db.record_analysis(cfg.database, folder.name, analysis, folder / "analysis.json")
        db.record_files(cfg.database, folder.name, folder)
        db.set_clip_status(cfg.database, folder.name, "rendered")
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
             quota: bool = False) -> NewsResult:
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
        stories = fetch_stories(cfg, hours)
        if not stories:
            result.note = f"no fresh stories in the last {hours or cfg.news['hours']:g}h (or all already used)"
            log.info("news: %s", result.note)
            return result
        log.info("news: %d candidate stories", len(stories))
        picks = pick_stories(cfg, stories, int(cfg.news["stories"]), cfg.data_dir / "news_work")
        result.picked = picks
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


# ------------------------------------------------------------------ scheduling inside `run`
def due_run(now_local: datetime, run_times: list[str], done: set[str], grace_hours: float = 3.0) -> str | None:
    """Key of a scheduled news run that is due now (not done yet, at most `grace_hours` late), else None."""
    for t in sorted(run_times):
        try:
            hh, mm = (int(x) for x in str(t).split(":"))
        except ValueError:
            continue
        at = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        key = f"{now_local:%Y-%m-%d} {t}"
        if key not in done and at <= now_local < at + timedelta(hours=grace_hours):
            return key
    return None


class NewsScheduler(threading.Thread):
    """Runs `run_news` at the [news] run_times (local time) while `python -m clipper run` is up."""

    def __init__(self, cfg: Config, stop: threading.Event, runner=None):
        super().__init__(name="news-scheduler", daemon=True)
        self.cfg, self.stop_event, self.done = cfg, stop, set()
        self.runner = runner or (lambda c: run_news(c, quota=True))
        self.tz = ZoneInfo(cfg.raw.get("general", {}).get("timezone", "Australia/Melbourne"))

    def run(self) -> None:
        times = list(self.cfg.news["run_times"])
        log.info("news scheduler on: %s", ", ".join(times))
        while not self.stop_event.is_set():
            try:
                key = due_run(datetime.now(self.tz), times, self.done)
                if key:
                    self.done.add(key)
                    log.info("news: scheduled run %s", key)
                    res = self.runner(self.cfg)
                    log.info("news: scheduled run finished: %d made, %d failed%s", len(res.made), len(res.failed),
                             f" ({res.note})" if res.note else "")
            except Exception:
                log.exception("news scheduler error; will keep going")
            self.stop_event.wait(30)
