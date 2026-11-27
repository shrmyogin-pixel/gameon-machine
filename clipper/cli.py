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
    c = sub.add_parser("test-cut", help="run whisper-cli and make frames.jpg for any video file (no live stream needed)")
    c.add_argument("video", help="path to any .mp4")
    c.add_argument("--lang", default="en", help="language code: en uses the English model, others the multilingual one (default en)")
    e = sub.add_parser("edit", help="run the Claude AI editor on an existing clip folder (needs payload.json + frames.jpg)")
    e.add_argument("folder", help="clip folder, e.g. data/clips/twitch-lacy-20260101-120000")
    rd = sub.add_parser("render", help="render clip.mp4 + thumb.jpg for a clip folder that the editor approved")
    rd.add_argument("folder", help="clip folder with payload.json, analysis.json and source.mp4")
    rd.add_argument("--layout", choices=["auto", "stack", "blur"], default="auto",
                    help="auto = facecam stack if a face is found in a corner, else blurred background")
    ru = sub.add_parser("run", help="unattended mode: watch + detect + clip + edit + render, restarting crashed workers")
    ru.add_argument("--only", help="only run this handle from config.toml (e.g. lacy)")
    ru.add_argument("--seconds", type=float, help="stop automatically after this many seconds (testing)")
    st = sub.add_parser("status", help="queue counts, sessions, disk usage and recent errors")
    st.add_argument("--watch", type=float, nargs="?", const=5.0, metavar="SECONDS",
                    help="refresh continuously (default every 5 seconds)")
    ex = sub.add_parser("export", help="package approved clips (mp4 + thumbnail + upload sheet) for manual upload")
    ex.add_argument("--all", action="store_true", help="re-export every approved clip, even ones exported before")
    ex.add_argument("--id", type=int, action="append", metavar="ID", help="export this clip id (repeatable; re-exports)")
    ex.add_argument("--dry-run", action="store_true", help="show what would be exported, copy nothing")
    nw = sub.add_parser("news", help="Breaking News Engine: make news Shorts from the RSS feeds")
    nw.add_argument("--hours", type=float, help="only stories newer than this (default [news] hours = 12)")
    nw.add_argument("--count", type=int, help="how many stories to make (default [news] stories = 2)")
    nw.add_argument("--dry-run", action="store_true", help="pick stories and write scripts only: no voice, video or database rows")
    nw.add_argument("--topic", choices=["gta", "creators", "tech", "promo"], help="only this theme's stories (looks back [news] <theme>_hours)")
    nw.add_argument("--only-gta", dest="topic", action="store_const", const="gta", help="same as --topic gta")
    nw.add_argument("--creators", dest="topic", action="store_const", const="creators", help="same as --topic creators")
    nw.add_argument("--no-trends", action="store_true", help="skip the trend-discovery Short(s) at the end of the run")
    tr = sub.add_parser("trends", help="Trend discovery: original Shorts from trending gaming topics (YouTube metadata, giveaways)")
    tr.add_argument("--count", type=int, help="how many trend Shorts to make (default [trends] per_run)")
    tr.add_argument("--dry-run", action="store_true", help="pick topics and write scripts only: no voice, video or database rows")
    cl = sub.add_parser("clean", help="delete skipped/rejected clips older than 2 days and posted clips older than 7 days")
    cl.add_argument("--dry-run", action="store_true", help="list what would be deleted, delete nothing")
    sub.add_parser("route", help="show / refresh the platform + peak-slot routing of the pending clips")
    sub.add_parser("watermark", help="(re)build assets/watermark.png from assets/watermark.jpg")
    rv = sub.add_parser("review", help="start the local review dashboard (http://127.0.0.1:8765, localhost only)")
    rv.add_argument("--port", type=int, default=8765, help="port to listen on (default 8765)")
    rv.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
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
        if args.command == "test-cut":
            from pathlib import Path

            from .worker import ClipError, test_cut

            try:
                folder = test_cut(cfg, Path(args.video).expanduser(), args.lang)
            except ClipError as exc:
                print(f"test-cut failed: {exc}")
                return 1
            print(f"Done. Files in {folder}")
            return 0
        if args.command == "edit":
            from pathlib import Path

            from .editor import edit_clip

            folder = Path(args.folder).expanduser().resolve()
            if not (folder / "payload.json").is_file():
                print(f"No payload.json in {folder}")
                return 1
            status = edit_clip(cfg, folder)
            print(f"Editor result: {status}  ({folder / 'analysis.json'})")
            return 0 if status != "failed" else 1
        if args.command == "render":
            from pathlib import Path

            from .render import render_clip

            folder = Path(args.folder).expanduser().resolve()
            status = render_clip(cfg, folder, args.layout)
            print(f"Render result: {status}  ({folder / 'clip.mp4'})")
            return 0 if status == "rendered" else 1
        if args.command == "run":
            import logging

            from .runner import run

            console = logging.StreamHandler()
            console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
            log.addHandler(console)
            streams = cfg.streams
            if args.only:
                streams = [s for s in streams if s["handle"].lower() == args.only.lower()]
            return run(cfg, streams, args.seconds)
        if args.command == "status":
            from .status import run_status

            return run_status(cfg, args.watch)
        if args.command == "export":
            from .export import run_export

            try:
                sys.stdout.reconfigure(errors="replace")
            except Exception:
                pass
            res = run_export(cfg, args.all, args.id, args.dry_run)
            verb = "Would export" if args.dry_run else "Exported"
            for clip, mp4 in res.exported:
                print(f"  {verb}: #{clip['id']} {clip['handle']} -> {mp4}")
            for clip, why in res.refused:
                print(f"  REFUSED: #{clip['id']} {clip.get('folder', '')}: {why}")
            if not res.exported and not res.refused:
                print("Nothing to export: no approved clips waiting (approve some in `python -m clipper review`,"
                      " or use --all / --id to re-export).")
            else:
                print(f"{len(res.exported)} {'would be exported' if args.dry_run else 'exported'}, {len(res.refused)} refused."
                      + (f"  Folder: {mp4.parent}" if res.exported else ""))
                print("Nothing was uploaded: post these by hand, then press 'Mark Uploaded' in the review dashboard.")
            return 0 if res.ok else 1
        if args.command in ("news", "trends"):
            import logging

            from .news import run_news

            try:
                sys.stdout.reconfigure(errors="replace")
            except Exception:
                pass
            console = logging.StreamHandler()
            console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
            log.addHandler(console)
            print("Reading feeds, asking Claude to pick the stories (this takes a minute)...")
            if args.command == "trends":
                from .trends import run_trends
                res = run_trends(cfg, args.count, args.dry_run)
            else:
                res = run_news(cfg, args.hours, args.count, args.dry_run, trends=not args.no_trends, only=args.topic)
            if res.note:
                print(res.note)
            for p in res.picked:
                print(f"\n[{p.kind}] {p.story.outlet}: {p.story.title}\n  {p.story.link}")
                print(f"  score {p.score:g} | hook ({p.hook.total:g}/10): {p.hook.hook}")
                for h in p.hooks:
                    print(f"     {h.total:4.1f}  {h.hook}" + ("" if h.valid else f"   [INVALID: {'; '.join(h.reasons)}]"))
                print(f"  script ({len(p.script.split())} words): {p.script}")
            for p, folder in res.made:
                print(f"  MADE: {folder}")
            for p, why in res.failed:
                print(f"  FAILED: {p.story.title[:60]}: {why}")
            if args.dry_run:
                print("\nDry run: nothing was voiced, rendered or saved.")
            elif res.made:
                print(f"\n{len(res.made)} news Short(s) ready. Review them with: python -m clipper review")
            print("Nothing was uploaded.")
            return 0 if (res.made or (args.dry_run and res.picked) or (not res.failed and not res.note.startswith("run failed"))) else 1
        if args.command == "clean":
            from .clean import run_clean, settings as clean_settings

            res = run_clean(cfg, args.dry_run)
            verb = "Would delete" if args.dry_run else "Deleted"
            for c in res.deleted:
                print(f"  {verb}: #{c['id']} {c['status']:<8} {c['folder']}")
            for c, why in res.failed:
                print(f"  COULD NOT DELETE: #{c['id']} {c['folder']}: {why}")
            cs = clean_settings(cfg)
            print(f"{len(res.deleted)} clip(s) {'would be ' if args.dry_run else ''}deleted, {res.freed_bytes / 1e6:.1f} MB "
                  f"{'would be ' if args.dry_run else ''}freed, {len(res.failed)} failed."
                  f"  (skipped/rejected > {cs['skipped_days']}/{cs['rejected_days']} days, posted > {cs['posted_days']} days)")
            return 0 if not res.failed else 1
        if args.command == "route":
            from .routing import assign_routes

            routes = assign_routes(cfg)
            from . import db
            clips = {c["id"]: c for c in db.list_clips(cfg.database, limit=1000)}
            if not routes:
                print("No rendered or approved clips in the routing window.")
            for cid, r in sorted(routes.items(), key=lambda kv: kv[1]["rank"] or 999):
                c = clips.get(cid, {})
                where = f"YOUTUBE  {r['slot']}  {r['at']}" if r["platform"] == "youtube" else r["platform"].upper()
                print(f"  #{r['rank']:<3} id {cid:<4} {c.get('platform', ''):<8} {c.get('handle', ''):<18} "
                      f"score {c.get('ai_score') if c.get('ai_score') is not None else '-':<4} -> {where}")
            return 0
        if args.command == "watermark":
            from .watermark import build_watermark, find_source, settings
            from .config import expand_path

            s = settings(cfg)
            src = find_source(expand_path(str(s["source"])))
            if not src:
                print(f"Source image not found: {s['source']}")
                return 1
            out = build_watermark(src, expand_path(str(s["file"])))
            print(f"Wrote {out}")
            return 0
        if args.command == "review":
            from .review import run_review

            return run_review(cfg, args.port, not args.no_browser)
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
