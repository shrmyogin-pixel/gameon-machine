"""banger12.bat / news --all-niches and dense Shorts. Claude, feeds and the web are mocked; one real render."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import cv2
import numpy as np

from clipper import batch, cli, news, news_render, watchlist
from clipper.config import PROJECT_ROOT, load_config
from clipper.hookscore import rank_hooks

HOOKS = ["GTA 6 leak shows a new map", "Why GTA 6 fans zoom in", "Your GTA 6 wait just changed"]


def pick(title, subject, hooks=HOOKS, pattern=""):
    ranked = rank_hooks(hooks, [subject])
    body = " ".join([f"{subject} shares fresh details."] + ["Players can read every part of it."] * 12)
    return NS(story=NS(title=title, link=f"https://x.com/{title}"), subjects=[subject], hooks=ranked, hook=ranked[0],
              body=body, script="", hook_pattern=pattern)


class BatchRuleTests(unittest.TestCase):
    def test_the_batch_file_and_its_options(self):
        text = (PROJECT_ROOT / "banger12.bat").read_text(encoding="utf-8")
        self.assertIn("-m clipper news --all-niches --count 12 --min-videos 12 --min-photos 22 --no-promo %*", text)
        seen = {}
        with mock.patch.object(batch, "run_batch", lambda cfg, count, hours, dry, **k: seen.update(k, count=count)
                               or news.NewsResult()):
            cli.main("news --all-niches --count 12 --min-videos 12 --min-photos 22 --no-promo".split())
        self.assertEqual((seen["count"], seen["dense"]), (12, {"videos": 12, "photos": 22}))
        for bad in ("news --all-niches --topic gta", "news --no-promo --topic promo"):
            with self.assertRaises(SystemExit):
                cli.main(bad.split())

    def test_the_cap_is_ceil_count_over_every_configured_niche(self):
        self.assertEqual(batch.niches(), list(watchlist.BUCKETS))
        self.assertEqual([batch.niche_cap(n, 5) for n in (12, 10, 11, 3, 1)], [3, 2, 3, 1, 1])

    def run_with(self, supply, count=12):
        """A batch where each niche has `supply[niche]` distinct stories -> the niche of every Short, in order."""
        cfg = load_config()
        pools = {n: [NS(title=f"Game{chr(97 + j)}{chr(97 + i // 26)}{chr(97 + i % 26)}", link=f"https://x.com/{n}/{i}",
                        summary="", id=0, watch=None, niche=n) for i in range(k)]
                 for j, (n, k) in enumerate(supply.items())}
        lookup = {s.title: s.niche for p in pools.values() for s in p}
        made = []

        def fake_pick(c, niche, candidates, want, state):
            out = []
            for s in candidates[:want]:
                p = pick(s.title, s.title, hooks=[f"{s.title} news now"])
                p.story = s
                out.append(p)
            return out
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg.database, cfg.data_dir = Path(tmp) / "c.sqlite", Path(tmp)
            with mock.patch.object(news, "fetch_stories", return_value=[s for p in pools.values() for s in p]), \
                    mock.patch.object(watchlist, "bucket_of", lambda title, summary="": lookup[title]), \
                    mock.patch.object(watchlist, "annotate"), mock.patch.object(batch, "_pick", fake_pick), \
                    mock.patch.object(news, "make_short", lambda c, p: made.append(p.story.niche)):
                res = batch.run_batch(cfg, count, research=False)
        return made, res

    def test_round_robin_and_a_flood_of_gta_stories_cannot_dominate(self):
        names = list(watchlist.BUCKETS)
        self.assertEqual(len(names), 8)
        made, res = self.run_with({n: 40 if n == "gta_6" else 6 for n in names})
        self.assertEqual(made, names + names[:4])                         # one each per turn
        self.assertEqual(res.note, "")
        made, _ = self.run_with({n: 5 for n in names}, count=3)
        self.assertEqual(made, names[:3])

    def test_short_niches_pass_their_places_on_until_the_batch_is_full(self):
        names = list(watchlist.BUCKETS)
        made, res = self.run_with({n: 0 if n in ("trailers", "creators", "free_games") else 40 for n in names})
        self.assertEqual((len(made), res.note), (12, ""))
        self.assertLessEqual(made.count("gta_6"), 3)
        counts = [made.count(n) for n in names if made.count(n) and n != "gta_6"]
        self.assertLessEqual(max(counts) - min(counts), 1)                # spread evenly
        made, res = self.run_with({n: {"gta_6": 40, "game_updates": 30}.get(n, 0) for n in names})
        self.assertEqual((made.count("gta_6"), made.count("game_updates")), (3, 9))
        made, res = self.run_with({n: {"gta_6": 40, "creators": 1}.get(n, 0) for n in names})
        self.assertEqual(made.count("gta_6"), 3)                          # never filled up with GTA
        self.assertIn("4 of 12", res.note)

    def test_the_new_categories_are_recognised(self):
        for title, bucket in (("Alan Wake 2 is free to keep on Epic this week", "free_games"),
                              ("Players find a hidden room in Hades 3", "secrets_glitches"),
                              ("No Man's Sky update adds new ships", "game_updates"),
                              ("GTA 6 easter egg found on a rooftop", "gta_6"),
                              ("Free, open-source upscaler rivals DLSS", "upcoming_tech")):
            self.assertEqual(watchlist.bucket_of(title), bucket, title)

    def test_gta_6_cannot_slip_in_through_another_niche(self):
        self.assertTrue(batch.Batch().repeats(pick("A console tech story", "GTA 6"), "upcoming_tech"))
        self.assertFalse(batch.Batch().repeats(pick("A map story", "GTA 6"), "gta_6"))

    def test_stories_games_and_hook_openings_do_not_repeat(self):
        cfg, state = load_config(), batch.Batch()
        state.add(pick("Elden Ring trailer shows a new boss", "Elden Ring"))
        self.assertTrue(state.repeats(pick("Elden Ring gets a release date", "Elden Ring"), "upcoming_games"))
        self.assertFalse(state.repeats(pick("Hades 3 trailer arrives", "Hades 3"), "trailers"))
        again = pick("Hades 3 trailer arrives", "Hades 3")
        first = again.hook.hook
        self.assertTrue(state.vary_hook(cfg, again))
        self.assertNotEqual(again.hook.hook, first)
        self.assertTrue(again.script.startswith(again.hook.hook))
        self.assertFalse(state.vary_hook(cfg, pick("Other", "Other", hooks=[HOOKS[0]])))

    def test_each_niche_is_picked_and_no_promo_or_upload_happens(self):
        from tests.test_news import story
        cfg = load_config()
        titles = ["Elden Ring release date confirmed", "Hades 3 trailer arrives", "Nvidia unveils a new RTX GPU",
                  "GTA 6 map detail spotted", "Streamer finishes a month-long subathon"]
        stories = []
        for i, title in enumerate(titles, start=1):
            s = story(i)
            s.title, s.link = title, f"https://x.com/{i}"
            stories.append(s)
        calls = []

        def fake_pick(c, candidates, want, work, **kw):
            calls.append((c.news.get("only"), c.news["no_promo"], want))
            p = pick(candidates[0].title, candidates[0].title.split()[0], hooks=[f"{candidates[0].title.split()[0]} news now"])
            p.story = candidates[0]
            return [p]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg.database, cfg.data_dir = Path(tmp) / "c.sqlite", Path(tmp)
            with mock.patch.object(news, "fetch_stories", return_value=stories), \
                    mock.patch.object(news, "pick_stories", fake_pick), \
                    mock.patch.object(news, "make_short", lambda c, p: Path(tmp) / "made"):
                res = batch.run_batch(cfg, 12, research=False)
        self.assertEqual(len(res.made), 5)                     # one story per niche, none made twice
        self.assertEqual([c[0] for c in calls], [None, None, "tech", "gta", "creators"])
        self.assertTrue(all(c[1] for c in calls))
        self.assertIn("5 of 12", res.note)
        self.assertNotIn("niche_note", cfg.news)
        self.assertFalse(cfg.raw["general"]["autopost"])

    def test_no_promo_drops_deals_and_sponsored_stories(self):
        from datetime import datetime, timezone
        import feedparser
        from tests.test_news import rss
        cfg = load_config()
        now = datetime.now(timezone.utc)
        items = [("Handheld is 40% off today", "https://x.com/1", "", now), ("Sponsored: a new RPG", "https://x.com/2", "", now),
                 ("Hades 3 trailer arrives", "https://x.com/3", "", now)]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg.database = Path(tmp) / "c.sqlite"
            cfg.raw = {**cfg.raw, "news": {**cfg.raw["news"], "feeds": [{"name": "X", "url": "u"}], "no_promo": True}}
            with mock.patch.object(feedparser, "parse", return_value=rss(items)):
                self.assertEqual([s.title for s in news.fetch_stories(cfg)], ["Hades 3 trailer arrives"])


class DenseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = load_config()
        self.cfg.data_dir = self.root
        self.cfg.raw = {**self.cfg.raw, "news": {**self.cfg.raw["news"], "dense": {"videos": 12, "photos": 22}}}
        self.pick = NS(subjects=["Hades 3"], story=NS(title="t"), bucket="update")

    def collect(self, clips, pictures, frames=0):
        def items(n, kind):
            return [{"file": self.root / f"{kind}{i}", "url": f"u{i}", "source": "steam", "kind": kind, "credit": "c"}
                    for i in range(n)]
        with mock.patch.object(news, "collect_clips", return_value=items(clips, "clip")), \
                mock.patch.object(news, "collect_visuals", return_value=items(pictures, "topic")), \
                mock.patch.object(news, "clip_frames", return_value=items(frames, "frame")):
            return news.collect_dense(self.cfg, self.pick, self.root, 34.0)

    def test_ten_video_cuts_or_twenty_pictures(self):
        kind, clips, pictures = self.collect(clips=10, pictures=0)
        self.assertEqual((kind, len(clips), pictures), ("video", 10, []))
        kind, credited, pictures = self.collect(clips=6, pictures=17, frames=5)
        self.assertEqual((kind, len(pictures), len(credited)), ("photo", 22, 5))
        with self.assertRaisesRegex(news.NewsError, "skipping"):
            self.collect(clips=9, pictures=15, frames=4)
        self.assertNotIn("images_floor_override", self.cfg.news)
        self.assertEqual(self.cfg.news["images_max"], 10)

    def test_cuts_never_get_faster_than_the_dense_floor(self):
        self.assertEqual(news_render.dense_count(30.3, 15, "video"), 12)
        self.assertEqual(news_render.dense_count(30.3, 30, "photo"), 24)

    def test_a_22_picture_short_renders_at_1080x1920(self):
        pictures = []
        for i in range(22):
            rng = np.random.default_rng(i)
            img = cv2.resize(rng.integers(0, 255, (9, 16, 3), dtype=np.uint8), (1600, 900), interpolation=cv2.INTER_CUBIC)
            f = self.root / f"news_image_{i}.jpg"
            cv2.imwrite(str(f), img)
            pictures.append(f)
        subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=220:duration=30", str(self.root / "voiceover.mp3")], check=True)
        self.cfg.raw = {**self.cfg.raw, "watermark": {"enabled": False}}
        info = {"hook": "Hades 3 changes now", "outlet": "IGN", "title": "t", "images": pictures, "dense": "photo"}
        render = news_render.render_news_video(self.cfg, self.root, info, news.estimate_words("one two three", 30.0), 30.0)
        self.assertEqual((render["cuts"], render["visuals"]), (22, 22))
        from clipper import qa
        self.assertEqual(qa.check(self.cfg, self.root / "clip.mp4", "Sources & Credits: IGN, https://ign.com/1"), [])
        with self.assertRaises(news_render.RenderError):
            news_render.render_news_video(self.cfg, self.root, {**info, "images": pictures[:19]}, [[0, 1, "a"]], 30.0)


if __name__ == "__main__":
    unittest.main()
