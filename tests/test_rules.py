"""The channel rules written down in .claude/skills/gameon-pipeline/SKILL.md, checked against the code and config."""
import unittest

from clipper import news, render
from clipper.buckets import BUCKETS, badge_label
from clipper.config import PROJECT_ROOT, load_config


class LockedPipelineRules(unittest.TestCase):
    def test_render_settings(self):
        cfg = load_config()
        self.assertEqual((render.W, render.H), (1080, 1920))
        self.assertIn("h264_qsv", cfg.raw["encoding"]["encoders"])
        self.assertTrue(cfg.raw["encoding"]["below_normal_priority"])
        self.assertEqual(cfg.render["max_renders"], 1)
        self.assertFalse(cfg.raw["general"]["autopost"])

    def test_each_bucket_has_its_pill(self):
        self.assertEqual([badge_label(b, subject="GTA 6") for b in BUCKETS],
                         ["GTA 6", "FREE GAME", "PATCH UPDATE", "WILD BUG", "CREATOR NEWS", "GAMING TECH"])
        self.assertEqual(badge_label("flagship", subject="Elden Ring"), "BIG GAME")
        self.assertEqual(badge_label("", "free_offer"), "FREE GAME")
        self.assertEqual(badge_label(""), "BREAKING NEWS")

    def test_the_pill_reaches_the_subtitle_file(self):
        from clipper.news_render import build_news_ass
        ass = build_news_ass({"hook": "Your car can fly now", "outlet": "IGN", "bucket": "glitch"}, [[0.0, 0.5, "fly"]], 5.0)
        self.assertTrue(any(",Kicker," in line and line.endswith("WILD BUG") for line in ass.splitlines()))

    def test_skill_file_is_there(self):
        text = (PROJECT_ROOT / ".claude" / "skills" / "gameon-pipeline" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: gameon-pipeline\n"))


class UnsafeStoryTests(unittest.TestCase):
    def test_piracy_cheat_scam_and_gambling_are_skipped_but_normal_news_is_not(self):
        for bad in ("GTA 6 torrent download is live", "Free V-Bucks generator no verification", "Best aimbot for Warzone",
                    "New mod menu gives unlimited cash", "Top casino sites for gamers", "FitGirl repack drops today"):
            self.assertTrue(news.HARD_SKIP.search(bad), bad)
        for good in ("GTA 6 money glitch found by players", "Anti-cheat update bans thousands", "Cheat codes return in GTA 6",
                     "A torrent of complaints hits the patch", "Free Steam game: claim it this week"):
            self.assertIsNone(news.HARD_SKIP.search(good), good)

    def test_betting_idioms_are_news_but_real_wagering_is_skipped(self):
        for idiom in ("\"I'm betting on the sickos\" - studio plans to expand the scope of RPGs", "Bungie's big gamble pays off",
                      "Sony is betting big on handhelds", "I bet you missed this secret"):
            self.assertIsNone(news.HARD_SKIP.search(idiom), idiom)
        for real in ("Best esports betting sites this week", "Team signs betting sponsor", "Pro banned for betting on his own matches",
                     "CS2 skin gambling crackdown", "New sportsbook offers a bonus", "Players placed bets on the final",
                     "Skin betting promo code inside"):
            self.assertTrue(news.HARD_SKIP.search(real), real)

    def test_a_hook_that_copies_the_headline_is_detected(self):
        title = "Exoprimal is shutting down in 2027, Capcom confirms"
        self.assertTrue(news.copies_headline(title, "Exoprimal is shutting down for good", ["Exoprimal"]))
        self.assertFalse(news.copies_headline(title, "Own Exoprimal? Your clock is ticking", ["Exoprimal"]))
        self.assertFalse(news.copies_headline("Call of Duty Black Ops gets a patch", "Your Call of Duty Black Ops loadout changed",
                                              ["Call of Duty Black Ops"]))

class StorySelectionTests(unittest.TestCase):
    def test_a_story_that_cannot_answer_its_hook_is_dropped(self):
        import json
        from tests.test_news import reply, story
        data = {"stories": [{**reply(1), "answers_hook": False}, {**reply(2), "answers_hook": True}]}
        picks = news.validate_reply(json.dumps(data), [story(1), story(2)], load_config())
        self.assertEqual([p.story.id for p in picks], [2])
        with self.assertRaisesRegex(news.NewsError, "cannot answer its own hook"):
            news.validate_reply(json.dumps({"stories": data["stories"][:1]}), [story(1)], load_config())

    def test_an_empty_story_list_means_nothing_today_not_a_failed_run(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from clipper import editor
        from tests.test_news import story
        cfg = load_config()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg.database = Path(tmp) / "c.sqlite"
            with mock.patch.object(editor, "call_claude", return_value='{"stories": []}') as call:
                self.assertEqual(news.pick_stories(cfg, [story(1)], 1, Path(tmp)), [])
        call.assert_called_once()

    def test_a_report_cannot_bring_a_covered_game_back(self):
        from types import SimpleNamespace as NS
        from clipper import topics

        def kept(title):
            pick = NS(score=8, subjects=["Ubisoft"], major_new=True, story=NS(title=title))
            return bool(topics.enforce([pick], ["assassin creed,ubisoft"])[0])
        self.assertFalse(kept("Report: Ubisoft CEO is demanding developers justify not using AI"))
        self.assertFalse(kept("Ubisoft rumoured to announce a delay"))
        self.assertFalse(kept("Ubisoft devs talk about crunch"))
        self.assertTrue(kept("Ubisoft announces a new release date for its next game"))

class GtaOnlyTests(unittest.TestCase):
    def test_only_real_gta_headlines_qualify(self):
        from clipper import gta
        for good in ("GTA 6 easter egg found in the second trailer", "GTA Online weekly update adds a new car",
                     "Grand Theft Auto VI delay rumour denied"):
            self.assertTrue(gta.is_gta_story(good), good)
        for bad in ("This GTA-like heist game is great", "Zelda patch out now", "GTA Online modded accounts for sale",
                    "GTA 6 beta download link appears"):
            self.assertFalse(gta.is_gta_story(bad), bad)
        self.assertFalse(gta.is_gta_story("Rockstar staff vote on a union", "The GTA maker held a ballot."))

    def test_several_angles_pass_but_the_same_event_does_not_repeat(self):
        from types import SimpleNamespace as NS
        from clipper import gta
        picks = [NS(score=s, story=NS(title=t)) for s, t in (
            (9, "GTA 6 breaks first weekend sales record"), (8, "GTA 6 first weekend sales record confirmed by Take-Two"),
            (7, "GTA 6 players find a hidden alligator easter egg"), (6, "GTA Online weekly bonus cars"))]
        kept, dropped = gta.enforce_angles(picks, [])
        self.assertEqual([p.score for p in kept], [9, 7, 6])
        self.assertEqual(len(dropped), 1)
        self.assertEqual(gta.enforce_angles(picks[2:3], ["Hidden alligator easter egg in GTA 6"])[0], [])

    def test_the_mode_filters_feeds_extends_the_window_and_is_undone_afterwards(self):
        import tempfile
        from datetime import datetime, timedelta, timezone
        from pathlib import Path
        from unittest import mock
        import feedparser
        from tests.test_news import rss
        cfg = load_config()
        now = datetime.now(timezone.utc)
        items = [(f"GTA 6 {word} spotted", f"https://x.com/{i}", "", now - timedelta(hours=30))
                 for i, word in enumerate(("alligator", "helicopter", "casino heist glitch", "jetski"))]
        items.append(("Zelda patch out now", "https://x.com/z", "", now - timedelta(hours=1)))
        seen = {}

        def fake_pick(c, stories, n, work):
            seen["titles"], seen["only"] = [s.title for s in stories], c.news.get("only")
            seen["prompt"] = news.build_prompt(c, stories, n)
            return []
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg.database = Path(tmp) / "c.sqlite"
            cfg.raw = {**cfg.raw, "news": {**cfg.raw["news"], "feeds": [{"name": "X", "url": "u"}]}}
            with mock.patch.object(feedparser, "parse", return_value=rss(items)), \
                    mock.patch.object(news, "pick_stories", fake_pick):
                result = news.run_news(cfg, dry_run=True, only="gta")
        self.assertEqual(len(seen["titles"]), 3)                  # 30 h old stories count; "casino" and Zelda do not
        self.assertIn("GTA-ONLY RUN", seen["prompt"])
        self.assertEqual(seen["only"], "gta")
        self.assertIsNone(cfg.news.get("only"))
        self.assertEqual(result.note, "no story worth making this time")

    def test_the_pill_says_gta_6_only_for_gta_6(self):
        self.assertEqual(badge_label("flagship", subject="GTA 6"), "GTA 6")
        self.assertEqual(badge_label("flagship", subject="GTA Online"), "GTA")
        self.assertEqual(badge_label("flagship", subject="GTA V"), "GTA")

    def test_the_command_and_batch_file(self):
        self.assertIn("-m clipper news --only-gta %*", (PROJECT_ROOT / "gta.bat").read_text(encoding="utf-8"))
        self.assertNotIn("gta", (PROJECT_ROOT / "news.bat").read_text(encoding="utf-8").lower())


class CreatorsOnlyTests(unittest.TestCase):
    def test_creator_news_qualifies_and_lookalikes_do_not(self):
        from clipper import creators
        for good in ("Kai Cenat finishes his month-long subathon", "Streamer completes Elden Ring no-hit run",
                     "Fnatic announce new Valorant roster", "Twitch viewership record falls to a Minecraft event",
                     "T1 wins the League of Legends world championship", "xQc reveals his own game"):
            self.assertTrue(creators.is_story(good), good)
        for bad in ("Marathon gets a release date", "Tekken adds to its character roster", "The creator of Final Fantasy speaks",
                    "Streamer arrested after police raid", "YouTuber accused in new allegations", "Zelda patch out now",
                    "Streamer starts slots stream"):
            self.assertFalse(creators.is_story(bad), bad)

    def test_one_short_per_creator_event_and_every_pill_is_creator_news(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        from clipper import editor
        from tests.test_news import reply, story
        cfg = load_config()
        cfg.raw = {**cfg.raw, "news": {**cfg.raw["news"], "stories": 3, "only": "creators"}}
        stories = [story(1), story(2), story(3)]
        stories[0].title = "Kai Cenat breaks the Twitch subscriber record"
        stories[1].title = "Kai Cenat sets new subscriber record on Twitch"
        stories[2].title = "Fnatic announce new Valorant roster"
        text = json.dumps({"stories": [{**reply(i), "bucket": "flagship"} for i in (1, 2, 3)]})
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg.database = Path(tmp) / "c.sqlite"
            with mock.patch.object(editor, "call_claude", return_value=text):
                picks = news.pick_stories(cfg, stories, 3, Path(tmp))
            self.assertIn("CREATORS-ONLY RUN", news.build_prompt(cfg, stories, 3))
        self.assertEqual([p.story.id for p in picks], [1, 3])
        self.assertEqual({badge_label(p.bucket) for p in picks}, {"CREATOR NEWS"})

    def test_the_command_reaches_the_creators_mode_and_other_launchers_are_unchanged(self):
        from unittest import mock
        from clipper import cli
        seen = []
        result = news.NewsResult()
        with mock.patch.object(news, "run_news", lambda *a, **k: seen.append(k.get("only")) or result):
            for args in (["news", "--creators"], ["news", "--topic", "creators"], ["news", "--only-gta"], ["news"]):
                cli.main(args)
        self.assertEqual(seen, ["creators", "creators", "gta", None])
        self.assertIn("-m clipper news --creators %*", (PROJECT_ROOT / "creators.bat").read_text(encoding="utf-8"))
        for name in ("news.bat", "export.bat"):
            self.assertNotIn("creators", (PROJECT_ROOT / name).read_text(encoding="utf-8").lower())


class RenderSlotTests(unittest.TestCase):
    def test_a_second_process_cannot_take_the_render_slot(self):
        import subprocess
        import sys
        import tempfile
        from pathlib import Path
        probe = ("import msvcrt, sys; h = open(sys.argv[1], 'a+b'); h.seek(0)\n"
                 "try: msvcrt.locking(h.fileno(), msvcrt.LK_NBLCK, 1); print('free')\n"
                 "except OSError: print('busy')")

        def state(path):
            return subprocess.run([sys.executable, "-c", probe, str(path)], capture_output=True, text=True).stdout.strip()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            with render._machine_slot(Path(tmp), 1):
                self.assertEqual(state(Path(tmp) / "render-0.lock"), "busy")
            self.assertEqual(state(Path(tmp) / "render-0.lock"), "free")


if __name__ == "__main__":
    unittest.main()
