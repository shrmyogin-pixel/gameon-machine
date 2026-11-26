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


if __name__ == "__main__":
    unittest.main()
