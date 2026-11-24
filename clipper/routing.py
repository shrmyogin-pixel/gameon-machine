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
    """-> {clip_id: {"platform", "slot", "at", "rank"}} for every clip given. Pure: reads nothing but its arguments."""
    s = settings(cfg)
    tz = ZoneInfo(cfg.raw.get("general", {}).get("timezone", "Australia/Melbourne"))
    now = now or datetime.now(timezone.utc)
    slots = list(cfg.slots)[:max(int(s["youtube_top"]), 0)]
    others = [str(p).lower() for p in s["other_platforms"]] or ["tiktok"]
    slot_at = {sl["name"]: next_slot_time(sl, now, tz).isoformat(timespec="minutes") for sl in slots}

    ranked = sorted(clips, key=lambda c: rank_key(c, float(s["news_bonus"])), reverse=True)
    out: dict[int, dict] = {}
    taken: set[str] = set()
    # clips already exported are locked to the route they were exported with
    for c in ranked:
        if c.get("exported_at") and c.get("route_platform"):
            out[c["id"]] = {"platform": c["route_platform"], "slot": c.get("route_slot"), "at": c.get("route_at"),
                            "rank": c.get("route_rank")}
            if c.get("route_slot"):
                taken.add(c["route_slot"])
    free = [sl["name"] for sl in slots if sl["name"] not in taken]
    other_i = 0
    for rank, c in enumerate(ranked, start=1):
        if c["id"] in out:
            out[c["id"]]["rank"] = out[c["id"]]["rank"] or rank
            continue
        if free and not c.get("sensitive"):
            name = free.pop(0)
            out[c["id"]] = {"platform": "youtube", "slot": name, "at": slot_at[name], "rank": rank}
        else:
            out[c["id"]] = {"platform": others[other_i % len(others)], "slot": None, "at": None, "rank": rank}
            other_i += 1
    return out


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
