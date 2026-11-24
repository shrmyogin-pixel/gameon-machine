"""Command line entry: python -m clipper <command>"""
from __future__ import annotations

import argparse
import sys

from .config import load_config
from .db import init_db
from .doctor import run_doctor
from .log import setup_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m clipper", description="gameon-clipper")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("doctor", help="check that every required tool is installed and working")
    d.add_argument("--deep", action="store_true", help="also send Claude a tiny real request")
    sub.add_parser("init", help="create the data folders and database")
    w = sub.add_parser("watch", help="run a recorder for every stream in config.toml")
    w.add_argument("--only", help="only watch this handle from config.toml (e.g. lacy)")
    w.add_argument("--url", help="watch one extra stream URL that is not in config.toml (testing)")
    w.add_argument("--seconds", type=float, help="stop automatically after this many seconds (testing)")
    t = sub.add_parser("test-chat", help="read a stream's chat anonymously and print messages/sec per 5s")
    t.add_argument("link", help="Twitch, YouTube or Kick stream link")
    t.add_argument("--seconds", type=int, default=120, help="how long to listen (default 120)")
    r = sub.add_parser("detect-replay", help="run spike detection over a saved session folder (tuning/testing)")
    r.add_argument("session", help="session folder, e.g. data/sessions/twitch-lacy/20260101-120000")
    r.add_argument("--no-audio", action="store_true", help="chat only (skip decoding audio)")
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
    except Exception as exc:
        print(f"Cannot read config.toml: {exc}")
        return 2
    log = setup_logging(cfg.log_file)

    try:
        if args.command == "doctor":
            return run_doctor(cfg, deep=args.deep)
        if args.command == "watch":
            import logging

            from .recorder import watch

            console = logging.StreamHandler()
            console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
            log.addHandler(console)
            if args.url:
                handle = args.url.rstrip("/").split("/")[-1]
                platform = "twitch" if "twitch.tv" in args.url else "youtube"
                streams = [{"platform": platform, "handle": handle, "url": args.url, "lang": "en"}]
            else:
                streams = cfg.streams
                if args.only:
                    streams = [s for s in streams if s["handle"].lower() == args.only.lower()]
            return watch(cfg, streams, args.seconds)
        if args.command == "init":
            cfg.clips_dir.mkdir(parents=True, exist_ok=True)
            init_db(cfg.database)
            print("Ready: " + str(cfg.database))
            return 0
        if args.command == "test-chat":
            import logging

            from .chat.testchat import run_test_chat

            if args.seconds < 1:
                parser.error("--seconds must be at least 1")
            console = logging.StreamHandler(sys.stderr)  # show reconnects/problems while testing
            console.setLevel(logging.WARNING)
            console.setFormatter(logging.Formatter("  ! %(message)s"))
            log.addHandler(console)
            return run_test_chat(args.link, args.seconds, cfg.sessions_dir)
        if args.command == "detect-replay":
            from pathlib import Path

            from .detect import build_params, replay

            folder = Path(args.session).expanduser().resolve()
            if not (folder / "chat.jsonl").is_file() and not any(folder.glob("seg_*.ts")):
                print(f"No chat.jsonl or segments in {folder}")
                return 1
            name = folder.parent.name
            stream = next((s for s in cfg.streams if f"{s['platform']}-{s['handle']}".lower() == name), None)
            moments = replay(folder, name, build_params(cfg.raw.get("detect"), stream), cfg.ffmpeg,
                             with_audio=not args.no_audio)
            for m in moments:
                print(m.describe())
            print(f"{len(moments)} moment(s)")
            return 0
    except Exception:
        log.exception("command %s crashed", args.command)
        print("Something went wrong; details are in data/logs/clipper.log")
        return 1
    return 0
