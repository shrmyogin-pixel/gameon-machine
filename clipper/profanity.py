"""Profanity masking (policy rule 4): `shit` -> `s***`. Used on any text that leaves the tool."""
from __future__ import annotations

import re

_STEMS = re.compile(
    r"(?i)(?<![a-z])(fuck\w*|motherfuck\w*|shit\w*|bullshit\w*|bitch\w*|bastard\w*|asshole\w*|"
    r"dickhead\w*|piss\w*|cunt\w*|wanker\w*|bollocks)(?![a-z])"
)


def mask(text: str) -> str:
    return _STEMS.sub(lambda m: m.group(0)[0] + "***", text)
