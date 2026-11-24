"""Moment detection: chat spikes + audio loudness, per stream.

`StreamDetector` is pure logic (no threads, no files, time is passed in) so it can be tested
with synthetic data. `SpikeMonitor` is the thread that feeds it from a live session folder
(chat.jsonl + finished seg_*.ts files). `replay()` runs the same logic over a saved session.

Chat message text is never logged (profanity policy); only counts and scores are.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import re
import statistics
import subprocess
import sys
import threading
import time
from array import array
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .proc import BELOW_NORMAL_PRIORITY_CLASS, CREATE_NO_WINDOW

log = logging.getLogger("clipper")

DEFAULTS: dict = {
    "bucket_seconds": 5,
    "baseline_buckets": 60,
    "warmup_buckets": 12,
    "std_multiplier": 2.5,
    "median_multiplier": 2.0,
    "min_messages": 10,
    "min_unique_ratio": 0.5,
    "max_per_user": 2,
    "cooldown_seconds": 90,
    "ignore_prefix": "!",
    "ignore_users": ["nightbot", "streamelements", "moobot", "fossabot", "wizebot",
                     "streamlabs", "soundalerts", "sery_bot"],
    "audio_enabled": True,
    "audio_baseline_seconds": 300,
    "audio_warmup_seconds": 30,
    "audio_min_std_db": 1.5,
    "audio_alone_z": 3.5,
    "audio_alone_hold_seconds": 2,
    "audio_boost_z": 1.5,
    "score": {
        "weight_chat": 3.5, "weight_unique": 2.0, "weight_words": 2.0, "weight_audio": 2.5,
        "chat_z_full": 8.0, "unique_full": 30, "words_full": 5, "audio_z_full": 4.0,
        "audio_alone_base": 3.0, "audio_alone_span": 5.0, "audio_alone_z_span": 4.0,
    },
    "words": {
        "laugh": ["lol", "lmao", "lmfao", "rofl", "haha", "kekw", "omegalul", "lul"],
        "shock": ["omg", "wtf", "wow", "no way", "holy", "insane", "bruh"],
        "hype": ["pog", "poggers", "let's go", "lets go", "gg", "hype", "clutch", "goated"],
    },
}

SAMPLE_RATE = 8000          # audio is decoded to 8 kHz mono just to measure loudness
SILENCE_DB = -90.0


# ---------------------------------------------------------------- settings
def build_params(detect_cfg: dict | None, stream: dict | None = None) -> dict:
    """Defaults < [detect] in config.toml < the stream's own overrides.

    A stream overrides by putting the key straight in its [[streams]] entry
    (min_messages = 6) or in a [streams.detect] table. Bad values fall back with a warning."""
    params = copy.deepcopy(DEFAULTS)
    layers = [detect_cfg or {}]
    if stream:
        layers.append({k: v for k, v in stream.items() if k in DEFAULTS})
        if isinstance(stream.get("detect"), dict):
            layers.append(stream["detect"])
    for layer in layers:
        for key, val in layer.items():
            if key not in params:
                log.warning("[detect] unknown setting %r ignored", key)
            elif isinstance(params[key], dict):
                if isinstance(val, dict):
                    params[key].update(val)
            else:
                params[key] = val
    for key, default in DEFAULTS.items():  # type check scalars/lists/sub-dicts after merging
        if isinstance(default, dict):
            for sub, sdefault in default.items():
                if not _same_kind(params[key].get(sub), sdefault):
                    log.warning("[detect.%s] bad value for %s; using default", key, sub)
                    params[key][sub] = sdefault
        elif not _same_kind(params[key], default):
            log.warning("[detect] bad value for %s; using default", key)
            params[key] = default
    return params


def _same_kind(val, default) -> bool:
    if isinstance(default, bool):
        return isinstance(val, bool)
    if isinstance(default, (int, float)):
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    return isinstance(val, type(default))


# ---------------------------------------------------------------- moments
@dataclass
class Moment:
    stream: str
    kind: str                 # "chat" | "chat+audio" | "audio"
    start_ts: float           # wall-clock epoch seconds where the moment starts
    end_ts: float
    offset: float             # seconds since the session started
    prescore: float           # 0..10
    numbers: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    def describe(self) -> str:
        n = self.numbers
        if self.kind == "audio":
            return (f"{self.kind} at +{self.offset:.0f}s: loudness z={n['audio_z_peak']:.2f} "
                    f"(>= {n['audio_alone_z']}) held {n['held_seconds']}s, db={n['audio_db']:.1f}, "
                    f"baseline mean={n['audio_mean_db']:.1f} std={n['audio_std_db']:.2f} -> prescore {self.prescore:.1f}")
        audio = f"{n['audio_z']:.2f}" if n.get("audio_z") is not None else "n/a"
        return (f"{self.kind} at +{self.offset:.0f}s: count={n['count']} "
                f"(mean={n['mean']:.2f} std={n['std']:.2f} -> need >={n['need_sigma']:.1f}; "
                f"median={n['median']:.1f} -> need >={n['need_median']:.1f}; min={n['min_messages']}) "
                f"unique={n['unique']}/{n['count']}={n['unique_ratio']:.2f} chat_z={n['chat_z']:.2f} "
                f"words={n['word_msgs']} audio_z={audio} -> prescore {self.prescore:.1f}")


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def compile_words(words: dict) -> list[re.Pattern]:
    pats = []
    for group in ("laugh", "shock", "hype"):
        parts = []
        for w in words.get(group, []):
            w = str(w).strip()
            if not w:
                continue
            esc = re.escape(w)
            parts.append(rf"(?<![\w']){esc}(?![\w'])" if re.fullmatch(r"[\w' ]+", w) else esc)
        if parts:
            pats.append(re.compile("|".join(parts), re.IGNORECASE))
    return pats


# ---------------------------------------------------------------- the detector
class _Bucket:
    __slots__ = ("per_user", "msgs", "word_msgs")

    def __init__(self):
        self.per_user: Counter = Counter()
        self.msgs = 0
        self.word_msgs = 0


class StreamDetector:
    """Feed it chat and audio, call advance(now), get Moments back."""

    def __init__(self, stream: str, params: dict, t0: float, has_chat: bool = True,
                 audio_lag: float | None = None):
        self.stream = stream
        self.p = params
        self.t0 = t0                       # session start (wall clock)
        self.has_chat = has_chat
        self.bucket_s = float(params["bucket_seconds"])
        # how long to wait after a bucket ends so its audio has been decoded
        self.lag = audio_lag if audio_lag is not None else 1.0
        self.ignore_users = {u.lower() for u in params["ignore_users"]}
        self.prefix = params["ignore_prefix"]
        self.word_pats = compile_words(params["words"])
        # chat
        self.buckets: dict[int, _Bucket] = {}
        self.baseline: deque[int] = deque(maxlen=int(params["baseline_buckets"]))
        self.next_idx = 0
        # audio
        self.db_baseline: deque[float] = deque(maxlen=int(params["audio_baseline_seconds"]))
        self.audio_z: deque[tuple[float, float, float]] = deque()   # (t, z, db)
        self._streak: list[tuple[float, float, float]] = []
        self.last_audio_t = -math.inf
        self.last_fire = -math.inf
        self._out: list[Moment] = []

    # ----- chat -----
    def add_chat(self, ts: float, user: str, text: str) -> None:
        text = (text or "").strip()
        user = (user or "").strip().lower()
        if not text or not user or user in self.ignore_users:
            return
        if self.prefix and text.startswith(self.prefix):
            return
        idx = int((ts - self.t0) // self.bucket_s)
        if idx < self.next_idx:            # before the session, or a bucket already judged
            return
        b = self.buckets.setdefault(idx, _Bucket())
        if b.per_user[user] >= int(self.p["max_per_user"]):
            return
        b.per_user[user] += 1
        b.msgs += 1
        if any(p.search(text) for p in self.word_pats):
            b.word_msgs += 1

    # ----- audio -----
    def add_audio(self, start_ts: float, db_values: list[float]) -> None:
        """One loudness value (dB) per second, the first one starting at start_ts."""
        if not self.p["audio_enabled"]:
            return
        p = self.p
        warm = int(p["audio_warmup_seconds"])
        for i, db in enumerate(db_values):
            t = start_ts + i
            if t <= self.last_audio_t:     # overlap with something already measured
                continue
            self.last_audio_t = t
            z = None
            if len(self.db_baseline) >= warm:
                mean = statistics.fmean(self.db_baseline)
                std = max(statistics.pstdev(self.db_baseline), float(p["audio_min_std_db"]))
                z = (db - mean) / std
            self.db_baseline.append(db)
            if z is None:
                continue
            self.audio_z.append((t, z, db))
            self._audio_alone_check(t, z, db)

    def _audio_alone_check(self, t: float, z: float, db: float) -> None:
        if self.has_chat:                  # chat reader present: audio only boosts chat spikes
            self._streak = []
            return
        if z < float(self.p["audio_alone_z"]):
            self._streak = []
            return
        if self._streak and t - self._streak[-1][0] > 1.5:
            self._streak = []
        self._streak.append((t, z, db))
        if len(self._streak) >= int(self.p["audio_alone_hold_seconds"]):
            self._fire_audio_alone(list(self._streak))
            self._streak = []

    def _fire_audio_alone(self, streak: list[tuple[float, float, float]]) -> None:
        p, s = self.p, self.p["score"]
        start, end = streak[0][0], streak[-1][0] + 1
        if not self._cooled(start):
            return
        peak = max(z for _, z, _ in streak)
        score = s["audio_alone_base"] + s["audio_alone_span"] * _clamp(
            (peak - p["audio_alone_z"]) / s["audio_alone_z_span"])
        mean = statistics.fmean(self.db_baseline) if self.db_baseline else SILENCE_DB
        std = max(statistics.pstdev(self.db_baseline), p["audio_min_std_db"]) if len(self.db_baseline) > 1 else p["audio_min_std_db"]
        self._emit(Moment(self.stream, "audio", start, end, start - self.t0, round(_clamp(score, 0, 10), 1), {
            "audio_z_peak": peak, "audio_alone_z": p["audio_alone_z"], "held_seconds": len(streak),
            "audio_db": max(db for _, _, db in streak), "audio_mean_db": mean, "audio_std_db": std,
        }))

    # ----- clock -----
    def advance(self, now: float) -> list[Moment]:
        """Judge every chat bucket that ended long enough ago. Returns new moments."""
        while self.t0 + (self.next_idx + 1) * self.bucket_s + self.lag <= now:
            self._close_bucket(self.next_idx)
            self.next_idx += 1
        out, self._out = self._out, []
        return out

    def _close_bucket(self, idx: int) -> None:
        p = self.p
        b = self.buckets.pop(idx, None) or _Bucket()
        count, unique = b.msgs, len(b.per_user)
        start = self.t0 + idx * self.bucket_s
        end = start + self.bucket_s
        base = list(self.baseline)
        self.baseline.append(count)
        # forget audio older than this bucket
        while self.audio_z and self.audio_z[0][0] < start - self.bucket_s:
            self.audio_z.popleft()
        if not self.has_chat or idx < int(p["warmup_buckets"]) or len(base) < 2:
            return
        mean, std, median = statistics.fmean(base), statistics.pstdev(base), statistics.median(base)
        need_sigma = mean + float(p["std_multiplier"]) * std
        need_median = float(p["median_multiplier"]) * median
        ratio = unique / count if count else 0.0
        if not (count >= need_sigma and count >= need_median
                and count >= int(p["min_messages"]) and ratio >= float(p["min_unique_ratio"])):
            return
        if not self._cooled(start):
            log.info("[%s] spike suppressed by %ds cooldown: count=%d at +%.0fs",
                     self.stream, p["cooldown_seconds"], count, start - self.t0)
            return
        in_bucket = [z for t, z, _ in self.audio_z if start <= t < end]
        audio_z = max(in_bucket) if in_bucket else None
        boosted = audio_z is not None and audio_z >= float(p["audio_boost_z"])
        chat_z = (count - mean) / max(std, 1.0)
        s = p["score"]
        score = (s["weight_chat"] * _clamp(chat_z / s["chat_z_full"])
                 + s["weight_unique"] * _clamp(unique / s["unique_full"])
                 + s["weight_words"] * _clamp(b.word_msgs / s["words_full"])
                 + (s["weight_audio"] * _clamp(max(audio_z / s["audio_z_full"], 0.4)) if boosted else 0.0))
        self._emit(Moment(self.stream, "chat+audio" if boosted else "chat", start, end, start - self.t0,
                          round(_clamp(score, 0, 10), 1), {
            "count": count, "mean": mean, "std": std, "median": median,
            "need_sigma": need_sigma, "need_median": need_median, "min_messages": int(p["min_messages"]),
            "unique": unique, "unique_ratio": ratio, "chat_z": chat_z, "word_msgs": b.word_msgs,
            "audio_z": audio_z, "baseline_buckets": len(base),
        }))

    def _cooled(self, t: float) -> bool:
        return abs(t - self.last_fire) >= float(self.p["cooldown_seconds"])

    def _emit(self, m: Moment) -> None:
        self.last_fire = m.start_ts
        self._out.append(m)


# ---------------------------------------------------------------- audio decoding
def measure_loudness(ffmpeg: str, segment: Path, low_priority: bool = True) -> tuple[list[float], float] | None:
    """Decode a segment's audio and return (dB per second, duration), or None on failure.
    ffmpeg runs at below-normal priority like every other ffmpeg process here."""
    flags = 0
    if sys.platform == "win32":
        flags = CREATE_NO_WINDOW | (BELOW_NORMAL_PRIORITY_CLASS if low_priority else 0)
    cmd = [ffmpeg, "-v", "error", "-i", str(segment), "-vn", "-map", "0:a:0",
           "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=60, creationflags=flags, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("audio decode failed for %s: %s", segment.name, exc)
        return None
    if p.returncode != 0 or not p.stdout:
        return None
    return loudness_per_second(p.stdout)


def loudness_per_second(pcm: bytes) -> tuple[list[float], float]:
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    out: list[float] = []
    for i in range(0, len(samples), SAMPLE_RATE):
        chunk = samples[i:i + SAMPLE_RATE]
        if len(chunk) < SAMPLE_RATE // 2:  # drop a stub shorter than half a second
            break
        rms = math.sqrt(sum(x * x for x in chunk) / len(chunk))
        out.append(max(SILENCE_DB, 20 * math.log10(rms / 32768)) if rms > 0 else SILENCE_DB)
    return out, len(samples) / SAMPLE_RATE


# ---------------------------------------------------------------- live monitor
def _parse_ts(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


class _ChatTail:
    """Reads new complete lines of chat.jsonl since last call."""

    def __init__(self, path: Path):
        self.path, self.pos = path, 0

    def read(self) -> list[tuple[float, str, str]]:
        try:
            with self.path.open("rb") as f:
                f.seek(self.pos)
                data = f.read()
        except OSError:
            return []
        end = data.rfind(b"\n")
        if end < 0:
            return []
        self.pos += end + 1
        rows = []
        for line in data[: end + 1].decode("utf-8", "replace").splitlines():
            try:
                r = json.loads(line)
                rows.append((_parse_ts(r["ts"]), str(r["username"]), str(r["text"])))
            except (ValueError, KeyError, TypeError):
                continue
        return rows


class SpikeMonitor(threading.Thread):
    """Watches one live session folder and writes moments to moments.jsonl + the log."""

    def __init__(self, session_dir: Path, name: str, params: dict, ffmpeg: str, started: float,
                 stop: threading.Event, chat_alive: Callable[[], bool], segment_seconds: float = 10,
                 low_priority: bool = True, on_moment: Callable[[Moment], None] | None = None):
        super().__init__(name=f"detect-{name}", daemon=True)
        self.dir, self.label, self.ffmpeg = session_dir, name, ffmpeg
        self.stop_event, self.chat_alive = stop, chat_alive
        self.low_priority = low_priority
        self.on_moment = on_moment
        audio_lag = segment_seconds + 3.0 if params["audio_enabled"] else 1.0
        self.det = StreamDetector(name, params, started, has_chat=chat_alive(), audio_lag=audio_lag)
        self.tail = _ChatTail(session_dir / "chat.jsonl")
        self.audio_done: set[str] = set()
        self.audio_failed = 0
        self.count = 0

    def run(self) -> None:
        try:
            while not self.stop_event.is_set():
                self._tick(final=False)
                self.stop_event.wait(1.0)
            self._tick(final=True)
        except Exception:
            log.exception("[%s] spike monitor crashed (recording is unaffected)", self.label)
        log.info("[%s] spike monitor closed, %d moments", self.label, self.count)

    def _tick(self, final: bool) -> None:
        try:
            if not final:  # chat capture is already stopped on the final pass; keep the last mode
                self.det.has_chat = bool(self.chat_alive())
            for ts, user, text in self.tail.read():
                self.det.add_chat(ts, user, text)
            if self.det.p["audio_enabled"]:
                self._audio(final)
            now = time.time() + (self.det.lag if final else 0)
            for m in self.det.advance(now):
                self._record(m)
        except Exception:
            log.exception("[%s] detection tick failed; continuing", self.label)

    def _audio(self, final: bool) -> None:
        segs = sorted(self.dir.glob("seg_*.ts"))
        if not final:
            segs = segs[:-1]               # newest segment is still being written
        for seg in segs:
            if seg.name in self.audio_done:
                continue
            self.audio_done.add(seg.name)
            try:
                end = seg.stat().st_mtime
            except OSError:
                continue
            res = measure_loudness(self.ffmpeg, seg, self.low_priority)
            if res is None:
                self.audio_failed += 1
                if self.audio_failed in (1, 10, 100):
                    log.warning("[%s] could not measure audio of %s (%d failures)", self.label, seg.name, self.audio_failed)
                continue
            db, duration = res
            self.det.add_audio(end - duration, db)

    def _record(self, m: Moment) -> None:
        self.count += 1
        log.info("[%s] MOMENT %s", self.label, m.describe())
        try:
            with (self.dir / "moments.jsonl").open("a", encoding="utf-8", newline="\n") as f:
                f.write(m.to_json() + "\n")
        except OSError as exc:
            log.warning("[%s] could not write moments.jsonl: %s", self.label, exc)
        if self.on_moment:
            try:
                self.on_moment(m)
            except Exception:
                log.exception("[%s] moment handler failed; detection continues", self.label)


# ---------------------------------------------------------------- replay a saved session
def replay(session_dir: Path, name: str, params: dict, ffmpeg: str, with_audio: bool = True) -> list[Moment]:
    """Run the detector over a saved session (chat.jsonl and, optionally, its segments)."""
    rows = _ChatTail(session_dir / "chat.jsonl").read()
    started = None
    try:
        info = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
        started = datetime.fromisoformat(info["started"]).timestamp()
    except (OSError, ValueError, KeyError):
        pass
    if started is None:
        started = min((r[0] for r in rows), default=time.time())
    det = StreamDetector(name, params, started, has_chat=bool(rows))
    if with_audio and params["audio_enabled"]:
        for seg in sorted(session_dir.glob("seg_*.ts")):
            res = measure_loudness(ffmpeg, seg)
            if res:
                det.add_audio(seg.stat().st_mtime - res[1], res[0])
    for ts, user, text in sorted(rows):
        det.add_chat(ts, user, text)
    last = max([r[0] for r in rows] + [det.last_audio_t if det.last_audio_t > 0 else 0])
    return det.advance(last + 3600)
