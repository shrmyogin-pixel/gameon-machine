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


if __name__ == "__main__":
    unittest.main()
