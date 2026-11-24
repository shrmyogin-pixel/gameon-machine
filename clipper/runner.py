"""Unattended runner: `python -m clipper run`.

Does everything `watch` does (recorders, chat + audio spike detection, clipper workers, headless Claude
editing, hardware rendering) plus:

* a supervisor that restarts any recorder thread that dies, with a growing delay (5s, 10s ... 2 min),
  so one broken stream never stops the others or the loop;
* Windows keep-awake while running (SetThreadExecutionState), released on exit;
* a heartbeat file (data/run_state.json) that `python -m clipper status` reads.

Per-clip failures are already caught inside the worker (the clip is marked 'failed' and the pool carries on).
Nothing here uploads anything (autopost = false).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import Config
from .recorder import Recorder

log = logging.getLogger("clipper")

TICK_SECONDS = 2.0
HEARTBEAT_SECONDS = 10.0
BACKOFF_FIRST = 5.0
BACKOFF_MAX = 120.0
HEALTHY_AFTER = 600.0          # a recorder alive this long counts as healthy: its restart counter resets
STATE_FILE = "run_state.json"

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake(on: bool) -> bool:
    """Ask Windows not to sleep (the screen may still turn off). Returns False where that is not possible.

    Applies to the calling thread, so call it from the thread that lives as long as the runner."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception:
        log.exception("could not change the keep-awake setting")
        return False


def state_path(cfg: Config) -> Path:
    return cfg.data_dir / STATE_FILE


def backoff(restarts: int) -> float:
    return min(BACKOFF_FIRST * (2 ** max(restarts, 0)), BACKOFF_MAX)


class Slot:
    """One supervised stream."""

    def __init__(self, stream: dict, rec: Recorder):
        self.stream = stream
        self.rec = rec
        self.started = time.monotonic()
        self.restarts = 0           # since last healthy spell
        self.total_restarts = 0
        self.retry_at: float | None = None
        self.last_crash = ""


def supervise(slots: list[Slot], make_recorder, stop: threading.Event, now: float | None = None) -> int:
    """One supervisor pass: restart dead recorders when their back-off is over. Returns how many restarted."""
    now = time.monotonic() if now is None else now
    restarted = 0
    for s in slots:
        if stop.is_set():
            break
        if s.rec.is_alive():
            if now - s.started > HEALTHY_AFTER:
                s.restarts = 0
            continue
        if s.retry_at is None:
            s.retry_at = now + backoff(s.restarts)
            s.last_crash = datetime.now().isoformat(timespec="seconds")
            log.error("[%s] recorder thread died; restarting in %.0fs", s.rec.name_, s.retry_at - now)
        elif now >= s.retry_at:
            try:
                s.rec = make_recorder(s.stream)
                s.rec.start()
                s.started, s.retry_at = now, None
                s.restarts += 1
                s.total_restarts += 1
                restarted += 1
                log.info("[%s] recorder restarted (restart #%d)", s.rec.name_, s.total_restarts)
            except Exception:
                log.exception("[%s] could not restart recorder; will try again", s.rec.name_)
                s.restarts += 1
                s.retry_at = now + backoff(s.restarts)
    return restarted


def write_state(cfg: Config, started: str, slots: list[Slot], stopping: bool = False) -> None:
    data = {"pid": os.getpid(), "started": started, "heartbeat": time.time(), "stopping": stopping,
            "keep_awake": True,
            "streams": [{"name": s.rec.name_, "alive": s.rec.is_alive(), "restarts": s.total_restarts,
                         "last_crash": s.last_crash,
                         "recording": bool(getattr(s.rec, "active_dir", None))} for s in slots]}
    try:
        path = state_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        log.warning("could not write %s", STATE_FILE)


def run(cfg: Config, streams: list[dict], seconds: float | None = None, recorder_cls=Recorder,
        pool_cls=None) -> int:
    """Run until Ctrl+C (or `seconds`, for testing). Returns 0 after a clean stop."""
    if not streams:
        print("No streams to watch. Add [[streams]] entries to config.toml.")
        return 1
    if pool_cls is None:
        from .worker import ClipperPool as pool_cls

    if cfg.raw.get("general", {}).get("autopost", False):
        log.warning("config has autopost=true; ignored - this tool never uploads")
    stop = threading.Event()
    pool = pool_cls(cfg)
    awake = keep_awake(True)
    started = datetime.now().isoformat(timespec="seconds")

    def make(stream):
        return recorder_cls(stream, cfg, stop, pool)

    slots = [Slot(s, make(s)) for s in streams]
    print(f"Unattended run: {len(slots)} stream(s). Ctrl+C to stop.  "
          f"Keep-awake: {'on' if awake else 'not available'}.")
    log.info("run started: %d stream(s), keep-awake %s", len(slots), "on" if awake else "unavailable")
    for s in slots:
        s.rec.start()
        print(f"  - {s.rec.name_}")
    deadline = time.monotonic() + seconds if seconds else None
    last_beat = 0.0
    try:
        while not stop.is_set():
            supervise(slots, make, stop)
            if time.monotonic() - last_beat >= HEARTBEAT_SECONDS:
                write_state(cfg, started, slots)
                last_beat = time.monotonic()
            stop.wait(TICK_SECONDS)
            if deadline and time.monotonic() >= deadline:
                break
    except KeyboardInterrupt:
        print("\nCtrl+C received, stopping...")
    finally:
        stop.set()
        write_state(cfg, started, slots, stopping=True)
        for s in slots:
            s.rec.join(timeout=45)
        print("Finishing any clips in progress...")
        try:
            pool.close()
        finally:
            keep_awake(False)
            try:
                state_path(cfg).unlink(missing_ok=True)
            except OSError:
                pass
    print("Stopped cleanly.")
    log.info("run stopped cleanly")
    return 0
