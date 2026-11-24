"""`test-chat <stream link> --seconds N`: read chat for N seconds and print rates per 5s bucket."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

from . import ChatWriter, parse_stream_link, read_chat, safe_handle

log = logging.getLogger("clipper")
BUCKET = 5  # seconds


def session_dir(sessions_dir: Path, platform: str, handle: str) -> Path:
    """data/sessions/<platform>-<handle>/<YYYYMMDD-HHMMSS>/ (same layout the recorder uses)."""
    return sessions_dir / f"{platform}-{safe_handle(handle)}".lower() / f"{datetime.now():%Y%m%d-%H%M%S}"


async def _run(link: str, seconds: int, sessions_dir: Path) -> int:
    try:
        target = parse_stream_link(link)
    except ValueError as exc:
        print(exc)
        return 2
    folder = session_dir(sessions_dir, target.platform, target.handle)
    writer = ChatWriter(folder / "chat.jsonl")
    print(f"Reading {target.platform} chat for '{target.handle}' for {seconds}s")
    print(f"Writing messages to {writer.path}\n")
    print(f"{'bucket':>11}  {'msgs':>5}  {'msgs/sec':>8}")

    stop = asyncio.Event()
    started = time.time()
    task = asyncio.create_task(read_chat(target, writer.write, stop))

    async def ticker() -> None:
        last = 0
        for i in range(0, seconds, BUCKET):
            span = min(BUCKET, seconds - i)
            await asyncio.sleep(max(0.0, started + i + span - time.time()))
            n = writer.count
            got = n - last
            last = n
            print(f"{i:>4}-{i + span:<4}s  {got:>5}  {got / span:>8.2f}", flush=True)

    try:
        await ticker()
    except asyncio.CancelledError:
        pass
    finally:
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
        writer.close()
    print(f"\nTotal: {writer.count} messages in {seconds}s ({writer.count / seconds:.2f}/sec)")
    if writer.count == 0:
        print("No messages arrived. Is the stream live with an active chat? See data/logs/clipper.log.")
    return 0


def run_test_chat(link: str, seconds: int, sessions_dir: Path) -> int:
    try:
        return asyncio.run(_run(link, seconds, sessions_dir))
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
