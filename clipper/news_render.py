"""Render a news Short: voiceover + background + dark scrim + headline card + watermark + 3-word bold captions.

The background is the article's real picture (feed image or og:image, see newsimage.py) with a slow Ken Burns
zoom and pan under a dark scrim; with no usable picture (or if ffmpeg cannot use it) it is an original animated
gradient. On top: the hook, a kicker ("BREAKING NEWS" / "FREE GAME OFFER"), the story headline and the source
outlet. It goes through the same
hardware-encoder / below-normal-priority / one-render-at-a-time path as live clips (render.py).
Layout on 1080x1920: hook banner y~150, kicker ~600, headline ~660-900, captions y=1180, source ~1330,
watermark bottom-left just above the y=1450 safe-zone line.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .config import Config
from .hookscore import words_of
from .profanity import mask
from .render import (CAPTION_Y, ENCODER_ARGS, FONT_DIRS, H, SAFE_BOTTOM, W, RenderError, _ass_text, _ass_time,
                     _ffmpeg, _fp, _slot, audio_chain, caption_fonts, caption_groups, pick_encoder, watermark_parts)

log = logging.getLogger("clipper")

TAIL_SECONDS = 0.5             # picture stays up a moment after the voice ends
FPS = 30
PICTURE_TIMEOUT = 600          # a picture render that takes longer than this is stuck: fall back to the gradient
KB_ZOOM = 0.14                 # Ken Burns: zoom grows from 1.00x to 1.14x over the Short (subtle)


def build_news_ass(info: dict, words: list, dur: float) -> str:
    """Hook + kicker + headline + source for the whole Short, and the captions from the word timestamps."""
    f = caption_fonts("en")
    hook = _ass_text(mask(info["hook"]).upper())
    n = len(words_of(hook))
    hook_size = 84 if n <= 5 else 72 if n <= 8 else 62
    kicker = {"free_offer": "FREE GAME OFFER"}.get(info.get("kind"), "BREAKING NEWS")
    headline = _ass_text(mask(info["headline"]))
    source = _ass_text(mask(f"Source: {info['outlet']}"))
    total = dur + TAIL_SECONDS
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,Impact,{hook_size},&H0000F0FF,&H000000FF,&H20000000,&H00000000,0,0,0,0,100,100,1,0,3,16,0,8,60,60,0,1
Style: Cap,Impact,{f['cap_size']},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,1,0,1,8,3,5,50,50,0,1
Style: Kicker,Arial,46,&H0000F0FF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,3,0,1,4,0,8,60,60,0,1
Style: Head,Arial,58,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,2,8,80,80,0,1
Style: Src,Arial,38,&H00D8D8D8,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,0,8,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    t0, t1 = _ass_time(0), _ass_time(total)
    lines = [f"Dialogue: 2,{t0},{t1},Hook,,0,0,0,,{{\\an8\\pos({W // 2},150)}}{hook}",
             f"Dialogue: 0,{t0},{t1},Kicker,,0,0,0,,{{\\an8\\pos({W // 2},590)}}{kicker}",
             f"Dialogue: 0,{t0},{t1},Head,,0,0,0,,{{\\an8\\pos({W // 2},660)}}{headline}",
             f"Dialogue: 0,{t0},{t1},Src,,0,0,0,,{{\\an8\\pos({W // 2},1330)}}{source}"]
    for a, b, text in caption_groups(words, 0.0, dur + 0.05):
        lines.append(f"Dialogue: 1,{_ass_time(a)},{_ass_time(b)},Cap,,0,0,0,,{{\\an5\\pos({W // 2},{CAPTION_Y})}}{text}")
    return header + "\n".join(lines) + "\n"


def background_source(total: float) -> list:
    """ffmpeg input args for the animated gradient background (falls back to a flat colour if unavailable)."""
    return ["-f", "lavfi", "-i",
            f"gradients=s={W}x{H}:d={total:.3f}:r=30:c0=0x0b1226:c1=0x1d2a6e:c2=0x0a4a66:nb_colors=3:speed=0.03:type=linear"]


def scrim_png(path: Path) -> Path:
    """Full-frame RGBA scrim: dark overall, darker behind the hook (top), headline and captions, lighter in between
    so the picture still shows. Written with OpenCV/numpy (both already dependencies)."""
    import cv2
    import numpy as np
    ys = np.linspace(0.0, 1.0, H)
    pts_y = [0.0, 0.10, 0.20, 0.30, 0.50, 0.62, 0.75, 1.0]           # fractions of the frame height
    pts_a = [0.62, 0.55, 0.45, 0.58, 0.60, 0.62, 0.60, 0.70]          # opacity of the black layer there
    alpha = np.interp(ys, pts_y, pts_a)
    img = np.zeros((H, W, 4), np.uint8)
    img[..., 3] = (alpha[:, None] * 255).astype(np.uint8)
    cv2.imwrite(str(path), img)
    return path


def check_picture(image: Path) -> None:
    """ffmpeg loops an undecodable still forever, so prove it decodes first. Raises RenderError."""
    import cv2
    img = cv2.imread(str(image))
    if img is None or min(img.shape[:2]) < 100:
        raise RenderError(f"{image.name} is not a usable picture")


def kenburns_chain(total: float, flip: bool) -> str:
    """Filter chain from the still picture (cover-cropped to 9:16) to a 1080x1920, 30 fps zoom-and-pan."""
    n = max(int(total * FPS), 1)
    prog = f"on/{n}"
    px = f"(1-{prog})" if flip else prog                  # pan direction alternates from story to story
    return (f"scale=1620:2880:force_original_aspect_ratio=increase:flags=lanczos,crop=1620:2880,setsar=1,"
            f"zoompan=z='1+{KB_ZOOM}*{prog}':x='(iw-iw/zoom)*{px}':y='(ih-ih/zoom)*0.38':d=1:s={W}x{H}:fps={FPS},"
            f"eq=saturation=0.92,format=yuv420p")


def render_news_video(cfg: Config, folder: Path, info: dict, words: list, dur: float) -> dict:
    """voiceover.mp3 + words -> clip.mp4 + thumb.jpg in `folder`. Raises RenderError.

    With info['image'] (a picture file) the background is the Ken Burns picture; if that render fails for any
    reason it is retried once with the gradient, so a bad picture never loses the Short."""
    image = info.get("image")
    if image and Path(image).is_file():
        try:
            return _render(cfg, folder, info, words, dur, Path(image))
        except RenderError as exc:
            log.warning("[%s] picture render failed (%s); using the gradient background", folder.name, str(exc)[:200])
    return _render(cfg, folder, info, words, dur, None)


def _render(cfg: Config, folder: Path, info: dict, words: list, dur: float, image: Path | None) -> dict:
    mp3 = folder / "voiceover.mp3"
    if not mp3.is_file():
        raise RenderError("voiceover.mp3 is missing")
    total = dur + TAIL_SECONDS
    ass = folder / "captions.ass"
    scrim = folder / "scrim.png"
    ass.write_text(build_news_ass(info, words, dur), encoding="utf-8")
    sub = f"ass=filename='{_fp(ass)}':fontsdir='{_fp(Path(FONT_DIRS))}'"
    encoder = pick_encoder(cfg)
    try:
        if image:
            check_picture(image)
            scrim_png(scrim)
            # inputs: 0 = picture (looped), 1 = voice, 2 = scrim, 3 = watermark
            args = ["-loop", "1", "-framerate", str(FPS), "-i", image, "-i", mp3, "-i", scrim]
            wm = watermark_parts(cfg, index=3)
            flip = sum(map(ord, folder.name)) % 2 == 1
            head = f"[0:v]{kenburns_chain(total, flip)}[bg];[bg][2:v]overlay=0:0:format=auto[vs]"
        else:
            args = [*background_source(total), "-i", mp3]
            wm = watermark_parts(cfg, index=2)                 # input 2 = watermark PNG (0 = background, 1 = voice)
            head = "[0:v]null[vs]"
        graph = (f"{head};{wm[1][0]};[vs][wm]{wm[1][1]},{sub},format=yuv420p[v]" if wm
                 else f"{head};[vs]{sub},format=yuv420p[v]")
        if wm:
            args += ["-i", wm[0]]
        args += ["-filter_complex", graph, "-map", "[v]", "-map", "1:a:0", "-af", audio_chain(cfg, mp3, 0.0, dur),
                 "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-r", str(FPS), "-c:v", encoder, *ENCODER_ARGS[encoder],
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-t", f"{total:.3f}",
                 "-metadata", f"title={info['title']}", "-metadata", f"comment=Source: {info['outlet']}"]
        out, tmp = folder / "clip.mp4", folder / "clip.part.mp4"
        try:
            with _slot(cfg):
                _ffmpeg(cfg, [*args, tmp], timeout=PICTURE_TIMEOUT if image else 1800)
            tmp.replace(out)
            _ffmpeg(cfg, ["-ss", "1.5", "-i", out, "-frames:v", "1", "-q:v", "2", folder / "thumb.jpg"])
        finally:
            tmp.unlink(missing_ok=True)
    finally:
        ass.unlink(missing_ok=True)
        scrim.unlink(missing_ok=True)
    log.info("[%s] news render: encoder %s, %.1fs, background %s, watermark %s", folder.name, encoder, total,
             "picture (Ken Burns)" if image else "gradient", "on" if wm else "off")
    return {"layout": "news", "encoder": encoder, "duration_s": round(total, 2), "watermark": bool(wm),
            "background": "picture" if image else "gradient"}
