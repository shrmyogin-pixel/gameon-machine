"""Stream recorder: waits for a channel to go live, pipes streamlink into ffmpeg,
and saves rolling 10-second .ts segments. One Recorder thread per stream."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import Config
from .proc import BELOW_NORMAL_PRIORITY_CLASS, CREATE_NO_WINDOW, run

log = logging.getLogger("clipper")

CREATE_NEW_PROCESS_GROUP = 0x00000200  # so Ctrl+C reaches us only; we stop children ourselves


def stream_name(stream: dict) -> str:
    return f"{stream['platform']}-{stream['handle']}".lower()


def _flags(low_priority: bool) -> int:
    if sys.platform != "win32":
        return 0
    flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    if low_priority:
        flags |= BELOW_NORMAL_PRIORITY_CLASS
    return flags


class Recorder(threading.Thread):
    def __init__(self, stream: dict, cfg: Config, stop: threading.Event):
        self.name_ = stream_name(stream)
        super().__init__(name=f"rec-{self.name_}", daemon=True)
        self.stream = stream
        self.cfg = cfg
        self.stop_event = stop
        self.opts = cfg.recorder
        self.stream_dir = cfg.sessions_dir / self.name_
        self.active_dir: Path | None = None
        self.streamlink_exe = shutil.which(cfg.streamlink) or cfg.streamlink
        self.ffmpeg_exe = shutil.which(cfg.ffmpeg) or cfg.ffmpeg

    # ---------- main loop ----------
    def run(self) -> None:
        log.info("[%s] waiting for stream: %s", self.name_, self.stream["url"])
        while not self.stop_event.is_set():
            try:
                self.cleanup()
                if self.is_live():
                    self.record_session()
            except Exception:  # one stream failing must never stop the others
                log.exception("[%s] recorder error, will retry", self.name_)
            self.stop_event.wait(self.opts["check_interval_seconds"])
        log.info("[%s] recorder stopped", self.name_)

    # ---------- live check ----------
    def is_live(self) -> bool:
        code, out = run([self.streamlink_exe, "--json", self.stream["url"]], timeout=45)
        start = out.find("{")
        if start < 0:
            log.warning("[%s] live check gave no JSON (code %s): %s", self.name_, code, out.strip()[:150])
            return False
        try:
            data, _ = json.JSONDecoder().raw_decode(out[start:])
        except ValueError:
            log.warning("[%s] live check output unreadable", self.name_)
            return False
        live = bool(data.get("streams"))
        log.info("[%s] %s", self.name_, "LIVE" if live else "offline")
        return live

    # ---------- recording ----------
    def _streamlink_cmd(self) -> list[str]:
        cmd = [self.streamlink_exe, "--loglevel", "info"]
        if self.opts["twitch_low_latency"] and "twitch.tv" in self.stream["url"]:
            cmd.append("--twitch-low-latency")
        cmd += [self.stream["url"], self.opts["quality"], "-O"]  # -O = write video to stdout
        return cmd

    def _ffmpeg_cmd(self, session_dir: Path) -> list[str]:
        return [
            self.ffmpeg_exe, "-hide_banner", "-loglevel", "warning",
            "-i", "pipe:0",
            "-map", "0:v:0", "-map", "0:a:0",   # only first video+audio; metadata tracks break the muxer
            "-c", "copy",
            "-f", "segment",
            "-segment_time", str(self.opts["segment_seconds"]),
            "-segment_format", "mpegts",
            "-reset_timestamps", "1",
            str(session_dir / "seg_%05d.ts"),
        ]

    def record_session(self) -> None:
        session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        session_dir = self.stream_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        self.active_dir = session_dir
        started = datetime.now()
        log.info("[%s] recording started -> %s", self.name_, session_dir)
        sl = ff = None
        sl_log = ff_log = None
        chat = None
        monitor = mon_stop = None
        reason = "error"
        try:
            sl_log = (session_dir / "streamlink.log").open("wb")
            ff_log = (session_dir / "ffmpeg.log").open("wb")
            sl = subprocess.Popen(self._streamlink_cmd(), stdout=subprocess.PIPE, stderr=sl_log,
                                  stdin=subprocess.DEVNULL, creationflags=_flags(True))
            ff = subprocess.Popen(self._ffmpeg_cmd(session_dir), stdin=sl.stdout, stdout=subprocess.DEVNULL,
                                  stderr=ff_log, creationflags=_flags(self.cfg.raw["encoding"].get("below_normal_priority", True)))
            sl.stdout.close()  # ffmpeg owns the pipe now, so it sees end-of-stream when streamlink exits
            chat = self._start_chat(session_dir)
            monitor, mon_stop = self._start_monitor(session_dir, started, chat)
            last_clean = time.monotonic()
            while not self.stop_event.is_set() and ff.poll() is None:
                self.stop_event.wait(2)
                if time.monotonic() - last_clean > 60:
                    self.cleanup()
                    last_clean = time.monotonic()
            reason = "Ctrl+C / shutdown" if self.stop_event.is_set() else "stream ended"
        except Exception:
            log.exception("[%s] recording crashed", self.name_)
            reason = "error"
        finally:
            chat_count = None
            if chat is not None:
                try:
                    chat_count = chat.stop()
                except Exception:
                    log.exception("[%s] could not stop chat capture", self.name_)
            self._shutdown(sl, ff)
            if monitor is not None:  # after ffmpeg is done, so the last segment is complete
                mon_stop.set()
                monitor.join(timeout=60)
            for f in (sl_log, ff_log):
                if f:
                    f.close()
            self._close_session(session_dir, started, reason, chat_count)
            self.active_dir = None

    def _start_chat(self, session_dir: Path):
        """Capture chat into <session>/chat.jsonl. Failure here must never affect recording."""
        if not self.opts.get("capture_chat", True):
            return None
        try:
            from .chat.capture import ChatCapture

            cap = ChatCapture(self.stream["url"], session_dir / "chat.jsonl", label=self.name_)
            cap.start()
            return cap
        except Exception:
            log.exception("[%s] could not start chat capture; recording continues without chat", self.name_)
            return None

    def _start_monitor(self, session_dir: Path, started: datetime, chat):
        """Spike detection (chat + audio). Failure here must never affect recording."""
        try:
            from .detect import SpikeMonitor, build_params

            params = build_params(self.cfg.raw.get("detect"), self.stream)
            stop = threading.Event()
            mon = SpikeMonitor(session_dir, self.name_, params, self.ffmpeg_exe, started.timestamp(), stop,
                               chat_alive=lambda: chat is not None and chat.is_alive(),
                               segment_seconds=self.opts["segment_seconds"],
                               low_priority=self.cfg.raw["encoding"].get("below_normal_priority", True))
            mon.start()
            return mon, stop
        except Exception:
            log.exception("[%s] could not start spike detection; recording continues", self.name_)
            return None, None

    def _shutdown(self, sl, ff) -> None:
        """Stop streamlink first; ffmpeg then finishes its last segment on its own."""
        if sl and sl.poll() is None:
            sl.terminate()
            try:
                sl.wait(timeout=10)
            except subprocess.TimeoutExpired:
                sl.kill()
        if ff:
            try:
                ff.wait(timeout=20)
            except subprocess.TimeoutExpired:
                ff.kill()
                ff.wait()

    def _close_session(self, session_dir: Path, started: datetime, reason: str, chat_count: int | None = None) -> None:
        segments = sorted(session_dir.glob("seg_*.ts"))
        info = {
            "stream": self.name_,
            "url": self.stream["url"],
            "started": started.isoformat(timespec="seconds"),
            "ended": datetime.now().isoformat(timespec="seconds"),
            "end_reason": reason,
            "segments_written": len(segments),
        }
        if chat_count is not None:
            info["chat_messages"] = chat_count
        try:
            (session_dir / "session.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
        except OSError:
            log.exception("[%s] could not write session.json", self.name_)
        log.info("[%s] session closed (%s), %d segments", self.name_, reason, len(segments))
        if not segments:  # nothing recorded; don't leave an empty folder around
            shutil.rmtree(session_dir, ignore_errors=True)

    # ---------- housekeeping ----------
    def cleanup(self) -> None:
        """Delete segments older than keep_hours, and closed sessions that became empty.

        The active session is never removed (its chat.jsonl is still being written); a closed
        session with no segments left is removed whole, chat.jsonl included, since chat without
        footage is useless."""
        if not self.stream_dir.is_dir():
            return
        cutoff = time.time() - float(self.opts["keep_hours"]) * 3600
        removed = 0
        for session in self.stream_dir.iterdir():
            if not session.is_dir():
                continue
            for seg in session.glob("seg_*.ts"):
                try:
                    if seg.stat().st_mtime < cutoff:
                        seg.unlink()
                        removed += 1
                except OSError:
                    pass  # file in use or already gone
            if session != self.active_dir and not any(session.glob("seg_*.ts")):
                shutil.rmtree(session, ignore_errors=True)
        if removed:
            log.info("[%s] deleted %d old segments", self.name_, removed)


def watch(cfg: Config, streams: list[dict], seconds: float | None = None) -> int:
    """Run one recorder per stream until Ctrl+C (or until `seconds` pass, for testing)."""
    if not streams:
        print("No streams to watch. Add [[streams]] entries to config.toml.")
        return 1
    stop = threading.Event()
    recorders = [Recorder(s, cfg, stop) for s in streams]
    print(f"Watching {len(recorders)} stream(s). Press Ctrl+C to stop.")
    for r in recorders:
        r.start()
        print(f"  - {r.name_}")
    deadline = time.monotonic() + seconds if seconds else None
    try:
        while not stop.is_set():
            stop.wait(0.5)
            if deadline and time.monotonic() >= deadline:
                break
    except KeyboardInterrupt:
        print("\nCtrl+C received, stopping recorders...")
    stop.set()
    for r in recorders:
        r.join(timeout=45)
    print("Stopped cleanly.")
    return 0
