"""`doctor` command: checks every tool and file the clipper needs."""
from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import sys

from .config import CONFIG_FILE, ENV_FILE, Config, env_key_status, load_config
from .db import init_db
from .proc import run

log = logging.getLogger("clipper")

OK, WARN, FAIL = "OK  ", "WARN", "FAIL"
MIN_MODEL_BYTES = 50 * 1024 * 1024  # real ggml-small models are hundreds of MB

results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str) -> None:
    results.append((status, name, detail))
    line = f"[{status}] {name}: {detail}"
    print(line)
    log.info("doctor %s", line)


def first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:120]
    return "(no output)"


def check_python() -> None:
    v = sys.version_info
    text = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) == (3, 13):
        record(OK, "Python", text)
    elif (v.major, v.minor) > (3, 13) or (v.major, v.minor) == (3, 12):
        record(WARN, "Python", f"{text} (project expects 3.13)")
    else:
        record(FAIL, "Python", f"{text} is too old, need 3.13")
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    record(OK if in_venv else WARN, "Virtual env", ".venv active" if in_venv else "not using .venv (run activate step)")


def check_ffmpeg(cfg: Config) -> None:
    exe = shutil.which(cfg.ffmpeg)
    if not exe:
        record(FAIL, "ffmpeg", f"'{cfg.ffmpeg}' not found on PATH")
        return
    code, out = run([exe, "-version"])
    if code != 0:
        record(FAIL, "ffmpeg", f"found but failed to run: {first_line(out)}")
        return
    record(OK, "ffmpeg", first_line(out))
    # Hardware encoder check: ask ffmpeg to actually encode 1 test frame.
    wanted = cfg.raw.get("encoding", {}).get("encoders", [])
    working = []
    for enc in wanted:
        c, _ = run(
            [exe, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1",
             "-frames:v", "1", "-c:v", enc, "-f", "null", "-"],
            timeout=20,
            low_priority=True,
        )
        if c == 0:
            working.append(enc)
    if working:
        record(OK, "ffmpeg hardware encoder", f"working: {', '.join(working)}")
    else:
        record(WARN, "ffmpeg hardware encoder", "none worked; will fall back to slower libx264 (CPU)")


def check_streamlink(cfg: Config) -> None:
    exe = shutil.which(cfg.streamlink)
    if not exe:
        record(FAIL, "streamlink", f"'{cfg.streamlink}' not found on PATH")
        return
    code, out = run([exe, "--version"])
    record(OK if code == 0 else FAIL, "streamlink", first_line(out))


def check_whisper(cfg: Config) -> None:
    exe = cfg.whisper_cli
    if not exe.is_file():
        record(FAIL, "whisper-cli", f"not found at {exe}")
    else:
        code, out = run([exe, "--help"])
        if re.search(r"usage|options", out, re.I):
            record(OK, "whisper-cli", str(exe))
        else:
            record(FAIL, "whisper-cli", f"found but did not run properly ({first_line(out)})")
    for label, model in (("English", cfg.whisper_model_en), ("Multilingual (Hindi etc.)", cfg.whisper_model_multi)):
        name = f"whisper model, {label}"
        if not model.is_file():
            record(FAIL, name, f"missing: {model}")
        elif model.stat().st_size < MIN_MODEL_BYTES:
            record(FAIL, name, f"file looks incomplete ({model.stat().st_size // 1024} KB): {model}")
        else:
            record(OK, name, f"{model.name} ({model.stat().st_size // (1024 * 1024)} MB)")


def check_claude(cfg: Config, deep: bool) -> None:
    exe = shutil.which(cfg.claude)
    if not exe:
        record(FAIL, "claude CLI", f"'{cfg.claude}' not found on PATH")
        return
    code, out = run([exe, "--version"])
    if code != 0:
        record(FAIL, "claude CLI", f"failed to run: {first_line(out)}")
        return
    record(OK, "claude CLI", first_line(out))
    if deep:
        code, out = run([exe, "-p", "Reply with exactly the word: pong"], timeout=90)
        if code == 0 and "pong" in out.lower():
            record(OK, "claude headless test", "replied correctly")
        else:
            record(FAIL, "claude headless test", f"no valid reply ({first_line(out)}). Try running 'claude' once to log in.")


def check_files_and_data(cfg: Config) -> None:
    record(OK if CONFIG_FILE.is_file() else FAIL, "config.toml", str(CONFIG_FILE))
    if ENV_FILE.is_file():
        status = env_key_status("GROQ_API_KEY")
        record(OK if status == "set" else WARN, "GROQ_API_KEY in .env", status)
    else:
        record(FAIL, ".env", "file missing")
    for f in ("voice.md", "CLAUDE.md"):
        p = CONFIG_FILE.parent / f
        record(OK if p.is_file() else FAIL, f, "present" if p.is_file() else "missing")
    record(OK, "streams / feeds", f"{len(cfg.streams)} streams, {len(cfg.feeds)} RSS feeds")
    if cfg.raw.get("general", {}).get("autopost", False):
        record(FAIL, "autopost", "must be false (Zero-Ban Policy)")
    else:
        record(OK, "autopost", "false")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(cfg.raw.get("general", {}).get("timezone", "Australia/Melbourne"))
        record(OK, "timezone data", "Australia/Melbourne available")
    except Exception as exc:
        record(WARN, "timezone data", f"missing; run: pip install -r requirements.txt ({exc})")
    try:
        cfg.clips_dir.mkdir(parents=True, exist_ok=True)
        init_db(cfg.database)
        with sqlite3.connect(cfg.database) as conn:
            conn.execute("SELECT count(*) FROM clips").fetchone()
        record(OK, "data folder + database", str(cfg.database))
    except Exception as exc:
        record(FAIL, "data folder + database", str(exc))


def check_news_and_watermark(cfg: Config) -> None:
    try:
        import edge_tts  # noqa: F401
        record(OK, "edge-tts (news voiceover)", "installed")
    except ImportError:
        record(WARN, "edge-tts (news voiceover)", "not installed; run: pip install -r requirements.txt")
    record(OK if cfg.news_feeds else WARN, "news feeds", f"{len(cfg.news_feeds)} RSS feeds for `news`")
    from .watermark import ensure_watermark, settings
    if not settings(cfg)["enabled"]:
        record(OK, "watermark", "disabled in config.toml")
    else:
        png = ensure_watermark(cfg)
        record(OK if png else WARN, "watermark", str(png) if png else "assets/watermark.jpg missing or unreadable; clips render without it")


def run_doctor(cfg: Config, deep: bool = False) -> int:
    print("gameon-clipper doctor\n")
    checks = [
        check_python,
        lambda: check_ffmpeg(cfg),
        lambda: check_streamlink(cfg),
        lambda: check_whisper(cfg),
        lambda: check_claude(cfg, deep),
        lambda: check_files_and_data(cfg),
        lambda: check_news_and_watermark(cfg),
    ]
    for check in checks:  # one broken check must not stop the others
        try:
            check()
        except Exception as exc:
            record(FAIL, "doctor internal error", str(exc))
    fails = sum(1 for s, _, _ in results if s == FAIL)
    warns = sum(1 for s, _, _ in results if s == WARN)
    print(f"\n{len(results) - fails - warns} OK, {warns} warnings, {fails} failed.")
    if fails:
        print("Fix the FAIL lines above, then run doctor again.")
    elif not deep:
        print("Tip: run 'python -m clipper doctor --deep' to also test Claude with a real request.")
    return 1 if fails else 0
