"""Topic diversity for the news engine: never two stories about the same game / franchise in one run, and nothing on a
game the channel already covered in the last 24 hours (unless Claude flags it as a completely new major announcement).

A "topic" is the franchise name with platforms, stores, sequel numbers and filler removed:
"GTA 6", "Grand Theft Auto VI" and "GTA VI trailer" are all the topic `gta`. Topics of every made Short are kept in the
`news_topics` table (so they survive Reject / Mark Uploaded deleting the clip row and files).
"""
from __future__ import annotations

import re
from collections import Counter

from . import db

# words that never identify a game: platforms, stores, outlets, news filler, months
GENERIC = set("""a an the and or of in on at to for with from by is are was were be been it its this that these those new
newly out now just first second third last next more most one two three best top how why what when who where all any
get gets got has have had will can could would should may might not no yes you your we our they their his her
game games gaming gamers player players play plays played playing video update updates patch patches release released
releases launch launches launched launching reveal reveals revealed trailer trailers leak leaks leaked rumor rumour
rumors report reports reportedly confirmed confirms officially official announced announces announcement free
giveaway giveaways deal deals sale sales price prices review reviews guide guides list ranked every here everything
need know knows coming comes come date dates delay delays delayed finally today tonight tomorrow week weekend
month year years season seasons dlc edition remaster remake remastered sequel mode modes pass access early beta alpha
demo store steam epic playstation xbox nintendo switch pc ps4 ps5 ps xsx series console consoles mobile ios android
sony microsoft valve gamepass prime plus ign gamespot kotaku polygon eurogamer vg247 dexerto reddit twitch youtube
bug bugs glitch glitches exploit exploits gpu drops handheld creator streamer esports tech
january february march april may june july august september october november december
says say said still back after before over under into than also like make makes made
taking take takes real really big huge major massive full""".split())

ALIASES = {"grand theft auto": "gta", "call of duty": "cod", "red dead redemption": "red dead", "the legend of zelda": "zelda",
           "legend of zelda": "zelda", "counter-strike": "counter strike", "cs2": "counter strike", "cs go": "counter strike",
           "csgo": "counter strike", "pokémon": "pokemon", "pokèmon": "pokemon"}
ROMAN = re.compile(r"^(?:i|ii|iii|iv|v|vi|vii|viii|ix|x|xi|xii|xiv|xv|xx)$")


def _norm(text: str) -> str:
    text = (text or "").lower().replace("’", "'")
    for long, short in ALIASES.items():
        text = re.sub(rf"\b{re.escape(long)}\b", short, text)
    return text


def topic_words(text: str) -> list[str]:
    """Distinctive lowercase words of a headline / subject (generic words, sequel numbers and short words dropped)."""
    out = []
    for w in re.findall(r"[a-z][a-z0-9'+-]*", _norm(text)):
        if w.endswith("'s"):
            w = w[:-2]
        if (len(w) < 3 and w not in {"gta", "cod"}) or w in GENERIC or ROMAN.match(w):
            continue
        out.append(w)
    return out


def subject_key(subject: str) -> str:
    """Canonical topic of one subject string (e.g. 'GTA 6' -> 'gta'); '' for a generic one ('PlayStation')."""
    return " ".join(topic_words(subject))


def pick_topics(subjects: list[str]) -> set[str]:
    return {k for k in (subject_key(s) for s in subjects) if k}


def share_topic(a: set[str], b: set[str]) -> bool:
    """True when two topic sets name the same game: an equal key, or one key's words contained in the other's."""
    for x in a:
        for y in b:
            xs, ys = set(x.split()), set(y.split())
            if x == y or xs <= ys or ys <= xs:
                return True
    return False


def cap_hot_topics(stories: list, per_topic: int = 2, hot_at: int = 3) -> list:
    """Candidates trimmed so a topic that floods the feeds (a word in >= `hot_at` headlines, e.g. 'gta') shows at most
    `per_topic` stories to Claude. Keeps the order (newest first)."""
    words = [set(topic_words(s.title)) for s in stories]
    counts = Counter(w for ws in words for w in ws)
    hot = {w for w, c in counts.items() if c >= hot_at}
    taken: Counter = Counter()
    kept = []
    for s, ws in zip(stories, words):
        mine = ws & hot
        if any(taken[w] >= per_topic for w in mine):
            continue
        taken.update(mine)
        kept.append(s)
    return kept


def covered_topics(cfg, since_utc: str) -> list[str]:
    """Topic sets (comma-joined) of the Shorts made since `since_utc` ('YYYY-MM-DD HH:MM:SS')."""
    try:
        return db.recent_topics(cfg.database, since_utc)
    except Exception:
        return []


def enforce(picks: list, covered: list[str], blocked: set[str] | None = None) -> tuple[list, list[str]]:
    """Keep picks best-first: drop any whose topic repeats an earlier pick in this run (or `blocked`), or a topic covered
    in the last 24 h unless pick.major_new is true. -> (kept, [why each one was dropped])."""
    seen_topics: list[set[str]] = [{k} for k in (blocked or set())]
    old = [set(c.split(",")) for c in covered]
    kept, dropped = [], []
    for p in sorted(picks, key=lambda p: -p.score):
        topics = pick_topics(p.subjects) or set(topic_words(p.story.title)[:2])
        if any(share_topic(topics, t) for t in seen_topics):
            dropped.append(f"'{p.story.title[:50]}' repeats a topic already picked in this run")
            continue
        if not getattr(p, "major_new", False) and any(share_topic(topics, t) for t in old):
            dropped.append(f"'{p.story.title[:50]}' is about a topic covered in the last 24 hours")
            continue
        seen_topics.append(topics)
        kept.append(p)
    return kept, dropped


def remember(cfg, pick) -> None:
    db.add_topics(cfg.database, sorted(pick_topics(pick.subjects)), pick.story.title)
