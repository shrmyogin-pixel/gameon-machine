"""Run a chat reader in a background thread, writing chat.jsonl next to a recording."""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

from . import ChatWriter, parse_stream_link, read_chat

log = logging.getLogger("clipper")


class ChatCapture(threading.Thread):
    """Records one stream's chat into `path` until stop() is called. Never raises into the caller."""

    def __init__(self, stream_url: str, path: Path, label: str = ""):
        super().__init__(name=f"chat-{label or path.parent.name}", daemon=True)
        self.stream_url = stream_url
        self.path = path
        self.label = label or path.parent.name
        self.count = 0
        self._halt = threading.Event()

    def stop(self, timeout: float = 10) -> int:
        """Stop the reader, wait for it, and return how many messages were written."""
        self._halt.set()
        if self.is_alive():
            self.join(timeout)
        return self.count

    def run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception:
            log.exception("[%s] chat capture crashed (recording is unaffected)", self.label)

    async def _main(self) -> None:
        try:
            target = parse_stream_link(self.stream_url)
        except ValueError as exc:
            log.warning("[%s] chat capture skipped: %s", self.label, exc)
            return
        writer = ChatWriter(self.path)
        stop = asyncio.Event()
        task = asyncio.create_task(read_chat(target, writer.write, stop))
        try:
            while not self._halt.is_set() and not task.done():
                await asyncio.sleep(0.5)
        finally:
            stop.set()
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
            except Exception:
                pass
            self.count = writer.count
            writer.close()
            log.info("[%s] chat capture closed, %d messages", self.label, self.count)
