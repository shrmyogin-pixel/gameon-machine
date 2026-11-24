"""Run:  python -m unittest discover tests -v   (from the project root, in the venv)

News visuals + music: finding 4-6 real pictures (article first, topic screenshots as top-up), refusing unsafe hosts, validating bytes,
cuts with varied Ken Burns motion, the ducked music mix and its automatic attribution. Real ffmpeg for the render
tests; no real network (downloads are mocked).
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

from clipper import db, export, imagesearch, music, news, news_render, newsimage
from clipper.config import load_config

NOW = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
CREDIT = "Sources & Credits: IGN, https://ign.com/1"
ATTRIBUTION = ('Music: "Test Track" Kevin MacLeod (incompetech.com) - Licensed under Creative Commons: '
               'By Attribution 4.0 License - https://creativecommons.org/licenses/by/4.0/')


def jpeg(w=1280, h=720, seed=1) -> bytes:
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 255, (max(h // 8, 1), max(w // 8, 1), 3), dtype=np.uint8)
    img = cv2.resize(img, (w, h), interpolation=cv2.INTER_CUBIC)
    ok, buf = cv2.imencode(".jpg", img)
    return buf.tobytes()


def smooth_jpeg(w=1600, h=900, seed=1) -> bytes:
    """A soft, colourful picture (blurred blobs): looks like a photo, so frame-to-frame motion is small and cuts are obvious."""
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 255, (6, 10, 3), dtype=np.uint8)
    img = cv2.resize(base, (w, h), interpolation=cv2.INTER_CUBIC)
    img = cv2.GaussianBlur(img, (0, 0), 25)
    ok, buf = cv2.imencode(".jpg", img)
    return buf.tobytes()


def entry(xml_item: str):
    feed = ("<?xml version='1.0'?><rss version='2.0' xmlns:media='http://search.yahoo.com/mrss/' "
            "xmlns:content='http://purl.org/rss/1.0/modules/content/'><channel><title>x</title>"
            f"<item><title>T</title><link>https://ign.com/a</link>{xml_item}</item></channel></rss>")
    return feedparser.parse(feed).entries[0]


PAGE = """<html><head>
<meta property="og:image" content="https://cdn.x/og-hero.jpg">
<meta name="twitter:image" content="https://cdn.x/og-hero.jpg">
</head><body>
<header><img src="https://cdn.x/header-banner.jpg" width="1200" height="400"></header>
<nav><img src="https://cdn.x/nav.jpg"></nav>
<img src="https://cdn.x/outside-article.jpg" width="900">
<article>
  <img src="https://cdn.x/site-logo.png">
  <img src="https://cdn.x/tiny.jpg" width="40" height="40">
  <img src="https://cdn.x/pixel.gif">
  <img src="https://cdn.x/drawing.svg">
  <img class="ad-slot" src="https://cdn.x/ad-unit.jpg">
  <img data-src="/media/body-one.jpg" width="1200">
  <img srcset="https://cdn.x/body-two-400.jpg 400w, https://cdn.x/body-two-1600.jpg 1600w" src="https://cdn.x/body-two-400.jpg">
  <img src="https://cdn.x/body-three-300x200.jpg">
</article>
<aside><img src="https://cdn.x/sidebar.jpg"></aside>
<footer><img src="https://cdn.x/footer.jpg"></footer>
</body></html>"""


class FindUrlTests(unittest.TestCase):
    def test_media_content_preferred_and_largest_wins(self):
        e = entry("<media:content url='https://cdn.x/small.jpg' medium='image' width='300'/>"
                  "<media:content url='https://cdn.x/big.jpg' medium='image' width='1200'/>")
        self.assertEqual(newsimage.images_from_entry(e), ["https://cdn.x/big.jpg", "https://cdn.x/small.jpg"])
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

    def test_parse_feed_carries_every_image(self):
        parsed = feedparser.parse(
            "<?xml version='1.0'?><rss version='2.0' xmlns:media='http://search.yahoo.com/mrss/'><channel><title>x</title>"
            "<item><title>Fresh news</title><link>https://ign.com/a</link><pubDate>Tue, 24 Nov 2026 09:00:00 GMT</pubDate>"
            "<media:content url='https://cdn.x/p.jpg' medium='image' width='900'/>"
            "<media:thumbnail url='https://cdn.x/q.jpg' width='300'/></item></channel></rss>")
        stories = news.parse_feed("IGN", parsed, datetime(2026, 11, 24, 0, 0, tzinfo=timezone.utc))
        self.assertEqual((stories[0].image_url, stories[0].image_urls), ("https://cdn.x/p.jpg", ["https://cdn.x/p.jpg", "https://cdn.x/q.jpg"]))

    def test_og_image_variants_and_relative_urls(self):
        page = ('<html><head><meta content="/img/a.jpg?x=1&amp;y=2" property="og:image">'
                '<meta name="twitter:image" content="https://cdn.x/tw.jpg"><meta property="og:title" content="t"></head>')
        self.assertEqual(newsimage.og_images(page, "https://ign.com/news/a"),
                         ["https://ign.com/img/a.jpg?x=1&y=2", "https://cdn.x/tw.jpg"])
        self.assertEqual(newsimage.og_images("<html>no meta</html>", "https://ign.com/a"), [])
        self.assertEqual(newsimage.og_images('<meta property="og:image" content="javascript:alert(1)">', "https://a.com/"), [])

    def test_article_images_filter_junk_and_order_article_first(self):
        got = newsimage.article_images(PAGE, "https://ign.com/news/a")
        self.assertEqual(got, ["https://cdn.x/og-hero.jpg",                                 # og first
                               "https://ign.com/media/body-one.jpg",                        # inside <article>, made absolute
                               "https://cdn.x/body-two-1600.jpg",                           # srcset: the widest
                               "https://cdn.x/body-three-300x200.jpg",
                               "https://cdn.x/outside-article.jpg"])                        # outside <article> last
        for bad in ("logo", "tiny", "pixel", "drawing", "ad-unit", "header-banner", "nav.jpg", "sidebar", "footer"):
            self.assertFalse(any(bad in u for u in got), bad)

    def test_upsized_and_identity(self):
        self.assertEqual(newsimage.upsized("https://cdn.x/a.jpg?w=300&h=200&q=80"), "https://cdn.x/a.jpg?w=1600&q=80")
        self.assertIsNone(newsimage.upsized("https://cdn.x/a.jpg"))
        self.assertIsNone(newsimage.upsized("https://cdn.x/a.jpg?w=2000"))
        self.assertEqual(newsimage._identity("https://cdn.x/p/a-300x200.jpg?z=1"), newsimage._identity("https://cdn.x/p/a.jpg"))


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
            for name, data in {"not an image": b"<html>nope</html>", "tiny": jpeg(200, 100), "soft 500px": jpeg(500, 300),
                               "banner": jpeg(2000, 300), "flat": flat}.items():
                with self.subTest(name):
                    dest.unlink(missing_ok=True)
                    self.assertIsNone(newsimage.save_picture(data, dest))
                    self.assertFalse(dest.exists())
            big = newsimage.save_picture(jpeg(4000, 3000), dest)
            self.assertEqual(max(big["width"], big["height"]), newsimage.MAX_SIDE)

    def test_duplicates_are_refused_even_at_another_size(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            a = newsimage.save_picture(jpeg(1280, 720, seed=5), Path(tmp) / "a.jpg")
            small = cv2.resize(cv2.imdecode(np.frombuffer(jpeg(1280, 720, seed=5), np.uint8), 1), (960, 540))
            smaller = cv2.imencode(".jpg", small)[1].tobytes()
            self.assertIsNone(newsimage.save_picture(smaller, Path(tmp) / "b.jpg", [a["hash"]]))
            self.assertIsNotNone(newsimage.save_picture(jpeg(1280, 720, seed=6), Path(tmp) / "c.jpg", [a["hash"]]))


class FetchImagesTests(unittest.TestCase):
    def make_fake(self, pictures, fail=()):
        def fake(url, limit, timeout, truncate=False, referer=None):
            if url == "https://ign.com/a":
                return PAGE.encode(), "text/html"
            if url in fail or url not in pictures:
                raise OSError("404")
            return jpeg(1280, 720, seed=pictures[url]), "image/jpeg"
        return fake

    def test_collects_up_to_five_distinct_pictures_best_sources_first(self):
        story = news.Story(1, "IGN", "H", "s", "https://ign.com/a", NOW, "https://cdn.x/feed1.jpg",
                           ["https://cdn.x/feed1.jpg", "https://cdn.x/feed2.jpg"])
        pics = {"https://cdn.x/og-hero.jpg": 1, "https://cdn.x/feed1.jpg": 2, "https://cdn.x/feed2.jpg": 1,       # feed2 = og again
                "https://ign.com/media/body-one.jpg": 3, "https://cdn.x/body-two-1600.jpg": 4,
                "https://cdn.x/body-three-300x200.jpg": 5, "https://cdn.x/outside-article.jpg": 6}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            with mock.patch.object(newsimage, "download", self.make_fake(pics)):
                got = newsimage.fetch_news_images(story, Path(tmp), 5, want=5)
            self.assertEqual([g["url"] for g in got], ["https://cdn.x/og-hero.jpg", "https://cdn.x/feed1.jpg",
                                                       "https://ign.com/media/body-one.jpg", "https://cdn.x/body-two-1600.jpg",
                                                       "https://cdn.x/body-three-300x200.jpg"])
            self.assertEqual([g["source"] for g in got], ["og:image", "feed", "article", "article", "article"])
            self.assertEqual(sorted(p.name for p in Path(tmp).glob("*.jpg")), [f"news_image_{i}.jpg" for i in range(1, 6)])
            self.assertTrue(all(g["width"] >= newsimage.MIN_WIDTH for g in got))

    def test_a_size_parameter_is_upsized_first_and_failures_are_skipped(self):
        story = news.Story(1, "IGN", "H", "s", "https://ign.com/a", NOW, "https://cdn.x/p.jpg?w=300", ["https://cdn.x/p.jpg?w=300"])
        calls = []

        def fake(url, limit, timeout, truncate=False, referer=None):
            calls.append(url)
            if url == "https://ign.com/a":
                return b"<html></html>", "text/html"
            return jpeg(), "image/jpeg"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp, mock.patch.object(newsimage, "download", fake):
            got = newsimage.fetch_news_images(story, Path(tmp), 5)
        self.assertEqual(calls[1], "https://cdn.x/p.jpg?w=1600")
        self.assertEqual(got[0]["url"], "https://cdn.x/p.jpg?w=1600")

    def test_offline_returns_nothing_and_never_raises(self):
        story = news.Story(1, "IGN", "H", "s", "https://ign.com/a", NOW, "https://cdn.x/feed.jpg", ["https://cdn.x/feed.jpg"])
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            with mock.patch.object(newsimage, "download", side_effect=OSError("offline")):
                self.assertEqual(newsimage.fetch_news_images(story, Path(tmp)), [])


class MusicLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self.tmp.name) / "music"
        self.cfg = load_config()
        self.cfg.raw = {**self.cfg.raw, "music": {"dir": str(self.dir)}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_shipped_library_lists_cc_by_tracks_with_attribution(self):
        shipped = load_config()
        tracks = music.read_credits(shipped)["tracks"]
        self.assertGreaterEqual(len(tracks), 3)
        for t in tracks:
            self.assertTrue((music.music_dir(shipped) / t["file"]).is_file(), t["file"])
            self.assertIn("Kevin MacLeod (incompetech.com)", t["attribution"])
            self.assertIn("Creative Commons: By Attribution 4.0", t["attribution"])
            self.assertIn("creativecommons.org/licenses/by/4.0", t["attribution"])
            self.assertIn(f'"{t["title"]}"', t["attribution"])

    def test_missing_files_are_redownloaded_then_a_synthesized_pad_is_the_fallback(self):
        self.dir.mkdir()
        (self.dir / "credits.json").write_text(json.dumps({"tracks": [
            {"file": "gone.mp3", "title": "Gone", "url": "https://cdn.x/gone.mp3", "attribution": ATTRIBUTION}]}), encoding="utf-8")
        with mock.patch.object(music, "_download", return_value=False) as dl:
            tracks = music.ensure_music(self.cfg)
        dl.assert_called_once()
        self.assertEqual([t["file"] for t in tracks], [music.SYNTH_NAME])
        self.assertTrue((self.dir / music.SYNTH_NAME).is_file())
        self.assertIsNone(music.attribution_line(tracks[0]))                          # original pad: no credit needed
        saved = json.loads((self.dir / "credits.json").read_text(encoding="utf-8"))["tracks"]
        self.assertIn(music.SYNTH_NAME, [t["file"] for t in saved])

    def test_pick_is_stable_per_seed_and_attribution_line(self):
        tracks = [{"file": f"{i}.mp3", "attribution": f"a{i}"} for i in range(4)]
        self.assertEqual(music.pick_track(tracks, "s"), music.pick_track(tracks, "s"))
        self.assertEqual(len({music.pick_track(tracks, f"seed{i}")["file"] for i in range(30)}), 4)
        self.assertIsNone(music.pick_track([], "s"))
        self.assertEqual(music.attribution_line(tracks[1]), "a1")
        self.assertEqual(music.start_offset(30, 36, "x"), 0.0)
        self.assertTrue(0 <= music.start_offset(180, 36, "x") <= 136)

    def test_gain_brings_the_track_to_the_target_level(self):
        self.dir.mkdir()
        f = self.dir / "noise.mp3"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "anoisesrc=d=20:c=pink:a=0.5", "-c:a", "libmp3lame", str(f)], check=True)
        gain = music.music_gain_db(self.cfg, f, 2.0, 12.0)
        from clipper.render import loudness_after
        got = loudness_after(self.cfg, f, 2.0, 12.0, f"volume={gain:.2f}dB")
        self.assertAlmostEqual(got, music.settings(self.cfg)["track_lufs"], delta=1.0)
        self.assertTrue(-28.0 <= got <= -24.0, got)                                   # the -24..-28 LUFS bed ([music] track_lufs = -25)


class PictureRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.audio = Path(cls.tmp.name) / "voice.mp3"
        cls.long_audio = Path(cls.tmp.name) / "voice16.mp3"
        cls.noise = Path(cls.tmp.name) / "bed.mp3"
        for out, spec in ((cls.audio, "sine=frequency=220:duration=4"), (cls.long_audio, "sine=frequency=220:duration=16"),
                          (cls.noise, "anoisesrc=d=60:c=pink:a=0.5")):
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", spec,
                            "-c:a", "libmp3lame", str(out)], check=True)

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
                        "notify": {"enabled": False}, "music": {"enabled": True, "dir": str(self.root / "music")},
                        "watermark": {**self.cfg.raw.get("watermark", {}), "file": str(self.root / "wm.png")}}
        self.folder = self.root / "news-ign-1"
        self.folder.mkdir()
        self.info = {"hook": "GTA 6 officially hits PlayStation", "headline": "GTA 6 breaks records", "kind": "news",
                     "outlet": "IGN", "title": "GTA 6 records"}
        self.track = {"file": "bed.mp3", "title": "Test Track", "path": self.noise, "attribution": ATTRIBUTION}

    def voice(self, long=False):
        shutil.copyfile(self.long_audio if long else self.audio, self.folder / "voiceover.mp3")
        d = 16.0 if long else 4.0
        return d, news.estimate_words("GTA six officially hits PlayStation and breaks every record today " * (3 if long else 1), d)

    def pics(self, n):
        out = []
        for i in range(n):
            f = self.folder / f"news_image_{i + 1}.jpg"
            f.write_bytes(smooth_jpeg(1600, 900, seed=10 + i))
            out.append(f)
        return out

    def probe(self, f, entries="stream=width,height,codec_name,r_frame_rate", sel="v:0"):
        return subprocess.run(["ffprobe", "-v", "error", "-select_streams", sel, "-show_entries", entries, "-of", "csv=p=0", str(f)],
                              capture_output=True, text=True).stdout.strip()

    def frame(self, t, name):
        out = self.folder / name
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(t), "-i", str(self.folder / "clip.mp4"),
                        "-frames:v", "1", str(out)], check=True)
        return cv2.imread(str(out)).astype(int)

    def test_segments_cut_every_three_and_a_half_to_five_seconds_and_cycle_the_visuals(self):
        for total in (30.5, 33.0, 35.8, 40.0):
            segs = news_render.plan_segments(total, 4)
            self.assertAlmostEqual(sum(s for _, s in segs), total, places=6)
            self.assertTrue(all(3.5 <= s <= 5.0 for _, s in segs), (total, segs))
            self.assertTrue(all(a[0] != b[0] for a, b in zip(segs, segs[1:])))       # never the same picture twice in a row
        self.assertEqual({i for i, _ in news_render.plan_segments(33.0, 3)}, {0, 1, 2})
        self.assertEqual(len(news_render.plan_segments(33.0, 1)), 8)                  # a single picture still gets motion cuts

    def test_motions_are_varied(self):
        self.assertEqual(len({news_render.motion(k, 150) for k in range(5)}), 5)

    def test_multi_picture_short_cuts_between_visuals_with_music(self):
        dur, words = self.voice(long=True)
        r = news_render.render_news_video(self.cfg, self.folder, {**self.info, "images": self.pics(3), "music": self.track}, words, dur)
        self.assertEqual((r["background"], r["visuals"], r["cuts"]), ("picture", 3, 4))
        self.assertEqual(self.probe(self.folder / "clip.mp4"), "h264,1080,1920,30/1")
        self.assertEqual(r["music"]["title"], "Test Track")
        self.assertEqual(r["music"]["attribution"], ATTRIBUTION)
        self.assertAlmostEqual(r["final_lufs"], -14.0, delta=1.5)                    # the whole Short lands on -14 LUFS
        self.assertEqual(self.probe(self.folder / "clip.mp4", "stream=codec_name", "a:0"), "aac")
        for left in ("scrim.png", "captions.ass") + tuple(f"visual_{i}.jpg" for i in range(3)):
            self.assertFalse((self.folder / left).exists(), left)
        # cut points are at ~4.1 s, ~8.3 s and ~12.4 s: frames either side of a cut differ a lot, frames within a cut differ little
        across = np.abs(self.frame(3.9, "a.png") - self.frame(4.4, "b.png")).mean()
        within = np.abs(self.frame(1.5, "c.png") - self.frame(2.0, "d.png")).mean()
        self.assertGreater(across, 3 * within)
        self.assertGreater(float(np.abs(self.frame(0.2, "e.png") - self.frame(3.6, "f.png")).mean()), 1.0)
        # scrim: the caption zone (dark scrim behind the captions) is darker than the same picture region unscrimmed
        g = self.frame(1.5, "g.png")
        self.assertLess(float(g[1500:1750].mean()), float(g[700:1000].mean()))

    def test_music_is_far_below_the_voice(self):
        # voice-less render: only the music bed (gain-matched to -29 LUFS) plus a silent voice must sit at -28..-32 LUFS
        gain = music.music_gain_db(self.cfg, self.noise, 3.0, 5.0)
        from clipper.render import loudness_after
        bed = loudness_after(self.cfg, self.noise, 3.0, 5.0, f"volume={gain:.2f}dB")
        self.assertTrue(-28.0 <= bed <= -24.0, bed)
        self.assertGreater(-14.0 - bed, 10.0)                                         # at least 10 dB under the voiceover

    def test_a_broken_track_is_dropped_but_the_pictures_stay(self):
        dur, words = self.voice()
        bad = {**self.track, "path": self.folder / "missing.mp3"}
        r = news_render.render_news_video(self.cfg, self.folder, {**self.info, "images": self.pics(3), "music": bad}, words, dur)
        self.assertEqual((r["background"], r["music"]), ("picture", None))
        self.assertTrue((self.folder / "clip.mp4").is_file())

    def test_one_broken_picture_is_dropped_and_the_rest_still_render(self):
        dur, words = self.voice()
        pics = self.pics(3)
        pics[1].write_bytes(b"not really a jpeg")
        r = news_render.render_news_video(self.cfg, self.folder, {**self.info, "images": pics, "music": self.track}, words, dur)
        self.assertEqual((r["background"], r["visuals"], r["music"]["title"]), ("picture", 2, "Test Track"))
        self.assertEqual(self.probe(self.folder / "clip.mp4"), "h264,1080,1920,30/1")

    def test_with_no_usable_picture_there_is_no_placeholder_render(self):
        dur, words = self.voice()
        pic = self.folder / "news_image_1.jpg"
        pic.write_bytes(b"not really a jpeg")
        for images in ([pic], [], [self.folder / "missing.jpg"]):
            with self.assertRaises(news_render.RenderError):
                news_render.render_news_video(self.cfg, self.folder, {**self.info, "images": images, "music": self.track}, words, dur)
        self.assertFalse((self.folder / "clip.mp4").exists())

    def make_short(self, pictures, track_cfg=True, topic_pictures=0):
        from tests.test_news import reply, story
        pick = news.validate_reply(json.dumps({"stories": [reply(1)]}), [story(1)], self.cfg)[0]
        pick.story.image_urls = [f"https://cdn.x/p{i}.jpg" for i in range(pictures)]
        pick.story.image_url = pick.story.image_urls[0] if pictures else None

        def fake_dl(url, limit, timeout, truncate=False, referer=None):
            if url == pick.story.link:
                return b"<html></html>", "text/html"
            if url in pick.story.image_urls:
                return jpeg(1600, 900, seed=int(url.split("/p")[1].split(".")[0]) + 20), "image/jpeg"
            raise OSError("404")

        def synth(cfg, text, out, rate=None):
            shutil.copyfile(self.audio, out)
            return "en-US-AndrewNeural"
        (self.root / "music").mkdir(exist_ok=True)
        shutil.copyfile(self.noise, self.root / "music" / "bed.mp3")
        (self.root / "music" / "credits.json").write_text(json.dumps({"tracks": [
            {"file": "bed.mp3", "title": "Test Track", "artist": "Kevin MacLeod", "license": "CC BY 4.0", "attribution": ATTRIBUTION}]}),
            encoding="utf-8")
        words = mock.patch.object(news, "get_words", side_effect=lambda c, m, sc, d: (news.estimate_words(sc, d), "estimated"))
        self.topic_calls = []

        def fake_topic(topic, folder, cache_root, need, known_hashes, start_n, timeout=15.0):
            self.topic_calls.append((topic, need, start_n))
            out = []
            for i in range(min(topic_pictures, need)):
                f = Path(folder) / f"news_image_{start_n + i}.jpg"
                f.write_bytes(smooth_jpeg(1600, 900, seed=90 + i))
                out.append({"file": f, "url": f"https://commons/x{i}.jpg", "source": "commons", "width": 1600, "height": 900})
            return out
        with mock.patch.object(newsimage, "download", fake_dl), mock.patch.object(news, "synthesize", synth), words,                 mock.patch.object(imagesearch, "fetch_topic_images", fake_topic):
            return news.make_short(self.cfg, pick)

    def test_make_short_three_plus_pictures_music_and_attribution(self):
        folder = self.make_short(pictures=3)
        a = json.loads((folder / "analysis.json").read_text(encoding="utf-8"))
        self.assertEqual((a["render"]["background"], a["render"]["visuals"]), ("picture", 3))
        self.assertEqual([i["kind"] for i in a["news"]["images"]], ["article"] * 3)
        self.assertEqual(a["music"]["title"], "Test Track")
        lines = a["description_full"].splitlines()
        self.assertEqual(lines[-1], CREDIT)                                           # the exact article URL stays last
        self.assertEqual(lines[-2], ATTRIBUTION)                                      # the music license credit sits above it
        self.assertIn("Images: IGN (article pictures)", lines)
        row = db.clip_by_folder(self.cfg.database, folder.name)
        self.assertEqual(row["description"].splitlines()[-2:], [ATTRIBUTION, CREDIT])

    def test_make_short_tops_up_with_topic_screenshots_when_the_article_has_few_pictures(self):
        folder = self.make_short(pictures=1, topic_pictures=5)
        a = json.loads((folder / "analysis.json").read_text(encoding="utf-8"))
        kinds = [i["kind"] for i in a["news"]["images"]]
        self.assertEqual(kinds, ["article", "topic", "topic", "topic", "topic", "topic"])      # article first, then the game
        self.assertEqual(a["render"]["visuals"], 6)
        self.assertEqual(self.topic_calls[0][0], "GTA 6")                                  # searched by the pick's main game name
        self.assertIn("Images: IGN (article pictures)", a["description_full"])
        self.assertIn("Game screenshots:", a["description_full"])
        self.assertTrue(a["description_full"].splitlines()[-1].startswith("Sources & Credits:"))

    def test_four_article_pictures_need_no_search(self):
        folder = self.make_short(pictures=4)
        a = json.loads((folder / "analysis.json").read_text(encoding="utf-8"))
        self.assertEqual(self.topic_calls, [])
        self.assertEqual(a["render"]["visuals"], 4)
        self.assertNotIn("Game screenshots:", a["description_full"])

    def test_too_few_real_pictures_skips_the_story_instead_of_showing_placeholders(self):
        with self.assertRaises(news.NewsError) as ctx:
            self.make_short(pictures=1, topic_pictures=1)                                 # 2 real pictures < images_floor (3)
        self.assertIn("placeholders", str(ctx.exception))
        row = db.clip_by_folder(self.cfg.database, next(self.cfg.clips_dir.iterdir()).name)
        self.assertEqual(row["status"], "failed")
        self.assertFalse(list(self.cfg.clips_dir.glob("*/clip.mp4")))

    def test_no_generated_card_code_is_left(self):
        import clipper
        self.assertFalse((Path(clipper.__file__).parent / "memes.py").exists())
        self.assertFalse(hasattr(news_render, "background_source"))

    def test_music_off_means_no_attribution_line(self):
        self.cfg.raw = {**self.cfg.raw, "music": {"enabled": False, "dir": str(self.root / "music")}}
        folder = self.make_short(pictures=3)
        a = json.loads((folder / "analysis.json").read_text(encoding="utf-8"))
        self.assertIsNone(a["music"])
        self.assertNotIn("Music:", a["description_full"])

    def test_export_sheet_carries_the_music_attribution_even_if_the_description_was_edited(self):
        self.cfg.raw = {**self.cfg.raw, "paths": {**self.cfg.raw["paths"], "exports_dir": str(self.root / "exports")}}
        folder = self.make_short(pictures=3)
        row = db.clip_by_folder(self.cfg.database, folder.name)
        # a human edit in the dashboard that deleted the music line
        edited = "\n".join(ln for ln in row["description"].splitlines() if not ln.startswith("Music:"))
        with db.connect(self.cfg.database) as conn:
            conn.execute("UPDATE clips SET description = ? WHERE id = ?", (edited, row["id"]))
        db.review_action(self.cfg.database, row["id"], "approve")
        res = export.run_export(self.cfg)
        self.assertTrue(res.ok, res.refused)
        sheet = next((self.root / "exports").rglob("*.txt")).read_text(encoding="utf-8-sig")
        desc = [ln for ln in export.sheet_description(sheet).splitlines() if ln.strip()]
        self.assertIn(ATTRIBUTION, desc)
        self.assertEqual(desc[-1], CREDIT)                                            # Sources & Credits is still the last line


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
            res = news.run_news(self.cfg, trends=False, dry_run=True, **kw)
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


class VoiceConfigTests(unittest.TestCase):
    def test_neutral_voice_natural_pace_and_neutral_fallbacks(self):
        cfg = load_config()
        self.assertIn(cfg.news["voice"], ("en-US-AndrewNeural", "en-US-BrianNeural"))
        self.assertEqual((cfg.news["rate"], cfg.news["pitch"]), ("+0%", "+0Hz"))
        self.assertEqual(news.FALLBACK_VOICES[0], "en-US-BrianNeural")
        self.assertTrue(all(v.startswith("en-US-") for v in news.FALLBACK_VOICES))

    def test_pitch_is_passed_to_edge_tts(self):
        cfg = load_config()
        seen = []

        async def fake(text, voice, rate, out, pitch="+0Hz"):
            seen.append((voice, rate, pitch))
            out.write_bytes(b"x" * 5000)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp, mock.patch.object(news, "_tts_once", side_effect=fake):
            news.synthesize(cfg, "hello", Path(tmp) / "v.mp3")
        self.assertEqual(seen, [(cfg.news["voice"], "+0%", "+0Hz")])


if __name__ == "__main__":
    unittest.main()
