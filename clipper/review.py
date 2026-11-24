"""Local review dashboard: `python -m clipper review` -> http://127.0.0.1:8765

* Binds to 127.0.0.1 only (never reachable from the network). Host and Origin headers are checked, and
  every change needs a JSON body, so a web page you happen to visit cannot click buttons for you.
* Approve / Reject / Mark Uploaded and the Slot / Send-to choices only record your decision in the database. Nothing is
  ever uploaded (autopost = false); you upload by hand. Reject and Mark Uploaded also delete the clip's folder at once
  (and remember a news story so it is never made again), see lifecycle.py.
* The default view ("active") is the inbox: clips to review plus approved clips.
* Video and thumbnail are served by clip id from the clip's own folder with fixed file names, so no
  path from the browser is ever opened.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import db
from .config import Config
from .lifecycle import discard
from .routing import assign_routes

log = logging.getLogger("clipper")

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PAGE = Path(__file__).with_name("review_page.html")
MEDIA = {"clip.mp4": "video/mp4", "thumb.jpg": "image/jpeg"}
MAX_BODY = 64 * 1024
CHUNK = 256 * 1024


def clip_view(clip: dict, clips_dir: Path) -> dict:
    """What the browser gets for one clip: the row plus media urls and the editable/locked description."""
    out = {k: v for k, v in clip.items() if k not in ("clip_path", "thumb_path", "frames_path", "analysis_path")}
    folder = clips_dir / clip["folder"]
    out["video_url"] = f"/media/{clip['id']}/clip.mp4" if (folder / "clip.mp4").is_file() else None
    out["thumb_url"] = f"/media/{clip['id']}/thumb.jpg" if (folder / "thumb.jpg").is_file() else None
    out["description_body"], out["credit"] = db.split_description(clip)
    out["can_review"] = clip["status"] in db.REVIEW_STATUSES
    out["can_route"] = clip["status"] in ("rendered", "approved")
    out["files_deleted"] = bool(clip.get("files_deleted_at"))
    return out


def _tri(value: str | None) -> bool | None:
    return {"yes": True, "no": False}.get(value or "")


class Handler(BaseHTTPRequestHandler):
    server_version = "gameon-review"
    protocol_version = "HTTP/1.1"

    @property
    def cfg(self) -> Config:
        return self.server.cfg

    @property
    def allowed_hosts(self) -> set[str]:
        return self.server.allowed_hosts

    def log_message(self, fmt, *args):  # keep the console quiet; problems go to the log file
        log.debug("review http: " + fmt, *args)

    # ---------------------------------------------------------------- helpers
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "script-src 'self' 'unsafe-inline'; media-src 'self'; img-src 'self' data:; "
                         "frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data, close: bool = False) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8",
                   {"Connection": "close"} if close else None)

    def _error(self, status: int, message: str) -> None:
        self.close_connection = True  # the request body may be unread; do not reuse the connection
        self._json(status, {"error": message}, close=True)

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "").lower() in self.allowed_hosts

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin.lower() in {f"http://{h}" for h in self.allowed_hosts}

    # ---------------------------------------------------------------- GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            if not self._host_ok():
                return self._error(HTTPStatus.FORBIDDEN, "bad Host header")
            url = urlsplit(self.path)
            if url.path == "/":
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/clips":
                return self._list(parse_qs(url.query))
            m = re.fullmatch(r"/media/(\d+)/([a-z0-9.]+)", url.path)
            if m:
                return self._media(int(m.group(1)), m.group(2))
            self._error(404, "not found")
        except (ConnectionError, TimeoutError):
            pass
        except Exception:
            log.exception("review GET %s failed", self.path)
            self._error(500, "internal error (see data/logs/clipper.log)")

    def _list(self, q: dict) -> None:
        def one(name):
            return (q.get(name) or [""])[0].strip() or None
        status = one("status") or "active"
        if status == "all":
            status = None
        elif status and status != "active" and status not in db.STATUSES:
            return self._error(400, "unknown status")
        sort = one("sort") or "newest"
        if sort not in ("newest", "score", "prescore"):
            return self._error(400, "unknown sort")
        clips = db.list_clips(self.cfg.database, status, one("streamer"), _tri(one("banger")),
                              _tri(one("sensitive")), sort)
        self._json(200, {"clips": [clip_view(c, self.cfg.clips_dir) for c in clips],
                         "counts": db.status_counts(self.cfg.database),
                         "streamers": db.streamers(self.cfg.database),
                         "slots": [{"name": s["name"], "time": s["time"]} for s in self.cfg.slots],
                         "targets": list(db.ASSIGN_TARGETS)})

    def _media(self, clip_id: int, name: str) -> None:
        ctype = MEDIA.get(name)
        clip = db.get_clip(self.cfg.database, clip_id)
        if not ctype or not clip:
            return self._error(404, "not found")
        path = (self.cfg.clips_dir / clip["folder"] / name).resolve()
        if self.cfg.clips_dir.resolve() not in path.parents or not path.is_file():
            return self._error(404, "not found")
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
            if not m or not (m.group(1) or m.group(2)):
                return self._send(416, b"", ctype, {"Content-Range": f"bytes */{size}"})
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:  # suffix: the last N bytes
                start = max(size - int(m.group(2)), 0)
            if start > end or start >= size:
                return self._send(416, b"", ctype, {"Content-Range": f"bytes */{size}"})
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(CHUNK, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    # ---------------------------------------------------------------- POST
    def _route(self, clip_id: int, body: dict) -> dict:
        """Save the human's posting slot ('' = none) and target ('' = automatic | youtube | other)."""
        slot = str(body.get("slot") or "").strip() or None
        target = str(body.get("target") or "").strip() or None
        if slot and slot not in {s["name"] for s in self.cfg.slots}:
            raise db.ReviewError("unknown slot")
        clip = db.set_assignment(self.cfg.database, clip_id, slot, target)
        assign_routes(self.cfg)                      # refresh the route badge (and re-rank the automatic picks)
        log.info("[%s] review: slot=%s target=%s", clip["folder"], slot, target)
        return db.get_clip(self.cfg.database, clip_id)

    def do_POST(self):
        try:
            if not self._host_ok() or not self._origin_ok():
                return self._error(HTTPStatus.FORBIDDEN, "request not allowed")
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                return self._error(415, "send application/json")
            m = re.fullmatch(r"/api/clips/(\d+)/(status|edit|route)", urlsplit(self.path).path)
            if not m:
                return self._error(404, "not found")
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._error(413, "request too large")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._error(400, "body must be a JSON object")
            clip_id = int(m.group(1))
            try:
                if m.group(2) == "status":
                    clip = db.review_action(self.cfg.database, clip_id, str(body.get("action", "")))
                    log.info("[%s] review: %s -> %s", clip["folder"], body.get("action"), clip["status"])
                    if clip["status"] in ("rejected", "posted"):          # reject / mark uploaded: files go now
                        outcome = discard(self.cfg, clip, "rejected" if clip["status"] == "rejected" else "uploaded")
                        clip = db.get_clip(self.cfg.database, clip_id)
                        return self._json(200, {"clip": clip_view(clip, self.cfg.clips_dir),
                                                "counts": db.status_counts(self.cfg.database), **outcome})
                elif m.group(2) == "route":
                    clip = self._route(clip_id, body)
                else:
                    clip = db.edit_clip_text(self.cfg.database, clip_id, body.get("title", ""),
                                             body.get("description", ""), body.get("tags", []))
            except db.ReviewError as exc:
                return self._error(exc.http, str(exc))
            self._json(200, {"clip": clip_view(clip, self.cfg.clips_dir),
                             "counts": db.status_counts(self.cfg.database)})
        except (ConnectionError, TimeoutError):
            pass
        except Exception:
            log.exception("review POST %s failed", self.path)
            self._error(500, "internal error (see data/logs/clipper.log)")


def make_server(cfg: Config, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """Create (not start) the server, bound to localhost only. port=0 picks a free port (tests)."""
    db.init_db(cfg.database)
    server = ThreadingHTTPServer((HOST, port), Handler)
    server.daemon_threads = True
    real = server.server_address[1]
    server.cfg = cfg
    server.allowed_hosts = {f"{HOST}:{real}", f"localhost:{real}"}
    return server


def run_review(cfg: Config, port: int = DEFAULT_PORT, open_browser: bool = True) -> int:
    try:
        server = make_server(cfg, port)
    except OSError as exc:
        print(f"Cannot start the review dashboard on {HOST}:{port}: {exc}")
        print("Is it already running? Try another port with --port.")
        return 1
    filled = db.backfill_from_folders(cfg.database, cfg.clips_dir)
    if filled:
        log.info("review: filled in %d older clip(s) from their analysis.json", filled)
    url = f"http://{HOST}:{server.server_address[1]}/"
    print(f"Review dashboard: {url}   (localhost only; Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0
