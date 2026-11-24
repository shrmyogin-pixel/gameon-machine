"""Twitch chat over anonymous IRC (justinfan login, read-only)."""
from __future__ import annotations

import asyncio
import logging
import random

from . import Emit, backoff_sleep

log = logging.getLogger("clipper")

HOST, PORT = "irc.chat.twitch.tv", 6667


def parse_privmsg(line: str) -> tuple[str, str] | None:
    """Parse '@tags :nick!nick@nick.tmi.twitch.tv PRIVMSG #chan :text' -> (user, text)."""
    tags: dict[str, str] = {}
    if line.startswith("@"):
        raw, _, line = line.partition(" ")
        for kv in raw[1:].split(";"):
            k, _, v = kv.partition("=")
            tags[k] = v
    if not line.startswith(":"):
        return None
    prefix, _, rest = line[1:].partition(" ")
    command, _, rest = rest.partition(" ")
    if command != "PRIVMSG":
        return None
    _, _, text = rest.partition(" :")
    nick = prefix.split("!", 1)[0]
    name = tags.get("display-name", "").replace("\\s", " ") or nick
    if text.startswith("\x01ACTION ") and text.endswith("\x01"):  # /me messages
        text = text[8:-1]
    return name, text


async def read(channel: str, emit: Emit, stop: asyncio.Event) -> None:
    attempt = 0
    while not stop.is_set():
        writer = None
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(HOST, PORT), 15)
            nick = f"justinfan{random.randint(10000, 99999)}"
            writer.write(
                f"CAP REQ :twitch.tv/tags\r\nPASS SCHMOOPIIE\r\nNICK {nick}\r\nJOIN #{channel}\r\n".encode()
            )
            await writer.drain()
            log.info("twitch chat: joined #%s as %s", channel, nick)
            buf = b""
            while not stop.is_set():
                try:
                    chunk = await asyncio.wait_for(reader.read(8192), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if not chunk:
                    raise ConnectionError("server closed the connection")
                attempt = 0  # got data: connection is healthy
                buf += chunk
                while b"\r\n" in buf:
                    raw, buf = buf.split(b"\r\n", 1)
                    line = raw.decode("utf-8", errors="replace")
                    if line.startswith("PING"):
                        writer.write(b"PONG" + line[4:].encode("utf-8") + b"\r\n")
                        await writer.drain()
                    elif line.endswith("RECONNECT") or " RECONNECT" in line:
                        raise ConnectionError("server asked us to reconnect")
                    else:
                        msg = parse_privmsg(line)
                        if msg:
                            emit(*msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            attempt += 1
            log.warning("twitch chat dropped (%s); retry #%d", exc, attempt)
        finally:
            if writer is not None:
                writer.close()
        if not stop.is_set():
            await backoff_sleep(stop, attempt)
