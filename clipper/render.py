"""Video renderer: analysis.json + source.mp4 -> clip.mp4 (1080x1920) + thumb.jpg.

* Cuts start..end from analysis.json with a re-encode (-ss before -i, so the cut is frame-accurate).
* Facecam in a corner (OpenCV Haar face detection) -> stacked layout; otherwise blurred background.
* Hook banner, meme caption and bold 3-words-at-a-time captions are burned in with one ASS file.
* Audio is normalised to -14 LUFS (two-pass loudnorm).
* Hardware H.264 encoder if one really works (tested once), else libx264; always below-normal priority.
* At most [render] max_renders encodes run at the same time.

Nothing here uploads anything (autopost = false). Transcript text is never written to the log.
"""
from __future__ import annotations

import contextlib
import json
import logging
import re
import threading
import time
from pathlib import Path

from .config import Config
from .db import record_files, set_clip_status
from .profanity import mask
from .proc import run
from .worker import probe_duration

log = logging.getLogger("clipper")

W, H = 1080, 1920
TOP_H = round(H * 0.35)            # 672: zoomed facecam panel
BOTTOM_H = H - TOP_H               # 1248: cropped gameplay panel
DIVIDER = 4
CAPTION_Y = 1180                   # centre of burned-in captions; the safe zone ends at y=1450
SAFE_BOTTOM = 1450
HOOK_Y = 190                       # hook top edge: below the Shorts top bar, clear of the search/camera icons
WORDS_PER_CAPTION = 3
TARGET_LUFS = -14.0
CAM_REGION = (0.45, 0.55)          # corner search area: fraction of frame width, height
CAM_SAMPLES = 24
CAM_MIN_HITS = 3                   # ...in at least this many sampled frames, all at about the same spot
CAM_MIN_HIT_RATIO = 0.12           # ...and in at least this share of them (streamers turn away a lot)
FONT_DIRS = "C:/Windows/Fonts"

ENCODER_ARGS = {
    "h264_nvenc": ["-preset", "p5", "-b:v", "10M", "-maxrate", "14M", "-bufsize", "20M"],
    "h264_qsv": ["-preset", "medium", "-b:v", "10M", "-maxrate", "14M", "-bufsize", "20M"],
    "h264_amf": ["-quality", "quality", "-b:v", "10M", "-maxrate", "14M", "-bufsize", "20M"],
    "libx264": ["-preset", "medium", "-crf", "18"],
}
_encoder_cache: dict[tuple, str] = {}
_render_slots: dict[int, threading.BoundedSemaphore] = {}
_slots_lock = threading.Lock()


class RenderError(Exception):
    pass


def _ffmpeg(cfg: Config, args: list, timeout: int = 1800) -> str:
    from .worker import ClipError, _ffmpeg as base  # lazy: worker imports this module lazily too
    try:
        base(cfg, args, timeout)
    except ClipError as exc:
        raise RenderError(str(exc))
    return ""


def _ffmpeg_out(cfg: Config, args: list, timeout: int = 600) -> tuple[int, str]:
    """ffmpeg run that returns (code, output); same low-priority launch as every other ffmpeg."""
    import shutil
    exe = shutil.which(cfg.ffmpeg) or cfg.ffmpeg
    low = cfg.raw.get("encoding", {}).get("below_normal_priority", True)
    return run([exe, "-hide_banner", "-nostats", "-y", *args], timeout=timeout, low_priority=low)


# ------------------------------------------------------------------ encoder choice
def pick_encoder(cfg: Config) -> str:
    """First hardware encoder that really encodes a tiny test clip, else the fallback (libx264)."""
    enc = cfg.raw.get("encoding", {})
    candidates = list(enc.get("encoders", ["h264_nvenc", "h264_qsv", "h264_amf"])) if enc.get("prefer_hardware", True) else []
    fallback = enc.get("fallback", "libx264")
    key = (tuple(candidates), fallback)
    if key in _encoder_cache:
        return _encoder_cache[key]
    chosen = fallback
    for name in candidates:
        code, _ = _ffmpeg_out(cfg, ["-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.4:r=30", "-c:v", name,
                                    "-pix_fmt", "yuv420p", "-f", "null", "-"], timeout=60)
        if code == 0:
            chosen = name
            break
        log.info("encoder %s not usable here, trying the next one", name)
    log.info("render encoder: %s", chosen)
    _encoder_cache[key] = chosen
    return chosen


# ------------------------------------------------------------------ facecam detection
def detect_facecam(source: Path, start: float, end: float) -> dict | None:
    """Look for a face in the four corners of sampled frames.

    -> {"corner": "bottom-left", "face": (x, y, w, h), "hits": n, "samples": n} or None."""
    import cv2
    base = Path(cv2.data.haarcascades)
    # (cascade, flip, minNeighbors): streamers often sit side-on, so profile (both directions) counts too
    detectors = [(cv2.CascadeClassifier(str(base / "haarcascade_frontalface_default.xml")), False, 6),
                 (cv2.CascadeClassifier(str(base / "haarcascade_profileface.xml")), False, 5),
                 (cv2.CascadeClassifier(str(base / "haarcascade_profileface.xml")), True, 5)]
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        return None
    try:
        fw, fh = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        rw, rh = int(fw * CAM_REGION[0]), int(fh * CAM_REGION[1])
        corners = {"top-left": (0, 0), "top-right": (fw - rw, 0),
                   "bottom-left": (0, fh - rh), "bottom-right": (fw - rw, fh - rh)}
        found: dict[str, list] = {c: [] for c in corners}
        samples = 0
        for i in range(CAM_SAMPLES):
            cap.set(cv2.CAP_PROP_POS_MSEC, (start + (i + 0.5) / CAM_SAMPLES * (end - start)) * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            samples += 1
            for name, (ox, oy) in corners.items():
                gray = cv2.cvtColor(frame[oy:oy + rh, ox:ox + rw], cv2.COLOR_BGR2GRAY)
                faces = []
                for cascade, flip, neighbors in detectors:
                    img = cv2.flip(gray, 1) if flip else gray
                    for x, y, w, h in cascade.detectMultiScale(img, scaleFactor=1.1, minNeighbors=neighbors,
                                                               minSize=(max(24, fw // 32),) * 2,
                                                               maxSize=(rw // 2, rh // 2)):
                        faces.append((rw - x - w if flip else x, y, w, h))
                if faces:
                    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
                    found[name].append((x + ox, y + oy, w, h))
    finally:
        cap.release()
    if samples == 0:
        return None
    best = max(found, key=lambda c: len(found[c]))
    hits = found[best]
    if len(hits) >= 2:  # drop stray detections that are not where the face usually is
        mx = sorted(h[0] + h[2] / 2 for h in hits)[len(hits) // 2]
        my = sorted(h[1] + h[3] / 2 for h in hits)[len(hits) // 2]
        mw = sorted(h[2] for h in hits)[len(hits) // 2]
        hits = [h for h in hits if abs(h[0] + h[2] / 2 - mx) < mw and abs(h[1] + h[3] / 2 - my) < mw]
    if len(hits) < max(CAM_MIN_HITS, round(samples * CAM_MIN_HIT_RATIO)):
        return None
    med = [sorted(v[i] for v in hits)[len(hits) // 2] for i in range(4)]
    return {"corner": best, "face": tuple(int(v) for v in med), "hits": len(hits), "samples": samples,
            "frame": (fw, fh)}


def facecam_crop(cam: dict) -> tuple[int, int, int, int]:
    """Crop box (w, h, x, y) around the face with the top panel's aspect ratio, inside the frame."""
    fw, fh = cam["frame"]
    x, y, w, h = cam["face"]
    aspect = W / TOP_H
    cw = min(max(w * 5.0, fw * 0.22), fw * 0.6)  # whole cam box, not a blown-up face
    ch = cw / aspect
    if ch > fh:
        ch, cw = fh, fh * aspect
    cw, ch = int(cw) // 2 * 2, int(ch) // 2 * 2
    cx = min(max(x + w / 2 - cw / 2, 0), fw - cw)
    cy = min(max(y + h / 2 - ch / 2, 0), fh - ch)
    return cw, ch, int(cx) // 2 * 2, int(cy) // 2 * 2


def gameplay_crop(fw: int, fh: int) -> tuple[int, int, int, int]:
    """Centre crop of the frame with the bottom panel's aspect ratio (1080:1248)."""
    gw = min(fw, int(fh * W / BOTTOM_H)) // 2 * 2
    return gw, fh // 2 * 2, (fw - gw) // 2 // 2 * 2, 0


# ------------------------------------------------------------------ captions (ASS)
def _ass_time(t: float) -> str:
    t = max(t, 0.0)
    cs = round(t * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _ass_text(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def caption_groups(words: list, start: float, end: float, per: int = WORDS_PER_CAPTION) -> list[tuple[float, float, str]]:
    """[(from_s, to_s, 'THREE WORDS')] relative to the cut, from whisper word timestamps (masked)."""
    inside = [(a, b, mask(str(w)).strip()) for a, b, w in words if a >= start - 0.05 and a < end and str(w).strip()]
    out = []
    for i in range(0, len(inside), per):
        grp = inside[i:i + per]
        nxt = inside[i + per][0] if i + per < len(inside) else None
        a = max(grp[0][0] - start, 0.0)
        b = grp[-1][1] - start
        if nxt is not None and nxt - start - b < 0.6:
            b = nxt - start                    # no flicker between back-to-back groups
        else:
            b += 0.25
        out.append((a, min(b, end - start), _ass_text(" ".join(g[2] for g in grp).upper())))
    return out


HOOK_COLOUR = "&H00FFFFFF&"
ACCENT = "&H0000D4FF&"          # ASS colour (BGR): golden yellow #FFD400 for the key word, same as the badge
ACCENT_WORDS = {"FREE", "LEAK", "LEAKED", "NEW", "BREAKING", "DELAYED", "CONFIRMED", "REVEALED", "BANNED", "EXCLUSIVE",
                "RELEASE", "DLC", "UPDATE", "WIN", "WINS", "FINALLY", "SECRET", "HUGE", "INSANE", "OFFICIAL"}


def pick_accent(words: list[str]) -> int | None:
    """Index of the word to highlight: a number / price, a news trigger word, else the longest word (>= 6 letters)."""
    plain = [re.sub(r"[^\w$%]", "", w).upper() for w in words]
    for i, w in enumerate(plain):
        if any(c.isdigit() for c in w) or w in ACCENT_WORDS:
            return i
    best = max(range(len(plain)), key=lambda i: len(plain[i]), default=None)
    return best if best is not None and len(plain[best]) >= 6 and len(words) > 1 else None


def accent_text(text: str, accent: str = ACCENT, base: str = "&H00FFFFFF&", index: int | None = -1) -> str:
    """ASS text with one word in the accent colour (then back to `base`). Authors may force words with *word*;
    otherwise pick_accent() chooses (index=None: no highlight)."""
    def mark(w: str) -> str:
        return "{" + "\\c" + accent + "}" + w + "{" + "\\c" + base + "}"
    forced = re.findall(r"\*([^*]+)\*", text)
    if forced:
        marked = {w for chunk in forced for w in chunk.split(" ")}
        return " ".join(mark(w) if w in marked else w for w in text.replace("*", "").split(" "))
    words = text.split(" ")
    i = pick_accent(words) if index == -1 else index
    if i is None:
        return text
    words[i] = mark(words[i])
    return " ".join(words)


def pop_tags(ms: int = 220, start_pct: int = 72, peak_pct: int = 112, fade_ms: int = 90) -> str:
    """Override tags: fade in + a quick scale pop (start small, overshoot, settle at 100%)."""
    a = int(ms * 0.6)
    return (r"\fad(%d,60)\fscx%d\fscy%d\t(0,%d,\fscx%d\fscy%d)\t(%d,%d,\fscx100\fscy100)"
            % (fade_ms, start_pct, start_pct, a, peak_pct, peak_pct, a, ms))


# ---- word-by-word pop captions: 2-3 words per line, the spoken word lights up and pops ----
CAP_WHITE = "&H00FFFFFF&"
CAP_SPOKEN = "&H0000D4FF&"           # golden yellow #FFD400 (BGR): the word being spoken
CAP_TRIGGER = "&H006EFF3D&"          # bright green #3DFF6E: the spoken word when it is a number / price / news trigger word
CAP_LINE_H = 1.25                    # line height as a multiple of the font size
SENTENCE_END = re.compile(r"[.!?…][\"')\]]*$")


def _balanced_chunks(n: int, per: int) -> list[int]:
    """Sizes of the lines for a run of n words, at most `per` each and as even as possible (7 -> 3,2,2; 4 -> 2,2)."""
    k = -(-n // per)
    base, extra = divmod(n, k)
    return [base + (1 if i < extra else 0) for i in range(k)]


def _split_run(run: list, per: int) -> list[list]:
    out, i = [], 0
    for size in (_balanced_chunks(len(run), per) if run else []):
        out.append(run[i:i + size])
        i += size
    return out


def word_lines(words: list, start: float, end: float, per: int = 3) -> list[list[tuple[float, float, str]]]:
    """Caption lines of 2-3 (sometimes 1) words from word timestamps [[from_s, to_s, word], ...], times relative to
    `start`. A line never runs across a sentence end. Profanity is masked. Each word keeps its own start / end."""
    inside = [(a - start, b - start, mask(str(w)).strip()) for a, b, w in words
              if a >= start - 0.05 and a < end and str(w).strip()]
    lines, run = [], []
    for item in inside:
        run.append(item)
        if SENTENCE_END.search(item[2]):
            lines += _split_run(run, per)
            run = []
    lines += _split_run(run, per)
    return lines


def pop_caption_events(words: list, start: float, end: float, lang: str = "en", y: int = CAPTION_Y, per: int = 3,
                       layer: int = 1, size: int | None = None, style: str = "Cap") -> list[str]:
    """ASS Dialogue lines: for every spoken word one event that shows its whole line with THAT word in golden yellow
    (bright green for numbers / trigger words) and a quick pop (80% -> 118% -> 100%); the line fades in on its first word.
    Centred on y (default 1180); a two-line caption of this font size is kept above the y=1450 safe-zone limit.
    Devanagari (lang 'hi') is case-less and uses the Nirmala UI / Mangal font set by caption_fonts()."""
    size = size or caption_fonts(lang)["cap_size"]
    if y + size * CAP_LINE_H * 1.1 > SAFE_BOTTOM:
        y = int(SAFE_BOTTOM - size * CAP_LINE_H * 1.1)
    horizon = end - start
    lines = word_lines(words, start, end, per)
    events = []
    for li, line in enumerate(lines):
        texts = [_ass_text(w.upper()) for _, _, w in line]
        nxt = lines[li + 1][0][0] if li + 1 < len(lines) else None
        line_end = line[-1][1] + 0.25                              # hold a beat after the last word...
        if nxt is not None and nxt - line[-1][1] < 0.6:
            line_end = nxt                                         # ...or until the next line when it follows right away
        line_end = min(line_end, horizon)
        for wi, (a, _b, _w) in enumerate(line):
            t0 = max(a, 0.0)
            t1 = max(line[wi + 1][0] if wi + 1 < len(line) else line_end, t0 + 0.08)
            plain = re.sub(r"[^\w]", "", texts[wi])
            hl = CAP_TRIGGER if (any(c.isdigit() for c in plain) or plain in ACCENT_WORDS) else CAP_SPOKEN
            parts = []
            for j, t in enumerate(texts):
                if j == wi:
                    parts.append("{\\c%s\\fscx80\\fscy80\\t(0,90,\\fscx118\\fscy118)\\t(90,190,\\fscx100\\fscy100)}%s{\\c%s\\fscx100\\fscy100}"
                                 % (hl, t, CAP_WHITE))
                else:
                    parts.append(t)
            fad = "\\fad(60,0)" if wi == 0 else ""
            events.append(f"Dialogue: {layer},{_ass_time(t0)},{_ass_time(t1)},{style},,0,0,0,,"
                          f"{{\\an5\\pos({W // 2},{y}){fad}}}{' '.join(parts)}")
    return events


def stream_lang(payload: dict) -> str:
    """Language of the stream ('hi' for CarryMinati...), from config via payload.json; whisper's guess as backup."""
    lang = payload.get("stream", {}).get("lang") or payload.get("transcript", {}).get("language") or "en"
    return str(lang).lower().split("-")[0]


# Devanagari needs a font that has the glyphs; Impact and Arial do not (boxes appear instead of Hindi).
DEVANAGARI_LANGS = {"hi", "mr", "ne", "sa"}
DEVANAGARI_FONTS = [("Nirmala UI", "Nirmala.ttf"), ("Nirmala UI", "Nirmala.ttc"), ("Mangal", "mangal.ttf")]


def caption_fonts(lang: str, fonts_dir: Path | str = FONT_DIRS) -> dict:
    """Font family for each ASS style. English keeps Impact/Arial; Hindi uses a Windows Devanagari font."""
    if lang not in DEVANAGARI_LANGS:
        return {"Hook": "Impact", "Cap": "Impact", "Meme": "Arial", "bold": 0, "cap_size": 100, "hook_size": 84,
                "head": "Segoe UI Black" if (Path(fonts_dir) / "seguibl.ttf").is_file() else "Arial"}
    family = next((fam for fam, f in DEVANAGARI_FONTS if (Path(fonts_dir) / f).is_file()), DEVANAGARI_FONTS[0][0])
    # Nirmala UI is bold-capable and wider than Impact, so sizes come down a little
    return {"Hook": family, "Cap": family, "Meme": family, "bold": -1, "cap_size": 84, "hook_size": 70, "head": family}


def build_ass(analysis: dict, words: list, start: float, end: float, layout: str, lang: str = "en",
              fonts_dir: Path | str = FONT_DIRS) -> str:
    dur = end - start
    hook = _ass_text(mask(analysis["hook"]).upper())
    f = caption_fonts(lang, fonts_dir)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,{f['Hook']},{f['hook_size']},&H0000F0FF,&H000000FF,&H00000000,&H96000000,{f['bold']},0,0,0,100,100,1,0,1,9,5,8,90,90,0,1
Style: Cap,{f['Cap']},{f['cap_size']},&H00FFFFFF,&H000000FF,&H00000000,&H96000000,{f['bold']},0,0,0,100,100,1,0,1,9,5,5,90,90,0,1
Style: Meme,{f['Meme']},46,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,4,0,8,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [f"Dialogue: 2,{_ass_time(0)},{_ass_time(dur)},Hook,,0,0,0,,{{\\an8\\pos({W // 2},{HOOK_Y}){pop_tags(260)}}}{accent_text(hook, ACCENT, HOOK_COLOUR)}"]
    lines += pop_caption_events(words, start, end, lang)
    if layout == "blur":  # the free space under the centred 16:9 gameplay, kept above the safe-zone limit
        meme = [_ass_text(mask(m)) for m in analysis.get("meme_caption", [])][:4]
        if meme:
            size = 46 if len(meme) <= 3 else 36
            y = (H + round(W * 9 / 16)) // 2 + 14
            lines.append(f"Dialogue: 0,{_ass_time(0)},{_ass_time(dur)},Meme,,0,0,0,,"
                         f"{{\\an8\\pos({W // 2},{y})\\fs{size}}}" + "\\N".join(meme))
    return header + "\n".join(lines) + "\n"


# ------------------------------------------------------------------ filter graph
def _fp(path: Path) -> str:
    """Path as a filtergraph value inside single quotes."""
    return str(path.resolve()).replace("\\", "/").replace(":", "\\:")


def build_filter(layout: str, src_wh: tuple[int, int], cam: dict | None, ass: Path,
                 wm: tuple[str, str] | None = None) -> str:
    """Filter graph, input 0 = the video. wm = (chain ending in [wm], 'overlay=x:y') when input 1 is the
    watermark PNG: it is laid over the picture BEFORE the captions, so captions always sit on top of it."""
    fw, fh = src_wh
    sub = f"ass=filename='{_fp(ass)}':fontsdir='{_fp(Path(FONT_DIRS))}'"
    tail = f"[vs];{wm[0]};[vs][wm]{wm[1]},{sub},format=yuv420p[v]" if wm else f",{sub},format=yuv420p[v]"
    if layout == "stack" and cam:
        cw, ch, cx, cy = facecam_crop(cam)
        gw, gh, gx, gy = gameplay_crop(fw, fh)
        return (f"[0:v]split=2[a][b];"
                f"[a]crop={cw}:{ch}:{cx}:{cy},scale={W}:{TOP_H}:flags=lanczos[top];"
                f"[b]crop={gw}:{gh}:{gx}:{gy},scale={W}:{BOTTOM_H}:flags=lanczos[bot];"
                f"[top][bot]vstack=inputs=2,drawbox=x=0:y={TOP_H - DIVIDER // 2}:w={W}:h={DIVIDER}:color=white@0.95:t=fill"
                + tail)
    return (f"[0:v]split=2[a][b];"
            f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,gblur=sigma=6,"
            f"eq=brightness=-0.08:saturation=1.1,scale={W}:{H}:flags=bicubic[bg];"
            f"[b]scale={W}:-2:flags=lanczos[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2" + tail)


def watermark_parts(cfg: Config, index: int = 1) -> tuple[Path, tuple[str, str]] | None:
    """(png, (chain, overlay)) for the channel watermark (ffmpeg input number `index`), or None when disabled."""
    from . import watermark
    png = watermark.ensure_watermark(cfg)
    if not png:
        return None
    return png, (watermark.overlay_filter(cfg, index), watermark.overlay_position(cfg, SAFE_BOTTOM, W))


# ------------------------------------------------------------------ audio
def measure_loudness(cfg: Config, source: Path, start: float, dur: float) -> dict | None:
    code, out = _ffmpeg_out(cfg, ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", source, "-vn",
                                  "-af", f"loudnorm=I={TARGET_LUFS:g}:TP=-1.5:LRA=11:print_format=json",
                                  "-f", "null", "-"])
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", out, re.S)
    if code != 0 or not m:
        return None
    try:
        d = json.loads(m.group(0))
        vals = {k: float(d[k]) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}
    except (ValueError, KeyError):
        return None
    return vals if all(abs(v) < 200 for v in vals.values()) else None  # '-inf' on silence -> single pass


def gain_chain(gain_db: float) -> str:
    return f"volume={gain_db:.2f}dB,alimiter=limit=0.89:level=disabled,aresample=48000"


def audio_filter(measured: dict | None) -> str:
    """-14 LUFS. With a measurement: exact gain + peak limiter (loudnorm's linear mode refuses to reach
    the target when the clip's peaks would pass the true-peak cap). Without: one-pass loudnorm."""
    if measured:
        return gain_chain(TARGET_LUFS - measured["input_i"])
    return f"loudnorm=I={TARGET_LUFS:g}:TP=-1.0:LRA=11,aresample=48000"


def loudness_after(cfg: Config, source: Path, start: float, dur: float, chain: str) -> float | None:
    """Integrated loudness (LUFS) of the cut after `chain`, via ebur128."""
    _, out = _ffmpeg_out(cfg, ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", source, "-vn",
                               "-af", f"{chain},ebur128=framelog=quiet", "-f", "null", "-"])
    m = re.findall(r"\bI:\s+(-?\d+(?:\.\d+)?) LUFS", out)
    return float(m[-1]) if m else None


def audio_chain(cfg: Config, source: Path, start: float, dur: float) -> str:
    """The -af chain that brings source[start:start+dur] to -14 LUFS (measured, then corrected for the limiter)."""
    measured = measure_loudness(cfg, source, start, dur)
    af = audio_filter(measured)
    if measured:  # the limiter eats some loudness on peaky audio: measure the result, correct once
        gain = TARGET_LUFS - measured["input_i"]
        for _ in range(3):
            got = loudness_after(cfg, source, start, dur, gain_chain(gain))
            if got is None or abs(got - TARGET_LUFS) <= 0.2:
                break
            gain += TARGET_LUFS - got
            af = gain_chain(gain)
    return af


def has_audio(cfg: Config, source: Path) -> bool:
    _, out = _ffmpeg_out(cfg, ["-i", source], timeout=60)
    return bool(re.search(r"Stream #\d+:\d+.*Audio:", out))


# ------------------------------------------------------------------ render
@contextlib.contextmanager
def _machine_slot(folder: Path, n: int):
    """One of `n` render slots shared by EVERY clipper process (gta.bat next to news.bat or run.bat): a locked byte in
    <data>/render-<i>.lock, which Windows releases by itself if a process dies."""
    try:
        import msvcrt
    except ImportError:                                 # not Windows: the in-process limit still applies
        yield
        return
    folder.mkdir(parents=True, exist_ok=True)
    handles = [open(folder / f"render-{i}.lock", "a+b") for i in range(n)]
    held, told = None, False
    try:
        while held is None:
            for handle in handles:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    held = handle
                    break
                except OSError:
                    continue
            else:
                if not told:
                    log.info("render: waiting for another clipper window to finish its render")
                    told = True
                time.sleep(2)
        yield
    finally:
        if held:
            held.seek(0)
            msvcrt.locking(held.fileno(), msvcrt.LK_UNLCK, 1)
        for handle in handles:
            handle.close()


@contextlib.contextmanager
def _slot(cfg: Config):
    """At most [render] max_renders encodes at once, across threads and processes."""
    n = max(1, int(cfg.render["max_renders"]))
    with _slots_lock:
        gate = _render_slots.setdefault(n, threading.BoundedSemaphore(n))
    with gate, _machine_slot(cfg.data_dir, n):
        yield


def render_video(cfg: Config, folder: Path, layout: str = "auto") -> dict:
    """Render clip.mp4 + thumb.jpg for a clip folder. Raises RenderError; see render_clip for the safe entry."""
    import cv2
    analysis = json.loads((folder / "analysis.json").read_text(encoding="utf-8"))
    payload = json.loads((folder / "payload.json").read_text(encoding="utf-8"))
    credit = str(analysis.get("credit_line", ""))
    if not credit.startswith("Sources & Credits:"):
        raise RenderError("analysis.json has no 'Sources & Credits:' line - export is invalid")
    source = folder / payload["clip"]["source"]
    if not source.is_file():
        raise RenderError(f"{source.name} is missing")
    start, end = float(analysis["start"]), float(analysis["end"])
    if not 0 <= start < end:
        raise RenderError("analysis.json start/end are invalid")
    dur = end - start

    cap = cv2.VideoCapture(str(source))
    src_wh = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
    cap.release()
    if not all(src_wh):
        raise RenderError("could not read the source video size")

    # a facecam does not move during a stream, so look at the whole source, not just the cut
    cam = detect_facecam(source, 0.0, probe_duration(cfg, source)) if layout in ("auto", "stack") else None
    if layout == "stack" and not cam:
        raise RenderError("layout=stack requested but no facecam was found")
    used = "stack" if cam else "blur"
    words = payload.get("transcript", {}).get("words", [])
    ass = folder / "captions.ass"
    ass.write_text(build_ass(analysis, words, start, end, used, stream_lang(payload)), encoding="utf-8")
    wm = watermark_parts(cfg)
    graph = build_filter(used, src_wh, cam, ass, wm[1] if wm else None)

    encoder = pick_encoder(cfg)
    audio = has_audio(cfg, source)
    args = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", source]
    if wm:
        args += ["-i", wm[0]]                         # input 1: the watermark PNG (one frame, held for the whole clip)
    args += ["-filter_complex", graph, "-map", "[v]"]
    if audio:
        af = audio_chain(cfg, source, start, dur)
        args += ["-map", "0:a:0", "-af", af,
                 "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    else:
        args += ["-an"]
    meta = f"{analysis['title']}"
    args += ["-r", "30", "-c:v", encoder, *ENCODER_ARGS[encoder], "-pix_fmt", "yuv420p", "-movflags", "+faststart",
             "-metadata", f"title={meta}", "-metadata", f"description={analysis['description_full']}",
             "-metadata", f"comment={credit}"]
    out = folder / "clip.mp4"
    tmp = folder / "clip.part.mp4"
    try:
        with _slot(cfg):
            _ffmpeg(cfg, [*args, tmp])
        tmp.replace(out)
        thumb_at = min(1.5, dur / 2)  # after the hook and first caption are on screen
        _ffmpeg(cfg, ["-ss", f"{thumb_at:.2f}", "-i", out, "-frames:v", "1", "-q:v", "2", folder / "thumb.jpg"])
    finally:
        tmp.unlink(missing_ok=True)
        ass.unlink(missing_ok=True)
    info = {"layout": used, "facecam": cam, "encoder": encoder, "duration_s": round(dur, 2), "audio": audio}
    log.info("[%s] rendered %s layout, encoder %s, %.1fs", folder.name, used, encoder, dur)
    return info


def render_clip(cfg: Config, folder: Path, layout: str = "auto") -> str:
    """Safe entry point: returns 'rendered', 'skipped' or 'failed'; never raises."""
    name = folder.name
    try:
        analysis = json.loads((folder / "analysis.json").read_text(encoding="utf-8"))
        if analysis.get("status") != "edited" or not analysis.get("post"):
            log.info("[%s] not rendering: editor status is %s", name, analysis.get("status"))
            return "skipped"
        info = render_video(cfg, folder, layout)
        analysis["render"] = info
        (folder / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
        set_clip_status(cfg.database, name, "rendered")
        record_files(cfg.database, name, folder)
        from .notify import notify_rendered  # toast for banger / high-score clips; never raises
        notify_rendered(cfg, name)
        from .routing import assign_routes  # advisory platform/slot targets; never raises
        assign_routes(cfg)
        return "rendered"
    except Exception as exc:
        log.error("[%s] render failed: %s", name, exc)
        try:
            set_clip_status(cfg.database, name, "failed", f"render: {exc}"[:300])
        except Exception:
            pass
        return "failed"
