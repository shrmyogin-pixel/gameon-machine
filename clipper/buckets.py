"""Story buckets for the news engine, so a night's Shorts mix six kinds of story instead of piling onto one game or topic:

    flagship   GTA 6 and other flagship games: secrets, easter eggs, Twitch Drops, verified leaks, bugs
    freebie    free games, giveaways, Twitch Drops and limited-time rewards
    update     major announcements: trailers, release dates, big patches, server shutdowns
    glitch     wild bugs and viral glitches: funny physics, exploits, community discoveries
    creator    creator and esports news: streamer milestones, roster moves, tournament upsets
    tech       gaming tech and AI: GPUs, handhelds, DLSS / FSR, AI in game development

classify() is a keyword guess used to balance the candidate list shown to Claude; Claude then names the bucket of each
pick, enforce_mix() caps how many picks one bucket may take in a run, and badge_label() gives the pill shown on screen.
Pure functions, no I/O.
"""
from __future__ import annotations

import re

BUCKETS = ("flagship", "freebie", "update", "glitch", "creator", "tech")
LABELS = {"flagship": "GTA 6", "freebie": "FREE GAME", "update": "PATCH UPDATE", "glitch": "WILD BUG",
          "creator": "CREATOR NEWS", "tech": "GAMING TECH"}
PROMO_LABELS = {"deal": "GAME DEAL", "pick": "TECH PICK"}      # promo.bat only: not part of the six-bucket mix
OTHER_FLAGSHIP_LABEL = "BIG GAME"          # a flagship story that is not about GTA must not wear a "GTA 6" badge
GTA = re.compile(r"\b(gta|grand theft auto)\b", re.I)
OLDER_GTA = re.compile(r"\b(?:gta|grand theft auto)[ :-]*(?:online|5|v|iv|4|iii|3|san andreas|vice city|trilogy)\b", re.I)

# whole-word / phrase patterns, checked in ORDER (a giveaway beats a patch note that merely mentions a free game)
PATTERNS = {
    "freebie": r"\b(free(?![ -](?:update|roam|to[ -]play))|giveaways?|giving away|claim|twitch drops?|prime gaming|freebies?|"
               r"free-to-keep|100% off|limited[- ]time rewards?)\b",
    "glitch": r"\b(glitch(?:es|ed)?|bugs?(?![ -]fix)|exploits?|ragdoll|(?:funny|weird|broken) physics|physics (?:bug|fail))\b",
    "tech": r"\b(gpus?|graphics cards?|geforce|rtx|radeon|nvidia|amd|intel|cpus?|dlss|fsr|xess|handhelds?|steam deck|rog ally|"
            r"legion go|ray tracing|frame generation|upscaling|ai|artificial intelligence|benchmarks?|drivers?|hardware)\b",
    "creator": r"\b(streamers?|youtubers?|content creators?|creators?|subscribers?|followers?|viewers?|kai cenat|ishowspeed|"
               r"pokimane|xqc|asmongold|shroud|hasan|dr disrespect|mrbeast|pewdiepie|markiplier|jacksepticeye|carryminati|"
               r"esports?|rosters?|tournament|champions?|championship|grand finals?|upset|signs with|benched)\b",
    "flagship": r"\b(gta|grand theft auto|rockstar|zelda|mario|pok[eé]mon|call of duty|fortnite|minecraft|elden ring|"
                r"elder scrolls|witcher|halo|god of war|cyberpunk|red dead|resident evil|final fantasy|easter eggs?)\b",
    "update": r"\b(trailers?|release date|announce[sd]?|announcement|reveal(?:s|ed)?|patch|patch notes|updates?|hotfix|season|"
              r"dlc|expansion|delayed|release[sd]?|launch(?:es|ed)?|out now|shut(?:s|ting)? ?down|shutdown|servers?)\b",
}
COMPILED = {k: re.compile(v, re.I) for k, v in PATTERNS.items()}
ORDER = ("freebie", "glitch", "tech", "creator", "flagship", "update")


def classify(text: str) -> str | None:
    """The bucket of a headline + summary, or None when no keyword fits (plain news)."""
    for name in ORDER:
        if COMPILED[name].search(text or ""):
            return name
    return None


def badge_label(bucket: str, kind: str = "news", subject: str = "") -> str:
    """The words on the yellow pill. `subject` is the story's main game (or its headline when no game is named)."""
    if bucket == "flagship":
        if not GTA.search(subject or ""):
            return OTHER_FLAGSHIP_LABEL
        if OLDER_GTA.search(subject):
            return "GTA"
    if bucket in LABELS or bucket in PROMO_LABELS:
        return {**LABELS, **PROMO_LABELS}[bucket]
    return LABELS["freebie"] if kind == "free_offer" else "BREAKING NEWS"


def balance(stories: list, limit: int, text_of=lambda s: f"{s.title} {s.summary}") -> list:
    """At most `limit` stories, newest first within each bucket, taken round-robin from the six buckets and the
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
