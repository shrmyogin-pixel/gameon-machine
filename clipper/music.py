"""Copyright-safe background music for news Shorts: assets/music/ + assets/music/credits.json.

credits.json lists the tracks (file, title, artist, license, download url and the exact `attribution` line the
license requires). The shipped tracks are by Kevin MacLeod (incompetech.com), licensed CC BY 4.0: free to use as long
as the attribution line is in the video description. This tool adds that line automatically to the Short's
description and to the exported upload sheet (export.py re-checks it), so you never have to remember it.

* A listed track whose file is missing is downloaded again from its `url`.
* If no track at all is usable (offline, files deleted), a clean synthesized ambient pad is generated instead: it is
  original, so it needs no attribution.
* The mix: the track is brought to about -25 LUFS (a clearly audible bed under the voiceover, which is at -14 LUFS), dips further while
  the voice speaks (sidechain ducking), fades in and out, and the final mixed Short is normalized to -14 LUFS.

Note: CC BY with attribution is the license, not a guarantee against automated Content ID claims; if YouTube ever
flags a track, dispute it with the license link (incompetech.com keeps a how-to).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.request
from pathlib import Path

from .config import Config, expand_path

log = logging.getLogger("clipper")

DEFAULTS = {"enabled": True, "dir": "assets/music", "track_lufs": -25.0, "final_lufs": -14.0}
MAX_TRACK_BYTES = 15_000_000
SYNTH_NAME = "synth-ambient.mp3"


def settings(cfg: Config) -> dict:
    return {**DEFAULTS, **cfg.raw.get("music", {})}


def music_dir(cfg: Config) -> Path:
    return expand_path(str(settings(cfg)["dir"]))


def credits_path(cfg: Config) -> Path:
    return music_dir(cfg) / "credits.json"


def read_credits(cfg: Config) -> dict:
    try:
        data = json.loads(credits_path(cfg).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("tracks"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"tracks": []}


def _download(url: str, dest: Path) -> bool:
    from .newsimage import public_host
    if not public_host(url):
        return False
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; gameon-clipper/1.0)"})
    tmp = dest.with_name(dest.name + ".part")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read(MAX_TRACK_BYTES + 1)
        if not 200_000 < len(data) <= MAX_TRACK_BYTES:
            return False
        tmp.write_bytes(data)
        tmp.replace(dest)
        return True
    except Exception as exc:
        log.warning("music: download of %s failed (%s: %s)", url[:80], type(exc).__name__, str(exc)[:80])
        return False
    finally:
        tmp.unlink(missing_ok=True)


def synth_ambient(cfg: Config, dest: Path, seconds: int = 120) -> bool:
    """A calm original pad (a few slowly swelling sine chords) as the last-resort track. Needs no attribution."""
    from .render import _ffmpeg
    expr = ("0.30*sin(2*PI*110*t)+0.22*sin(2*PI*164.81*t)+0.18*sin(2*PI*220*t)+0.14*sin(2*PI*329.63*t)"
            "+0.10*sin(2*PI*261.63*t)*sin(2*PI*0.05*t)")
    swell = "(0.65+0.35*sin(2*PI*0.08*t))"
    try:
        _ffmpeg(cfg, ["-f", "lavfi", "-i", f"aevalsrc='({expr})*{swell}':s=44100:d={seconds}",
                      "-af", "lowpass=f=1800,aecho=0.8:0.7:420:0.35,volume=0.6", "-c:a", "libmp3lame", "-b:a", "128k", dest],
                timeout=300)
        return dest.is_file() and dest.stat().st_size > 50_000
    except Exception as exc:
        log.warning("music: could not synthesize the fallback pad (%s)", str(exc)[:120])
        return False


def ensure_music(cfg: Config) -> list[dict]:
    """Tracks that are really on disk (re-downloading missing ones; synthesizing a pad if there are none). Never raises."""
    try:
        d = music_dir(cfg)
        d.mkdir(parents=True, exist_ok=True)
        credits = read_credits(cfg)
        changed = False
        for t in credits["tracks"]:
            f = d / str(t.get("file", ""))
            if t.get("file") and not f.is_file() and t.get("url"):
                log.info("music: downloading %s", t.get("title"))
                _download(t["url"], f)
        tracks = [t for t in credits["tracks"] if t.get("file") and (d / t["file"]).is_file()]
        if not tracks:
            f = d / SYNTH_NAME
            if (f.is_file() or synth_ambient(cfg, f)):
                entry = {"file": SYNTH_NAME, "title": "Game On ambient pad", "artist": "synthesized in-house",
                         "license": "original (no attribution required)", "attribution": None}
                if not any(t.get("file") == SYNTH_NAME for t in credits["tracks"]):
                    credits["tracks"].append(entry)
                    changed = True
                tracks = [entry]
        if changed:
            credits_path(cfg).write_text(json.dumps(credits, indent=2, ensure_ascii=False), encoding="utf-8")
        return [{**t, "path": d / t["file"]} for t in tracks]
    except Exception:
        log.exception("music: unavailable (ignored)")
        return []


def pick_track(tracks: list[dict], seed: str) -> dict | None:
    """A track chosen by a hash of `seed` (stable per story, varied across stories)."""
    if not tracks:
        return None
    return tracks[int(hashlib.sha1(seed.encode()).hexdigest(), 16) % len(tracks)]


def attribution_line(track: dict | None) -> str | None:
    """The one-line credit the track's license requires (None for original / public-domain tracks)."""
    line = (track or {}).get("attribution")
    return str(line).strip() if line else None


def start_offset(track_seconds: float, need: float, seed: str) -> float:
    """Where in the track to start so the Short is not always the intro (0 when the track is short)."""
    room = track_seconds - need - 8
    if room <= 1:
        return 0.0
    return round(int(hashlib.sha1((seed + "start").encode()).hexdigest(), 16) % int(room), 1)


def measure_lufs(cfg: Config, path: Path, start: float, dur: float) -> float | None:
    from .render import loudness_after
    return loudness_after(cfg, path, start, dur, "anull")


def music_gain_db(cfg: Config, path: Path, start: float, dur: float) -> float:
    """dB to apply so the segment lands at [music] track_lufs (clamped; 0 when it cannot be measured)."""
    got = measure_lufs(cfg, path, start, dur)
    if got is None or got < -90:
        return 0.0
    return max(min(float(settings(cfg)["track_lufs"]) - got, 24.0), -30.0)


def mix_graph(voice_in: str, music_in: str, voice_chain: str, gain_db: float, dur: float, final_lufs: float) -> str:
    """filter_complex pieces (ending in [a]): voice chain + ducked, faded music -> amix -> loudnorm to `final_lufs`."""
    fade_out = max(dur - 2.0, 0.5)
    return (f"[{voice_in}]{voice_chain},asplit=2[va][vsc];"
            f"[{music_in}]volume={gain_db:.2f}dB,alimiter=limit=0.89:level=disabled,aresample=48000,"
            f"afade=t=in:st=0:d=1.2,afade=t=out:st={fade_out:.2f}:d=2[mus];"
            f"[mus][vsc]sidechaincompress=threshold=0.09:ratio=2.2:attack=20:release=400:makeup=1[duck];"
            f"[va][duck]amix=inputs=2:duration=first:normalize=0,loudnorm=I={final_lufs:g}:TP=-1.5:LRA=11,aresample=48000[a]")


def safe_title(track: dict) -> str:
    return re.sub(r"\s+", " ", str(track.get("title", ""))).strip()
