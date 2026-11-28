"""Pre-render source check: runs before any voiceover, B-roll download or render.

A Short is refused when it has no usable source link, states a price / percentage / larger number / month that neither
the feed entry nor the article page contains, or tells a leak or rumour without saying so. It is a mechanical safety
net, not proof that the story is true: review still matters. When the article page cannot be read the check uses the
headline and summary only, and says so.
"""
from __future__ import annotations

import logging
import re
import urllib.parse

from . import newsimage
from .config import Config

log = logging.getLogger("clipper")

MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
          "december")
NUMBER = re.compile(r"(?<![A-Za-z0-9.])([$£€₹]?)(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(%?)")
NUMBER_WORDS = {w: str(n) for n, w in enumerate(
    "ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split(), start=10)}
NUMBER_WORDS.update(thirty="30", forty="40", fifty="50", sixty="60", seventy="70", eighty="80", ninety="90", hundred="100")
HEARSAY_SOURCE = re.compile(r"\b(rumou?r(?:s|ed)?|leak(?:s|ed|er)?|reportedly|allegedly)\b|^report:", re.I)
HEDGED = re.compile(r"\b(rumou?r|leak|report|unconfirmed|alleged|claim|according to)", re.I)


def _value(whole: str, fraction: str | None) -> str:
    text = whole.replace(",", "") + (fraction or "")
    return text.rstrip("0").rstrip(".") if "." in text else text


def claimed_numbers(text: str) -> set[str]:
    """Numbers worth checking: money, percentages and anything of two or more digits."""
    out = set()
    for money, whole, fraction, percent in NUMBER.findall(text or ""):
        value = _value(whole, fraction)
        if money or percent or len(value) >= 2:
            out.add(value)
    return out


def source_numbers(text: str) -> set[str]:
    found = {_value(w, f) for _, w, f, _ in NUMBER.findall(text or "")}
    found |= {NUMBER_WORDS[w] for w in re.findall(r"[a-z]+", (text or "").lower()) if w in NUMBER_WORDS}
    return found


def months_in(text: str, claim: bool = False) -> set[str]:
    """Months named in a text. In a claim "may" counts only as a date ("May 5"); a source may abbreviate."""
    low, out = (text or "").lower(), set()
    for i, month in enumerate(MONTHS, start=1):
        if claim:
            pattern = r"\bmay\s+\d" if month == "may" else rf"\b{month}\b"
            hit = re.search(pattern, low)
        else:
            short = "sept?" if month == "september" else month[:3]
            hit = re.search(rf"\b(?:{month}|{short})\b|\b20\d\d-{i:02d}-\d\d", low)
        if hit:
            out.add(month)
    return out


def article_text(url: str, timeout: float) -> str:
    """The article's paragraphs, '' when the page cannot be read."""
    try:
        from bs4 import BeautifulSoup
        raw, _ = newsimage.download_retry(url, 3_000_000, timeout, truncate=True, tries=2)
        soup = BeautifulSoup(raw.decode("utf-8", "replace"), "html.parser")
        node = soup.find("article") or soup
        meta = [m.get("content", "") for m in soup.find_all("meta")
                if (m.get("property") or m.get("name")) in ("og:title", "og:description", "description")]
        return " ".join([*meta, *(p.get_text(" ", strip=True) for p in node.find_all(["h1", "h2", "p", "li", "time"]))])
    except Exception as exc:
        log.info("fact check: could not read the article (%s: %s)", type(exc).__name__, str(exc)[:80])
        return ""


def check(cfg: Config, pick) -> tuple[list[str], list[str]]:
    """-> (problems that stop the Short, notes for the reviewer)."""
    story = pick.story
    parts = urllib.parse.urlsplit(story.link or "")
    if parts.scheme not in ("http", "https") or not parts.hostname or not (story.outlet or "").strip():
        return ["no usable source link and outlet to credit"], []
    problems, notes = [], []
    page = ""
    if not story.trend:                                     # a trend item's link is a video or offer page, not an article
        page = article_text(story.link, float(cfg.news["image_timeout"]))
        if not page:
            notes.append("source check used the headline and summary only (article page unreadable)")
    published = story.published
    source = f"{story.title} {story.summary} {page}"
    claims = " . ".join([pick.script, pick.headline, pick.title, pick.description])
    missing = claimed_numbers(claims) - source_numbers(source) - {str(published.year)}
    if missing:
        problems.append("number(s) not in the source: " + ", ".join(sorted(missing)))
    months = months_in(claims, claim=True) - months_in(source)
    if months:
        problems.append("month(s) not in the source: " + ", ".join(sorted(months)))
    if HEARSAY_SOURCE.search(story.title) and not HEDGED.search(pick.script):
        problems.append("the source calls this a leak or rumour but the script does not")
    return problems, notes
