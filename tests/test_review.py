"""Run:  python -m unittest discover tests -v   (from the project root, in the venv)

End-to-end: a real review server on a free localhost port, a temp database and temp clip folders,
driven over real HTTP.
"""
import http.client
import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from clipper import db, review
from clipper.config import load_config

MP4 = bytes(range(256)) * 40          # 10,240 fake bytes: enough to test Range requests
CREDIT = "Sources & Credits: Twitch - lacy (https://twitch.tv/lacy)"


def analysis(score=7.0, banger=False, sensitive=False, title="Great clutch"):
    return {"score": score, "banger": banger, "sensitive": sensitive,
            "sensitive_reason": "mild insult" if sensitive else "", "title": title, "hook": "NO WAY",
            "meme_caption": ["one", "two"], "description_full": f"Lacy clutches.\nWatch lacy live: u\n{CREDIT}",
            "credit_line": CREDIT, "tags": ["fortnite", "clutch"], "start": 5.0, "end": 35.0}


class ReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = Path(cls.tmp.name)
        cls.cfg = load_config()
        cls.cfg.database = cls.root / "clips.sqlite"
        cls.cfg.clips_dir = cls.root / "clips"
        cls.server = review.make_server(cls.cfg, port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    def setUp(self):
        """Fresh database and clip folders for every test, so tests cannot affect each other."""
        cls = self
        shutil.rmtree(cls.cfg.clips_dir, ignore_errors=True)
        cls.cfg.database.unlink(missing_ok=True)
        cls.ids = {}
        # name: (handle, status, ai score, pre score, banger, sensitive, has video)
        rows = {"a": ("lacy", "rendered", 8.5, 6.0, True, False, True),
                "b": ("lacy", "rendered", 5.0, 9.0, False, True, True),
                "c": ("CarryMinati", "rendered", 7.0, 3.0, False, False, True),
                "d": ("lacy", "skipped", 2.0, 4.0, False, False, False),
                "e": ("lacy", "failed", None, 2.0, False, False, False)}
        for i, (key, (handle, status, ai, pre, banger, sens, video)) in enumerate(rows.items()):
            folder = f"twitch-{handle.lower()}-2026010{i + 1}-120000"
            (cls.cfg.clips_dir / folder).mkdir(parents=True)
            db.add_clip(cls.cfg.database, "twitch", handle, folder, stream="https://twitch.tv/" + handle,
                        lang="hi" if handle == "CarryMinati" else "en", moment_kind="chat", pre_score=pre)
            if ai is not None:
                a = analysis(ai, banger, sens, title=f"Clip {key}")
                (cls.cfg.clips_dir / folder / "analysis.json").write_text(json.dumps(a), encoding="utf-8")
                db.record_analysis(cls.cfg.database, folder, a, cls.cfg.clips_dir / folder / "analysis.json")
            if video:
                (cls.cfg.clips_dir / folder / "clip.mp4").write_bytes(MP4)
                (cls.cfg.clips_dir / folder / "thumb.jpg").write_bytes(b"\xff\xd8jpeg")
                db.record_files(cls.cfg.database, folder, cls.cfg.clips_dir / folder)
            db.set_clip_status(cls.cfg.database, folder, status, "boom" if status == "failed" else None,
                               "score too low" if status == "skipped" else None)
        for c in db.list_clips(cls.cfg.database, limit=100):
            cls.ids[c["title"][-1] if c["title"] else "e"] = c["id"]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    # ---------------------------------------------------------------- helpers
    def call(self, method, path, body=None, headers=None, raw=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body)
            h.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=h)
        res = conn.getresponse()
        payload = res.read()
        conn.close()
        return res.status, (payload if raw else (json.loads(payload) if payload else None)), res

    def clips(self, qs=""):
        status, data, _ = self.call("GET", "/api/clips" + qs)
        self.assertEqual(status, 200)
        return data

    def titles(self, qs):
        return [c["title"] for c in self.clips(qs)["clips"]]

    # ---------------------------------------------------------------- tests
    def test_binds_to_localhost_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_page_served(self):
        status, body, res = self.call("GET", "/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b"Gameon Review", body)
        self.assertIn("text/html", res.getheader("Content-Type"))

    def test_filters_and_sorting(self):
        self.assertEqual(sorted(self.titles("?status=rendered")), ["Clip a", "Clip b", "Clip c"])
        self.assertEqual(self.titles("?status=rendered&sort=score"), ["Clip a", "Clip c", "Clip b"])
        self.assertEqual(self.titles("?status=rendered&sort=prescore"), ["Clip b", "Clip a", "Clip c"])
        self.assertEqual(self.titles("?status=rendered&sort=newest"), ["Clip c", "Clip b", "Clip a"])
        self.assertEqual(self.titles("?status=rendered&streamer=carryminati"), ["Clip c"])
        self.assertEqual(self.titles("?status=rendered&banger=yes"), ["Clip a"])
        self.assertEqual(sorted(self.titles("?status=rendered&banger=no")), ["Clip b", "Clip c"])
        self.assertEqual(self.titles("?status=rendered&sensitive=yes"), ["Clip b"])
        self.assertEqual(self.titles("?status=skipped"), ["Clip d"])
        self.assertEqual(len(self.clips("?status=all")["clips"]), 5)
        data = self.clips()
        self.assertEqual(data["streamers"], ["CarryMinati", "lacy"])
        self.assertEqual(data["counts"]["rendered"], 3)
        status, _, _ = self.call("GET", "/api/clips?status=bogus")
        self.assertEqual(status, 400)
        status, _, _ = self.call("GET", "/api/clips?sort=bogus")
        self.assertEqual(status, 400)

    def test_card_fields(self):
        clip = next(c for c in self.clips("?status=rendered&sensitive=yes")["clips"])
        for key in ("stream", "platform", "handle", "pre_score", "ai_score", "banger", "sensitive",
                    "sensitive_reason", "status", "title", "hook", "meme_caption", "description", "tags",
                    "video_url", "thumb_url", "credit", "description_body", "moment_at", "created_at"):
            self.assertIn(key, clip)
        self.assertEqual(clip["sensitive_reason"], "mild insult")
        self.assertEqual(clip["credit"], CREDIT)
        self.assertNotIn(CREDIT, clip["description_body"])
        self.assertNotIn(str(self.cfg.clips_dir), json.dumps(clip))     # no local paths leak to the browser
        skipped = self.clips("?status=skipped")["clips"][0]
        self.assertIsNone(skipped["video_url"])
        self.assertEqual(skipped["skip_reason"], "score too low")

    def test_video_range_requests(self):
        clip = self.clips("?status=rendered&sort=score")["clips"][0]
        url = clip["video_url"]
        status, body, res = self.call("GET", url, raw=True)
        self.assertEqual((status, len(body), res.getheader("Accept-Ranges")), (200, len(MP4), "bytes"))
        self.assertEqual(res.getheader("Content-Type"), "video/mp4")
        status, body, res = self.call("GET", url, headers={"Range": "bytes=100-199"}, raw=True)
        self.assertEqual((status, body), (206, MP4[100:200]))
        self.assertEqual(res.getheader("Content-Range"), f"bytes 100-199/{len(MP4)}")
        status, body, _ = self.call("GET", url, headers={"Range": "bytes=10000-"}, raw=True)
        self.assertEqual((status, body), (206, MP4[10000:]))
        status, body, _ = self.call("GET", url, headers={"Range": "bytes=-50"}, raw=True)
        self.assertEqual((status, body), (206, MP4[-50:]))
        status, _, _ = self.call("GET", url, headers={"Range": "bytes=99999-"}, raw=True)
        self.assertEqual(status, 416)
        status, body, res = self.call("GET", clip["thumb_url"], raw=True)
        self.assertEqual((status, res.getheader("Content-Type")), (200, "image/jpeg"))

    def test_media_cannot_escape_or_guess(self):
        for path in ("/media/1/../../../config.toml", "/media/1/analysis.json", "/media/1/payload.json",
                     "/media/999/clip.mp4", "/media/1/%2e%2e%2fconfig.toml", "/nope"):
            with self.subTest(path=path):
                status, _, _ = self.call("GET", path, raw=True)
                self.assertEqual(status, 404)

    def test_host_origin_and_content_type_are_enforced(self):
        cid = self.ids["c"]
        status, _, _ = self.call("GET", "/api/clips", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)                                        # DNS-rebinding guard
        status, _, _ = self.call("POST", f"/api/clips/{cid}/status", {"action": "approve"},
                                 headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)                                        # cross-site request
        status, _, _ = self.call("POST", f"/api/clips/{cid}/status", {"action": "approve"},
                                 headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertEqual(db.get_clip(self.cfg.database, cid)["status"], "rendered")  # nothing changed

    def test_approve_reject_posted_undo_flow(self):
        cid = self.ids["a"]
        for action, expect in [("approve", "approved"), ("reject", "rejected"), ("approve", "approved"),
                               ("posted", "posted"), ("undo", "rendered")]:
            status, data, _ = self.call("POST", f"/api/clips/{cid}/status", {"action": action})
            self.assertEqual(status, 200, data)
            self.assertEqual(data["clip"]["status"], expect)
            self.assertEqual(db.get_clip(self.cfg.database, cid)["status"], expect)   # persisted
        self.assertIn("counts", data)
        # posted records a time; undo clears it
        self.assertIsNone(db.get_clip(self.cfg.database, cid)["posted_at"])
        self.call("POST", f"/api/clips/{cid}/status", {"action": "posted"})
        self.assertIsNotNone(db.get_clip(self.cfg.database, cid)["posted_at"])
        self.call("POST", f"/api/clips/{cid}/status", {"action": "undo"})

    def test_illegal_actions_rejected(self):
        skipped = self.clips("?status=skipped")["clips"][0]["id"]
        failed = self.clips("?status=failed")["clips"][0]["id"]
        for cid in (skipped, failed):
            for action in ("approve", "reject", "posted", "undo"):
                status, data, _ = self.call("POST", f"/api/clips/{cid}/status", {"action": action})
                self.assertEqual(status, 409, (cid, action, data))
        status, _, _ = self.call("POST", f"/api/clips/{self.ids['c']}/status", {"action": "explode"})
        self.assertEqual(status, 400)
        status, _, _ = self.call("POST", "/api/clips/9999/status", {"action": "approve"})
        self.assertEqual(status, 404)
        status, _, _ = self.call("POST", f"/api/clips/{self.ids['c']}/status", None,
                                 headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)                                         # missing action
        self.assertEqual(db.get_clip(self.cfg.database, skipped)["status"], "skipped")

    def test_assign_slot_and_target(self):
        cid = self.ids["a"]
        slots = self.clips()["slots"]
        self.assertEqual([s["time"] for s in slots], ["10:00", "00:00", "02:00"])
        status, data, _ = self.call("POST", f"/api/clips/{cid}/route", {"slot": slots[1]["name"], "target": "youtube"})
        self.assertEqual(status, 200)
        self.assertEqual((data["clip"]["assign_slot"], data["clip"]["assign_target"]),
                         (slots[1]["name"], "youtube"))
        self.assertEqual((data["clip"]["route_platform"], data["clip"]["route_slot"]), ("youtube", slots[1]["name"]))
        status, data, _ = self.call("POST", f"/api/clips/{cid}/route", {"slot": slots[2]["name"], "target": "other"})
        self.assertEqual((data["clip"]["route_slot"], data["clip"]["route_platform"] != "youtube"),
                         (slots[2]["name"], True))
        status, data, _ = self.call("POST", f"/api/clips/{cid}/route", {"slot": "", "target": ""})     # back to auto
        self.assertEqual((data["clip"]["assign_slot"], data["clip"]["assign_target"]), (None, None))
        for bad in ({"slot": "Slot 9"}, {"target": "tiktok"}):
            self.assertEqual(self.call("POST", f"/api/clips/{cid}/route", bad)[0], 400)
        skipped = next(c["id"] for c in db.list_clips(self.cfg.database) if c["status"] == "skipped")
        self.assertEqual(self.call("POST", f"/api/clips/{skipped}/route", {"target": "other"})[0], 409)
        self.assertEqual(self.call("POST", "/api/clips/99999/route", {"target": "other"})[0], 404)

    def test_edit_masks_profanity_and_keeps_credit_line(self):
        cid = self.ids["c"]
        status, data, _ = self.call("POST", f"/api/clips/{cid}/edit", {
            "title": "What the fuck just happened", "tags": ["#Gaming", "gaming", " shit ", ""],
            "description": f"New line about shit.\nSources & Credits: totally fake\nlast line"})
        self.assertEqual(status, 200, data)
        clip = data["clip"]
        self.assertEqual(clip["title"], "What the f*** just happened")
        self.assertEqual(clip["tags"], ["Gaming", "s***"])
        lines = db.get_clip(self.cfg.database, cid)["description"].splitlines()
        self.assertEqual(lines[-1], CREDIT)                                   # real credit line, user's fake dropped
        self.assertEqual(sum(l.startswith("Sources & Credits:") for l in lines), 1)
        self.assertIn("New line about s***.", lines)
        saved = json.loads((self.cfg.clips_dir / clip["folder"] / "analysis.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["title"], clip["title"])                       # analysis.json kept in step
        self.assertTrue(saved["description_full"].endswith(CREDIT))
        # credit survives even if the whole description is emptied
        status, data, _ = self.call("POST", f"/api/clips/{cid}/edit", {"title": "t", "description": "", "tags": []})
        self.assertEqual((status, data["clip"]["description"]), (200, CREDIT))

    def test_edit_validation(self):
        cid = self.ids["c"]
        for body in ({"title": "  ", "description": "x", "tags": []}, {"title": "x" * 101, "description": "x", "tags": []},
                     {"title": "ok", "description": "x", "tags": "not a list"},
                     {"title": "ok", "description": "x" * 6000, "tags": []}):
            with self.subTest(body=str(body)[:40]):
                status, _, _ = self.call("POST", f"/api/clips/{cid}/edit", body)
                self.assertEqual(status, 400)

    def test_unicode_hindi_edit_roundtrip(self):
        cid = self.ids["c"]
        status, data, _ = self.call("POST", f"/api/clips/{cid}/edit",
                                    {"title": "गजब का मोमेंट", "description": "क्या खेल है", "tags": ["हिंदी"]})
        self.assertEqual(status, 200)
        self.assertEqual(data["clip"]["title"], "गजब का मोमेंट")
        self.assertEqual(data["clip"]["tags"], ["हिंदी"])


class MigrationTests(unittest.TestCase):
    def test_old_database_is_upgraded_and_backfilled(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp = Path(tmp)
            path = tmp / "old.sqlite"
            conn = sqlite3.connect(path)
            conn.executescript("""CREATE TABLE clips (id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL,
                handle TEXT NOT NULL, folder TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'new',
                skip_reason TEXT, error TEXT, slot TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')));
                INSERT INTO clips (platform, handle, folder, status) VALUES ('twitch', 'lacy', 'old-1', 'rendered');""")
            conn.commit()
            conn.close()
            folder = tmp / "clips" / "old-1"
            folder.mkdir(parents=True)
            (folder / "analysis.json").write_text(json.dumps(analysis(9.0, True)), encoding="utf-8")
            (folder / "payload.json").write_text(json.dumps({"stream": {"url": "https://twitch.tv/lacy", "lang": "en"},
                                                              "moment": {"kind": "chat", "prescore": 6.5}, "clip": {}}),
                                                 encoding="utf-8")
            (folder / "clip.mp4").write_bytes(MP4)
            db.init_db(path)
            self.assertEqual(db.backfill_from_folders(path, tmp / "clips"), 1)
            clip = db.list_clips(path)[0]
            self.assertEqual((clip["ai_score"], clip["banger"], clip["pre_score"], clip["status"]), (9.0, True, 6.5, "rendered"))
            self.assertTrue(clip["clip_path"].endswith("clip.mp4"))
            self.assertEqual(db.backfill_from_folders(path, tmp / "clips"), 0)   # idempotent


if __name__ == "__main__":
    unittest.main()
