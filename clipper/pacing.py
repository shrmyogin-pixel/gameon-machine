"""Dead-air trimming for the news voiceover: pauses longer than `max_gap` between two spoken words are shortened to
`keep_gap`, using the word timestamps (so a cut can only ever land in a gap between words, never inside speech).

plan_trim()   words -> the [(from_s, to_s)] stretches of silence to remove
shift_words() words + removed stretches -> the same words on the shortened timeline
trim_audio()  ffmpeg (below-normal priority, via worker._ffmpeg) cuts those stretches out of the voiceover

Pure functions except trim_audio(). Nothing is trimmed when no gap is longer than `max_gap`.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .config import Config

log = logging.getLogger("clipper")

MAX_GAP = 0.7           # a pause longer than this is dead air
KEEP_GAP = 0.3          # ...and is cut down to this (a natural breath)
MIN_SAVED = 0.15        # not worth an audio re-encode below this total


def plan_trim(words: list, max_gap: float = MAX_GAP, keep_gap: float = KEEP_GAP) -> list[tuple[float, float]]:
    """Silent stretches to cut: for every gap between consecutive words longer than max_gap, everything except keep_gap
    (half of it kept on each side of the cut)."""
    cuts = []
    for prev, nxt in zip(words, words[1:]):
        gap = nxt[0] - prev[1]
        if gap > max_gap:
            half = keep_gap / 2
            cuts.append((round(prev[1] + half, 3), round(nxt[0] - half, 3)))
    return cuts


def shift_words(words: list, cuts: list[tuple[float, float]]) -> list[list]:
    """Word timestamps after the `cuts` are removed from the audio."""
    def moved(t: float) -> float:
        return round(t - sum(min(max(t - a, 0.0), b - a) for a, b in cuts), 3)
    return [[moved(a), moved(b), w] for a, b, w in words]


def trim_audio(cfg: Config, src: Path, cuts: list[tuple[float, float]]) -> None:
    """Rewrite `src` (mp3) without the `cuts`. Raises RuntimeError if ffmpeg fails (the caller keeps the original)."""
    from .worker import ClipError, _ffmpeg
    drop = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in cuts)
    tmp = src.with_name(src.stem + ".trim.mp3")
    try:
        _ffmpeg(cfg, ["-i", src, "-af", f"aselect='not({drop})',asetpts=N/SR/TB", "-c:a", "libmp3lame", "-q:a", "2", tmp],
                timeout=120)
        if not tmp.is_file() or tmp.stat().st_size < 2000:
            raise RuntimeError("trimmed audio is empty")
        tmp.replace(src)
    except ClipError as exc:
        raise RuntimeError(str(exc)[:200])
    finally:
        tmp.unlink(missing_ok=True)


def tighten(cfg: Config, mp3: Path, words: list, dur: float) -> tuple[list, float, float]:
    """-> (words, duration, seconds removed). Never raises: on any problem the original audio and words are kept."""
    cuts = plan_trim(words)
    saved = sum(b - a for a, b in cuts)
    if saved < MIN_SAVED:
        return words, dur, 0.0
    try:
        from .worker import probe_duration
        trim_audio(cfg, mp3, cuts)
        new_dur = float(probe_duration(cfg, mp3))
        log.info("[%s] pacing: cut %d pause(s), %.2fs of dead air (%.1fs -> %.1fs)", mp3.parent.name, len(cuts), saved, dur, new_dur)
        return shift_words(words, cuts), new_dur, saved
    except Exception as exc:
        log.warning("[%s] pacing: could not trim pauses (%s); keeping the original", mp3.parent.name, exc)
        return words, dur, 0.0
