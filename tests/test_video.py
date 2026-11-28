import unittest

from clipper import buckets, lifecycle, news, promo, routing, tech, videosearch


def hit(**kw):
    return {"id": "abc123", "title": "Grand Theft Auto VI Trailer 2", "channel": "Rockstar Games",
            "channel_is_verified": True, "duration": 160, "live_status": None, **kw}


class VideoTests(unittest.TestCase):
    def test_only_the_same_game_matches(self):
        self.assertTrue(videosearch.same_game("Hades", "Hades"))
        self.assertFalse(videosearch.same_game("Hades", "Hades II"))
        self.assertFalse(videosearch.same_game("GTA 6", "Grand Theft Auto V"))
        self.assertTrue(videosearch.same_game("Monster Hunter", "Monster Hunter Wilds"))

    def test_youtube_hits_must_be_verified_relevant_and_clean(self):
        self.assertTrue(videosearch.usable_hit("GTA 6", hit()))
        for bad in (hit(channel_is_verified=None), hit(live_status="is_live"), hit(duration=4000),
                    hit(title="Grand Theft Auto V Trailer"), hit(title="GTA 6 trailer REACTION"),
                    hit(title="GTA 6 concept trailer")):
            self.assertFalse(videosearch.usable_hit("GTA 6", bad), bad)
        self.assertFalse(videosearch.usable_hit("GTA 6", hit(), exclude="https://youtu.be/abc123"))
        self.assertFalse(videosearch.title_matches("Hades", "Hades II Official Trailer"))

    def test_only_listed_hosts(self):
        self.assertTrue(videosearch._allowed("https://video.akamai.steamstatic.com/a.m3u8"))
        self.assertFalse(videosearch._allowed("https://evilsteamstatic.com/a.m3u8"))

    def test_clip_starts_skip_the_intro_and_end_card(self):
        starts = videosearch.start_times(90.0, 4)
        self.assertEqual((len(starts), starts[0]), (4, 5.0))
        self.assertLessEqual(starts[-1] + videosearch.CLIP_SECONDS, 80.0)
        self.assertEqual(videosearch.start_times(12.0, 3), [])

    def test_the_liveliest_windows_win_and_still_ones_never_do(self):
        self.assertEqual(videosearch.best_windows([(5, 9.0), (10, 1.0), (15, 30.0), (20, 12.0)], 2), [15, 20])
        self.assertEqual(videosearch.best_windows([(5, 0.0)], 3), [])

    def test_tech_headlines(self):
        self.assertTrue(tech.is_story("Nvidia driver adds DLSS to 12 games"))
        self.assertFalse(tech.is_story("Hades 2 gets a new boss"))


class PromoTests(unittest.TestCase):
    def test_which_headlines_are_promo(self):
        self.assertTrue(promo.is_story("Hades is 50% off on Steam"))
        self.assertTrue(promo.is_story("Valve reveals a new controller"))
        self.assertFalse(promo.is_story("Studio signs publishing deal"))
        self.assertFalse(promo.is_story("Hades 2 gets a new boss"))

    def test_badges(self):
        self.assertEqual(buckets.badge_label(promo.bucket_for("Black Friday GPU deals")), "GAME DEAL")
        self.assertEqual(buckets.badge_label(promo.bucket_for("Valve reveals a new controller")), "TECH PICK")

    def test_promo_is_a_mode_of_its_own(self):
        self.assertIn("promo", news.MODES)

    def test_promo_never_takes_a_youtube_slot(self):
        from clipper.config import load_config
        clips = [{"id": 1, "ai_score": 9.9, "platform": "news", "moment_kind": "promo"},
                 {"id": 2, "ai_score": 3.0, "platform": "news", "moment_kind": "news"}]
        out = routing.plan(load_config(), clips)
        self.assertEqual(out[2]["platform"], "youtube")
        self.assertNotEqual(out[1]["platform"], "youtube")

    def test_promo_history_is_invisible_to_other_commands(self):
        import tempfile
        from pathlib import Path

        from clipper import db
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.sqlite"
            db.add_clip(path, "news", "IGN", "a", moment_kind="promo", source_url="https://x.com/a")
            db.add_clip(path, "news", "IGN", "b", moment_kind="news", source_url="https://x.com/b")
            self.assertEqual(db.news_urls_seen(path), {"https://x.com/b"})
            self.assertEqual(len(db.news_urls_seen(path, promo=True)), 2)
        self.assertTrue(callable(lifecycle.record_seen))


class ReuseTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from clipper.config import load_config
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = load_config()
        self.cfg.data_dir, self.cfg.database = self.root, self.root / "c.sqlite"

    def video(self, name, source, seconds=5):
        import shutil
        import subprocess
        out = self.root / name
        subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"{source}=s=1280x720:r=30:d={seconds}", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)], check=True)
        return out

    def test_the_same_footage_is_recognised_and_other_footage_is_not(self):
        from clipper import videosearch
        first = videosearch.window_prints(self.video("a.mp4", "testsrc2"))
        again = videosearch.window_prints(self.video("b.mp4", "testsrc2"))
        other = videosearch.window_prints(self.video("c.mp4", "smptebars"))
        self.assertTrue(first and other)
        self.assertTrue(videosearch.shown_before(again, first))
        self.assertFalse(videosearch.shown_before(other, first))
        self.assertFalse(videosearch.shown_before(again, []))
        self.assertEqual(videosearch.window_prints(self.root / "no.mp4"), [])

    def test_used_footage_is_remembered_and_expires(self):
        from clipper import db, news
        db.add_used_clips(self.cfg.database, [(1, 2)])
        self.assertEqual(news._recently_used_clips(self.cfg), [(1, 2)])
        self.cfg.raw = {**self.cfg.raw, "news": {**self.cfg.raw["news"], "video_repeat_hours": 0}}
        self.assertEqual(news._recently_used_clips(self.cfg), [])

    def test_a_source_that_was_already_shown_gives_no_clip(self):
        from clipper import videosearch
        source = self.video("own.mp4", "testsrc2", seconds=30)
        seen = [p for s in videosearch.start_times(30, 16) for p in videosearch.window_prints(source, s)]
        video = {"url": str(source), "source": "own", "credit": "c"}
        folder = self.root / "short"
        folder.mkdir()
        self.assertEqual(videosearch.clips_from(self.cfg, video, folder, 4, 1, seen=seen), [])


if __name__ == "__main__":
    unittest.main()
