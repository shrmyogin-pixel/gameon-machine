"""Story buckets for the news engine, so a run mixes four kinds of Shorts instead of piling onto one game or topic:

    streamer      streamer / creator news: milestones, sub / view / record achievements, big events, drama, updates
    giveaway      legitimate giveaways: free games, Epic / Steam / console freebies, Twitch / YouTube drops, verified creator giveaways
    achievement   world firsts, records, tournament upsets, esports and community milestones, studio milestones
    whats_new     what's new and worth knowing: patches, free updates, notable releases, hidden mechanics and tips

classify() is a keyword guess used to balance the candidate list shown to Claude; Claude then names the bucket of each
pick, and enforce_mix() caps how many picks one bucket may take in a run. Pure functions, no I/O.
"""
from __future__ import annotations

import re

BUCKETS = ("streamer", "giveaway", "achievement", "whats_new")
LABELS = {"streamer": "STREAMER NEWS", "giveaway": "FREE GIVEAWAY", "achievement": "BIG ACHIEVEMENT", "whats_new": "WHAT'S NEW"}

# whole-word / phrase patterns, checked in this order (a giveaway beats a patch note that merely mentions "free")
PATTERNS = {
    "giveaway": r"\b(free|giveaways?|giving away|claim|drops?|twitch drops|prime gaming|freebies?|free-to-keep|100% off)\b",
    "streamer": r"\b(streamers?|streaming|streamed|youtubers?|creators?|content creators?|twitch|subs|subscribers?|followers?|"
                r"viewers?|ninja|kai cenat|ishowspeed|pokimane|xqc|asmongold|sypherpk|shroud|tfue|banned from twitch|"
                r"hasan|dr disrespect|mrbeast|pewdiepie|markiplier|jacksepticeye|carryminati)\b",
    "achievement": r"\b(world first|world-first|record|records|record-breaking|breaks?|broke|broken|milestone|beat|beats|beaten|"
                   r"defeats?|upset|champions?|championship|wins?|won|victory|finals?|grand final|tournament|speedrun|"
                   r"speedrunners?|first ever|first-ever|sold|million|billion|achievement|trophy|worlds)\b",
    "whats_new": r"\b(patch|patch notes|update|updates|hotfix|season|new mode|release|released|launch|launches|launched|"
                 r"out now|dlc|expansion|tips?|trick|hidden|secret|easter egg|mechanic|buff|nerf|changes?|added|adds)\b",
}
COMPILED = {k: re.compile(v, re.I) for k, v in PATTERNS.items()}
ORDER = ("giveaway", "streamer", "achievement", "whats_new")


def classify(text: str) -> str | None:
    """The bucket of a headline + summary, or None when no keyword fits (plain news)."""
    for name in ORDER:
        if COMPILED[name].search(text or ""):
            return name
    return None


def balance(stories: list, limit: int, text_of=lambda s: f"{s.title} {s.summary}") -> list:
    """At most `limit` stories, newest first within each bucket, taken round-robin from the four buckets and the
    unclassified rest, so no bucket (and no prolific outlet) can crowd the others out. Input is newest first."""
    queues: dict[str, list] = {b: [] for b in (*ORDER, "other")}
    for s in stories:
        queues[classify(text_of(s)) or "other"].append(s)
    out: list = []
    while len(out) < limit and any(queues.values()):
        for name in (*ORDER, "other"):
            if queues[name] and len(out) < limit:
                out.append(queues[name].pop(0))
    return sorted(out, key=lambda s: s.published, reverse=True)


def enforce_mix(picks: list, per_bucket: int) -> tuple[list, list[str]]:
    """Best-first: keep at most `per_bucket` picks per bucket (picks without a bucket are never dropped).
    -> (kept, [why each one was dropped])."""
    counts: dict[str, int] = {}
    kept, dropped = [], []
    for p in sorted(picks, key=lambda p: -p.score):
        b = getattr(p, "bucket", "") or ""
        if b and counts.get(b, 0) >= per_bucket:
            dropped.append(f"'{p.story.title[:50]}' is a {b} story and this run already has {per_bucket}")
            continue
        counts[b] = counts.get(b, 0) + 1
        kept.append(p)
    return kept, dropped
