"""Reaction / meme visuals in assets/memes/, used when an article gives fewer than 3 usable pictures.

* Put your own gameplay screenshots, reaction images or memes in assets/memes/ (jpg / png / webp). They are used as
  they are. Only add images you have the right to use: they are not credited in the description.
* If the folder has no pictures, six ORIGINAL reaction cards (big "?!", "WOW", "GG" ... on colour gradients) are
  generated there with ffmpeg, so there is never a copyright question and the folder is never empty.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from .config import Config, expand_path

log = logging.getLogger("clipper")

EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
# glyph, gradient colours (c0, c1, c2)
CARDS = [("?!", ("0x1b2a6b", "0x7a1fa2", "0x0b4f8f")), ("WOW", ("0x7a1f1f", "0xd1481f", "0x5a1a6a")),
         ("GG", ("0x0b4f3c", "0x13a07a", "0x1b2a6b")), ("NEW", ("0x1f3a8a", "0x1ea0d8", "0x0a2a4a")),
         ("!!", ("0x5a0b3c", "0xc2185b", "0x2a1b6b")), ("LOL", ("0x4a3a0b", "0xd8a31e", "0x7a2a1f"))]


def memes_dir(cfg: Config) -> Path:
    return expand_path(str(cfg.raw.get("news", {}).get("memes_dir", "assets/memes")))


def list_memes(cfg: Config) -> list[Path]:
    d = memes_dir(cfg)
    return sorted(p for p in d.glob("*") if p.suffix.lower() in EXTENSIONS and p.is_file()) if d.is_dir() else []


def make_card(cfg: Config, glyph: str, colours: tuple, out: Path) -> None:
    """One 1080x1920 original card: a diagonal gradient with a huge, translucent glyph."""
    from .render import FONT_DIRS, _ffmpeg, _fp
    font = _fp(Path(FONT_DIRS) / "impact.ttf")
    c0, c1, c2 = colours
    graph = (f"gradients=s=1080x1920:c0={c0}:c1={c1}:c2={c2}:nb_colors=3:speed=0.01:type=linear:x0=0:y0=0:x1=1080:y1=1920,"
             f"drawtext=fontfile='{font}':text='{glyph}':fontsize=760:fontcolor=white@0.42:"
             f"x=(w-text_w)/2:y=(h-text_h)/2:borderw=0")
    _ffmpeg(cfg, ["-f", "lavfi", "-i", graph, "-frames:v", "1", "-q:v", "2", out], timeout=120)


def ensure_memes(cfg: Config) -> list[Path]:
    """The pictures in assets/memes (generating the original cards first when the folder has none). Never raises."""
    try:
        d = memes_dir(cfg)
        d.mkdir(parents=True, exist_ok=True)
        have = list_memes(cfg)
        if have:
            return have
        for i, (glyph, colours) in enumerate(CARDS, start=1):
            try:
                make_card(cfg, glyph, colours, d / f"card-{i}-{glyph.strip('?!').lower() or 'hype'}.jpg")
            except Exception as exc:
                log.warning("memes: could not draw card %s (%s)", glyph, str(exc)[:120])
        return list_memes(cfg)
    except Exception:
        log.exception("memes: unavailable (ignored)")
        return []


def pick_memes(cfg: Config, n: int, seed: str) -> list[Path]:
    """`n` different meme pictures, chosen (and ordered) by a hash of `seed` so each story gets its own mix."""
    pool = ensure_memes(cfg)
    pool = sorted(pool, key=lambda p: hashlib.sha1((seed + p.name).encode()).hexdigest())
    return pool[:max(n, 0)]
