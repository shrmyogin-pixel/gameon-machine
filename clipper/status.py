"""`python -m clipper status`: a read-only snapshot of the queue, sessions, disk and recent errors.

Reads the database, the data/sessions folders, the log and the runner's heartbeat file. It never writes
anything and never prints secrets (only file names and counts).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from .config import Config
from .db import connect, init_db
from .runner import state_path

QUEUE_ORDER = ("new", "skipped", "rendered", "approved", "rejected", "posted", "failed")
HEARTBEAT_FRESH = 45.0          # seconds; the runner writes every 10
RECENT_SESSION_HOURS = 24
MAX_SESSIONS = 12
MAX_ERRORS = 8
LOG_TAIL_BYTES = 400_000
LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ (ERROR|CRITICAL) (.*)$")


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def dir_size(path: Path) -> int:
    total = 0
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259                              # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ collectors
def runner_info(cfg: Config) -> dict:
    try:
        data = json.loads(state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"running": False}
    age = time.time() - float(data.get("heartbeat", 0))
    data["running"] = age <= HEARTBEAT_FRESH and pid_alive(int(data.get("pid", 0)))
    data["age"] = age
    return data


def queue_counts(cfg: Config) -> dict:
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM clips GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def recent_failures(cfg: Config, limit: int = 5) -> list[dict]:
    with connect(cfg.database) as conn:
        rows = conn.execute("SELECT folder, error, COALESCE(updated_at, created_at) AS at FROM clips"
                            " WHERE status = 'failed' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def sessions(cfg: Config, segment_seconds: float, now: float | None = None) -> list[dict]:
    """Active and recent recording sessions, newest first. Skips the _chat-tests scratch folders."""
    now = time.time() if now is None else now
    out = []
    if not cfg.sessions_dir.is_dir():
        return out
    for stream_dir in cfg.sessions_dir.iterdir():
        if not stream_dir.is_dir() or stream_dir.name.startswith("_"):
            continue
        for sd in stream_dir.iterdir():
            if not sd.is_dir():
                continue
            segs = list(sd.glob("seg_*.ts"))
            newest = max((s.stat().st_mtime for s in segs if s.exists()), default=0.0)
            info_file = sd / "session.json"
            info = {}
            if info_file.is_file():
                try:
                    info = json.loads(info_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass
            mtime = max(newest, sd.stat().st_mtime)
            if not segs and not (not info and now - sd.stat().st_mtime <= 90):
                continue                   # empty leftover folder; only a just-started session is shown
            if info:
                state = "closed"
            elif now - max(newest, sd.stat().st_mtime) <= segment_seconds * 3 + 30:
                state = "RECORDING"
            else:
                state = "interrupted"      # no session.json and nothing written lately (crash / power cut)
            if state != "RECORDING" and now - mtime > RECENT_SESSION_HOURS * 3600:
                continue
            out.append({"stream": stream_dir.name, "session": sd.name, "state": state, "segments": len(segs),
                        "chat": info.get("chat_messages"), "end_reason": info.get("end_reason", ""),
                        "mtime": mtime, "size": sum(s.stat().st_size for s in segs if s.exists())})
    out.sort(key=lambda s: (s["state"] != "RECORDING", -s["mtime"]))
    return out[:MAX_SESSIONS]


def disk_usage(cfg: Config) -> dict:
    parts = {}
    if cfg.data_dir.is_dir():
        for child in sorted(cfg.data_dir.iterdir()):
            parts[child.name] = dir_size(child) if child.is_dir() else child.stat().st_size
    free = shutil.disk_usage(cfg.data_dir if cfg.data_dir.exists() else cfg.data_dir.parent)
    return {"parts": parts, "total": sum(parts.values()), "free": free.free, "disk_total": free.total}


def recent_log_errors(cfg: Config, limit: int = MAX_ERRORS) -> list[tuple[str, str]]:
    """Last ERROR lines from clipper.log (first line of each; tracebacks are in the log itself)."""
    try:
        with cfg.log_file.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(f.tell() - LOG_TAIL_BYTES, 0))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    found = [(m.group(1), m.group(3)[:160]) for line in text.splitlines() if (m := LOG_LINE.match(line))]
    return found[-limit:][::-1]


# ------------------------------------------------------------------ output
def render(cfg: Config) -> str:
    now = datetime.now()
    L = [f"gameon-clipper status   {now:%Y-%m-%d %H:%M:%S}", ""]

    r = runner_info(cfg)
    if r["running"]:
        L.append(f"Runner: RUNNING (pid {r['pid']}, since {r['started']}, heartbeat {r['age']:.0f}s ago)")
        for s in r.get("streams", []):
            tag = "recording" if s.get("recording") else ("waiting" if s.get("alive") else "DEAD (restarting)")
            extra = f", restarted {s['restarts']}x" if s.get("restarts") else ""
            L.append(f"  - {s['name']}: {tag}{extra}")
    else:
        L.append("Runner: not running" + ("  (stale heartbeat file: the last run did not stop cleanly)" if r.get("heartbeat") else ""))
        L.append("  start it with run.bat (or: python -m clipper run)")

    counts = queue_counts(cfg)
    L += ["", "Clip queue:"]
    for st in QUEUE_ORDER:
        L.append(f"  {st:<9} {counts.get(st, 0):>5}")
    if counts.get("edited"):
        L.append(f"  {'in-progress':<9} {counts['edited']:>5}   (edited, waiting to render)")
    others = {k: v for k, v in counts.items() if k not in QUEUE_ORDER and k != "edited"}
    for k, v in others.items():
        L.append(f"  {k:<9} {v:>5}")
    L.append(f"  {'total':<9} {sum(counts.values()):>5}")

    seg = float(cfg.recorder["segment_seconds"])
    ss = sessions(cfg, seg)
    L += ["", "Sessions (active + last 24h):"]
    if not ss:
        L.append("  none")
    for s in ss:
        chat = f", chat {s['chat']}" if s["chat"] is not None else ""
        why = f", {s['end_reason']}" if s["end_reason"] else ""
        L.append(f"  {s['state']:<11} {s['stream']:<26} {s['session']}  {s['segments']} segs "
                 f"({human_size(s['size'])}){chat}{why}")

    d = disk_usage(cfg)
    L += ["", f"Disk, {cfg.data_dir.name}/: {human_size(d['total'])} used, {human_size(d['free'])} free on the drive"]
    for name, size in sorted(d["parts"].items(), key=lambda kv: -kv[1]):
        L.append(f"  {name:<14} {human_size(size):>10}")
    if d["free"] < 5 * 1024 ** 3:
        L.append("  WARNING: less than 5 GB free; recordings and renders may fail")

    L += ["", "Recent errors:"]
    errs = recent_log_errors(cfg)
    fails = recent_failures(cfg)
    if not errs and not fails:
        L.append("  none")
    for at, msg in errs:
        L.append(f"  [{at}] {msg}")
    if fails:
        L.append("  failed clips:")
        for f in fails:
            L.append(f"    {f['folder']}: {(f['error'] or 'unknown')[:120]}")
    return "\n".join(L)


def run_status(cfg: Config, watch: float | None = None) -> int:
    import sys
    try:
        sys.stdout.reconfigure(errors="replace")   # never die on an odd character in a name
    except Exception:
        pass
    if not watch:
        print(render(cfg))
        return 0
    try:
        while True:
            print("\033[2J\033[H" + render(cfg) + f"\n\n(refreshing every {watch:g}s, Ctrl+C to quit)", flush=True)
            time.sleep(watch)
    except KeyboardInterrupt:
        return 0
