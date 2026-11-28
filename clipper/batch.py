"""`news --all-niches` (banger12.bat): up to 12 Shorts in one run, spread over the niches of config/watchlist.json.

Selection is round-robin: one Short from each niche in turn, up to ceil(count / niches) each. When niches run short,
their places pass to the next ones, the cap rising by one at a time until the batch is full; GTA 6 may only ever take one
more than the first cap, so it cannot take over. Each niche is written in its own Claude call. Across the whole batch there are no two Shorts on one story or game (GTA 6:
no two on one angle), no two hooks that open the same way, and a hook-bank pattern is followed at most PATTERN_MAX
times. Every Short is dense (see news.collect_dense). Same source check, QA, queue and manual review as every news run;
never promo or affiliate items, and nothing is uploaded (autopost = false).
"""
from __future__ import annotations

import logging
import math
from collections import Counter, deque

from . import db, factcheck, gta, news, topics, watchlist
from .config import Config
from .hookscore import names_subject, opener_of, restates_hook

log = logging.getLogger("clipper")

MAX_COUNT = 12
ROUNDS = 5                  # most Claude calls for one niche
PER_NICHE_CANDIDATES = 15
PATTERN_MAX = 2
MODE = {"gta_6": "gta", "creators": "creators", "upcoming_tech": "tech"}          # niches with their own theme rules
DESCRIPTIONS = {"upcoming_games": "an upcoming game's release date, window, delay or launch detail",
                "trailers": "a new game trailer", "upcoming_tech": "upcoming gaming hardware or technology",
                "gta_6": "GTA 6", "creators": "a gaming creator or esports moment",
                "free_games": "a game that is free to claim or keep (say the store and the end date only if stated)",
                "secrets_glitches": "an easter egg, hidden secret, glitch or exploit players found",
                "game_updates": "a patch, update, season or expansion for a game"}


def niches() -> list[str]:
    """The niches configured in the watchlist (all of them when it cannot be read)."""
    wl = watchlist.load()
    return [b for b in watchlist.BUCKETS if not wl or wl["buckets"].get(b)]


def niche_cap(count: int, buckets: int) -> int:
    """Most Shorts one niche may have in a batch."""
    return math.ceil(count / max(buckets, 1))


def prompt_note(niche: str, used_hooks: list[str]) -> str:
    note = (f"\n\nONE-NICHE PART OF A BATCH. This overrides the six-bucket balance and bucket limit above: every candidate "
            f"is about {DESCRIPTIONS[niche]}. Pick different stories; give each its natural bucket.")
    if used_hooks:
        note += " Hooks already used in this batch (open yours differently):\n" + "\n".join(f"- {h}" for h in used_hooks)
    return note


class Batch:
    """What the batch has used so far, so nothing repeats."""

    def __init__(self):
        self.headlines: list[str] = []
        self.topics: set[str] = set()
        self.hooks: list[str] = []
        self.openers: set[str] = set()
        self.patterns: Counter = Counter()

    def repeats(self, pick, niche: str) -> bool:
        if any(gta.same_angle(pick.story.title, t) for t in self.headlines):
            return True
        if niche != "gta_6" and watchlist.GTA6.search(" ".join(pick.subjects)):
            return True                                  # GTA 6 counts against its own niche's cap only
        mine = topics.pick_topics(pick.subjects)
        return niche != "gta_6" and any(topics.share_topic(mine, {t}) for t in self.topics)

    def vary_hook(self, cfg: Config, pick) -> bool:
        """Keep the chosen hook if it is fresh, else switch to another valid one. False when none is left."""
        overused = pick.hook_pattern and self.patterns[pick.hook_pattern] >= PATTERN_MAX
        if opener_of(pick.hook.hook) not in self.openers and not overused:
            return True
        lo, hi = int(cfg.news["script_min_words"]), int(cfg.news["script_max_words"])
        for h in pick.hooks:                                                    # best first
            if (h.valid and h.hook != pick.hook.hook and opener_of(h.hook) not in self.openers
                    and lo <= h.words + len(pick.body.split()) <= hi
                    and not restates_hook(h.hook, pick.body, subjects=pick.subjects)
                    and (names_subject(h.hook, pick.subjects) or names_subject(pick.body.split(".")[0], pick.subjects))
                    and not news.copies_headline(pick.story.title, h.hook, pick.subjects)):
                pick.hook, pick.script, pick.hook_pattern = h, news.final_script(h.hook, pick.body), ""
                return True
        return False

    def add(self, pick) -> None:
        self.headlines.append(pick.story.title)
        self.topics |= topics.pick_topics(pick.subjects)
        self.hooks.append(pick.hook.hook)
        self.openers.add(opener_of(pick.hook.hook))
        if pick.hook_pattern:
            self.patterns[pick.hook_pattern] += 1


def _pick(cfg: Config, niche: str, candidates: list, want: int, state: Batch) -> list:
    before = cfg.raw["news"]
    cfg.raw["news"] = {**before, "stories": want, "bucket_max": want, "niche_note": prompt_note(niche, state.hooks),
                       **({"only": MODE[niche]} if niche in MODE else {})}
    try:
        return news.pick_stories(cfg, candidates, want, cfg.data_dir / "news_work")
    finally:
        cfg.raw["news"] = before


def run_batch(cfg: Config, count: int = MAX_COUNT, hours: float | None = None, dry_run: bool = False,
              research: bool = True, dense: dict | None = None) -> news.NewsResult:
    """Up to `count` Shorts across every niche. Never raises."""
    result = news.NewsResult()
    before = dict(cfg.raw.get("news", {}))
    try:
        db.init_db(cfg.database)
        count = max(1, min(int(count), MAX_COUNT))
        cfg.raw["news"] = {**before, "no_promo": True, "all_niches": True, "max_candidates": 2000,
                           "dense": dense or {"videos": 12, "photos": 22}}
        found = []
        if research:
            try:
                found = watchlist.scan(cfg)
                log.info("watchlist: %d outlier topic(s)", len(found))
            except Exception:
                log.exception("watchlist scan failed (ignored)")
        stories = news.fetch_stories(cfg, hours if hours is not None else float(cfg.news.get("banger_hours", 72)),
                                     outliers=found)
        names = niches()
        pool: dict[str, list] = {n: [] for n in names}
        for s in sorted(stories, key=lambda s: watchlist.best_match(s, found) is None):      # watched topics first
            niche = watchlist.bucket_of(s.title, s.summary)
            if niche in pool:
                pool[niche].append(s)
        log.info("batch: candidates per niche: %s", ", ".join(f"{n} {len(pool[n])}" for n in names))
        state, cap, tried = Batch(), niche_cap(count, len(names)), set()
        taken, calls = Counter(), Counter()
        queues: dict[str, deque] = {n: deque() for n in names}
        empty: set[str] = set()

        limit = cap                                              # rises when niches run short

        def refill(niche: str) -> bool:
            """One more Claude call for this niche into its queue. False when it has nothing left to offer."""
            pool[niche] = [s for s in pool[niche] if s.link not in tried]
            candidates = pool[niche][:PER_NICHE_CANDIDATES]
            if not candidates or niche in empty or calls[niche] >= ROUNDS:
                return False
            calls[niche] += 1
            unseen = len(pool[niche]) > len(candidates)
            pool[niche] = pool[niche][len(candidates):] + candidates          # next call: headlines not offered yet first
            for i, s in enumerate(candidates, start=1):
                s.id, s.watch = i, None
            try:
                watchlist.annotate(cfg, candidates, found, niche=niche)
                want = limit - taken[niche]
                picks = _pick(cfg, niche, candidates, want if limit == cap else max(want, 3), state)
            except Exception as exc:                              # one niche failing never stops the others
                log.warning("batch: %s gave nothing (%s)", niche, str(exc)[:200])
                picks = []
            if not picks and not unseen:                          # everything it has was just turned down
                empty.add(niche)
            queues[niche].extend(picks)
            return True

        def take_one(niche: str) -> bool:
            """Make the niche's next Short. False when it cannot give one."""
            while queues[niche] or refill(niche):
                if not queues[niche]:
                    continue
                pick = queues[niche].popleft()
                tried.add(pick.story.link)
                if state.repeats(pick, niche) or not state.vary_hook(cfg, pick):
                    log.info("batch: dropped (repeats this batch): %s", pick.story.title[:80])
                    continue
                if dry_run:
                    if factcheck.check(cfg, pick)[0]:
                        result.picked.append(pick)                # shown with its failed check, not counted
                        continue
                else:
                    try:
                        cfg.raw["news"]["only"] = MODE.get(niche)
                        result.made.append((pick, news.make_short(cfg, pick)))
                    except Exception as exc:
                        log.exception("batch: story %s failed", pick.story.link)
                        result.failed.append((pick, str(exc)))
                        continue
                    finally:
                        cfg.raw["news"].pop("only", None)
                result.picked.append(pick)
                state.add(pick)
                taken[niche] += 1
                return True
            return False

        while sum(taken.values()) < count:
            progressed = False
            for niche in names:                                   # one each per turn
                ceiling = min(limit, cap + 1) if niche == "gta_6" else limit
                if sum(taken.values()) < count and taken[niche] < ceiling and take_one(niche):
                    progressed = True
            if not progressed:                                    # nobody can add one under this limit
                if limit >= count:
                    break
                limit += 1
        done = sum(taken.values())
        log.info("batch: made per niche (cap %d): %s", cap, ", ".join(f"{n} {taken[n]}" for n in names))
        if done < count:
            result.note = f"batch made {done} of {count}: not enough suitable fresh stories or footage"
            log.info("batch: %s", result.note)
    except Exception as exc:
        log.exception("batch run failed")
        result.note = f"run failed: {exc}"
    finally:
        cfg.raw["news"] = before
    return result
