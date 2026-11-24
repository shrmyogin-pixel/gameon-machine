"""Hook scoring for news Shorts (voice.md: under 12 words, name the game/platform/offer, state the news now).

score_hook() gives 0-10:
    4.0  a game / platform / store name inside the FIRST 6 WORDS
    3.0  urgency inside the first 6 words (free, today, leak, breaking, ...): 1.5 for one cue, 3 for two or more
    1.5  a concrete specific (number, price, date) anywhere in the hook
    1.5  length: full marks for 6-10 words, half for 4-5 or 11-12
A hook of more than 12 words, or one containing banned filler, is invalid and scores 0.

Pure functions, no I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_WORDS = 12
EARLY_WORDS = 6

# From voice.md "Banned filler (zero tolerance)". Checked against hooks AND scripts.
BANNED = ("what is up guys", "what's up guys", "hey guys", "welcome back", "you won't believe",
          "you wont believe", "let's get into it", "lets get into it", "before we start", "smash that like button",
          "without further ado", "make sure to subscribe", "in today's video", "in todays video", "as you can see",
          "basically", "literally", "stay tuned", "check this out", "get ready", "are you ready", "buckle up",
          "gamers rejoice")

PLATFORMS = ("playstation", "ps5", "ps4", "ps plus", "xbox", "game pass", "nintendo", "switch", "switch 2", "steam",
             "epic", "epic games", "gog", "pc", "ubisoft", "ea", "battle.net", "riot", "valve", "sony", "microsoft",
             "twitch", "prime gaming", "meta quest", "android", "ios")

URGENCY = ("free", "today", "tonight", "now", "just", "breaking", "leak", "leaked", "leaks", "confirmed", "official",
           "officially", "revealed", "reveals", "announced", "announces", "drops", "dropped", "delayed", "cancelled",
           "canceled", "ends", "ending", "expires", "last", "finally", "new", "returns", "surprise", "shadow-drop",
           "rumour", "rumor", "urgent", "claim", "live", "out")


@dataclass
class HookScore:
    hook: str
    total: float
    valid: bool
    words: int
    parts: dict = field(default_factory=dict)
    reasons: list = field(default_factory=list)


def words_of(text: str) -> list[str]:
    """Words with edge punctuation removed; keeps apostrophes, dots and hyphens inside a word (Battle.net, shadow-drop)."""
    return [w for w in (t.strip(" \t\n.,:;!?\"'()[]{}—–-…") for t in text.split()) if w]


def banned_in(text: str) -> list[str]:
    low = re.sub(r"[’‘]", "'", text.lower())
    return [b for b in BANNED if re.search(r"(?<![a-z])" + re.escape(b) + r"(?![a-z])", low)]


def _has_phrase(tokens: list[str], phrase: str) -> bool:
    target = phrase.lower().split()
    low = [t.lower() for t in tokens]
    return any(low[i:i + len(target)] == target for i in range(len(low) - len(target) + 1))


def score_hook(hook: str, subjects: list[str] | tuple = ()) -> HookScore:
    """subjects = names of the story's game(s)/platform/store, as given by the story picker."""
    toks = words_of(hook)
    n = len(toks)
    res = HookScore(hook=hook.strip(), total=0.0, valid=True, words=n)
    if n == 0:
        res.valid = False
        res.reasons.append("empty hook")
        return res
    if n > MAX_WORDS:
        res.valid = False
        res.reasons.append(f"{n} words, max {MAX_WORDS}")
    bad = banned_in(hook)
    if bad:
        res.valid = False
        res.reasons.append("banned filler: " + ", ".join(bad))
    if not res.valid:
        return res

    early = toks[:EARLY_WORDS]
    names = [s for s in list(subjects) + list(PLATFORMS) if s and s.strip()]
    hit = next((s for s in sorted(names, key=len, reverse=True) if _has_phrase(early, s.strip())), None)
    res.parts["name_early"] = 4.0 if hit else 0.0
    res.reasons.append(f"name in first {EARLY_WORDS} words: {hit}" if hit else f"no game/platform name in first {EARLY_WORDS} words")

    cues = [t for t in early if t.lower() in URGENCY]
    res.parts["urgency_early"] = 3.0 if len(cues) >= 2 else 1.5 if cues else 0.0
    res.reasons.append(f"urgency: {', '.join(cues)}" if cues else f"no urgency word in first {EARLY_WORDS} words")

    specific = any(re.search(r"\d", t) or t.startswith("$") or t.startswith("£") for t in toks)
    res.parts["specific"] = 1.5 if specific else 0.0
    if specific:
        res.reasons.append("has a number / price / date")

    res.parts["length"] = 1.5 if 6 <= n <= 10 else 0.75
    res.total = round(min(sum(res.parts.values()), 10.0), 2)
    return res


def rank_hooks(hooks: list[str], subjects: list[str] | tuple = ()) -> list[HookScore]:
    """Every hook scored, best first (valid ones before invalid; ties keep the original order)."""
    scored = [score_hook(h, subjects) for h in hooks]
    return sorted(scored, key=lambda s: (not s.valid, -s.total))


def best_hook(hooks: list[str], subjects: list[str] | tuple = ()) -> HookScore | None:
    """The best VALID hook, or None if every hook is invalid."""
    ranked = rank_hooks(hooks, subjects)
    return ranked[0] if ranked and ranked[0].valid else None
