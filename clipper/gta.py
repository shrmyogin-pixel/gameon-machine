"""GTA-only mode (`news --only-gta`, gta.bat): which stories qualify, what Claude is told, and how repeats are judged.

The normal rules treat every GTA story as "the same game" (one per run, none within 24 h). Here a story is a repeat only
when it covers the same angle. Gambling, piracy, cheat and scam stories are already removed by news.HARD_SKIP.
Pure functions, no I/O.
"""
from __future__ import annotations

import re

GTA = re.compile(r"\b(gta(?:[ -]?(?:6|5|vi|v|iv|online))?|grand theft auto)\b", re.I)
LOOKALIKE = re.compile(r"\b(?:gta|grand theft auto)[- ](?:like|style|inspired|clone)\b|\bgta[- ]?killer\b", re.I)
# link bait and account trading, on top of news.HARD_SKIP
BAIT = re.compile(r"\b(modded (?:accounts?|money|cash)|accounts? for sale|money drops?|free money|free shark cards?|"
                  r"(?:rp|money|rank) boost(?:ing)?|boosting service|download link|link in (?:bio|description))\b", re.I)

# words that do not tell one GTA story from another: the franchise itself, filler, and the kind of story
COMMON = set("""gta rockstar grand theft auto online the and for with from into this that its are was new now just has have
will can you your how why what game games player players fan fans find finds found hidden secret easter egg eggs leak leaks
leaked update updates trailer glitch glitches bug bugs weekly week today official officially""".split())

PROMPT_NOTE = """

GTA-ONLY RUN. This overrides the six-bucket balance, the "one story per game or franchise" rule and the 24-hour same-game rule
above: every candidate is about GTA, so pick several GTA stories as long as each is about a DIFFERENT thing (a gameplay
discovery, an easter egg, a leak, a bug or glitch, an official update). Never two Shorts on the same event, and never an
event already covered. A leak counts only when a named outlet reports it: call it a leak and never present it as fact.
Never mention mod downloads, money or account selling, boosting services or any third-party link. Start subjects with the
exact game ("GTA 6", "GTA Online", "GTA 5"). Fewer strong stories are better than weak ones: reply {"stories": []} if none
is worth making."""


def is_gta_story(title: str, summary: str = "") -> bool:
    """GTA must be named in the headline (a passing mention in the summary is not a GTA story)."""
    text = f"{title} {summary}"
    return bool(GTA.search(title or "")) and not LOOKALIKE.search(title or "") and not BAIT.search(text)


def _angle(title: str, common: set[str]) -> set[str]:
    """The words that say WHICH story this is (plural "s" removed so "cars" matches "car")."""
    words = re.findall(r"[a-z][a-z'-]{2,}", (title or "").lower())
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words if w not in common}


def same_angle(a: str, b: str, common: set[str] = COMMON) -> bool:
    """Two headlines tell the same story: the shorter one's distinctive words are all in the other, or at least two of
    them (and half) are shared."""
    x, y = _angle(a, common), _angle(b, common)
    shared, smaller = len(x & y), min(len(x), len(y))
    return shared > 0 and (shared == smaller or (shared >= 2 and shared >= smaller / 2))


def enforce_angles(picks: list, covered_headlines: list[str], common: set[str] = COMMON) -> tuple[list, list[str]]:
    """Best first: drop a pick that repeats an angle already picked or already made. -> (kept, [why dropped])."""
    kept, dropped, seen = [], [], list(covered_headlines)
    for p in sorted(picks, key=lambda p: -p.score):
        if any(same_angle(p.story.title, t, common) for t in seen):
            dropped.append(f"'{p.story.title[:50]}' covers an angle already made")
            continue
        seen.append(p.story.title)
        kept.append(p)
    return kept, dropped
