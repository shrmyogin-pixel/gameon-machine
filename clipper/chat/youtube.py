"""YouTube live chat, anonymous, via the InnerTube endpoint the website itself uses.

No Data API key, no quota. Flow:
  1. GET the live page, pull the first chat continuation token out of ytInitialData and the
     (public, page-embedded) InnerTube key + client version out of ytcfg.
  2. POST youtubei/v1/live_chat/get_live_chat with that continuation, repeatedly. Each reply
     holds new messages and the next continuation + how long to wait before asking again.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from . import Emit, backoff_sleep
from .http import http_text

log = logging.getLogger("clipper")

WATCH = "https://www.youtube.com/watch?v={vid}"
CHAT_API = "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key={key}&prettyPrint=false"
DEFAULT_VERSION = "2.20240101.00.00"


def _page_url(handle: str) -> str:
    if handle.startswith("@") or handle.startswith(("channel/", "c/", "user/")):
        return f"https://www.youtube.com/{handle}/live"
    return WATCH.format(vid=handle)


def _json_after(html: str, marker: str) -> dict | None:
    """Decode the JSON object that follows `marker` (e.g. 'ytInitialData = ')."""
    i = html.find(marker)
    if i < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[i + len(marker):].lstrip())
        return obj
    except ValueError:
        return None


def first_continuation(initial: dict) -> str | None:
    try:
        conts = initial["contents"]["twoColumnWatchNextResults"]["conversationBar"]["liveChatRenderer"]["continuations"]
    except (KeyError, TypeError):
        return None
    for c in conts:
        for kind in ("reloadContinuationData", "invalidationContinuationData", "timedContinuationData"):
            if kind in c:
                return c[kind]["continuation"]
    return None


def runs_to_text(runs: list[dict]) -> str:
    out = []
    for r in runs:
        if "text" in r:
            out.append(r["text"])
        elif "emoji" in r:
            e = r["emoji"]
            shortcuts = e.get("shortcuts") or []
            out.append(shortcuts[0] if shortcuts else e.get("emojiId", ""))
    return "".join(out)


def parse_actions(actions: list[dict], emit: Emit, seen: set[str], since_usec: int = 0) -> None:
    """Emit new text messages. `since_usec` drops the backlog YouTube sends on the first poll."""
    for a in actions:
        item = (a.get("addChatItemAction") or {}).get("item") or {}
        r = item.get("liveChatTextMessageRenderer")
        if not r:
            continue  # joins, super-chat banners, system notices ...
        mid = r.get("id")
        if mid in seen:
            continue
        seen.add(mid)
        if int(r.get("timestampUsec", 0) or 0) < since_usec:
            continue
        name = (r.get("authorName") or {}).get("simpleText") or "unknown"
        text = runs_to_text((r.get("message") or {}).get("runs", []))
        if text:
            emit(name, text)


def next_continuation(cont: dict) -> tuple[str | None, float]:
    """-> (next token, seconds to wait)."""
    for c in cont.get("continuations", []):
        for kind in ("timedContinuationData", "invalidationContinuationData", "reloadContinuationData"):
            if kind in c:
                d = c[kind]
                return d.get("continuation"), max(0.2, min(d.get("timeoutMs", 1000) / 1000, 5.0))
    return None, 1.0


async def read(handle: str, emit: Emit, stop: asyncio.Event) -> None:
    attempt = 0
    seen: set[str] = set()
    since_usec = int((time.time() - 2) * 1_000_000)  # ignore chat history from before we attached
    while not stop.is_set():
        try:
            html = await http_text(_page_url(handle), headers={"Cookie": "CONSENT=YES+1; SOCS=CAI"})
            initial = _json_after(html, "ytInitialData = ") or _json_after(html, "ytInitialData\":")
            token = first_continuation(initial or {})
            if not token:
                raise RuntimeError("no live chat found on that page (not live, or chat disabled)")
            key = (re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html) or [None, None])[1]
            ver = (re.search(r'"INNERTUBE_CONTEXT_CLIENT_VERSION":"([^"]+)"', html) or [None, DEFAULT_VERSION])[1]
            if not key:
                raise RuntimeError("InnerTube key not found on the live page")
            log.info("youtube chat: attached to %s", handle)
            url = CHAT_API.format(key=key)
            while token and not stop.is_set():
                body = {"context": {"client": {"clientName": "WEB", "clientVersion": ver, "hl": "en", "gl": "US"}},
                        "continuation": token}
                resp = json.loads(await http_text(url, body=body, headers={"Origin": "https://www.youtube.com"}))
                cont = (resp.get("continuationContents") or {}).get("liveChatContinuation")
                if cont is None:
                    raise RuntimeError("chat ended or continuation expired")
                parse_actions(cont.get("actions", []), emit, seen, since_usec)
                attempt = 0
                token, wait = next_continuation(cont)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
            if not token and not stop.is_set():
                raise RuntimeError("no further continuation (stream ended?)")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            attempt += 1
            log.warning("youtube chat dropped (%s); retry #%d", exc, attempt)
        if not stop.is_set():
            await backoff_sleep(stop, attempt)
