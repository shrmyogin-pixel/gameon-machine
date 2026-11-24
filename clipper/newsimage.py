"""Real article pictures for news Shorts.

Order of preference: the image the RSS feed itself gives (media:content / media:thumbnail / enclosure / first <img>),
then the article page's og:image / twitter:image. Everything is optional: any failure returns None and the Short is
rendered with the animated gradient instead, so a missing picture never stops a news Short.

Safety: only http(s); the host (and every redirect hop) must resolve to a public address (no localhost, LAN or
link-local); page and image downloads are size-capped and time-limited; the bytes must decode as a real picture of a
sensible size and shape (logos, icons and banners are rejected) before they are saved as <folder>/news_image.jpg.
The picture belongs to the source outlet: the Short's credit line names the outlet and the exact article URL.
"""
from __future__ import annotations

import html
import ipaddress
import logging
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

log = logging.getLogger("clipper")

USER_AGENT = "Mozilla/5.0 (compatible; gameon-clipper/1.0)"
MAX_PAGE_BYTES = 600_000
MAX_IMAGE_BYTES = 12_000_000
MIN_WIDTH, MIN_HEIGHT = 480, 270
MAX_ASPECT = 2.6                  # width / height: wider than this is a banner, not a photo
MIN_ASPECT = 0.4
MAX_SIDE = 2400                   # larger pictures are shrunk before saving (the Short is 1080x1920)
IMAGE_NAME = "news_image.jpg"
META_KEYS = ("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src")
JUNK = re.compile(r"(logo|favicon|sprite|avatar|icon|placeholder|blank|pixel|1x1)", re.I)


# ------------------------------------------------------------------ finding candidate URLs
def _http(url: str | None) -> str | None:
    url = (url or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else None


def image_from_entry(entry) -> str | None:
    """Best picture URL an RSS entry carries itself, or None."""
    cands: list[tuple[int, str]] = []

    def add(url, width=0):
        url = _http(html.unescape(url or ""))
        if url and not JUNK.search(url.rsplit("/", 1)[-1]):
            cands.append((int(width or 0), url))

    def num(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0

    for m in entry.get("media_content") or []:
        if str(m.get("medium", "")) == "image" or str(m.get("type", "")).startswith("image/") \
                or re.search(r"\.(jpe?g|png|webp)(\?|$)", m.get("url", ""), re.I):
            add(m.get("url"), num(m.get("width")))
    for m in entry.get("media_thumbnail") or []:
        add(m.get("url"), num(m.get("width")))
    for link in list(entry.get("enclosures") or []) + list(entry.get("links") or []):
        if str(link.get("type", "")).startswith("image/") and link.get("rel", "enclosure") == "enclosure":
            add(link.get("href") or link.get("url"))
    blobs = [c.get("value", "") for c in entry.get("content") or []] + [entry.get("summary", ""), entry.get("description", "")]
    for blob in blobs:
        for m in re.finditer(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", blob or "", re.I):
            add(m.group(1))
    if not cands:
        return None
    return max(cands, key=lambda c: c[0])[1] if any(c[0] for c in cands) else cands[0][1]


class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        key = (a.get("property") or a.get("name") or "").lower()
        if key in META_KEYS and a.get("content", "").strip():
            self.found.setdefault(key, a["content"].strip())


def og_images(page: str, base_url: str) -> list[str]:
    """og:image / twitter:image URLs from an article page, best first, made absolute."""
    parser = _MetaParser()
    try:
        parser.feed(page)
    except Exception:
        pass
    out = []
    for key in META_KEYS:
        if key in parser.found:
            url = _http(urllib.parse.urljoin(base_url, html.unescape(parser.found[key])))
            if url and url not in out:
                out.append(url)
    return out


# ------------------------------------------------------------------ safe downloading
def public_host(url: str) -> bool:
    """True only when every address the host resolves to is a public one (blocks localhost / LAN / link-local)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
        return bool(infos) and all(ipaddress.ip_address(i[4][0].split("%")[0]).is_global for i in infos)
    except (OSError, ValueError):
        return False


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not public_host(urllib.parse.urljoin(req.full_url, newurl)):
            raise urllib.error.URLError("redirect to a non-public address refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str, limit: int, timeout: float, truncate: bool = False) -> tuple[bytes, str]:
    """-> (bytes, content type). Raises on anything unsafe or slow, and on too big unless `truncate` (pages: the
    og tags are in the head, so the first `limit` bytes are enough)."""
    if not public_host(url):
        raise ValueError("not a public http(s) address")
    opener = urllib.request.build_opener(_SafeRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*,text/html;q=0.8,*/*;q=0.5"})
    with opener.open(req, timeout=timeout) as resp:
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        data = resp.read(limit + 1)
    if len(data) > limit:
        if not truncate:
            raise ValueError("download too large")
        data = data[:limit]
    return data, ctype


# ------------------------------------------------------------------ validating + saving
def save_picture(data: bytes, dest: Path) -> dict | None:
    """Decode `data`; if it is a usable photo save it as a JPEG at `dest` and return {'width','height'}, else None."""
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    if w < MIN_WIDTH or h < MIN_HEIGHT or not MIN_ASPECT <= w / h <= MAX_ASPECT:
        return None
    if float(img.std()) < 6.0:                                   # an almost flat picture: a placeholder, not a photo
        return None
    if max(w, h) > MAX_SIDE:
        s = MAX_SIDE / max(w, h)
        img = cv2.resize(img, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]
    tmp = dest.with_name(dest.stem + ".part.jpg")
    try:
        if not cv2.imwrite(str(tmp), img, [cv2.IMWRITE_JPEG_QUALITY, 92]):
            return None
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return {"width": w, "height": h}


def fetch_news_image(story, folder: Path, timeout: float = 15.0) -> dict | None:
    """Download the story's real picture into folder/news_image.jpg.

    -> {'file': Path, 'url', 'source': 'feed' | 'og:image', 'width', 'height'} or None. Never raises."""
    dest = folder / IMAGE_NAME
    tried: list[str] = []

    def attempt(url: str, source: str) -> dict | None:
        if url in tried:
            return None
        tried.append(url)
        try:
            data, ctype = download(url, MAX_IMAGE_BYTES, timeout)
            if ctype.startswith("text/"):
                return None
            info = save_picture(data, dest)
            if info:
                return {"file": dest, "url": url, "source": source, **info}
            log.info("news image: %s rejected (not a usable photo)", url[:100])
        except Exception as exc:
            log.info("news image: %s failed (%s: %s)", url[:100], type(exc).__name__, str(exc)[:80])
        return None

    try:
        if getattr(story, "image_url", None):
            found = attempt(story.image_url, "feed")
            if found:
                return found
        try:
            page, ctype = download(story.link, MAX_PAGE_BYTES, timeout, truncate=True)
            text = page.decode("utf-8", errors="replace")
        except Exception as exc:
            log.info("news image: article page failed (%s: %s)", type(exc).__name__, str(exc)[:80])
            return None
        for url in og_images(text, story.link):
            found = attempt(url, "og:image")
            if found:
                return found
    except Exception:
        log.exception("news image: unexpected error (ignored)")
    return None
