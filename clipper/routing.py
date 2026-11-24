"""Platform routing: which clips go to YouTube (and when), and which go to other platforms.

The pending queue (rendered / approved clips and news Shorts of the last `window_hours`) is ranked by
quality. The top `youtube_top` (2-3) "best of the best" are targeted to YouTube, one per AEDT peak slot
from config.toml [[slots]] (best clip -> first slot, i.e. US Peak 10:00). Everything else is spread over
`other_platforms` in turn. This only fills the route_* columns; nothing is ever uploaded (autopost = false).

Clips that are already exported keep their route (their upload sheet says where they go), and they keep
occupying their YouTube slot, so a later, better clip cannot take a slot that is already promised.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db
from .config import Config

log = logging.getLogger("clipper")

DEFAULTS = {"youtube_top": 3, "other_platforms": ["tiktok", "instagram", "facebook"], "window_hours": 36,
            "news_bonus": 0.5}


def settings(cfg: Config) -> dict:
    return {**DEFAULTS, **cfg.raw.get("routing", {})}


def rank_key(clip: dict, news_bonus: float = 0.5) -> tuple:
    """Higher is better: AI score, +1.5 for a banger, a small bonus for news (original, evergreen-ish), then pre-score."""
    score = float(clip.get("ai_score") or 0)
    score += 1.5 if clip.get("banger") else 0.0
    score += news_bonus if clip.get("platform") == "news" else 0.0
    score -= 3.0 if clip.get("sensitive") else 0.0          # needs a careful human look: never a headline slot by default
    return (score, float(clip.get("pre_score") or 0), -clip["id"] if "id" in clip else 0)


def next_slot_time(slot: dict, now: datetime, tz: ZoneInfo) -> datetime:
    """Next occurrence of a slot's clock time (HH:MM, Melbourne time) strictly after `now`."""
    hh, mm = (int(x) for x in str(slot["time"]).split(":"))
    local = now.astimezone(tz)
    cand = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cand <= local:
        cand = (local + timedelta(days=1)).replace(hour=hh, minute=mm, second=0, microsecond=0)
    return cand


def utc_string(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def plan(cfg: Config, clips: list[dict], now: datetime | None = None) -> dict[int, dict]:
    """-> {clip_id: {"platform", "slot", "at", "rank"}} for every clip given. Pure: reads nothing but its arguments.

    A human choice (assign_slot / assign_target, set in the review dashboard) always wins:
      * assign_slot only            -> YouTube in that slot (the slots are the YouTube peak times)
      * assign_target = 'youtube'   -> YouTube, in the given slot or else the next free one
      * assign_target = 'other'     -> the next of `other_platforms`, keeping the given slot (if any) as its post time
    Slots a human picked are not handed out again by the automatic ranking."""
    s = settings(cfg)
    tz = ZoneInfo(cfg.raw.get("general", {}).get("timezone", "Australia/Melbourne"))
    now = now or datetime.now(timezone.utc)
    all_slots = list(cfg.slots)
    slots = all_slots[:max(int(s["youtube_top"]), 0)]
    names = {sl["name"] for sl in all_slots}
    others = [str(p).lower() for p in s["other_platforms"]] or ["tiktok"]
    slot_at = {sl["name"]: next_slot_time(sl, now, tz).isoformat(timespec="minutes") for sl in all_slots}

    def manual(c: dict) -> tuple[str | None, str | None]:
        slot = c.get("assign_slot") if c.get("assign_slot") in names else None      # a renamed slot no longer counts
        target = c.get("assign_target") if c.get("assign_target") in db.ASSIGN_TARGETS else None
        return slot, target

    ranked = sorted(clips, key=lambda c: rank_key(c, float(s["news_bonus"])), reverse=True)
    out: dict[int, dict] = {}
    taken: set[str] = set()
    other_i = 0

    def other_platform() -> str:
        nonlocal other_i
        other_i += 1
        return others[(other_i - 1) % len(others)]

    for rank, c in enumerate(ranked, start=1):                  # 1) human choices and already-exported clips
        slot, target = manual(c)
        if slot or target:
            if target == "other":
                platform = other_platform()
            else:
                platform = "youtube"
            out[c["id"]] = {"platform": platform, "slot": slot, "at": slot_at.get(slot), "rank": rank}
            if slot and platform == "youtube":
                taken.add(slot)
        elif c.get("exported_at") and c.get("route_platform"):       # exported: locked to the route it was exported with
            out[c["id"]] = {"platform": c["route_platform"], "slot": c.get("route_slot"), "at": c.get("route_at"),
                            "rank": c.get("route_rank") or rank}
            if c.get("route_slot") and c["route_platform"] == "youtube":
                taken.add(c["route_slot"])
    free = [sl["name"] for sl in slots if sl["name"] not in taken]
    for c in ranked:                                            # 2) a human said 'youtube' but no slot: next free one
        r = out.get(c["id"])
        if r and r["platform"] == "youtube" and not r["slot"] and manual(c)[1] == "youtube" and free:
            r["slot"] = free.pop(0)
            r["at"] = slot_at[r["slot"]]
    for rank, c in enumerate(ranked, start=1):                  # 3) the automatic ranking fills what is left
        if c["id"] in out:
            continue
        if free and not c.get("sensitive"):
            name = free.pop(0)
            out[c["id"]] = {"platform": "youtube", "slot": name, "at": slot_at[name], "rank": rank}
        else:
            out[c["id"]] = {"platform": other_platform(), "slot": None, "at": None, "rank": rank}
    return out


def destination(clip: dict, slot_names: list[str]) -> tuple[str | None, str | None]:
    """-> (slot name or None, 'youtube' | 'other' or None): the human's choice first, else the saved automatic route."""
    slot = clip.get("assign_slot") if clip.get("assign_slot") in slot_names else None
    target = clip.get("assign_target") if clip.get("assign_target") in db.ASSIGN_TARGETS else None
    if not slot and not target:
        slot = clip.get("route_slot") if clip.get("route_slot") in slot_names else None
        rp = clip.get("route_platform")
        target = None if not rp else ("youtube" if rp == "youtube" else "other")
    elif not target:
        target = "youtube"
    return slot, target


def assign_routes(cfg: Config, now: datetime | None = None) -> dict[int, dict]:
    """Recompute and save routes for the pending queue. Never raises (routing is advisory)."""
    try:
        now = now or datetime.now(timezone.utc)
        hours = float(settings(cfg)["window_hours"])
        clips = db.pending_for_routing(cfg.database, utc_string(now - timedelta(hours=hours)))
        result = plan(cfg, clips, now)
        for cid, r in result.items():
            db.set_route(cfg.database, cid, r["platform"], r["slot"], r["at"], r["rank"])
        return result
    except Exception:
        log.exception("routing failed (ignored)")
        return {}


def day_start_utc(cfg: Config, now: datetime | None = None) -> str:
    """Start of the current Melbourne day, as a UTC 'YYYY-MM-DD HH:MM:SS' string (matches the DB's created_at)."""
    tz = ZoneInfo(cfg.raw.get("general", {}).get("timezone", "Australia/Melbourne"))
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    return utc_string(local.replace(hour=0, minute=0, second=0, microsecond=0))
