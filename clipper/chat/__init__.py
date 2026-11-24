"""Anonymous live-chat readers (Twitch, YouTube, Kick). No accounts, no API keys.

Every reader is an async function `read(target, emit, stop)`:
  * `emit(username, text)` is called for each chat message,
  * `stop` is an asyncio.Event; the reader returns soon after it is set,
  * a reader reconnects with backoff on its own and never raises.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("clipper")

Emit = Callable[[str, str], None]


@dataclass(frozen=True)
class Target:
    platform: str  # twitch | youtube | kick
    handle: str    # channel name, video id, or @handle
    url: str


def parse_stream_link(link: str) -> Target:
    """Work out platform + handle from a stream link. Raises ValueError if unknown."""
    link = link.strip()
    if "://" not in link:
        link = "https://" + link
    u = urlparse(link)
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    parts = [p for p in u.path.split("/") if p]
    if host == "twitch.tv" and parts:
        if parts[0] in {"popout", "embed"} and len(parts) > 1:
            parts = parts[1:]
        return Target("twitch", parts[0].lower(), link)
    if host == "kick.com" and parts:
        return Target("kick", parts[0].lower(), link)
    if host in {"youtube.com", "music.youtube.com"} or host == "youtu.be":
        if host == "youtu.be" and parts:
            return Target("youtube", parts[0], link)
        vid = parse_qs(u.query).get("v", [""])[0]
        if vid:
            return Target("youtube", vid, link)
        if parts and parts[0] in {"live", "embed", "shorts"} and len(parts) > 1:
            return Target("youtube", parts[1], link)
        if parts and parts[0].startswith("@"):
            return Target("youtube", parts[0], link)  # resolved via /@handle/live
        if parts and parts[0] in {"channel", "c", "user"} and len(parts) > 1:
            return Target("youtube", "/".join(parts[:2]), link)
    raise ValueError(f"Unrecognised stream link (need Twitch, YouTube or Kick): {link}")


class ChatWriter:
    """Appends one JSON object per line: {"ts", "username", "text"}."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8", newline="\n")
        self.count = 0
        self.times: list[float] = []  # time.time() of every message, for rate stats

    def write(self, username: str, text: str) -> None:
        now = time.time()
        rec = {
            "ts": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="milliseconds"),
            "username": username,
            "text": text,
        }
        try:
            self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._fh.flush()
        except OSError as exc:  # a full disk must not kill the reader
            log.warning("chat.jsonl write failed: %s", exc)
        self.count += 1
        self.times.append(now)

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


async def backoff_sleep(stop: asyncio.Event, attempt: int) -> None:
    """Exponential backoff with jitter (1s .. 60s); wakes early when `stop` is set."""
    delay = min(60.0, 2.0 ** min(attempt, 6)) * random.uniform(0.7, 1.3)
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
    except asyncio.TimeoutError:
        pass


async def read_chat(target: Target, emit: Emit, stop: asyncio.Event) -> None:
    """Dispatch to the right reader. Never raises (except cancellation)."""
    from . import kick, twitch, youtube

    readers = {"twitch": twitch.read, "youtube": youtube.read, "kick": kick.read}
    try:
        await readers[target.platform](target.handle, emit, stop)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # last-resort guard: one stream must never crash the tool
        log.error("chat reader %s/%s crashed: %s", target.platform, target.handle, exc)


_SAFE = re.compile(r"[^A-Za-z0-9_.@-]+")


def safe_handle(handle: str) -> str:
    return _SAFE.sub("_", handle).strip("_") or "unknown"
