"""Run:  python -m unittest discover tests -v   (from the project root, in the venv)

Article pictures for news Shorts: finding the URL, refusing unsafe hosts, validating the bytes, and a real-ffmpeg
Ken Burns render. No real network is used (downloads are mocked).
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import cv2
import feedparser
import numpy as np

from clipper import db, news, news_render, newsimage
from clipper.config import load_config

NOW = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
CREDIT = "Sources & Credits: IGN, https://ign.com/1"


def jpeg(w=1280, h=720, seed=1) -> bytes:
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 255, (max(h // 8, 1), max(w // 8, 1), 3), dtype=np.uint8)
    img = cv2.resize(img, (w, h), interpolation=cv2.INTER_CUBIC)
    ok, buf = cv2.imencode(".jpg", img)
    return buf.tobytes()


def entry(xml_item: str):
    feed = ("<?xml version='1.0'?><rss version='2.0' xmlns:media='http://search.yahoo.com/mrss/' "
            "xmlns:content='http://purl.org/rss/1.0/modules/content/'><channel><title>x</title>"
            f"<item><title>T</title><link>https://ign.com/a</link>{xml_item}</item></channel></rss>")
    return feedparser.parse(feed).entries[0]


class FindUrlTests(unittest.TestCase):
    def test_media_content_preferred_and_largest_wins(self):
        e = entry("<media:content url='https://cdn.x/small.jpg' medium='image' width='300'/>"
                  "<media:content url='https://cdn.x/big.jpg' medium='image' width='1200'/>")
        self.assertEqual(newsimage.image_from_entry(e), "https://cdn.x/big.jpg")

    def test_thumbnail_enclosure_and_inline_img(self):
        self.assertEqual(newsimage.image_from_entry(entry("<media:thumbnail url='https://cdn.x/t.jpg'/>")), "https://cdn.x/t.jpg")
        self.assertEqual(newsimage.image_from_entry(entry("<enclosure url='https://cdn.x/e.png' type='image/png' length='1'/>")),
                         "https://cdn.x/e.png")
        self.assertEqual(newsimage.image_from_entry(
            entry("<description>&lt;p&gt;&lt;img src=\"https://cdn.x/i.jpg?a=1&amp;b=2\"&gt;</description>")),
            "https://cdn.x/i.jpg?a=1&b=2")

    def test_none_and_junk_and_non_http(self):
        self.assertIsNone(newsimage.image_from_entry(entry("")))
        self.assertIsNone(newsimage.image_from_entry(entry("<media:thumbnail url='https://cdn.x/site-logo.png'/>")))
        self.assertIsNone(newsimage.image_from_entry(entry("<media:thumbnail url='data:image/png;base64,AAAA'/>")))
        self.assertIsNone(newsimage.image_from_entry(entry("<enclosure url='https://cdn.x/a.mp3' type='audio/mpeg' length='1'/>")))

    def test_parse_feed_carries_the_image(self):
        parsed = feedparser.parse(
            "<?xml version='1.0'?><rss version='2.0' xmlns:media='http://search.yahoo.com/mrss/'><channel><title>x</title>"
            "<item><title>Fresh news</title><link>https://ign.com/a</link><pubDate>Tue, 24 Nov 2026 09:00:00 GMT</pubDate>"
            "<media:content url='https://cdn.x/p.jpg' medium='image'/></item></channel></rss>")
        stories = news.parse_feed("IGN", parsed, datetime(2026, 11, 24, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(stories[0].image_url, "https://cdn.x/p.jpg")

    def test_og_image_variants_and_relative_urls(self):
        page = ('<html><head><meta content="/img/a.jpg?x=1&amp;y=2" property="og:image">'
                '<meta name="twitter:image" content="https://cdn.x/tw.jpg"><meta property="og:title" content="t"></head>')
        self.assertEqual(newsimage.og_images(page, "https://ign.com/news/a"),
                         ["https://ign.com/img/a.jpg?x=1&y=2", "https://cdn.x/tw.jpg"])
        self.assertEqual(newsimage.og_images("<html>no meta</html>", "https://ign.com/a"), [])
        self.assertEqual(newsimage.og_images('<meta property="og:image" content="javascript:alert(1)">', "https://a.com/"), [])


class SafetyTests(unittest.TestCase):
    def test_non_public_hosts_are_refused(self):
        for url in ("http://127.0.0.1/a.jpg", "http://localhost/a.jpg", "http://192.168.1.5/a.jpg",
                    "http://169.254.169.254/latest", "http://[::1]/a.jpg", "file:///c:/x.jpg", "ftp://a.com/x.jpg", "not a url"):
            with self.subTest(url=url):
                self.assertFalse(newsimage.public_host(url))
                with self.assertRaises(Exception):
                    newsimage.download(url, 1000, 1)

    def test_public_ip_is_allowed(self):
        self.assertTrue(newsimage.public_host("https://8.8.8.8/x.jpg"))

    def test_bytes_are_validated(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            dest = Path(tmp) / "n.jpg"
            self.assertEqual(newsimage.save_picture(jpeg(1280, 720), dest)["width"], 1280)
            self.assertTrue(dest.is_file())
            flat = cv2.imencode(".jpg", np.full((720, 1280, 3), 90, np.uint8))[1].tobytes()
            for name, data in {"not an image": b"<html>nope</html>", "tiny": jpeg(200, 100), "banner": jpeg(2000, 300),
                               "flat": flat}.items():
                with self.subTest(name):
                    dest.unlink(missing_ok=True)
                    self.assertIsNone(newsimage.save_picture(data, dest))
                    self.assertFalse(dest.exists())
            big = newsimage.save_picture(jpeg(4000, 3000), dest)
            self.assertEqual(max(big["width"], big["height"]), newsimage.MAX_SIDE)

    def test_feed_image_then_og_image_then_none(self):
        story = news.Story(1, "IGN", "H", "s", "https://ign.com/a", NOW, "https://cdn.x/feed.jpg")
        page = b'<meta property="og:image" content="https://cdn.x/og.jpg">'
        good = jpeg()

        def fake(url, limit, timeout, truncate=False):
            if url == "https://cdn.x/feed.jpg":
                raise OSError("404")
            if url == "https://ign.com/a":
                return page, "text/html"
            return good, "image/jpeg"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            with mock.patch.object(newsimage, "download", fake):
                got = newsimage.fetch_news_image(story, Path(tmp))
            self.assertEqual((got["source"], got["url"]), ("og:image", "https://cdn.x/og.jpg"))
            self.assertTrue((Path(tmp) / newsimage.IMAGE_NAME).is_file())
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            with mock.patch.object(newsimage, "download", side_effect=OSError("offline")):
                self.assertIsNone(newsimage.fetch_news_image(story, Path(tmp)))              # never raises


class PictureRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.audio = Path(cls.tmp.name) / "voice.mp3"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=220:duration=4", "-c:a", "libmp3lame", str(cls.audio)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=self.tmp.name))
        self.cfg = load_config()
        self.cfg.database = self.root / "c.sqlite"
        self.cfg.clips_dir = self.root / "clips"
        self.cfg.data_dir = self.root
        self.cfg.raw = {**self.cfg.raw, "clip": {**self.cfg.raw.get("clip", {}), "min_seconds": 1, "max_seconds": 40},
                        "notify": {"enabled": False},
                        "watermark": {**self.cfg.raw.get("watermark", {}), "file": str(self.root / "wm.png")}}
        self.folder = self.root / "news-ign-1"
        self.folder.mkdir()
        shutil.copyfile(self.audio, self.folder / "voiceover.mp3")
        self.info = {"hook": "GTA 6 officially hits PlayStation", "headline": "GTA 6 breaks records", "kind": "news",
                     "outlet": "IGN", "title": "GTA 6 records"}
        self.words = news.estimate_words("GTA six officially hits PlayStation and breaks every record today", 4.0)

    def probe(self, f):
        return subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=width,height,codec_name,r_frame_rate", "-of", "csv=p=0", str(f)],
                              capture_output=True, text=True).stdout.strip()

    def test_picture_background_is_ken_burns_1080x1920_and_cleans_up(self):
        pic = self.folder / "news_image.jpg"
        pic.write_bytes(jpeg(1600, 900))
        r = news_render.render_news_video(self.cfg, self.folder, {**self.info, "image": pic}, self.words, 4.0)
        self.assertEqual(r["background"], "picture")
        self.assertEqual(self.probe(self.folder / "clip.mp4"), "h264,1080,1920,30/1")
        self.assertTrue((self.folder / "thumb.jpg").is_file())
        self.assertFalse((self.folder / "scrim.png").exists())
        self.assertFalse((self.folder / "captions.ass").exists())
        frames = []                                    # the picture really moves: first and last frames differ
        for t in ("0.2", "3.8"):
            out = self.folder / f"f{t}.png"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", t, "-i", str(self.folder / "clip.mp4"),
                            "-frames:v", "1", str(out)], check=True)
            frames.append(cv2.imread(str(out)))
        self.assertGreater(float(np.abs(frames[0].astype(int) - frames[1].astype(int)).mean()), 1.0)
        # the dark scrim: the middle of the frame is darker than the raw picture
        self.assertLess(float(frames[0][700:900].mean()), float(cv2.imread(str(pic))[300:600].mean()))

    def test_broken_picture_falls_back_to_gradient(self):
        pic = self.folder / "news_image.jpg"
        pic.write_bytes(b"not really a jpeg")
        r = news_render.render_news_video(self.cfg, self.folder, {**self.info, "image": pic}, self.words, 4.0)
        self.assertEqual(r["background"], "gradient")
        self.assertEqual(self.probe(self.folder / "clip.mp4"), "h264,1080,1920,30/1")

    def test_no_picture_is_the_old_gradient(self):
        r = news_render.render_news_video(self.cfg, self.folder, {**self.info, "image": None}, self.words, 4.0)
        self.assertEqual(r["background"], "gradient")

    def test_make_short_uses_the_article_picture_and_keeps_the_credit(self):
        from tests.test_news import reply, story
        pick = news.validate_reply(json.dumps({"stories": [reply(1)]}), [story(1)], self.cfg)[0]
        page = b'<meta property="og:image" content="https://cdn.x/og.jpg">'

        def fake_dl(url, limit, timeout, truncate=False):
            return (page, "text/html") if url == pick.story.link else (jpeg(1600, 900), "image/jpeg")

        def synth(cfg, text, out, rate=None):
            shutil.copyfile(self.audio, out)
            return "en-US-AndrewNeural"
        words = mock.patch.object(news, "get_words", side_effect=lambda c, m, sc, d: (news.estimate_words(sc, d), "estimated"))
        with mock.patch.object(newsimage, "download", fake_dl), mock.patch.object(news, "synthesize", synth), words:
            folder = news.make_short(self.cfg, pick)
        a = json.loads((folder / "analysis.json").read_text(encoding="utf-8"))
        self.assertEqual(a["render"]["background"], "picture")
        self.assertEqual(a["news"]["image"], {"url": "https://cdn.x/og.jpg", "source": "og:image", "used": True})
        lines = a["description_full"].splitlines()
        self.assertEqual(lines[-1], CREDIT)                                           # exact article URL, last line
        self.assertIn("Image: IGN (article picture)", lines)
        row = db.clip_by_folder(self.cfg.database, folder.name)
        self.assertEqual(row["description"].splitlines()[-1], CREDIT)

        # offline: no picture at all -> still a complete, credited Short on the gradient
        with mock.patch.object(newsimage, "download", side_effect=OSError("offline")), \
                mock.patch.object(news, "synthesize", synth), words:
            folder2 = news.make_short(self.cfg, pick)
        a2 = json.loads((folder2 / "analysis.json").read_text(encoding="utf-8"))
        self.assertEqual((a2["render"]["background"], a2["news"]["image"]), ("gradient", None))
        self.assertEqual(a2["description_full"].splitlines()[-1], CREDIT)


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.cfg = load_config()
        self.cfg.database = Path(self.tmp.name) / "c.sqlite"
        self.cfg.raw = {**self.cfg.raw, "volume": {**self.cfg.raw.get("volume", {}), "news_per_day": 6}}
        db.init_db(self.cfg.database)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, start, n):
        for i in range(start, start + n):
            db.add_clip(self.cfg.database, "news", "IGN", f"n{i}", source_url=f"https://ign.com/{i}")
            db.set_clip_status(self.cfg.database, f"n{i}", "rendered")

    def run_quota(self, **kw):
        from tests.test_news import reply, story
        seen = {}

        def fake_pick(cfg, stories, n, work):
            seen["n"] = n
            return news.validate_reply(json.dumps({"stories": [reply(1)]}), [story(1)], cfg)
        with mock.patch.object(news, "fetch_stories", return_value=[story(1)]), mock.patch.object(news, "pick_stories", fake_pick):
            res = news.run_news(self.cfg, dry_run=True, **kw)
        return res, seen

    def test_scheduled_runs_stop_at_the_daily_quota(self):
        self.make(0, 4)
        self.assertEqual(news.news_left_today(self.cfg), 2)
        res, seen = self.run_quota(quota=True)
        self.assertEqual(seen["n"], 2)                                     # 4 per run, but only 2 left today
        self.make(10, 2)
        res, seen = self.run_quota(quota=True)
        self.assertIn("quota", res.note)
        self.assertEqual(seen, {})

    def test_a_manual_run_ignores_the_quota_and_count_overrides(self):
        self.make(0, 6)
        res, seen = self.run_quota()
        self.assertEqual(seen["n"], load_config().news["stories"])
        res, seen = self.run_quota(count=3)
        self.assertEqual(seen["n"], 3)


if __name__ == "__main__":
    unittest.main()
