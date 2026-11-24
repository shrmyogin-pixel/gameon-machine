"""Kick chat over Kick's realtime websocket, anonymous (guest token).

Kick has changed realtime provider before (Pusher -> Centrifugo), so we never hard-code one.
Every connect we ask Kick's realtime endpoint which provider to use, exactly like kick.com does:

    POST https://web.kick.com/api/v1/realtime/channels/{channel_id}/chat/connection
         {"client": {...}, "capabilities": {"accepted_providers": [pusher, centrifugo]}}

and speak whichever one it answers with (Centrifugo today; Pusher kept as fallback).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid

import websockets

from . import Emit, backoff_sleep
from .http import http_json

log = logging.getLogger("clipper")

CHANNEL_API = "https://kick.com/api/v2/channels/{slug}"
REALTIME = "https://web.kick.com/api/v1/realtime"
EMOTE = re.compile(r"\[emote:\d+:([^\]]*)\]")


def clean_content(text: str) -> str:
    return EMOTE.sub(lambda m: m.group(1), text)


async def lookup_channel(slug: str) -> tuple[int, int]:
    """-> (channel_id, chatroom_id)"""
    data = await http_json(CHANNEL_API.format(slug=slug))
    return int(data["id"]), int(data["chatroom"]["id"])


async def choose_provider(channel_id: int, client_id: str) -> dict:
    """Ask Kick which realtime provider to use. Returns {'provider': ..., **credentials}."""
    body = {
        "client": {"id": client_id, "type": "web"},
        "capabilities": {"accepted_providers": [{"provider": "pusher"}, {"provider": "centrifugo"}]},
    }
    resp = await http_json(f"{REALTIME}/channels/{channel_id}/chat/connection", body=body)
    data = resp.get("data") or {}
    if data.get("mode") != "websocket":
        raise RuntimeError(f"Kick realtime mode is {data.get('mode')!r}, expected websocket")
    for conn in data.get("connections", []):
        cred = conn.get("credentials") or {}
        if conn.get("provider") == "centrifugo" and cred.get("url"):
            return {"provider": "centrifugo", "url": cred["url"]}
        if conn.get("provider") == "pusher" and cred.get("app_key") and cred.get("cluster"):
            return {"provider": "pusher", "app_key": cred["app_key"], "cluster": cred["cluster"]}
    raise RuntimeError(f"Kick offered no provider we support: {[c.get('provider') for c in data.get('connections', [])]}")


def handle_event(event: str, data, emit: Emit) -> None:
    """Both providers carry the same Laravel event envelope."""
    if event != r"App\Events\ChatMessageEvent":
        return
    if isinstance(data, str):
        data = json.loads(data)
    sender = (data.get("sender") or {}).get("username") or "unknown"
    content = clean_content(data.get("content") or "")
    if content:
        emit(sender, content)


async def run_centrifugo(url: str, chatroom_id: int, client_id: str, emit: Emit, stop: asyncio.Event) -> None:
    token = (await http_json(f"{REALTIME}/auth/connection", body={"client_id": client_id}))["data"]["token"]
    channel = f"chatrooms.{chatroom_id}.v2"
    # Public channels return an empty subscription token, which is what the website sends too.
    try:
        sub_token = (await http_json(f"{REALTIME}/auth/channel", body={"channel": channel}))["data"].get("token", "")
    except Exception:
        sub_token = ""
    async with websockets.connect(url, origin="https://kick.com", open_timeout=15, max_size=2**22) as ws:
        await ws.send(json.dumps({"id": 1, "connect": {"token": token, "name": "js"}}))
        subscribed = False
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            for part in str(raw).split("\n"):
                if not part.strip():
                    continue
                frame = json.loads(part)
                if frame == {}:  # Centrifugo ping -> answer with an empty pong
                    await ws.send("{}")
                elif "error" in frame:
                    raise RuntimeError(f"Kick centrifugo error: {frame['error']}")
                elif "connect" in frame and not subscribed:
                    await ws.send(json.dumps({"id": 2, "subscribe": {"channel": channel, "token": sub_token}}))
                    subscribed = True
                    log.info("kick chat: subscribed to %s", channel)
                elif "push" in frame:
                    pub = (frame["push"].get("pub") or {}).get("data")
                    if isinstance(pub, dict):
                        handle_event(pub.get("event", ""), pub.get("data"), emit)


async def run_pusher(app_key: str, cluster: str, chatroom_id: int, emit: Emit, stop: asyncio.Event) -> None:
    url = f"wss://ws-{cluster}.pusher.com/app/{app_key}?protocol=7&client=js&version=8.4.0&flash=false"
    channel = f"chatrooms.{chatroom_id}.v2"
    async with websockets.connect(url, origin="https://kick.com", open_timeout=15, max_size=2**22) as ws:
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            frame = json.loads(raw)
            event = frame.get("event", "")
            if event == "pusher:connection_established":
                await ws.send(json.dumps({"event": "pusher:subscribe", "data": {"auth": "", "channel": channel}}))
                log.info("kick chat: subscribed to %s (pusher)", channel)
            elif event == "pusher:ping":
                await ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
            elif event == "pusher:error":
                raise RuntimeError(f"Kick pusher error: {frame.get('data')}")
            else:
                handle_event(event, frame.get("data"), emit)


async def read(slug: str, emit: Emit, stop: asyncio.Event) -> None:
    attempt = 0
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        started = loop.time()
        try:
            channel_id, chatroom_id = await lookup_channel(slug)
            client_id = uuid.uuid4().hex
            prov = await choose_provider(channel_id, client_id)
            log.info("kick chat: Kick chose provider %s for %s", prov["provider"], slug)
            if prov["provider"] == "centrifugo":
                await run_centrifugo(prov["url"], chatroom_id, client_id, emit, stop)
            else:
                await run_pusher(prov["app_key"], prov["cluster"], chatroom_id, emit, stop)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A connection that stayed up a while was healthy: restart the backoff.
            attempt = 1 if loop.time() - started > 30 else attempt + 1
            log.warning("kick chat dropped (%s); retry #%d", exc, attempt)
        if not stop.is_set():
            await backoff_sleep(stop, attempt)
