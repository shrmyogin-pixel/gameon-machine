"""Hook scoring for news Shorts (voice.md: under 12 words, name the game/platform/offer, state the news now).

score_hook() gives 0-10:
    4.0  a game / platform / store name inside the FIRST 6 WORDS (3.0 when a real curiosity gap - two cues - withholds
         the name on purpose; the script's first sentence must then name it, see news.validate_reply)
    3.0  urgency inside the first 6 words (free, today, leak, breaking, ...): 1.5 for one cue, 3 for two or more
    1.5  a concrete specific (number, price, date) anywhere in the hook
    1.5  length: full marks for 6-10 words, half for 4-5 or 11-12
    +3.0 curiosity bonus: a curiosity gap, bold claim or personal stake ("stop scrolling", "nobody thought", "48 hours",
         "before it's gone", a question, "record", "never") - 3 for two or more cues, 1.5 for one
The four parts above are scaled to 7 points and the curiosity bonus adds up to 3, so a plain "X just did Y" headline hook
tops out at 7 while a hook with a gap or a stake can reach 10.
A hook of more than 12 words, or one containing banned filler, is invalid and scores 0.
Three penalties on the total: a bare stock opener ("Stop scrolling if you play X" with nothing at stake) loses 4.5, a vague
teaser that says nothing concrete ("Nobody expected this ...", "a surprising call") loses 2.5, and a plain headline read
("X is / just got / gets ..." with no gap or stake) loses 2.

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
          "gamers rejoice", "hey gamers", "hello gamers", "hi gamers", "what's good", "welcome to", "welcome everyone",
          "breaking news today", "today in gaming", "in this video")

# Slow / generic outros that kill retention: banned as the last line of a script (voice.md "Closing").
BANNED_CLOSERS = ("thanks for watching", "thank you for watching", "follow for more", "follow game on", "follow us",
                  "follow for", "see you next time", "see you in the next", "see you soon", "until next time",
                  "watch this space", "like and subscribe", "hit subscribe", "don't forget to", "dont forget to",
                  "that's all for", "thats all for", "that's it for", "catch you", "more to come", "stay tuned",
                  "subscribe")

PLATFORMS = ("playstation", "ps5", "ps4", "ps plus", "xbox", "game pass", "nintendo", "switch", "switch 2", "steam",
             "epic", "epic games", "gog", "pc", "ubisoft", "ea", "battle.net", "riot", "valve", "sony", "microsoft",
             "twitch", "prime gaming", "meta quest", "android", "ios")

URGENCY = ("free", "today", "tonight", "now", "just", "breaking", "leak", "leaked", "leaks", "confirmed", "official",
           "officially", "revealed", "reveals", "announced", "announces", "drops", "dropped", "delayed", "cancelled",
           "canceled", "ends", "ending", "expires", "last", "finally", "new", "returns", "surprise", "shadow-drop",
           "rumour", "rumor", "urgent", "claim", "live", "out", "stop", "scrolling", "record", "broke", "broken",
           "nobody", "hours", "ends", "only", "never", "first", "worldwide")


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


CURIOSITY = (r"stop scrolling", r"stop what", r"nobody", r"no one", r"never", r"only", r"secret", r"hidden", r"record",
             r"broke", r"before (?:it'?s|they'?re|it) gone", r"\d+ (?:hours?|days?|minutes?)", r"why", r"what happens",
             r"if you play", r"if you own", r"you (?:have|need|can still|missed)", r"missed", r"\?\s*$", r"this is why",
             r"nobody (?:saw|expected|thought)", r"can'?t believe", r"wait", r"impossible", r"gone")


# concrete stakes: what the viewer stands to lose
CURIOSITY += (r"your (?:clock|time|save|saves|library|progress|money|account)", r"ticking", r"expiry", r"last chance",
              r"for good", r"forever", r"\blos(?:e|es|ing)\b", r"disappear", r"vanish")

# a possible consequence for the viewer ("Your library could ...")
CURIOSITY += (r"\byour\b.*\b(?:could|might|is about to|are about to)\b",)

# teasers that point at something without saying anything about it
VAGUE = re.compile(r"\b(?:nobody|no one) (?:expected|saw|thought)\b.*\bthis\b|"
                   r"\b(?:surprising|surprise|big|huge|major|bold|strange) (?:call|move|news|decision|confession|change)\b|"
                   r"\bsomething (?:big|huge|new|special)\b|\bchang(?:ed|es) everything\b", re.I)
# newsroom phrasing: reads as a headline wherever the name sits
HEADLINE_PHRASES = re.compile(r"\b(?:just got|just gets|gets a new|is getting|has been|have been)\b", re.I)

# "Stop scrolling if you play X" and nothing else: no consequence, no gap
STOCK_OPENER = re.compile(r"^stop scrolling[,:]? if you (?:still )?(?:play|own|have|love) [^,:;?]+?[.!]?$", re.I)
HEADLINE_VERBS = {"is", "are", "just", "gets", "adds", "finally", "officially", "confirms", "announces", "reveals", "can",
                  "has", "will", "launches", "drops"}


def curiosity_cues(hook: str) -> list[str]:
    low = re.sub(r"[’‘]", "'", hook.lower())
    return [c for c in CURIOSITY if re.search(c, low)]


def names_subject(text: str, subjects: list[str] | tuple = ()) -> bool:
    """True when the text mentions one of the story's subjects (game / creator / store) or a platform."""
    toks = words_of(text)
    return any(_has_phrase(toks, n.strip()) for n in list(subjects) + list(PLATFORMS) if n and n.strip())


def closer_banned(script: str) -> list[str]:
    """Generic outro phrases in the LAST sentence of a script (the closing line must be a twist / question / loop line)."""
    sentences = [x for x in re.split(r"(?<=[.!?])\s+", script.strip()) if x.strip()]
    if not sentences:
        return []
    low = re.sub(r"[’‘]", "'", sentences[-1].lower())
    return [b for b in BANNED_CLOSERS if re.search(r"(?<![a-z])" + re.escape(b) + r"(?![a-z])", low)]


# words that frame a hook without carrying its fact
FRAMING = {"stop", "scrolling", "play", "have", "your", "this", "that", "these", "just", "with", "from", "what", "will",
           "about", "still", "love", "nobody", "expected", "there", "they", "them", "when", "into", "game", "games"}


def _stem(word: str) -> str:
    """Crude stem so "shutting", "shuts" and "shutdown" can be matched."""
    w = re.sub(r"['’]s$", "", word.lower())
    for suffix in ("ing", "ed", "es", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            w = w[:-len(suffix)]
            break
    return w[:-1] if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "aeiouls0123456789" else w


def _same_fact(a: str, b: str) -> bool:
    if a == b:
        return True
    return min(len(a), len(b)) >= 4 and a.isalpha() and b.isalpha() and (a.startswith(b) or b.startswith(a))


def restates_hook(hook: str, body: str, threshold: float = 0.6, subjects: list[str] | tuple = ()) -> bool:
    """True when the script's first sentence just says the hook again: most of the hook's content words repeat, or at
    least two of its facts (ignoring the subject's name and framing words) come back in another form."""
    def content(text: str) -> set[str]:
        return {w.lower() for w in words_of(text) if len(w) > 3}
    first = re.split(r"(?<=[.!?])\s+", body.strip(), maxsplit=1)[0]
    h = content(hook)
    if h and len(h & content(first)) / len(h) >= threshold:
        return True
    names = {_stem(w) for s in subjects for w in words_of(s)}
    facts = {_stem(w) for w in h if w not in FRAMING} - names
    said = {_stem(w) for w in content(first)}
    repeated = [f for f in facts if any(_same_fact(f, s) for s in said)]
    return len(repeated) >= 2 and len(repeated) / len(facts) >= threshold


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
    hit = next((s for s in sorted(names, key=len, reverse=True)
                if _has_phrase(toks[:EARLY_WORDS + len(s.split()) - 1], s.strip())), None)   # the name may START in the first 6 words
    deferred = not hit and len(curiosity_cues(hook)) >= 2            # a real curiosity gap may name the game in sentence 2
    res.parts["name_early"] = 4.0 if hit else 3.0 if deferred else 0.0
    res.reasons.append(f"name in first {EARLY_WORDS} words: {hit}" if hit else
                       "name withheld on purpose (curiosity gap): named in sentence 2" if deferred else
                       f"no game/platform name in first {EARLY_WORDS} words")

    cues = [t for t in early if t.lower() in URGENCY]
    res.parts["urgency_early"] = 3.0 if len(cues) >= 2 else 1.5 if cues else 0.0
    res.reasons.append(f"urgency: {', '.join(cues)}" if cues else f"no urgency word in first {EARLY_WORDS} words")

    specific = any(re.search(r"\d", t) or t.startswith("$") or t.startswith("£") for t in toks)
    res.parts["specific"] = 1.5 if specific else 0.0
    if specific:
        res.reasons.append("has a number / price / date")

    res.parts["length"] = 1.5 if 6 <= n <= 10 else 0.75
    cues = curiosity_cues(hook)
    res.parts["curiosity"] = 3.0 if len(cues) >= 2 else 1.5 if cues else 0.0
    res.reasons.append("curiosity gap / stake" if cues else "no curiosity gap or stake (plain headline)")
    base = sum(v for k, v in res.parts.items() if k != "curiosity")
    total = min(base * 0.7 + res.parts["curiosity"], 10.0)
    if STOCK_OPENER.match(hook.strip()):
        res.parts["stock_opener"] = -4.5
        res.reasons.append("bare stock opener: say what is at stake")
    elif not cues and (_reads_as_headline(toks, names) or HEADLINE_PHRASES.search(hook)):
        res.parts["plain_headline"] = -2.0
        res.reasons.append("reads like a plain headline")
    if VAGUE.search(hook):
        res.parts["vague"] = -2.5
        res.reasons.append("vague teaser: name the concrete thing at stake")
    total += sum(res.parts.get(k, 0.0) for k in ("stock_opener", "plain_headline", "vague"))
    res.total = round(max(total, 0.0), 2)
    return res


def _reads_as_headline(toks: list[str], names: list[str]) -> bool:
    """The hook starts with the subject's name and goes straight into a headline verb."""
    low = [t.lower() for t in toks]
    for name in names:
        parts = name.lower().split()
        if low[:len(parts)] == parts and len(low) > len(parts) and low[len(parts)] in HEADLINE_VERBS:
            return True
    return False


def rank_hooks(hooks: list[str], subjects: list[str] | tuple = ()) -> list[HookScore]:
    """Every hook scored, best first (valid before invalid; on a tie the stronger curiosity gap, then original order)."""
    scored = [score_hook(h, subjects) for h in hooks]
    return sorted(scored, key=lambda s: (not s.valid, -s.total, -s.parts.get("curiosity", 0.0)))


def opener_of(hook: str) -> str:
    """The first two words, lower case: what makes two hooks in one batch sound alike."""
    return " ".join(words_of(hook)[:2]).lower()


def best_hook(hooks: list[str], subjects: list[str] | tuple = ()) -> HookScore | None:
    """The best VALID hook, or None if every hook is invalid."""
    ranked = rank_hooks(hooks, subjects)
    return ranked[0] if ranked and ranked[0].valid else None
