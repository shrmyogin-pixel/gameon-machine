"""Channel watermark: assets/watermark.jpg -> assets/watermark.png (transparent background).

The logo is a white/silver shape with a dark outline on a near-white background. The background is the
part of the picture that (a) is close to the border colour and (b) touches the picture's edge, so the
white parts INSIDE the dark outline stay solid. The renderer overlays the PNG at low opacity in the
bottom-left corner, above the platform UI safe-zone line (y = 1450 on a 1080x1920 frame).
"""
from __future__ import annotations

import logging
from pathlib import Path

from .config import PROJECT_ROOT, Config, expand_path

log = logging.getLogger("clipper")

DEFAULTS = {"enabled": True, "source": "assets/watermark.jpg", "file": "assets/watermark.png",
            "opacity": 0.40, "width": 170, "margin": 36, "corner": "left"}


def settings(cfg: Config) -> dict:
    return {**DEFAULTS, **cfg.raw.get("watermark", {})}


def find_source(path: Path) -> Path | None:
    """assets/watermark.jpg, matching the file name case-insensitively (the file may be named .JPG)."""
    if path.is_file():
        return path
    if path.parent.is_dir():
        for p in path.parent.iterdir():
            if p.name.lower() == path.name.lower() and p.is_file():
                return p
    return None


def build_watermark(src: Path, dst: Path, tolerance: int = 30, pad: int = 6) -> Path:
    """Write dst (RGBA PNG, background transparent, cropped to the logo). Raises ValueError on a bad image."""
    import cv2
    import numpy as np

    img = cv2.imread(str(src), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"cannot read image {src}")
    border = np.concatenate([img[0], img[-1], img[:, 0], img[:, -1]]).astype(np.int16)
    bg = np.median(border, axis=0)
    near_bg = (np.abs(img.astype(np.int16) - bg).max(axis=2) <= tolerance).astype(np.uint8)
    _, labels = cv2.connectedComponents(near_bg, connectivity=4)
    edge = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
    outside = np.isin(labels, edge[edge != 0]) & (near_bg == 1)
    alpha = np.where(outside, 0, 255).astype(np.uint8)
    alpha = cv2.erode(alpha, np.ones((3, 3), np.uint8))          # drop the light halo around the outline
    alpha = cv2.GaussianBlur(alpha, (3, 3), 0)                   # soft, anti-aliased edge
    ys, xs = np.where(alpha > 8)
    if len(xs) == 0:
        raise ValueError("no logo found (the whole picture looks like background)")
    y0, y1 = max(ys.min() - pad, 0), min(ys.max() + pad + 1, img.shape[0])
    x0, x1 = max(xs.min() - pad, 0), min(xs.max() + pad + 1, img.shape[1])
    rgba = np.dstack([img, alpha])[y0:y1, x0:x1]
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(dst), rgba):
        raise ValueError(f"cannot write {dst}")
    return dst


def ensure_watermark(cfg: Config) -> Path | None:
    """Path of the transparent PNG, built from the JPG if needed. None = render without a watermark."""
    s = settings(cfg)
    if not s["enabled"]:
        return None
    png = expand_path(str(s["file"]))
    src = find_source(expand_path(str(s["source"])))
    try:
        if png.is_file() and (src is None or png.stat().st_mtime >= src.stat().st_mtime):
            return png
        if src is None:
            log.warning("watermark: neither %s nor %s exists; rendering without a watermark", png.name, s["source"])
            return None
        build_watermark(src, png)
        log.info("watermark: built %s from %s", png.name, src.name)
        return png
    except Exception as exc:
        log.warning("watermark unavailable (%s); rendering without it", exc)
        return None


def overlay_filter(cfg: Config, index: int = 1) -> str:
    """Filter chain for the watermark input (ffmpeg input number `index`), ending in [wm]; place it with overlay_position()."""
    s = settings(cfg)
    opacity = min(max(float(s["opacity"]), 0.05), 1.0)
    return f"[{index}:v]format=rgba,scale={int(s['width'])}:-1:flags=lanczos,colorchannelmixer=aa={opacity:g}[wm]"


def overlay_position(cfg: Config, safe_bottom: int, width: int) -> str:
    """overlay=x:y in a bottom corner, just above the safe-zone line so captions and the hook stay clear.

    Default is the LEFT corner: the Shorts like/comment/share rail covers the bottom-right edge."""
    s = settings(cfg)
    m = int(s["margin"])
    x = f"{width}-w-{m}" if str(s["corner"]).lower() == "right" else str(m)
    return f"overlay=x={x}:y={safe_bottom}-h-{m}"
