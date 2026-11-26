"""Render a news Short: voiceover + background music + cutting visuals + dark scrim + headline card + watermark + captions.

The background is 8-10 distinct REAL pictures (the article's own, see newsimage.py, topped up with topic-matched game
screenshots from imagesearch.py) with a clean cut every 2.5-4.5 seconds, each under a slow, subtle Ken Burns zoom/pan and
a light scrim (only behind the text). There is never a gradient, text slide or generated placeholder behind the Short: a picture that cannot be
decoded is dropped, and with none left the render fails (the story is skipped). Under the voiceover (-14 LUFS)
a Creative Commons track sits at about -25 LUFS (music.py) and the final mix is normalized to -14 LUFS. On top: the
hook, a kicker ("BREAKING NEWS" / "FREE GAME OFFER"), the story headline, the word-by-word 2-3 word pop captions and the source
outlet. It goes through the same hardware-encoder / below-normal-priority / one-render-at-a-time path as live
clips (render.py).
Layout on 1080x1920: hook banner y~150, kicker ~600, headline ~660-900, captions y=1180, source ~1330,
watermark bottom-left just above the y=1450 safe-zone line.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .buckets import LABELS as BUCKET_KICKERS
from .config import Config
from .hookscore import words_of
from .profanity import mask
from .render import (ACCENT, CAPTION_Y, accent_text, pop_tags, pop_caption_events, ENCODER_ARGS, FONT_DIRS, H, SAFE_BOTTOM, W, RenderError,
                     _ass_text, _ass_time, _ffmpeg, _fp, _slot, audio_chain, caption_fonts, pick_encoder, watermark_parts)

log = logging.getLogger("clipper")

TAIL_SECONDS = 0.3             # picture stays up a moment after the voice ends
FPS = 30
PICTURE_TIMEOUT = 600          # a picture render that takes longer than this is stuck
KB_ZOOM = 0.08                 # Ken Burns: zoom grows from 1.00x to 1.08x within a cut (subtle, no shaky crops)
CYAN, MAGENTA, WHITE, NAVY = "&H00FFF000&", "&H00D62BFF&", "&H00FFFFFF&", "&H00300A14&"   # Tokyo Neon (BGR)
BADGE_YELLOW, BADGE_TEXT = "&H0000D4FF&", "&H00141414&"   # category badge: plain solid yellow, near-black text (BGR)
KICKER_Y, NEWS_HOOK_Y = 150, 235      # kicker chip then the big hook, both below the Shorts top bar
CUT_SECONDS = 3.4              # target length of a cut
MIN_CUT, MAX_CUT = 2.5, 4.5    # every cut lasts between these
XFADE = 0.4                    # seconds each transition overlaps the next picture
TRANSITIONS = ["fade", "smoothleft", "slideleft", "smoothright", "wipeleft"]      # clean and calm: no spinning / zooming wipes


def build_news_ass(info: dict, words: list, dur: float) -> str:
    """Hook + kicker + headline + source for the whole Short, and the captions from the word timestamps."""
    lang = str(info.get("lang") or "en")
    f = caption_fonts(lang)
    hook = _ass_text(mask(info["hook"]).upper())
    n = len(words_of(hook))
    hook_size = 118 if n <= 5 else 104 if n <= 8 else 90
    kicker = {"free_offer": "FREE GAME OFFER", "trend": "TRENDING NOW"}.get(info.get("kind")) \
        or BUCKET_KICKERS.get(info.get("bucket"), "BREAKING NEWS")
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
Style: Hook,{f['Hook']},{hook_size},{WHITE},&H000000FF,{CYAN},{MAGENTA},0,0,0,0,100,100,2,0,1,8,6,8,90,90,0,1
Style: Glow,{f['Hook']},{hook_size},{CYAN},&H000000FF,{MAGENTA},&H00000000,0,0,0,0,100,100,2,0,1,16,0,8,90,90,0,1
Style: Cap,{f['Cap']},{f['cap_size']},{WHITE},&H000000FF,{NAVY},{MAGENTA},0,0,0,0,100,100,1,0,1,9,6,5,90,90,0,1
Style: Kicker,{f['head']},46,{BADGE_TEXT},&H000000FF,{BADGE_YELLOW},{BADGE_YELLOW},0,0,0,0,100,100,3,0,3,14,0,8,90,90,0,1
Style: Src,{f['head']},34,&H00F0F0F0,&H000000FF,{NAVY},&H96000000,0,0,0,0,100,100,0,0,1,5,3,8,90,90,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    t0, t1 = _ass_time(0), _ass_time(total)
    def ev(layer: int, style: str, x: int, y: int, tags: str, text: str, an: int = 8) -> str:
        return f"Dialogue: {layer},{t0},{t1},{style},,0,0,0,,{{\\an{an}\\pos({x},{y}){tags}}}{text}"
    lines = [ev(1, "Glow", W // 2, NEWS_HOOK_Y, "\\blur10" + pop_tags(260), hook),            # neon glow under the hook
             ev(2, "Hook", W // 2, NEWS_HOOK_Y, "\\blur1" + pop_tags(260), accent_text(hook, ACCENT, WHITE)),
             ev(3, "Kicker", W // 2, KICKER_Y, pop_tags(240, 60, 110), kicker),
             ev(0, "Src", W // 2, 1330, "", source)]
    lines += pop_caption_events(words, 0.0, dur + 0.05, lang, y=CAPTION_Y, size=f["cap_size"])
    return header + "\n".join(lines) + "\n"


def scrim_png(path: Path) -> Path:
    """Full-frame RGBA scrim: dark overall, darker behind the hook (top), headline and captions, lighter in between
    so the picture still shows. Written with OpenCV/numpy (both already dependencies)."""
    import cv2
    import numpy as np
    ys = np.linspace(0.0, 1.0, H)
    pts_y = [0.0, 0.07, 0.20, 0.30, 0.40, 0.55, 0.60, 0.72, 1.0]     # fractions of the frame height
    pts_a = [0.42, 0.36, 0.18, 0.0, 0.0, 0.0, 0.06, 0.30, 0.38]      # dark only behind the hook and the captions
    alpha = np.interp(ys, pts_y, pts_a)
    img = np.zeros((H, W, 4), np.uint8)
    img[..., 3] = (alpha[:, None] * 255).astype(np.uint8)
    cv2.imwrite(str(path), img)
    return path


def plan_segments(total: float, n_visuals: int, target: float = CUT_SECONDS) -> list[tuple[int, float]]:
    """[(visual index, seconds)] covering `total`: a cut every MIN_CUT-MAX_CUT seconds (about `target`), cycling the
    visuals so the same one never plays twice in a row (when there are at least two)."""
    n_seg = max(1, round(total / target))
    while n_seg > 1 and total / n_seg < MIN_CUT:
        n_seg -= 1
    while total / n_seg > MAX_CUT:
        n_seg += 1
    if n_visuals >= 2 and MIN_CUT <= total / n_visuals <= MAX_CUT:
        n_seg = n_visuals                       # one cut per picture: nothing repeats
    seg = total / n_seg
    order = [i % max(n_visuals, 1) for i in range(n_seg)]
    return [(idx, seg) for idx in order]


def lift_dark(img):
    """Gamma-lift a dark / muted picture so it stays vibrant under the lighter scrim (bright pictures are left alone)."""
    import cv2
    import numpy as np
    mean = float(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).mean())
    if mean >= 105:
        return img
    gamma = max(0.6, 0.6 + 0.4 * (mean / 105))                        # darker -> stronger lift (0.6 .. 1.0)
    lut = (np.linspace(0, 1, 256) ** gamma * 255).astype(np.uint8)
    return cv2.LUT(img, lut)


def prep_visual(src: Path, dst: Path, anchor: float) -> Path:
    """Cover-crop a picture to 9:16 (1620x2880, 1.5x the frame so the zoom stays sharp); `anchor` 0..1 picks which
    part of a wide picture is kept (0.5 = centre). Raises RenderError when the picture cannot be decoded."""
    import cv2
    img = cv2.imread(str(src))
    if img is None or min(img.shape[:2]) < 100:
        raise RenderError(f"{src.name} is not a usable picture")
    h, w = img.shape[:2]
    img = lift_dark(img)
    scale = max(1620 / w, 2880 / h)
    img = cv2.resize(img, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA)
    h, w = img.shape[:2]
    x = round((w - 1620) * min(max(anchor, 0.0), 1.0))
    y = round((h - 2880) * 0.4)
    cv2.imwrite(str(dst), img[y:y + 2880, x:x + 1620], [cv2.IMWRITE_JPEG_QUALITY, 95])
    return dst


def motion(kind: int, frames: int) -> str:
    """zoompan z/x/y expressions for one of five varied Ken Burns moves (subtle: at most 1.16x)."""
    p = f"(on/{max(frames, 1)})"
    zin, zout = f"z='1+{KB_ZOOM}*{p}'", f"z='1+{KB_ZOOM}*(1-{p})'"
    cx, cy = "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"
    moves = [
        f"{zin}:x='(iw-iw/zoom)*{p}':y='(ih-ih/zoom)*0.4'",         # zoom in, drift right
        f"{zin}:x='(iw-iw/zoom)*(1-{p})':y='(ih-ih/zoom)*0.4'",     # zoom in, drift left
        f"{zout}:x='{cx}':y='{cy}'",                                 # pull back
        f"{zin}:x='{cx}':y='(ih-ih/zoom)*(1-{p})'",                 # zoom in, drift up
        f"{zin}:x='{cx}':y='(ih-ih/zoom)*{p}'",                     # zoom in, drift down
    ]
    return moves[kind % len(moves)]


def render_news_video(cfg: Config, folder: Path, info: dict, words: list, dur: float) -> dict:
    """voiceover.mp3 + words -> clip.mp4 + thumb.jpg in `folder`. Raises RenderError.

    info['images'] = real picture files to cut between (see newsimage.py / imagesearch.py); info['music'] = a track
    from music.py (or None). Pictures that cannot be decoded are dropped first (never a blank frame); if the render
    fails it is retried once without the music. With no usable picture at all it raises: there is no placeholder."""
    images = usable_pictures(info.get("images") or ([info["image"]] if info.get("image") else []))
    if not images:
        raise RenderError("no usable real picture to show (placeholders are not allowed)")
    music = info.get("music")
    last = None
    for track in ([music, None] if music else [None]):
        try:
            return _render(cfg, folder, info, words, dur, images, track)
        except RenderError as exc:
            last = exc
            log.warning("[%s] render with %d picture(s)%s failed (%s)", folder.name, len(images),
                        " + music" if track else "", str(exc)[:200])
    raise last or RenderError("render failed")


def usable_pictures(files) -> list[Path]:
    """The files that exist and really decode as a picture big enough to fill a Short (the rest are dropped)."""
    import cv2
    out = []
    for f in files:
        f = Path(f)
        try:
            img = cv2.imread(str(f)) if f.is_file() else None
        except Exception:
            img = None
        if img is not None and img.shape[1] >= 600 and img.shape[0] >= 300:
            out.append(f)
        else:
            log.warning("news render: dropped unusable picture %s", f.name)
    return out


def _render(cfg: Config, folder: Path, info: dict, words: list, dur: float, visuals: list[Path], track: dict | None) -> dict:
    from . import music as music_lib
    from .worker import probe_duration
    mp3 = folder / "voiceover.mp3"
    if not mp3.is_file():
        raise RenderError("voiceover.mp3 is missing")
    total = dur + TAIL_SECONDS
    ass = folder / "captions.ass"
    scrim = folder / "scrim.png"
    made: list[Path] = []
    ass.write_text(build_news_ass(info, words, dur), encoding="utf-8")
    sub = f"ass=filename='{_fp(ass)}':fontsdir='{_fp(Path(FONT_DIRS))}'"
    encoder = pick_encoder(cfg)
    seed = sum(map(ord, folder.name))
    if not visuals:
        raise RenderError("no usable real picture to show (placeholders are not allowed)")
    segs = plan_segments(total, len(visuals))
    music_info = None
    try:
        args: list = []
        scrim_png(scrim)
        chains = []
        k = len(segs)
        xf = XFADE if k > 1 else 0.0
        cuts = [sum(sec for _, sec in segs[:i]) for i in range(k + 1)]      # cut times T_0..T_k (T_k = total)
        for i, (vi, seconds) in enumerate(segs):
            # each picture runs a little past its cut so the next one can fade/slide in over it
            n = round((seconds + (xf if i < k - 1 else 0.0)) * FPS)
            anchors = (0.5, 0.3, 0.7, 0.45, 0.6)
            vis = prep_visual(visuals[vi], folder / f"visual_{i}.jpg", anchors[(seed + i) % len(anchors)])
            made.append(vis)
            args += ["-loop", "1", "-framerate", str(FPS), "-t", f"{n / FPS:.3f}", "-i", vis]
            chains.append(f"[{i}:v]zoompan={motion(seed + i, n)}:d=1:s={W}x{H}:fps={FPS},eq=saturation=1.2:contrast=1.08:brightness=0.02,"
                           f"setsar=1,format=yuv420p[z{i}]")
        args += ["-i", mp3, "-i", scrim]
        voice_idx, scrim_idx, next_idx = k, k + 1, k + 2
        joins, prev = [], "z0"
        for i in range(1, k):                                   # varied transition at every cut
            name = TRANSITIONS[(seed + i) % len(TRANSITIONS)]
            joins.append(f"[{prev}][z{i}]xfade=transition={name}:duration={xf}:offset={cuts[i]:.3f}[x{i}]")
            prev = f"x{i}"
        head = (";".join(chains + joins) + f";[{prev}][{scrim_idx}:v]overlay=0:0:format=auto[vs]")
        voice_chain = audio_chain(cfg, mp3, 0.0, dur)
        if track:
            tpath = Path(track["path"])
            try:
                tdur = float(probe_duration(cfg, tpath))
                start = music_lib.start_offset(tdur, total, folder.name)
                gain = music_lib.music_gain_db(cfg, tpath, start, min(total, max(tdur - start, 5)))
            except Exception as exc:                                # a missing / corrupt track: the caller retries without it
                raise RenderError(f"music track {tpath.name} is unusable: {str(exc)[:120]}")
            args += ["-stream_loop", "-1", "-ss", f"{start:.1f}", "-i", tpath]
            music_idx = next_idx
            next_idx += 1
            audio = music_lib.mix_graph(f"{voice_idx}:a", f"{music_idx}:a", voice_chain, gain, dur,
                                        float(music_lib.settings(cfg)["final_lufs"]))
            music_info = {"title": music_lib.safe_title(track), "file": track.get("file"), "gain_db": round(gain, 1),
                          "start_s": start, "attribution": music_lib.attribution_line(track)}
        else:
            audio = f"[{voice_idx}:a]{voice_chain}[a]"
        wm = watermark_parts(cfg, index=next_idx)
        video = (f"{head};{wm[1][0]};[vs][wm]{wm[1][1]},{sub},format=yuv420p[v]" if wm
                 else f"{head};[vs]{sub},format=yuv420p[v]")
        if wm:
            args += ["-i", wm[0]]
        args += ["-filter_complex", f"{video};{audio}", "-map", "[v]", "-map", "[a]",
                 "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-r", str(FPS), "-c:v", encoder, *ENCODER_ARGS[encoder],
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-t", f"{total:.3f}",
                 "-metadata", f"title={info['title']}", "-metadata", f"comment=Source: {info['outlet']}"]
        out, tmp = folder / "clip.mp4", folder / "clip.part.mp4"
        try:
            with _slot(cfg):
                _ffmpeg(cfg, [*args, tmp], timeout=PICTURE_TIMEOUT)
            tmp.replace(out)
            _ffmpeg(cfg, ["-ss", "1.5", "-i", out, "-frames:v", "1", "-q:v", "2", folder / "thumb.jpg"])
        finally:
            tmp.unlink(missing_ok=True)
    finally:
        ass.unlink(missing_ok=True)
        scrim.unlink(missing_ok=True)
        for f in made:
            f.unlink(missing_ok=True)
    final = music_lib.measure_lufs(cfg, out, 0.0, total)
    log.info("[%s] news render: encoder %s, %.1fs, %d visual(s) in %d cut(s), music %s, final %s LUFS, watermark %s",
             folder.name, encoder, total, len(visuals), len(segs), music_info["title"] if music_info else "none",
             f"{final:.1f}" if final is not None else "?", "on" if wm else "off")
    return {"layout": "news", "encoder": encoder, "duration_s": round(total, 2), "watermark": bool(wm),
            "background": "picture", "visuals": len(visuals), "cuts": len(segs),
            "music": music_info, "final_lufs": round(final, 1) if final is not None else None}
