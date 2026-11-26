"""Run:  python -m unittest discover tests -v   (from the project root, in the venv)

News engine tests. Network services (feeds, Claude, edge-tts, Groq) are mocked; ffmpeg is real for the one
end-to-end render test.
"""
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import cv2
import feedparser
import numpy as np

from clipper import db, hookscore, imagesearch, news, news_render, newsimage
from clipper.config import load_config
from clipper.hookscore import best_hook, rank_hooks, score_hook

NOW = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)


def rss(items):
    body = "".join(f"<item><title>{t}</title><link>{l}</link><description>{d}</description>"
                   f"<pubDate>{p:%a, %d %b %Y %H:%M:%S} GMT</pubDate></item>" for t, l, d, p in items)
    return feedparser.parse(f"<?xml version='1.0'?><rss version='2.0'><channel><title>x</title>{body}</channel></rss>")


def fake_article_pictures(story, folder, timeout=15.0, want=5):
    """Stands in for the article scraper: four soft, photo-like pictures saved the way newsimage saves them."""
    out = []
    for i in range(4):
        rng = np.random.default_rng(40 + i)
        img = cv2.GaussianBlur(cv2.resize(rng.integers(0, 255, (6, 10, 3), dtype=np.uint8), (1600, 900), interpolation=cv2.INTER_CUBIC), (0, 0), 25)
        f = Path(folder) / f"news_image_{i + 1}.jpg"
        cv2.imwrite(str(f), img)
        out.append({"file": f, "url": f"https://cdn.x/p{i}.jpg", "source": "feed", "width": 1600, "height": 900})
    return out


# ---------------------------------------------------------------- hook scoring
class HookScoreTests(unittest.TestCase):
    def test_perfect_hook(self):
        s = score_hook("Free on Epic today: Alan Wake 2 costs nothing", ["Alan Wake 2"])
        self.assertTrue(s.valid)
        self.assertEqual(s.parts, {"name_early": 4.0, "urgency_early": 3.0, "specific": 1.5, "length": 1.5, "curiosity": 0.0})
        self.assertEqual(s.total, 7.0)                                   # a plain headline hook tops out at 7

    def test_name_and_urgency_must_be_in_first_six_words(self):
        early = score_hook("GTA 6 leak shows a new Vice City map", ["GTA 6"])
        late = score_hook("This incredible reveal changes everything for PlayStation fans leak", ["GTA 6"])
        self.assertEqual(early.parts["name_early"], 4.0)
        self.assertEqual(late.parts["name_early"], 0.0)
        self.assertEqual(late.parts["urgency_early"], 0.0)               # 'leak' is the 9th word
        self.assertGreater(early.total, late.total)

    def test_story_subject_counts_even_if_not_a_known_platform(self):
        self.assertEqual(score_hook("Hades 2 finally launches", ["Hades 2"]).parts["name_early"], 4.0)
        self.assertEqual(score_hook("Hades 2 finally launches", []).parts["name_early"], 0.0)

    def test_urgency_levels(self):
        one = score_hook("Steam adds something cool to the store page", [])
        two = score_hook("Steam now free today for everyone here", [])
        self.assertEqual(one.parts["urgency_early"], 0.0)
        self.assertEqual(two.parts["urgency_early"], 3.0)
        self.assertEqual(score_hook("Steam finally adds a cool page here", []).parts["urgency_early"], 1.5)

    def test_more_than_twelve_words_is_invalid(self):
        s = score_hook("PlayStation confirms that the new game is finally coming to PC next year for everyone", [])
        self.assertFalse(s.valid)
        self.assertEqual(s.total, 0.0)
        self.assertIn("max 12", s.reasons[0])
        self.assertTrue(score_hook("one two three four five six seven eight nine ten eleven twelve", []).valid)

    def test_banned_filler_is_invalid(self):
        for bad in ("Hey guys GTA 6 leak is here", "You won't believe this PlayStation news", "Basically Xbox is free now",
                    "Epic literally gives away a free game"):
            with self.subTest(bad=bad):
                self.assertFalse(score_hook(bad, []).valid)
        self.assertEqual(hookscore.banned_in("Let's get into it, in today's video"), ["let's get into it", "in today's video"])

    def test_empty_hook_and_punctuation(self):
        self.assertFalse(score_hook("   ", []).valid)
        self.assertEqual(hookscore.words_of("Free — on Epic, today!"), ["Free", "on", "Epic", "today"])

    def test_best_hook_picks_highest_valid(self):
        hooks = ["Something happened in gaming today again", "Free on Epic today: Alan Wake 2 costs nothing",
                 "Hey guys Epic is free now", "x " * 14]
        ranked = rank_hooks(hooks, ["Alan Wake 2"])
        self.assertEqual(ranked[0].hook, hooks[1])
        self.assertEqual([h.valid for h in ranked], [True, True, False, False])
        self.assertEqual(best_hook(hooks, ["Alan Wake 2"]).hook, hooks[1])
        self.assertIsNone(best_hook(["Hey guys", "literally nothing here"], []))


# ---------------------------------------------------------------- feeds
class FeedTests(unittest.TestCase):
    def test_parse_feed_keeps_fresh_stories_with_exact_links(self):
        parsed = rss([("Fresh <b>news</b>", "https://ign.com/a?x=1&y=2", "<p>Summary &amp; more</p>", NOW - timedelta(hours=2)),
                      ("Old", "https://ign.com/b", "", NOW - timedelta(hours=30)),
                      ("No link", "", "", NOW - timedelta(hours=1)),
                      ("Bad link", "javascript:alert(1)", "", NOW - timedelta(hours=1))])
        stories = news.parse_feed("IGN", parsed, NOW - timedelta(hours=12))
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0].link, "https://ign.com/a?x=1&y=2")           # the exact URL, untouched
        self.assertEqual((stories[0].title, stories[0].summary), ("Fresh news", "Summary & more"))

    def test_fetch_dedupes_filters_gambling_and_skips_used_urls(self):
        cfg = load_config()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg.database = Path(tmp) / "c.sqlite"
            db.add_clip(cfg.database, "news", "IGN", "news-1", source_url="https://ign.com/used")
            db.set_clip_status(cfg.database, "news-1", "rendered")
            db.add_clip(cfg.database, "news", "IGN", "news-2", source_url="https://ign.com/failed-before")
            db.set_clip_status(cfg.database, "news-2", "failed", "boom")
            cfg.raw = {**cfg.raw, "news": {**cfg.raw.get("news", {}), "feeds": [{"name": "IGN", "url": "u1"}, {"name": "PCG", "url": "u2"}, {"name": "Dead", "url": "u3"}]}}
            feeds = {"u1": rss([("Used story", "https://ign.com/used", "", NOW - timedelta(hours=1)),
                                ("Retry me", "https://ign.com/failed-before", "", NOW - timedelta(hours=2)),
                                ("Best casino bonuses", "https://ign.com/casino", "", NOW - timedelta(hours=1)),
                                ("Same Title!", "https://ign.com/x", "", NOW - timedelta(hours=3))]),
                     "u2": rss([("Same title", "https://pcg.com/x", "", NOW - timedelta(hours=1)),
                                ("Newest", "https://pcg.com/n", "", NOW - timedelta(minutes=5))])}

            def fake_parse(url, agent=None):
                if url == "u3":
                    raise RuntimeError("feed down")
                return feeds[url]
            with mock.patch.object(feedparser, "parse", side_effect=fake_parse):
                stories = news.fetch_stories(cfg, 12, NOW)
            titles = [s.title for s in stories]
            self.assertEqual(titles[0], "Newest")                                   # newest first
            self.assertIn("Retry me", titles)                                       # a failed Short may be retried
            self.assertNotIn("Used story", titles)
            self.assertNotIn("Best casino bonuses", titles)
            self.assertEqual(sum(t.lower().startswith("same title") for t in titles), 1)   # duplicate headline dropped
            self.assertEqual([s.id for s in stories], list(range(1, len(stories) + 1)))


    def test_stories_rejected_or_uploaded_before_are_never_fetched_again(self):
        from clipper import lifecycle
        cfg = load_config()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg.database = Path(tmp) / "c.sqlite"
            db.init_db(cfg.database)
            # the story is remembered by its normalized URL (tracking parameters and www. do not matter) ...
            db.add_seen(cfg.database, lifecycle.story_keys("https://www.ign.com/a/old-story/?utm_source=x", "Old story headline here"),
                        "https://www.ign.com/a/old-story/", "Old story headline here", "rejected")
            cfg.raw = {**cfg.raw, "news": {**cfg.raw.get("news", {}), "feeds": [{"name": "IGN", "url": "u1"}, {"name": "PCG", "url": "u2"}]}}
            feeds = {"u1": rss([("Brand new thing", "https://ign.com/a/old-story?fbclid=zzz", "", NOW - timedelta(hours=1)),
                                ("Fresh one today", "https://ign.com/a/fresh", "", NOW - timedelta(hours=2))]),
                     # ... or by its headline (another outlet, another URL)
                     "u2": rss([("OLD story: headline here!", "https://pcg.com/other-url", "", NOW - timedelta(hours=1))])}
            with mock.patch.object(feedparser, "parse", side_effect=lambda url, agent=None: feeds[url]):
                titles = [s.title for s in news.fetch_stories(cfg, 12, NOW)]
        self.assertEqual(titles, ["Fresh one today"])


# ---------------------------------------------------------------- Claude reply validation
def story(i=1, outlet="IGN"):
    return news.Story(i, outlet, f"Headline {i}", "summary", f"https://{outlet.lower()}.com/{i}", NOW)


def reply(sid=1, body_words=82, hooks=None, **over):
    d = {"id": sid, "kind": "news", "score": 8, "subjects": ["GTA 6"],
         "hooks": hooks or ["GTA 6 officially hits PlayStation records today", "PlayStation says GTA 6 broke records",
                            "GTA 6 now owns PlayStation's biggest launch", "Officially: GTA 6 is PlayStation's biggest",
                            "Sony confirms GTA 6 player records"],
         "script_body": " ".join(["Rockstar shares numbers."] * (body_words // 3 + 1)).split(" ", body_words)[0:body_words] and
         " ".join((["Rockstar", "shares", "numbers."] * 40)[:body_words]),
         "headline": "GTA 6 breaks PlayStation records", "title": "GTA 6 breaks PlayStation launch records",
         "description": "GTA 6 is PlayStation's biggest launch.", "tags": [f"t{i}" for i in range(8)],
         "hashtags": ["#gta6", "ps5"], "sensitive": False, "sensitive_reason": "", "reason": "big"}
    d.update(over)
    return d


class ValidateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.stories = [story(1), story(2, "PC Gamer"), story(3, "GameSpot")]

    def check(self, *stories, fenced=False):
        text = json.dumps({"stories": list(stories)})
        return news.validate_reply(f"```json\n{text}\n```" if fenced else text, self.stories, self.cfg)

    def test_valid_reply_picks_best_hook_and_builds_script(self):
        picks = self.check(reply(1), reply(2))
        self.assertEqual(len(picks), 2)
        p = picks[0]
        self.assertEqual(p.story.link, "https://ign.com/1")
        self.assertEqual(len(p.hooks), 5)
        self.assertTrue(p.hook.valid)
        self.assertEqual(p.hook.total, max(h.total for h in p.hooks))
        self.assertTrue(p.script.startswith(p.hook.hook))
        self.assertTrue(80 <= len(p.script.split()) <= 105)
        self.assertEqual(p.hashtags, ["#gta6", "#ps5"])

    def test_code_fences_are_tolerated(self):
        self.assertEqual(len(self.check(reply(1), fenced=True)), 1)

    def test_default_is_four_stories_per_run(self):
        self.assertEqual(load_config().news["stories"], 4)

    def test_at_most_requested_stories(self):
        self.cfg.raw = {**self.cfg.raw, "news": {**self.cfg.raw.get("news", {}), "stories": 2}}
        self.assertEqual(len(self.check(reply(1), reply(2), reply(3))), 2)

    def test_hook_that_breaks_total_word_range_is_skipped_for_one_that_fits(self):
        long_hook = "GTA 6 officially hits PlayStation records today for real now yes"      # 11 words
        short_hook = "GTA 6 now owns records"                                               # 5 words
        hooks = [long_hook, short_hook, "Officially: GTA 6 is PlayStation's biggest", "Sony confirms GTA 6 records", "GTA 6 leak today"]
        pick = self.check(reply(1, body_words=94, hooks=hooks))[0]     # 94 + 11 = 105 ok, but make it tighter below
        self.assertLessEqual(len(pick.script.split()), 105)
        pick = self.check(reply(1, body_words=93, hooks=[long_hook] + hooks[1:]))[0]
        self.assertLessEqual(len(pick.script.split()), 105)

    def test_rejections(self):
        cases = {
            "unknown id": reply(99),
            "four hooks": reply(1, hooks=["a b c d e", "a b c d e", "a b c d e", "a b c d e"]),
            "body too short": reply(1, body_words=40),
            "body too long": reply(1, body_words=130),
            "banned filler in body": reply(1, script_body="Hey guys " + " ".join(["Rockstar shares numbers."] * 40)),
            "no valid hook": reply(1, hooks=["Hey guys look", "Basically a hook here", "x y z a b c d e f g h i j k l", "literally nothing", "welcome back"]),
            "bad kind": reply(1, kind="rumour"),
            "bad score": reply(1, score=11),
            "headline too long": reply(1, headline="x" * 80),
            "sensitive without reason": reply(1, sensitive=True),
            "description too long": reply(1, description="y" * 150),
        }
        for name, bad in cases.items():
            with self.subTest(name), self.assertRaises(news.NewsError):
                self.check(bad)
        for name, text in {"prose": "Here are my picks!", "empty": '{"stories": []}', "list": "[]"}.items():
            with self.subTest(name), self.assertRaises(news.NewsError):
                news.validate_reply(text, self.stories, self.cfg)
        with self.assertRaises(news.NewsError):
            self.check(reply(1), reply(1))                                            # same story twice

    def test_one_bad_story_is_dropped_and_the_rest_are_kept(self):
        with self.assertLogs("clipper", "WARNING") as logs:
            picks = self.check(reply(1, body_words=40), reply(2))
        self.assertEqual([p.story.id for p in picks], [2])
        self.assertIn("story 1:", " ".join(logs.output))
        with self.assertRaises(news.NewsError) as ctx:                                 # none pass: retried with every reason
            self.check(reply(1, body_words=40), reply(2, score=11))
        self.assertIn("story 1:", str(ctx.exception))
        self.assertIn("story 2:", str(ctx.exception))

    def test_profanity_is_masked_everywhere(self):
        pick = self.check(reply(1, headline="GTA 6 is some shit", title="This shit is real in GTA 6"))[0]
        self.assertNotIn("shit", pick.headline.lower())
        self.assertNotIn("shit", pick.title.lower())

    def test_prompt_lists_recently_covered_events_so_they_are_not_repeated(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            self.cfg.database = Path(tmp) / "c.sqlite"
            now = datetime.now(timezone.utc)
            for i, (status, title) in enumerate([("rendered", "GTA 6 breaks PlayStation records"), ("failed", "Failed one")]):
                db.add_clip(self.cfg.database, "news", "IGN", f"n{i}", source_url=f"u{i}")
                db.record_analysis(self.cfg.database, f"n{i}", {"score": 5, "title": title})
                db.set_clip_status(self.cfg.database, f"n{i}", status)
            prompt = news.build_prompt(self.cfg, self.stories, 2, now)
            self.assertIn("ALREADY MADE IN THE LAST 24 HOURS", prompt)
            self.assertIn("- GTA 6 breaks PlayStation records", prompt)
            self.assertNotIn("Failed one", prompt)
            self.assertNotIn("{covered}", prompt)
            empty = load_config()
            empty.database = Path(tmp) / "empty.sqlite"
            self.assertNotIn("ALREADY MADE", news.build_prompt(empty, self.stories, 2, now))

    def test_prompt_has_voice_rules_stories_and_no_unfilled_placeholders(self):
        prompt = news.build_prompt(self.cfg, self.stories, 2, NOW)
        self.assertIn("Banned filler", prompt)                                         # voice.md is included
        self.assertIn("1 | IGN | 0m ago | Headline 1", prompt)
        self.assertIn("{\"stories\"", prompt)                                          # literal JSON example intact
        for key in ("{hours}", "{rows}", "{voice}", "{n}", "{bmin}", "{bmax}", "\0"):
            self.assertNotIn(key, prompt)
        self.assertIn("76-93", prompt)                                                 # body range keeps hook+body in 80-105


# ---------------------------------------------------------------- voice + timestamps
class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.out = Path(self.tmp.name) / "v.mp3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_synthesize_retries_then_falls_back_to_another_voice(self):
        calls = []

        async def fake(text, voice, rate, out, pitch=None):
            calls.append((voice, rate))
            if voice == self.cfg.news["voice"]:
                raise RuntimeError("NoAudioReceived")
            out.write_bytes(b"x" * 5000)
        with mock.patch.object(news, "_tts_once", side_effect=fake), mock.patch.object(news.time, "sleep"):
            voice = news.synthesize(self.cfg, "hello", self.out)
        self.assertEqual(voice, "en-US-BrianNeural")
        self.assertEqual(len(calls), 4)                                               # 3 tries on the first voice, then the fallback
        self.assertEqual({r for _, r in calls}, {self.cfg.news["rate"]})

    def test_synthesize_gives_up_with_a_clear_error(self):
        async def fail(*a):
            raise RuntimeError("down")
        with mock.patch.object(news, "_tts_once", side_effect=fail), mock.patch.object(news.time, "sleep"):
            with self.assertRaisesRegex(news.NewsError, "could not produce audio"):
                news.synthesize(self.cfg, "hello", self.out)

    def test_rate_helpers(self):
        self.assertEqual(news._rate_value("+6%"), 6)
        self.assertEqual(news._rate_value("-8%"), -8)
        self.assertEqual(news._rate_text(20), "+20%")
        self.assertEqual(news._rate_text(-4), "-4%")

    def test_estimated_words_cover_the_audio_in_order(self):
        words = news.estimate_words("one two three four five six", 6.0)
        self.assertEqual([w[2] for w in words], "one two three four five six".split())
        self.assertTrue(all(a < b for a, b, _ in words))
        self.assertTrue(all(words[i][1] <= words[i + 1][0] + 1e-6 for i in range(len(words) - 1)))
        self.assertLess(words[-1][1], 6.0)
        self.assertEqual(news.estimate_words("", 5.0), [])

    def test_groq_request_and_parse(self):
        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"words": [{"word": " Hello", "start": 0.1, "end": 0.4},
                                                         {"word": "world.", "start": 0.4, "end": 0.9}]}).encode()
        self.out.write_bytes(b"ID3audio")
        with mock.patch.object(news.urllib.request, "urlopen", return_value=Resp()) as op:
            words = news.groq_words(self.cfg, self.out, "SECRET-KEY")
        self.assertEqual(words, [[0.1, 0.4, "Hello"], [0.4, 0.9, "world."]])
        req = op.call_args.args[0]
        self.assertEqual(req.full_url, news.GROQ_URL)
        self.assertEqual(req.get_header("Authorization"), "Bearer SECRET-KEY")
        self.assertIn(b"whisper-large-v3-turbo", req.data)
        self.assertIn(b"timestamp_granularities[]", req.data)

    def test_groq_failure_never_leaks_the_key_and_falls_back(self):
        err = news.urllib.error.HTTPError("u", 401, "bad key SECRET-KEY", {}, None)
        self.out.write_bytes(b"ID3audio")
        with mock.patch.object(news.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(news.NewsError) as ctx:
                news.groq_words(self.cfg, self.out, "SECRET-KEY")
        self.assertNotIn("SECRET-KEY", str(ctx.exception))
        with mock.patch.object(news, "get_env_key", return_value="SECRET-KEY"), \
                mock.patch.object(news, "groq_words", side_effect=news.NewsError("Groq transcription failed (HTTP 401)")):
            words, how = news.get_words(self.cfg, self.out, "a b c d", 4.0)
        self.assertEqual((how, len(words)), ("estimated", 4))
        with mock.patch.object(news, "get_env_key", return_value=None):
            self.assertEqual(news.get_words(self.cfg, self.out, "a b c d", 4.0)[1], "estimated")
        with mock.patch.object(news, "get_env_key", return_value="k"), \
                mock.patch.object(news, "groq_words", return_value=[[0, 1, "x"]]):
            self.assertEqual(news.get_words(self.cfg, self.out, " ".join(["w"] * 50), 20.0)[1], "estimated")   # heard far too few words


# ---------------------------------------------------------------- a whole Short, real ffmpeg
class MakeShortTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.audio = Path(cls.tmp.name) / "voice.mp3"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=220:duration=5", "-c:a", "libmp3lame", str(cls.audio)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        root = Path(tempfile.mkdtemp(dir=self.tmp.name))
        self.cfg = load_config()
        # these tests build 3-4 fixture pictures: pin the old 4-6 picture settings (the shipped 8-10 are tested in test_pacing)
        self.cfg.raw["news"] = {**self.cfg.raw.get("news", {}), "images_min": 4, "images_max": 6, "images_floor": 3}
        self.cfg.database = root / "c.sqlite"
        self.cfg.clips_dir = root / "clips"
        self.cfg.data_dir = root
        self.cfg.raw = {**self.cfg.raw, "clip": {**self.cfg.raw.get("clip", {}), "min_seconds": 1, "max_seconds": 40},
                        "notify": {"enabled": False}, "watermark": {**self.cfg.raw.get("watermark", {}), "file": str(root / "wm.png")},
                        "music": {"enabled": False}}          # music + pictures have their own tests (test_newsimage.py)
        patcher = mock.patch.object(newsimage, "download", side_effect=OSError("offline in tests"))
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("search_ddgs", "search_commons"):                      # never touch the network
            p2 = mock.patch.object(imagesearch, name, return_value=[])
            p2.start()
            self.addCleanup(p2.stop)
        p3 = mock.patch.object(news, "fetch_news_images", side_effect=fake_article_pictures)    # 4 real-looking pictures
        p3.start()
        self.addCleanup(p3.stop)

    def pick(self, sid=1, outlet="IGN"):
        return news.validate_reply(json.dumps({"stories": [reply(sid)]}), [story(sid, outlet)], self.cfg)[0]

    def fake_voice(self):
        def synth(cfg, text, out, rate=None):
            shutil.copyfile(self.audio, out)
            return "en-US-AndrewNeural"
        return synth

    def test_full_pipeline_creates_a_credited_1080x1920_short_in_the_database(self):
        p = self.pick()
        with mock.patch.object(news, "synthesize", self.fake_voice()), \
                mock.patch.object(news, "get_words", side_effect=lambda cfg, mp3, script, dur: (news.estimate_words(script, dur), "estimated")):
            folder = news.make_short(self.cfg, p)
        for name in ("clip.mp4", "thumb.jpg", "analysis.json", "script.txt", "voiceover.mp3", "words.json"):
            self.assertTrue((folder / name).is_file(), name)
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=width,height,codec_name", "-of", "csv=p=0", str(folder / "clip.mp4")],
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual(probe, "h264,1080,1920")
        row = db.clip_by_folder(self.cfg.database, folder.name)
        self.assertEqual((row["platform"], row["status"], row["handle"], row["moment_kind"]), ("news", "rendered", "IGN", "news"))
        self.assertEqual(row["source_url"], "https://ign.com/1")                       # the exact article URL
        self.assertEqual(row["stream"], "https://ign.com/1")
        self.assertEqual(row["ai_score"], 8.0)
        self.assertEqual(row["pre_score"], p.hook.total)
        self.assertTrue(row["description"].splitlines()[-1].startswith("Sources & Credits: IGN, https://ign.com/1"))
        self.assertTrue(row["clip_path"].endswith("clip.mp4"))
        self.assertIsNotNone(row["route_platform"])                                    # routed after rendering
        a = json.loads((folder / "analysis.json").read_text(encoding="utf-8"))
        self.assertFalse(a["autopost"])
        self.assertEqual(a["news"]["url"], "https://ign.com/1")
        self.assertEqual(len(a["news"]["hooks"]), 5)
        self.assertTrue(a["render"]["watermark"])
        self.assertEqual(a["credit_line"], "Sources & Credits: IGN, https://ign.com/1")

    def test_the_new_short_passes_the_exporter(self):
        from clipper import export
        self.cfg.raw = {**self.cfg.raw, "paths": {**self.cfg.raw["paths"], "exports_dir": str(self.cfg.data_dir / "exports")}}
        with mock.patch.object(news, "synthesize", self.fake_voice()), \
                mock.patch.object(news, "get_words", side_effect=lambda cfg, mp3, script, dur: (news.estimate_words(script, dur), "estimated")):
            folder = news.make_short(self.cfg, self.pick())
        row = db.clip_by_folder(self.cfg.database, folder.name)
        db.review_action(self.cfg.database, row["id"], "approve")
        res = export.run_export(self.cfg)
        self.assertTrue(res.ok, res.refused)
        sheet = next((self.cfg.data_dir / "exports").rglob("*.txt")).read_text(encoding="utf-8-sig")
        self.assertIn("Source URL:  https://ign.com/1", sheet)
        self.assertIn("Route:", sheet)
        desc = [ln for ln in sheet.split("DESCRIPTION")[1].split("HASHTAGS")[0].splitlines() if ln.strip() and not set(ln) <= {"="}]
        self.assertTrue(desc[-1].startswith("Sources & Credits: IGN, https://ign.com/1"))

    def test_failure_marks_the_row_failed_and_run_continues_with_the_next_story(self):
        stories = [story(1), story(2, "PC Gamer")]
        second_hooks = ["Xbox confirms Halo returns to PlayStation today", "Halo finally lands on PS5 officially",
                        "Xbox says Halo is coming to PlayStation", "Officially: Halo heads to PlayStation 5",
                        "Halo on PlayStation is now confirmed"]
        picks = news.validate_reply(json.dumps({"stories": [reply(1), reply(2, hooks=second_hooks, subjects=["Halo"])]}),
                                    stories, self.cfg)
        self.assertNotEqual(picks[0].script, picks[1].script)
        good = self.fake_voice()

        def synth(cfg, text, out, rate=None):
            if picks[0].script in text:
                raise news.NewsError("edge-tts could not produce audio (down)")
            return good(cfg, text, out, rate)
        with mock.patch.object(news, "fetch_stories", return_value=stories), \
                mock.patch.object(news, "pick_stories", return_value=picks), \
                mock.patch.object(news, "synthesize", synth), \
                mock.patch.object(news, "get_words", side_effect=lambda cfg, mp3, script, dur: (news.estimate_words(script, dur), "estimated")):
            res = news.run_news(self.cfg, trends=False)
        self.assertEqual((len(res.made), len(res.failed)), (1, 1))
        rows = {r["handle"]: r for r in db.list_clips(self.cfg.database)}
        self.assertEqual(rows["IGN"]["status"], "failed")
        self.assertIn("edge-tts", rows["IGN"]["error"])
        self.assertEqual(rows["PC Gamer"]["status"], "rendered")

    def test_dry_run_writes_nothing(self):
        picks = news.validate_reply(json.dumps({"stories": [reply(1)]}), [story(1)], self.cfg)
        with mock.patch.object(news, "fetch_stories", return_value=[story(1)]), mock.patch.object(news, "pick_stories", return_value=picks):
            res = news.run_news(self.cfg, trends=False, dry_run=True)
        self.assertEqual((len(res.picked), len(res.made)), (1, 0))
        self.assertEqual(db.list_clips(self.cfg.database), [])

    def test_no_fresh_stories_is_not_an_error(self):
        with mock.patch.object(news, "fetch_stories", return_value=[]):
            res = news.run_news(self.cfg, trends=False)
        self.assertIn("no fresh stories", res.note)

    def test_claude_failure_is_reported_not_raised(self):
        from clipper.editor import EditorError
        with mock.patch.object(news, "fetch_stories", return_value=[story(1)]), \
                mock.patch("clipper.editor.call_claude", side_effect=EditorError("claude timed out")):
            res = news.run_news(self.cfg, trends=False)
        self.assertTrue(res.note.startswith("run failed"))
        self.assertEqual(res.made, [])

    def test_news_ass_layout(self):
        info = {"hook": "GTA 6 officially hits PlayStation records", "headline": "GTA 6 breaks records", "kind": "free_offer",
                "outlet": "IGN", "title": "t"}
        words = [[0.2, 0.5, "this"], [0.5, 0.8, "is"], [0.8, 1.1, "shit"], [1.1, 1.4, "so"]]
        ass = news_render.build_news_ass(info, words, 5.0)
        self.assertIn("FREE GAME", ass)
        import re
        plain = re.sub(r"\{[^}]*\}", "", ass)                                           # text without override tags
        self.assertIn("GTA 6 OFFICIALLY HITS PLAYSTATION RECORDS", plain)
        self.assertIn("{\\c&H0000F2FF&}6{", ass)                                        # the number is the accent word
        self.assertIn("\\fscx72", ass)                                                  # pop-in animation on the captions
        self.assertIn("xfade", Path(news_render.__file__).read_text(encoding="utf-8"))
        self.assertIn("Source: IGN", ass)
        self.assertIn("S*** SO", plain)                                                 # captions: 2-3 words per line, masked
        self.assertIn(r"{\c&H00FFF000&\fscx80", ass)                                    # the spoken word: cyan + pop
        self.assertIn(f"\\pos(540,{news_render.CAPTION_Y})", ass)
        self.assertLess(news_render.CAPTION_Y, news_render.SAFE_BOTTOM)
        self.assertIn("Style: Cap,Impact,100", ass)


if __name__ == "__main__":
    unittest.main()
