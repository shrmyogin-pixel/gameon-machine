"""Creators-only mode (`news --creators`, creators.bat): major creator / streamer and esports news about any game.

Same safety filters as every run (news.HARD_SKIP), plus: nothing about arrests, allegations, abuse, deaths or private
life, which this channel does not cover about real people. One Short per creator or event: a repeat is a headline that
tells the same story. Pure functions, no I/O.
"""
from __future__ import annotations

import re

NAMES = (r"kai cenat|ishowspeed|ninja|pokimane|xqc|asmongold|sypherpk|shroud|tfue|hasanabi|dr disrespect|mrbeast|pewdiepie|"
         r"markiplier|jacksepticeye|carryminati|ludwig|jynxzi|caseoh|ironmouse|valkyrae|sykkuno|tarik|summit1g|moistcr1tikal|"
         r"penguinz0|dantdm|ksi|sidemen|techno ?gamerz|mortal")
MATCH = re.compile(
    r"\b(?:streamers?|youtubers?|content creators?|vtubers?|twitch star|speed ?run(?:s|ner|ners|ning)?|subathon|"
    r"(?:\d+[- ]hour|charity|stream(?:ing)?) marathon|marathon (?:stream|run)|challenge run|no[- ]hit run|"
    r"viewership|peak viewers|concurrent viewers|viewer record|subscriber record|sub record|"
    r"(?<!character )(?<!fighter )(?<!hero )(?<!operator )roster|esports?|grand finals?|benched|"
    r"(?:wins?|won) (?:the )?[\w ]{0,30}?(?:major|championship|world title|worlds)|" + NAMES + r")\b", re.I)
# not for this channel: real people's legal trouble, abuse claims, deaths, private life, gambling streams
UNSUITABLE = re.compile(r"\b(arrest(?:s|ed)?|police|charged|lawsuit|sued|allegations?|alleged(?:ly)?|accus(?:es?|ed|ation)|"
                        r"assault|abus(?:e|ed|ive)|harass\w*|grooming|minors?|underage|swatt(?:ed|ing)|dies|died|death|"
                        r"suicide|onlyfans|leaked (?:video|photos)|slots? stream)\b", re.I)
# words that do not tell one creator story from another
COMMON = set("""streamer streamers youtuber creator creators content twitch youtube kick stream streams streaming live new now
the and for with from into this that its are was has have will can you your how why what after first record records breaks
broke broken sets set hits viewers viewer announces announced reveals revealed esports team season game games""".split())

PROMPT_NOTE = """

CREATORS-ONLY RUN. This overrides the six-bucket balance above: every candidate is creator, streamer or esports news, so set
bucket "creator" for each. Pick major moments across ANY game: marathon completions, challenge and speed runs, viewership and
subscriber records, roster moves, tournament wins and creator reveals. Never two Shorts on the same creator, team or event.
Use only what the row states about a real person: no speculation, no private life, no drama, allegations, legal trouble or
anything about minors, and set sensitive true if you are unsure. Start subjects with the game being played when the row names
one (it is used to find pictures), otherwise the creator or team. Fewer strong stories are better than weak ones: reply
{"stories": []} if none is worth making."""


def is_story(title: str, summary: str = "") -> bool:
    """Creator / esports news must be clear from the headline, with nothing unsuitable in it or the summary."""
    return bool(MATCH.search(title or "")) and not UNSUITABLE.search(f"{title} {summary}")
