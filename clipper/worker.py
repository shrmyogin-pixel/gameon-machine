"""Clipper worker: turns a detector Moment into a clip folder ready for the AI editor.

For each moment (after waiting context_after seconds) it
  1. joins the recorded segments around the spike into source.mp4,
  2. extracts 16 kHz mono audio and runs whisper-cli (word timestamps),
  3. makes frames.jpg (a 4x3 contact sheet with burned-in timestamps),
  4. writes payload.json: metadata + 5-second chat/audio timeline + transcript + frames.

Nothing here posts or uploads anything. Transcript text is never written to the log.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from .chat import safe_handle
from .config import Config
from .db import add_clip, set_clip_status
from .detect import _ChatTail, build_params, compile_words, measure_loudness
from .profanity import mask
from .proc import run

log = logging.getLogger("clipper")

TIMELINE_BUCKET = 5          # seconds per timeline row
FRAME_COLS, FRAME_ROWS = 4, 3
FONT_CANDIDATES = ["arialbd.ttf", "arial.ttf", "segoeui.ttf", "consola.ttf", "verdana.ttf"]


class ClipError(Exception):
    pass


# ------------------------------------------------------------------ ffmpeg helpers
def _ffmpeg(cfg: Config, args: list, timeout: int = 600) -> None:
    exe = shutil.which(cfg.ffmpeg) or cfg.ffmpeg
    low = cfg.raw.get("encoding", {}).get("below_normal_priority", True)
    code, out = run([exe, "-hide_banner", "-loglevel", "error", "-y", *args], timeout=timeout, low_priority=low)
    if code != 0:
        raise ClipError(f"ffmpeg failed ({code}): {out.strip()[-300:]}")


def probe_duration(cfg: Config, path: Path) -> float:
    exe = shutil.which(cfg.ffmpeg) or cfg.ffmpeg
    _, out = run([exe, "-hide_banner", "-i", path], timeout=60)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if not m:
        raise ClipError(f"could not read the length of {path.name}")
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])


def extract_wav(cfg: Config, media: Path, wav: Path) -> None:
    _ffmpeg(cfg, ["-i", media, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav])


def _font_option() -> str:
    fonts = Path("C:/Windows/Fonts")
    for name in FONT_CANDIDATES:
        if (fonts / name).is_file():
            return f"fontfile='C\\:/Windows/Fonts/{name}':"
    return ""


def make_frames(cfg: Config, media: Path, out: Path, duration: float) -> None:
    """4x3 contact sheet: 12 frames evenly spread over the clip, timestamp in each corner."""
    n = FRAME_COLS * FRAME_ROWS
    vf = (f"fps={n}/{max(duration, 1.0):.3f},scale=480:-2,"
          f"drawtext={_font_option()}text='%{{pts\\:hms}}':x=8:y=8:fontsize=26:fontcolor=white:"
          f"box=1:boxcolor=black@0.65:boxborderw=5,tile={FRAME_COLS}x{FRAME_ROWS}")
    _ffmpeg(cfg, ["-i", media, "-an", "-vf", vf, "-frames:v", "1", "-q:v", "3", out])
    if not out.is_file():
        raise ClipError("frames.jpg was not created")


# ------------------------------------------------------------------ whisper
def run_whisper(cfg: Config, wav: Path, out_base: Path, lang: str) -> Path:
    """Runs whisper-cli with -ojf; returns the JSON path (out_base + '.json')."""
    model = cfg.whisper_model_for(lang)
    for need, label in ((cfg.whisper_cli, "whisper-cli"), (model, "whisper model")):
        if not need.is_file():
            raise ClipError(f"{label} not found: {need}")
    code_lang = (lang or "en").lower().split("-")[0]
    cmd = [cfg.whisper_cli, "-m", model, "-f", wav, "-ojf", "-of", out_base, "-l", code_lang, "-np"]
    started = time.monotonic()
    code, out = run(cmd, timeout=1800, low_priority=True)
    result = out_base.with_suffix(".json")
    if code != 0 or not result.is_file():
        raise ClipError(f"whisper-cli failed ({code}): {out.strip()[-300:]}")
    log.info("whisper (%s, %s) finished in %.0fs", model.name, code_lang, time.monotonic() - started)
    return result


def parse_words(whisper_json: Path, duration: float | None = None) -> tuple[str, list[list]]:
    """-> (language, [[start_s, end_s, word], ...]) from whisper.cpp -ojf output.

    Tokens are merged into words (a token starting with a space begins a new word). If a segment
    has broken multi-byte pieces (common for Hindi), its words are spread evenly over the segment."""
    data = json.loads(whisper_json.read_text(encoding="utf-8", errors="replace"))
    language = (data.get("result") or {}).get("language") or (data.get("params") or {}).get("language", "")
    words: list[list] = []
    for seg in data.get("transcription", []):
        toks = [t for t in seg.get("tokens", []) if not str(t.get("text", "")).startswith("[_")]
        seg_from = seg.get("offsets", {}).get("from", 0) / 1000
        seg_to = seg.get("offsets", {}).get("to", 0) / 1000
        if not toks or any("\ufffd" in t.get("text", "") for t in toks):
            parts = (seg.get("text") or "").split()
            step = (seg_to - seg_from) / len(parts) if parts else 0
            for i, w in enumerate(parts):
                words.append([round(seg_from + i * step, 2), round(seg_from + (i + 1) * step, 2), w])
            continue
        cur = None
        for t in toks:
            text = t.get("text", "")
            a, b = t.get("offsets", {}).get("from", 0) / 1000, t.get("offsets", {}).get("to", 0) / 1000
            if cur is None or text[:1].isspace():
                if cur and cur[2].strip():
                    words.append(cur)
                cur = [a, b, text]
            else:
                cur[1], cur[2] = b, cur[2] + text
        if cur and cur[2].strip():
            words.append(cur)
    clean = []
    for a, b, w in words:
        w = w.strip()
        if duration is not None:  # whisper's last timestamps can run slightly past the end
            a, b = min(a, duration), min(b, duration)
        if w:
            clean.append([round(a, 2), round(max(b, a), 2), w])
    return language, clean


# ------------------------------------------------------------------ source.mp4
def segment_spans(session_dir: Path, segment_seconds: float) -> list[tuple[Path, float, float]]:
    """(file, start, end) in wall-clock seconds. A segment's mtime is when it was finished, and it
    starts when the previous one finished."""
    spans, prev_end = [], None
    for seg in sorted(session_dir.glob("seg_*.ts")):
        try:
            end = seg.stat().st_mtime
        except OSError:
            continue
        start = prev_end if prev_end is not None and 0 < end - prev_end <= segment_seconds * 2.5 else end - segment_seconds
        spans.append((seg, start, end))
        prev_end = end
    return spans


def pick_segments(spans, t_from: float, t_to: float):
    return [s for s in spans if s[2] > t_from and s[1] < t_to]


def build_source(cfg: Config, session_dir: Path, folder: Path, spike_ts: float, segment_seconds: float):
    clip = cfg.clip
    chosen = pick_segments(segment_spans(session_dir, segment_seconds),
                           spike_ts - float(clip["context_before"]), spike_ts + float(clip["context_after"]))
    if not chosen:
        raise ClipError("no recorded segments cover this moment (already deleted?)")
    lst = folder / "segments.txt"
    lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p, _, _ in chosen), encoding="utf-8")
    source = folder / "source.mp4"
    try:
        _ffmpeg(cfg, ["-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", "-movflags", "+faststart", source])
    finally:
        lst.unlink(missing_ok=True)
    return source, chosen[0][1], chosen[-1][2]


# ------------------------------------------------------------------ payload
def build_timeline(session_dir: Path, source: Path, cfg: Config, stream: dict, source_start: float,
                   duration: float) -> list[list]:
    """Rows of [t, chat_msgs, unique_chatters, hype_msgs, loud_db_max] per 5 seconds (no chat text)."""
    p = build_params(cfg.raw.get("detect"), stream)
    ignore = {u.lower() for u in p["ignore_users"]}
    pats = compile_words(p["words"])
    n = int(duration // TIMELINE_BUCKET) + 1
    users: list[dict] = [{} for _ in range(n)]
    hype = [0] * n
    for ts, user, text in _ChatTail(session_dir / "chat.jsonl").read():
        i = int((ts - source_start) // TIMELINE_BUCKET)
        user, text = user.lower().strip(), text.strip()
        if not (0 <= i < n) or not text or user in ignore or (p["ignore_prefix"] and text.startswith(p["ignore_prefix"])):
            continue
        if users[i].get(user, 0) >= int(p["max_per_user"]):
            continue
        users[i][user] = users[i].get(user, 0) + 1
        hype[i] += any(pat.search(text) for pat in pats)
    loud = None
    if p["audio_enabled"]:
        res = measure_loudness(cfg.ffmpeg, source)
        loud = res[0] if res else None
    rows = []
    for i in range(n):
        db = None
        if loud:
            chunk = loud[i * TIMELINE_BUCKET:(i + 1) * TIMELINE_BUCKET]
            db = round(max(chunk), 1) if chunk else None
        rows.append([i * TIMELINE_BUCKET, sum(users[i].values()), len(users[i]), hype[i], db])
    return rows


def credit_line(stream: dict) -> str:
    names = {"twitch": "Twitch", "youtube": "YouTube", "kick": "Kick"}
    return f"Sources & Credits: {names.get(stream['platform'], stream['platform'])} - {stream['handle']} ({stream['url']})"


def build_payload(stream: dict, moment, folder: Path, session_dir: Path, source: Path, duration: float,
                  source_start: float, timeline: list[list], language: str, words: list[list], cfg: Config) -> dict:
    return {
        "stream": {"platform": stream["platform"], "handle": stream["handle"], "url": stream["url"],
                   "lang": stream.get("lang", "en"), "credit_line": credit_line(stream),
                   "session": session_dir.name},
        "moment": {"kind": moment.kind, "prescore": moment.prescore,
                   "spike_at_s": round(moment.start_ts - source_start, 1), "numbers": moment.numbers},
        "clip": {"source": source.name, "duration_s": round(duration, 1),
                 "context_before_s": cfg.clip["context_before"], "context_after_s": cfg.clip["context_after"],
                 "starts_at": datetime.fromtimestamp(source_start).isoformat(timespec="seconds")},
        "timeline_bucket_s": TIMELINE_BUCKET,
        "timeline_columns": ["t_s", "chat_msgs", "unique_chatters", "laugh_shock_hype_msgs", "loudest_db"],
        "timeline": timeline,
        "transcript": {"language": language, "words": [[a, b, mask(w)] for a, b, w in words]},
        "frames": {"file": "frames.jpg", "layout": f"{FRAME_COLS}x{FRAME_ROWS}, row by row, 12 frames evenly spaced "
                   "over the clip; the clip time is burned into each frame's top-left corner"},
    }


# ------------------------------------------------------------------ jobs
def _unique_folder(cfg: Config, stream: dict, ts: float) -> Path:
    base = f"{stream['platform']}-{safe_handle(stream['handle'])}-{datetime.fromtimestamp(ts):%Y%m%d-%H%M%S}".lower()
    folder, n = cfg.clips_dir / base, 1
    while folder.exists():
        n += 1
        folder = cfg.clips_dir / f"{base}-{n}"
    folder.mkdir(parents=True)
    return folder


def build_clip(cfg: Config, stream: dict, session_dir: Path, moment, segment_seconds: float = 10) -> Path | None:
    """Full pipeline for one moment. Never raises; returns the clip folder or None."""
    folder = None
    try:
        folder = _unique_folder(cfg, stream, moment.start_ts)
        add_clip(cfg.database, stream["platform"], stream["handle"], folder.name)
        t0 = time.monotonic()
        source, src_start, src_end = build_source(cfg, session_dir, folder, moment.start_ts, segment_seconds)
        duration = probe_duration(cfg, source)
        wav = folder / "audio.wav"
        extract_wav(cfg, source, wav)
        words_json = run_whisper(cfg, wav, folder / "transcript", stream.get("lang", "en"))
        language, words = parse_words(words_json, duration)
        make_frames(cfg, source, folder / "frames.jpg", duration)
        timeline = build_timeline(session_dir, source, cfg, stream, src_start, duration)
        payload = build_payload(stream, moment, folder, session_dir, source, duration, src_start, timeline,
                                language, words, cfg)
        (folder / "payload.json").write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                                             encoding="utf-8")
        wav.unlink(missing_ok=True)
        log.info("[%s] clip ready: %s (%.0fs, %d words, %.0fs of work)", folder.name.rsplit("-", 2)[0],
                 folder.name, duration, len(words), time.monotonic() - t0)
        from .editor import edit_clip  # AI editor step; never raises
        edit_clip(cfg, folder)
        return folder
    except Exception as exc:
        log.exception("clip failed for %s at +%.0fs", stream.get("handle"), getattr(moment, "offset", 0))
        if folder is not None:
            try:
                set_clip_status(cfg.database, folder.name, "failed", str(exc)[:300])
            except Exception:
                pass
        return None


def test_cut(cfg: Config, media: Path, lang: str = "en") -> Path:
    """Whisper + contact sheet for any video file; no live stream needed."""
    media = media.resolve()
    if not media.is_file():
        raise ClipError(f"file not found: {media}")
    folder = _unique_folder(cfg, {"platform": "test-cut", "handle": media.stem}, time.time())
    duration = probe_duration(cfg, media)
    wav = folder / "audio.wav"
    extract_wav(cfg, media, wav)
    print(f"  audio extracted ({duration:.0f}s of video); running whisper ({cfg.whisper_model_for(lang).name})...")
    words_json = run_whisper(cfg, wav, folder / "transcript", lang)
    language, words = parse_words(words_json, duration)
    make_frames(cfg, media, folder / "frames.jpg", duration)
    print(f"  language={language or '?'}  words={len(words)}  duration={duration:.1f}s")
    if words:
        print(f"  first word at {words[0][0]:.2f}s, last word ends at {words[-1][1]:.2f}s")
    return folder


# ------------------------------------------------------------------ pool
class ClipperPool:
    """Waits until context_after seconds past each spike, then runs the clip job on one of
    `workers` threads (so at most that many clips are being processed at once)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.workers = max(1, int(cfg.clip["workers"]))
        self.pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="clipper")
        self._lock = threading.Lock()
        self._pending: dict[threading.Timer, tuple] = {}
        self._closed = False

    def submit(self, stream: dict, session_dir: Path, moment, segment_seconds: float = 10) -> None:
        # +segment_seconds so the segment holding the last second has been closed
        due = moment.start_ts + float(self.cfg.clip["context_after"]) + segment_seconds + 1
        job = (stream, session_dir, moment, segment_seconds)
        with self._lock:
            if self._closed:
                return
            timer = threading.Timer(max(0.0, due - time.time()), self._fire)
            timer.daemon = True
            self._pending[timer] = job
        timer.args = (timer,)
        timer.start()
        log.info("[%s-%s] moment queued; clip starts in %.0fs", stream["platform"], stream["handle"],
                 max(0.0, due - time.time()))

    def _fire(self, timer: threading.Timer) -> None:
        with self._lock:
            job = self._pending.pop(timer, None)
            if job is None or self._closed:
                return
            self.pool.submit(build_clip, self.cfg, *job)

    def close(self) -> None:
        """Stop: moments still waiting are processed now with the footage that exists, then wait."""
        with self._lock:
            self._closed = True
            waiting = list(self._pending.items())
            self._pending.clear()
        for timer, _ in waiting:
            timer.cancel()
        if waiting:
            log.info("processing %d queued moment(s) before exit", len(waiting))
        for _, job in waiting:
            self.pool.submit(build_clip, self.cfg, *job)
        self.pool.shutdown(wait=True)
