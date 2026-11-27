"""Gaming-tech mode (`news --topic tech`, tech.bat): which stories qualify and what Claude is told. Pure functions."""
from __future__ import annotations

from .buckets import COMPILED

PROMPT_NOTE = """

GAMING-TECH-ONLY RUN. This overrides the six-bucket balance above: every candidate is about gaming technology, so set bucket
"tech" for each. Pick GPUs, consoles, handhelds, upscaling, drivers and game-development tech that players care about, each
about a DIFFERENT product or announcement and, where possible, from a different outlet. Skip shopping deals and buying guides.
Start subjects with the exact game the story is about when it names one (it is used to find footage), otherwise the product.
Fewer strong stories are better than weak ones: reply {"stories": []} if none is worth making."""

COMMON = set("""gaming game games gpu graphics card cards nvidia amd intel new now the and for with from into this that its are
was has have will can you your how why what update driver performance tech hardware pc console handheld""".split())


def is_story(title: str, summary: str = "") -> bool:
    """The headline itself must be about gaming tech."""
    return bool(COMPILED["tech"].search(title or ""))
