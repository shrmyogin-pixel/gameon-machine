"""The shared video B-roll fetcher: up to 15 short, high-action clips for every news Short (news, gta, creators, tech, promo).

Sources, in this order, until there are enough clips:
  1. assets/gameplay/<game-slug>/   your own recordings
  2. YouTube via yt-dlp             official trailers, gameplay, creator clips and reveals from VERIFIED channels only
  3. Steam store trailers           the publisher's official promotional videos (public store API, no key)
  4. Wikimedia Commons              freely licensed videos, credited with author and license

Nothing is signed in to or unlocked: age-restricted, private and live videos are left alone. Source audio is dropped,
every source is credited, and a downloaded video is deleted as soon as its clips are cut. Everything is best-effort:
any failure returns fewer clips (the Short then uses pictures).
"""
from __future__ import annotations

import html
import json
import logging
import re
import shutil
import urllib.parse
from pathlib import Path

from . import creators, newsimage
from .imagesearch import topic_slug, topic_tokens
from .render import ENCODER_ARGS, _slot, pick_encoder
from .topics import topic_words
from .worker import ClipError, _ffmpeg, probe_duration

log = logging.getLogger("clipper")

CLIP_SECONDS = 4.5                 # 3-4 s on screen plus the transition into the next visual
INTRO_SKIP, OUTRO_SKIP = 5.0, 10.0  # studio logos at the start, end cards at the end
PER_VIDEO = 4                      # clips from one source video, so a Short shows at least two when it can
MIN_MOTION = 4.0                   # mean pixel change between samples; below this it is a still card or menu
EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
ALLOWED_HOSTS = (".steamstatic.com", "upload.wikimedia.org")
BLOCKED_DESCRIPTORS = {2, 3, 4}    # Steam: frequent violence / gore, adult-only sexual content, frequent nudity
WARN_DESCRIPTORS = BLOCKED_DESCRIPTORS | {1}      # dashboard warning: also "some nudity or sexual content"
STEAM = "https://store.steampowered.com/api/"
TRAILERS_PER_GAME = 4
YT_RESULTS, YT_DOWNLOADS = 8, 4    # search hits looked at per query; videos downloaded per Short
YT_MIN_S, YT_MAX_S = 20, 1200
YT_MAX_BYTES = 300_000_000
# not footage of the thing itself, or not fit for the channel
YT_UNWANTED = re.compile(r"\b(react(?:s|ion|ing)?|concept|fan[- ]?made|fake|parody|podcast|leak(?:s|ed)?|mods?|modded|"
                         r"review|explained|theor(?:y|ies)|cheats?|hacks?|gore|brutal|fatalit(?:y|ies)|18\+|nsfw|"
                         r"casino|gambling|betting)\b", re.I)


def _json(url: str, timeout: float):
    raw, _ = newsimage.download_retry(url, 3_000_000, timeout, tries=2)
    return json.loads(raw.decode("utf-8", "replace"))


def _allowed(url: str) -> bool:
    parts = urllib.parse.urlparse(url or "")
    host = parts.hostname or ""
    return parts.scheme == "https" and any(host == h.lstrip(".") or host.endswith("." + h.lstrip(".")) for h in ALLOWED_HOSTS)


def _name(text: str) -> str:
    """Lower-case words only, with the usual spellings of one name made equal."""
    text = (text or "").lower().replace("’", "").replace("'", "")
    name = " ".join(re.findall(r"[a-z0-9]+", text))
    name = re.sub(r"\bgrand theft auto\b", "gta", name)
    return re.sub(r"\bgta vi\b", "gta 6", re.sub(r"\bgta v\b", "gta 5", name))


def same_game(topic: str, store_name: str) -> bool:
    """Footage of the wrong game is worse than a picture: the store entry must be the game itself (or, for a name of
    two or more words, that game with a subtitle). A number right after it means a different sequel."""
    t, n = _name(topic), _name(store_name)
    if not t or re.search(r"\b(soundtrack|demo|playtest|dlc|season pass|pack|artbook|server)\b", n):
        return False
    if t == n:
        return True
    rest = n[len(t) + 1:] if n.startswith(t + " ") else ""
    return len(t.split()) >= 2 and bool(rest) and not re.match(r"(\d+|[ivx]+)\b", rest)


def title_matches(topic: str, title: str, channel: str = "") -> bool:
    """A video counts when its title (or channel) names every distinctive word of the topic, and not a later sequel."""
    tokens = topic_tokens(_name(topic))
    text = _name(f"{title} {channel}")
    if not tokens or not all(re.search(rf"\b{re.escape(t)}\b", text) for t in tokens):
        return False
    return not re.search(rf"\b{re.escape(_name(topic))} (?:\d{{1,2}}|ii|iii|iv|v|vi)\b", _name(title))


# ------------------------------------------------------------------ sources
def own_sources(topic: str, root: Path) -> list[dict]:
    folder = root / topic_slug(topic)
    if not folder.is_dir():
        return []
    return [{"url": str(f), "source": "own", "credit": f"Video: own recording ({f.name})"}
            for f in sorted(folder.iterdir()) if f.suffix.lower() in EXTENSIONS]


def queries(topic: str, kind: str) -> list[str]:
    if kind == "creator":
        return [f"{topic} stream highlights", f"{topic} official"]
    if kind in ("tech", "pick"):
        return [f"{topic} official reveal", f"{topic} official trailer"]
    return [f"{topic} official trailer", f"{topic} gameplay"]


def usable_hit(topic: str, entry: dict, exclude: str = "") -> bool:
    title, channel = str(entry.get("title") or ""), str(entry.get("channel") or "")
    duration = entry.get("duration") or 0
    text = f"{title} {channel}"
    return bool(entry.get("id") and entry.get("channel_is_verified")
                and entry.get("live_status") not in ("is_live", "is_upcoming")
                and YT_MIN_S <= duration <= YT_MAX_S
                and entry["id"] not in (exclude or "")
                and title_matches(topic, title, channel)
                and not YT_UNWANTED.search(title)
                and not newsimage.GAMBLING_SOURCE.search(text)
                and not creators.UNSUITABLE.search(title))


class _Quiet:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): log.debug("yt-dlp: %s", str(msg)[:160])


def _ydl(options: dict):
    import yt_dlp
    return yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "logger": _Quiet(), "noplaylist": True,
                             "socket_timeout": 20, **options})


def youtube_sources(topic: str, kind: str, exclude: str = "") -> list[dict]:
    """Verified-channel search hits, official ones first. [] on any failure."""
    hits: dict[str, dict] = {}
    for query in queries(topic, kind):
        try:
            with _ydl({"extract_flat": True}) as ydl:
                found = ydl.extract_info(f"ytsearch{YT_RESULTS}:{query}", download=False)
        except Exception as exc:
            log.info("video search: youtube failed for %r (%s: %s)", query, type(exc).__name__, str(exc)[:100])
            continue
        for entry in (found or {}).get("entries") or []:
            if entry and usable_hit(topic, entry, exclude):
                hits.setdefault(entry["id"], entry)
        if len(hits) >= YT_DOWNLOADS * 2:
            break
    ranked = sorted(hits.values(), key=lambda e: not re.search(r"\b(official|trailer|reveal)\b", e["title"], re.I))
    return [{"url": f"https://www.youtube.com/watch?v={e['id']}", "source": "youtube",
             "credit": f"Video: {' '.join(str(e['channel']).split())} (YouTube) https://youtu.be/{e['id']}"}
            for e in ranked]


def download_youtube(url: str, folder: Path) -> Path | None:
    """The video stream only (no audio), 720p-1080p, into `folder`. None when it cannot be had without signing in."""
    options = {"format": "bv*[height<=1080][height>=720][vcodec^=avc1]/bv*[height<=1080][height>=720][vcodec^=vp9]",
               "outtmpl": str(folder / "source_%(id)s.%(ext)s"), "max_filesize": YT_MAX_BYTES, "retries": 2}
    try:
        with _ydl(options) as ydl:
            info = ydl.extract_info(url, download=True)
        path = Path(info["requested_downloads"][0]["filepath"])
        return path if path.is_file() else None
    except Exception as exc:
        log.info("video search: could not download %s (%s: %s)", url, type(exc).__name__, str(exc)[:120])
        return None


def steam_app(topic: str, timeout: float) -> tuple[int, str, dict] | None:
    """(app id, store name, details) of the game itself, None when the store has no such game. Raises on a failed lookup."""
    found = _json(STEAM + "storesearch/?" + urllib.parse.urlencode({"term": topic, "l": "english", "cc": "US"}), timeout)
    app = next((i for i in found.get("items", []) if i.get("type") == "app" and same_game(topic, i.get("name", ""))), None)
    if not app:
        return None
    appid = int(app["id"])
    return appid, app["name"], _json(STEAM + f"appdetails?appids={appid}&l=english&cc=US", timeout)[str(appid)]["data"]


def _descriptors(data: dict) -> set:
    return set((data.get("content_descriptors") or {}).get("ids") or [])


def mature_flag(subjects: list[str], timeout: float = 15.0) -> str:
    """For the review dashboard only (nothing is skipped): the story's games Steam flags for gore or sexual content,
    comma separated. '' = none flagged (or not sold on Steam); 'unchecked' = Steam could not be asked."""
    flagged, failed = [], False
    for topic in [t.strip() for t in subjects if t and topic_words(t)]:
        try:
            app = steam_app(topic, timeout)
        except Exception as exc:
            log.info("steam flags: lookup failed for %r (%s: %s)", topic, type(exc).__name__, str(exc)[:100])
            failed = True
            continue
        if app and WARN_DESCRIPTORS & _descriptors(app[2]):
            flagged.append(app[1])
    return ", ".join(dict.fromkeys(flagged)) or ("unchecked" if failed else "")


def steam_sources(topic: str, timeout: float) -> list[dict]:
    try:
        app = steam_app(topic, timeout)
        if not app:
            return []
        appid, name, data = app
        if BLOCKED_DESCRIPTORS & _descriptors(data):
            log.info("video search: %s trailers skipped (store marks gore or sexual content)", name)
            return []
        credit = (f"Video: official {data.get('name', name)} trailers (c) "
                  f"{', '.join(data.get('publishers') or ['the publisher'])}, https://store.steampowered.com/app/{appid}/")
        return [{"url": m["hls_h264"], "source": "steam", "credit": credit}
                for m in (data.get("movies") or [])[:TRAILERS_PER_GAME] if _allowed(m.get("hls_h264", ""))]
    except Exception as exc:
        log.info("video search: steam failed for %r (%s: %s)", topic, type(exc).__name__, str(exc)[:100])
        return []


def commons_sources(topic: str, timeout: float) -> list[dict]:
    params = {"action": "query", "format": "json", "generator": "search", "gsrnamespace": "6", "gsrlimit": "10",
              "gsrsearch": f"{topic} filetype:video", "prop": "videoinfo", "viprop": "url|size|extmetadata"}
    try:
        data = _json("https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params), timeout)
        out = []
        for page in ((data.get("query") or {}).get("pages") or {}).values():
            info = (page.get("videoinfo") or [{}])[0]
            url = str(info.get("url", "")).split("?")[0]
            if int(info.get("height") or 0) < 720 or int(info.get("size") or 0) > 200_000_000:
                continue
            if not title_matches(topic, page.get("title", "")) or not _allowed(url):
                continue
            meta = info.get("extmetadata") or {}

            def field(key: str, fallback: str) -> str:
                value = re.sub(r"<[^>]+>", "", html.unescape(str((meta.get(key) or {}).get("value", ""))))
                return " ".join(value.split())[:80] or fallback
            credit = (f"Video: {field('Artist', 'Wikimedia Commons contributor')}, {field('LicenseShortName', 'see file page')}, "
                      f"{info.get('descriptionurl', url)}")
            out.append({"url": url, "source": "commons", "credit": credit})
        return out[:3]
    except Exception as exc:
        log.info("video search: commons failed for %r (%s: %s)", topic, type(exc).__name__, str(exc)[:100])
        return []


# ------------------------------------------------------------------ choosing and cutting
def start_times(duration: float, n: int) -> list[float]:
    """Up to `n` non-overlapping clip starts spread over a video, skipping its first 5 s and last 10 s."""
    lo, hi = INTRO_SKIP, duration - OUTRO_SKIP - CLIP_SECONDS
    if hi < lo:
        return []
    n = max(1, min(n, int((hi - lo) // CLIP_SECONDS) + 1))
    step = (hi - lo) / max(n - 1, 1)
    return [round(lo + i * step, 2) for i in range(n)]


def motion_score(path: Path, start: float = 0.0) -> float:
    """How much happens in the window at `start`: mean change between frames 0.6 s apart. 0 for a window that is mostly
    black, flat or unreadable."""
    import cv2
    import numpy as np
    cap = cv2.VideoCapture(str(path))
    try:
        frames = []
        for i in range(6):
            cap.set(cv2.CAP_PROP_POS_MSEC, (start + 0.3 + i * 0.6) * 1000)
            ok, img = cap.read()
            if ok:
                frames.append(cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (160, 90)).astype(np.int16))
    finally:
        cap.release()
    if len(frames) < 4 or sum(f.mean() >= 22 and f.std() >= 18 for f in frames) < len(frames) - 1:
        return 0.0
    return float(np.mean([np.abs(a - b).mean() for a, b in zip(frames, frames[1:])]))


def best_windows(scored: list[tuple[float, float]], n: int) -> list[float]:
    """[(start, motion)] -> the starts of the `n` liveliest windows, in playing order; still ones are never used."""
    top = sorted((s for s in scored if s[1] >= MIN_MOTION), key=lambda s: -s[1])[:max(n, 0)]
    return sorted(start for start, _ in top)


def _cut(cfg, url: str, start: float, dest: Path) -> bool:
    encoder = pick_encoder(cfg)
    try:
        with _slot(cfg):                                   # counts as an encode: [render] max_renders
            _ffmpeg(cfg, ["-ss", f"{start:.2f}", "-i", url, "-t", str(CLIP_SECONDS), "-map", "0:v:0", "-an",
                          "-vf", "scale=-2:'min(1080,ih)',fps=30", "-c:v", encoder, *ENCODER_ARGS[encoder],
                          "-pix_fmt", "yuv420p", dest], timeout=120)
        return dest.is_file()
    except (ClipError, OSError) as exc:
        log.info("video search: could not cut a clip (%s)", str(exc)[:120])
        dest.unlink(missing_ok=True)
        return False


def _remote_duration(cfg, url: str) -> float:
    from .proc import run
    exe = shutil.which(cfg.ffmpeg) or cfg.ffmpeg
    _, out = run([exe, "-hide_banner", "-i", url], timeout=60, low_priority=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if not m:
        raise ClipError("could not read the length of a remote video")
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])


def clips_from(cfg, video: dict, folder: Path, want: int, first_n: int) -> list[dict]:
    """Up to `want` lively clips from one source video, saved as folder/news_video_<n>.mp4."""
    local: Path | None = None
    try:
        if video["source"] == "own":
            local = Path(video["url"])
        elif video["source"] == "youtube":
            local = download_youtube(video["url"], folder)
            if local is None:
                return []
        src = str(local) if local else video["url"]
        duration = probe_duration(cfg, local) if local else _remote_duration(cfg, src)
        made: list[tuple[float, float, Path]] = []        # (start, motion, trial file)
        if local:                                          # score first, cut only the best
            starts = best_windows([(s, motion_score(local, s)) for s in start_times(duration, 16)], want)
        else:                                              # a stream: cut a few more than needed, then keep the best
            starts = start_times(duration, want + 2)
        for i, start in enumerate(starts):
            trial = folder / f"trial_{i}.mp4"
            if _cut(cfg, src, start, trial):
                made.append((start, motion_score(trial), trial))
        keep = set(best_windows([(s, m) for s, m, _ in made], want))
        out = []
        for start, _, trial in made:
            if start in keep:
                dest = folder / f"news_video_{first_n + len(out)}.mp4"
                trial.replace(dest)
                out.append({"file": dest, "url": None if video["source"] == "own" else video["url"],
                            "source": video["source"], "credit": video["credit"]})
            else:
                trial.unlink(missing_ok=True)
        return out
    except Exception as exc:
        log.info("video search: unusable video (%s: %s)", type(exc).__name__, str(exc)[:120])
        return []
    finally:
        for leftover in [*folder.glob("source_*"), *folder.glob("trial_*")]:      # downloads, partial ones too
            leftover.unlink(missing_ok=True)


def fetch_clips(cfg, subjects: list[str], folder: Path, own_root: Path, need: int = 5, timeout: float = 15.0,
                kind: str = "", exclude: str = "", youtube: bool = True) -> list[dict]:
    """Up to `need` clips for the first of `subjects` that has footage. A generic subject ("Steam", "PlayStation") is
    not searched: it would match anything. `exclude` is a URL whose video must not be used.

    -> [{'file', 'url', 'source', 'credit'}]. Never raises."""
    for topic in [t.strip() for t in subjects if t and topic_words(t)]:
        kept: list[dict] = []
        sources = [lambda: own_sources(topic, own_root),
                   lambda: youtube_sources(topic, kind, exclude)[:YT_DOWNLOADS] if youtube else [],
                   lambda: steam_sources(topic, timeout),
                   lambda: commons_sources(topic, timeout)]
        for find in sources:
            for video in find():
                if len(kept) >= need:
                    return kept
                kept += clips_from(cfg, video, folder, min(PER_VIDEO, need - len(kept)), len(kept) + 1)
        if kept:
            return kept
    return []
