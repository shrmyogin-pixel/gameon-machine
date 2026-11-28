"""Promo mode (`news --topic promo`, promo.bat): game deals, discounts, bundles, and gaming tech / gear spotlights.

Isolated from the other commands: promo Shorts do not count in their repeat checks or daily quota, and never take a
YouTube slot from them. Pure functions, no I/O.
"""
from __future__ import annotations

import re

DEAL = re.compile(r"\b(deals?(?! with\b)|discount(?:s|ed)?|\d{1,3}% off|on sale|(?:steam|summer|winter|autumn|spring|holiday) sale|"
                  r"bundles?|price (?:drop|cut)s?|lowest[- ](?:ever )?price|black friday|cyber monday|save [$£€]\d+|"
                  r"[$£€]\d+ off)\b", re.I)
GEAR = re.compile(r"\b(gpus?|graphics cards?|handhelds?|steam deck|rog ally|legion go|headsets?|keyboards?|mouse|mice|"
                  r"controllers?|gamepads?|monitors?|consoles?|ssds?|webcams?|microphones?|gaming (?:laptop|pc|chair)s?)\b", re.I)
SPOTLIGHT = re.compile(r"\b(announc\w+|reveal\w*|launch\w*|unveil\w*|release[sd]?|out now|new|first look)\b", re.I)
# business "deals" are not shopping deals
NOT_A_DEAL = re.compile(r"\b(publishing|distribution|licensing|sponsorship|multi[- ]year|acquisition) deals?\b|"
                        r"\b(signs?|signed|inks?|strikes?) (?:a |an )?(?:new )?[\w -]{0,20}deal\b|\blayoffs?\b", re.I)


def is_deal(text: str) -> bool:
    return bool(DEAL.search(text or "")) and not NOT_A_DEAL.search(text or "")


def is_story(title: str, summary: str = "") -> bool:
    """A deal, or a new piece of gaming gear, named in the headline."""
    title = title or ""
    return is_deal(title) or bool(GEAR.search(title) and SPOTLIGHT.search(title) and not NOT_A_DEAL.search(title))


def bucket_for(title: str) -> str:
    return "deal" if is_deal(title) else "pick"


PROMPT_NOTE = """

PROMO RUN. This overrides the six-bucket balance above: every candidate is a game deal, discount or bundle, or a gaming
tech / gear spotlight. Set bucket "deal" or "pick". Say prices, discounts, stores and end dates only when the row states
them, and say that prices can change. Never include links, promo codes or a sales pitch, and never tell viewers to buy.
Each Short is about a DIFFERENT deal or product, where possible from a different outlet. Start subjects with the exact
game or product name (it is used to find footage). Fewer strong stories are better than weak ones: reply {"stories": []}
if none is worth making."""

COMMON = set("""deal deals discount sale off bundle price drop cut lowest ever black friday cyber monday save game games
gaming new now the and for with from into this that its are was has have will can you your how why what get best today
announces announced reveals revealed launches launch""".split())
