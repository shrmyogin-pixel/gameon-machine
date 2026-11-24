"""Real article pictures for news Shorts: 3-5 distinct, high-resolution visuals per story.

Where they come from, best first: the article page's og:image / twitter:image, the pictures the RSS entry carries
(media:content / media:thumbnail / enclosure / inline <img>), then the article's own body images: <picture>/<source>,
srcset, data-src and plain <img> tags (inside <article> / <main> first). Parsed with BeautifulSoup. Icons, logos,
avatars, ads, tracking pixels, SVG/GIF and anything smaller than MIN_WIDTH x MIN_HEIGHT or banner-shaped are dropped,
and so are near-duplicates (the same photo at another size, found by a perceptual hash).

Everything is optional: any failure just returns fewer pictures. The caller (news.collect_visuals) tops up with
topic-matched game screenshots from imagesearch.py when fewer than 4 are found. There are no placeholder cards.

Safety: only http(s); the host (and every redirect hop) must resolve to a public address (no localhost, LAN or
link-local); page and image downloads are size- and time-limited; the bytes must decode as a real picture of a
sensible shape before they are saved as <folder>/news_image_<n>.jpg. The pictures belong to the source outlet: the
Short's credit line names the outlet and the exact article URL.
"""
from __future__ import annotations

import html
import ipaddress
import logging
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("clipper")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")      # a plain bot UA gets 403 from many image CDNs
MAX_PAGE_BYTES = 1_500_000
MAX_IMAGE_BYTES = 12_000_000
MIN_WIDTH, MIN_HEIGHT = 600, 300          # "high-res": smaller pictures look soft on a 1080x1920 Short
MAX_ASPECT = 2.4                          # width / height: wider than this is a banner, not a photo
MIN_ASPECT = 0.4
MAX_SIDE = 2400                           # larger pictures are shrunk before saving (the Short is 1080x1920)
MAX_ATTEMPTS = 14                         # downloads tried per story
DEADLINE_SECONDS = 90                     # whole picture hunt per story
DUPLICATE_BITS = 6                        # perceptual-hash distance (of 64) at or below which two pictures are the same
IMAGE_NAME = "news_image_{n}.jpg"
META_KEYS = ("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src")
JUNK = re.compile(r"(logo|favicon|sprite|avatar|icon|placeholder|blank|pixel|1x1|gravatar|emoji|badge|spacer|"
                  r"(^|[-_/.])(ads?|advert\w*|banner|sponsor\w*|promo|tracking|share|button|author|profile)([-_/.]|$))", re.I)
SKIP_PARENTS = {"nav", "header", "footer", "aside", "form", "button", "svg", "script", "style", "noscript"}
SIZE_SUFFIX = re.compile(r"-\d{2,4}x\d{2,4}(?=\.\w+$)")


# ------------------------------------------------------------------ finding candidate URLs
def _http(url: str | None) -> str | None:
    url = (url or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else None


def _num(v) -> int:
    try:
        return int(float(re.sub(r"[a-z%]+$", "", str(v).strip().lower())))
    except (TypeError, ValueError):
        return 0


def _usable_url(url: str | None, name_blob: str = "") -> bool:
    if not url or not _http(url):
        return False
    path = urllib.parse.urlsplit(url).path.lower()
    if path.endswith((".svg", ".gif", ".ico")):
        return False
    return not JUNK.search(path.rsplit("/", 1)[-1]) and not JUNK.search(name_blob or "")


def images_from_entry(entry) -> list[str]:
    """Every usable picture URL an RSS entry carries itself, widest declared first."""
    cands: list[tuple[int, str]] = []

    def add(url, width=0):
        url = _http(html.unescape(url or ""))
        if _usable_url(url):
            cands.append((_num(width), url))

    for m in entry.get("media_content") or []:
        if str(m.get("medium", "")) == "image" or str(m.get("type", "")).startswith("image/") \
                or re.search(r"\.(jpe?g|png|webp)(\?|$)", m.get("url", ""), re.I):
            add(m.get("url"), m.get("width"))
    for m in entry.get("media_thumbnail") or []:
        add(m.get("url"), m.get("width"))
    for link in list(entry.get("enclosures") or []) + list(entry.get("links") or []):
        if str(link.get("type", "")).startswith("image/") and link.get("rel", "enclosure") == "enclosure":
            add(link.get("href") or link.get("url"))
    blobs = [c.get("value", "") for c in entry.get("content") or []] + [entry.get("summary", ""), entry.get("description", "")]
    for blob in blobs:
        for m in re.finditer(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", blob or "", re.I):
            add(m.group(1))
    seen, out = set(), []
    for _, url in sorted(cands, key=lambda c: -c[0]):          # stable: equal widths keep feed order
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def image_from_entry(entry) -> str | None:
    found = images_from_entry(entry)
    return found[0] if found else None


def _best_from_srcset(srcset: str) -> str | None:
    best, best_w = None, -1
    for part in re.split(r",\s+", (srcset or "").strip()):
        bits = part.strip().split()
        if not bits:
            continue
        w = _num(bits[1]) if len(bits) > 1 and bits[1].endswith("w") else 0
        if w > best_w:
            best, best_w = bits[0], w
    return best


def _soup(page: str):
    from bs4 import BeautifulSoup
    try:
        return BeautifulSoup(page, "lxml")
    except Exception:                                   # lxml missing / confused: the stdlib parser always works
        return BeautifulSoup(page, "html.parser")


class _PageParser:
    """og/twitter meta tags and every candidate <img> / <picture><source> of a page (BeautifulSoup), each with whether
    it sits inside <article> / <main>. Pictures inside nav / header / footer / aside / forms are ignored."""

    def __init__(self):
        self.found: dict[str, str] = {}
        self.imgs: list[tuple[str, bool, str]] = []

    def feed(self, page: str) -> None:
        soup = _soup(page)
        for tag in soup.find_all("meta"):
            key = (tag.get("property") or tag.get("name") or "").lower()
            content = (tag.get("content") or "").strip()
            if key in META_KEYS and content:
                self.found.setdefault(key, content)
        for tag in soup.find_all(["img", "source"]):
            if tag.name == "source" and (tag.parent is None or tag.parent.name != "picture"):
                continue
            if any(p.name in SKIP_PARENTS for p in tag.parents if p.name):
                continue
            url = (_best_from_srcset(tag.get("srcset") or tag.get("data-srcset") or "") or tag.get("data-src")
                   or tag.get("data-lazy-src") or tag.get("data-original") or tag.get("src"))
            w, h = _num(tag.get("width")), _num(tag.get("height"))
            if url and not (0 < w < 300) and not (0 < h < 150) and not url.startswith("data:"):
                blob = " ".join([" ".join(tag.get("class") or []), tag.get("alt") or "", tag.get("id") or ""])
                in_article = any(p.name in ("article", "main") for p in tag.parents if p.name)
                self.imgs.append((url, in_article, blob))


def article_images(page: str, base_url: str) -> list[str]:
    """Picture URLs from an article page, best first: og/twitter image, then body images (article/main first)."""
    parser = _PageParser()
    try:
        parser.feed(page)
    except Exception:
        pass
    out: list[str] = []

    def add(raw, blob=""):
        url = _http(urllib.parse.urljoin(base_url, html.unescape(raw or "").strip()))
        if _usable_url(url, blob) and url not in out:
            out.append(url)
    for key in META_KEYS:
        if key in parser.found:
            add(parser.found[key])
    for want_article in (True, False):
        for url, in_article, blob in parser.imgs:
            if in_article == want_article:
                add(url, blob)
    return out


def og_images(page: str, base_url: str) -> list[str]:
    """Only the og:image / twitter:image URLs of a page, best first, made absolute."""
    parser = _PageParser()
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


def upsized(url: str) -> str | None:
    """The same picture asked for at a larger size (?w=300 -> ?w=1600), or None when the URL has no size parameter."""
    parts = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    changed = False
    out = []
    for k, v in q:
        if k.lower() in ("w", "width") and 0 < _num(v) < 1200:
            v, changed = "1600", True
        elif k.lower() in ("h", "height") and changed:
            continue
        out.append((k, v))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(out))) if changed else None


def _identity(url: str) -> str:
    """Same picture, different size/query -> same identity (host + path without WordPress-style size suffixes)."""
    parts = urllib.parse.urlsplit(url)
    return (parts.netloc.lower() + SIZE_SUFFIX.sub("", parts.path.lower()))


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


def download(url: str, limit: int, timeout: float, truncate: bool = False, referer: str | None = None) -> tuple[bytes, str]:
    """-> (bytes, content type). Raises on anything unsafe or slow, and on too big unless `truncate` (pages: the
    tags we need are early, so the first `limit` bytes are enough)."""
    if not public_host(url):
        raise ValueError("not a public http(s) address")
    opener = urllib.request.build_opener(_SafeRedirect)
    headers = {"User-Agent": USER_AGENT, "Accept": "image/*,text/html;q=0.8,*/*;q=0.5"}
    if referer:
        headers["Referer"] = referer                    # many CDNs block hotlinking unless the article page is the referer
    req = urllib.request.Request(url, headers=headers)
    with opener.open(req, timeout=timeout) as resp:
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        data = resp.read(limit + 1)
    if len(data) > limit:
        if not truncate:
            raise ValueError("download too large")
        data = data[:limit]
    return data, ctype


def download_retry(url: str, limit: int, timeout: float, truncate: bool = False, referer: str | None = None,
                   tries: int = 3) -> tuple[bytes, str]:
    """download() with clean retries: every request carries the article as Referer (hotlink protection), a 401 / 403
    is retried with the image host as Referer, timeouts and 5xx errors are retried after a short pause, 404 / unsafe hosts are not."""
    last: Exception | None = None
    for attempt in range(tries):
        try:
            return download(url, limit, timeout, truncate, referer=referer)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (400, 404, 410):
                break
            referer = referer or f"{urllib.parse.urlsplit(url).scheme}://{urllib.parse.urlsplit(url).netloc}/"
        except ValueError:
            raise                                       # not a public address / too big: retrying cannot help
        except Exception as exc:                        # timeouts, resets, DNS blips
            last = exc
        time.sleep(0.8 * (attempt + 1))
    raise last or RuntimeError("download failed")


# ------------------------------------------------------------------ validating + saving
def ahash(img) -> int:
    """64-bit average hash of a picture (near-identical pictures differ in only a few bits)."""
    import cv2
    small = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (8, 8), interpolation=cv2.INTER_AREA)
    bits = (small > small.mean()).flatten()
    return sum(1 << i for i, b in enumerate(bits) if b)


def save_picture(data: bytes, dest: Path, known_hashes: list[int] | None = None) -> dict | None:
    """Decode `data`; if it is a usable, not-yet-seen photo save it as a JPEG at `dest`.

    -> {'width', 'height', 'hash'} or None. `known_hashes`: hashes of pictures already kept (duplicates are refused)."""
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
    digest = ahash(img)
    if any(bin(digest ^ k).count("1") <= DUPLICATE_BITS for k in (known_hashes or [])):
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
    return {"width": w, "height": h, "hash": digest}


def fetch_news_images(story, folder: Path, timeout: float = 15.0, want: int = 5) -> list[dict]:
    """Download up to `want` distinct pictures for the story into folder/news_image_<n>.jpg.

    -> [{'file': Path, 'url', 'source': 'feed' | 'og:image' | 'article', 'width', 'height'}, ...]. Never raises."""
    kept: list[dict] = []
    hashes: list[int] = []
    tried: set[str] = set()
    attempts = 0
    deadline = time.monotonic() + DEADLINE_SECONDS

    def attempt(url: str, source: str) -> None:
        nonlocal attempts
        for u in [x for x in (upsized(url), url) if x]:           # a bigger version first, the original as the fallback
            ident = _identity(u)
            if len(kept) >= want or attempts >= MAX_ATTEMPTS or time.monotonic() > deadline:
                return
            if u in tried or (ident in tried and u == url):
                continue
            tried.add(u)
            attempts += 1
            try:
                data, ctype = download_retry(u, MAX_IMAGE_BYTES, timeout, referer=story.link, tries=2)
                if ctype.startswith("text/"):
                    continue
                info = save_picture(data, folder / IMAGE_NAME.format(n=len(kept) + 1), hashes)
                if info:
                    hashes.append(info.pop("hash"))
                    tried.add(ident)
                    kept.append({"file": folder / IMAGE_NAME.format(n=len(kept) + 1), "url": u, "source": source, **info})
                    return
                log.info("news image: %s skipped (too small, odd shape, flat or a duplicate)", u[:100])
            except Exception as exc:
                log.info("news image: %s failed (%s: %s)", u[:100], type(exc).__name__, str(exc)[:80])

    try:
        feed_urls = list(getattr(story, "image_urls", None) or ([story.image_url] if getattr(story, "image_url", None) else []))
        og: list[str] = []
        body: list[str] = []
        try:
            page, _ctype = download_retry(story.link, MAX_PAGE_BYTES, timeout, truncate=True, tries=2)
            text = page.decode("utf-8", errors="replace")
            og = og_images(text, story.link)
            body = [u for u in article_images(text, story.link) if u not in og]
        except Exception as exc:
            log.info("news image: article page failed (%s: %s)", type(exc).__name__, str(exc)[:80])
        order = ([(u, "og:image") for u in og[:1]] + [(u, "feed") for u in feed_urls[:2]]
                 + [(u, "og:image") for u in og[1:]] + [(u, "article") for u in body]
                 + [(u, "feed") for u in feed_urls[2:]])
        for url, source in order:
            attempt(url, source)
            if len(kept) >= want:
                break
    except Exception:
        log.exception("news image: unexpected error (ignored)")
    return kept
