"""Stronger-hook scoring, sentence-2 repetition and the plain yellow category badge."""
import unittest

from clipper.hookscore import rank_hooks, restates_hook, score_hook


class StakesHookTests(unittest.TestCase):
    def test_stakes_hook_beats_the_plain_headline(self):
        ranked = rank_hooks(["Exoprimal is shutting down in 2027.", "Own Exoprimal? Your clock is now ticking."], ["Exoprimal"])
        self.assertEqual(ranked[0].hook, "Own Exoprimal? Your clock is now ticking.")
        self.assertIn("plain_headline", ranked[1].parts)

    def test_bare_stock_opener_is_penalised_but_one_with_a_stake_is_not(self):
        bare = score_hook("Stop scrolling if you play Mario Kart World.", ["Mario Kart World"])
        stake = score_hook("Stop scrolling if you love puzzles: Last Room's free.", ["Last Room"])
        self.assertEqual(bare.parts["stock_opener"], -4.5)
        self.assertNotIn("stock_opener", stake.parts)


class WeakHookTests(unittest.TestCase):
    def test_hooks_that_won_real_runs_now_lose_to_a_concrete_alternative(self):
        for weak, better, subject in (
                ("Stop scrolling if you have PS Plus.", "Three PS Plus games unlock December 1st.", "PS Plus"),
                ("Nobody expected this confession about how a rival console began.",
                 "Did PlayStation's creator trick Microsoft into building a console?", "PlayStation"),
                ("Nobody expected to hear this football commentator ever again.",
                 "Your EA Sports FC 27 matches could sound hauntingly familiar.", "EA Sports FC 27")):
            with self.subTest(weak=weak):
                self.assertEqual(rank_hooks([weak, better], [subject])[0].hook, better)

    def test_newsroom_phrasing_and_vague_teasers_are_marked(self):
        self.assertIn("plain_headline", score_hook("Nioh 3 DLC 2 just got a new date", ["Nioh 3"]).parts)
        self.assertIn("vague", score_hook("Blizzard just made a surprising call about Diablo 4.", ["Diablo 4"]).parts)


class BatchOpenerTests(unittest.TestCase):
    def test_a_second_story_avoids_the_opening_the_first_one_used(self):
        import json
        from clipper import news
        from clipper.config import load_config
        from tests.test_news import reply, story
        hooks = ["Your Steam library could lose a classic today", "Free on Steam today: a classic could vanish",
                 "Steam", "Steam now", "Steam page"]
        data = [{**reply(i), "hooks": hooks, "subjects": ["Steam"]} for i in (1, 2)]
        picks = news.validate_reply(json.dumps({"stories": data}), [story(1), story(2)], load_config())
        self.assertEqual(len(picks), 2)
        self.assertNotEqual(picks[0].hook.hook, picks[1].hook.hook)


class RepeatedHookTests(unittest.TestCase):
    def test_reworded_repeat_in_sentence_two_is_caught(self):
        self.assertTrue(restates_hook("Exoprimal is shutting down in 2027.",
                                      "The shutdown is set for 2027, and Capcom has made it official.", subjects=["Exoprimal"]))

    def test_naming_the_game_again_with_new_facts_is_fine(self):
        self.assertFalse(restates_hook("Your Exoprimal progress has an expiry date.",
                                       "Capcom will switch Exoprimal's servers off in 2027.", subjects=["Exoprimal"]))


class BadgeTests(unittest.TestCase):
    def test_badge_is_a_solid_box_without_glow_and_captions_are_unchanged(self):
        from clipper.news_render import build_news_ass
        ass = build_news_ass({"hook": "Free game today", "outlet": "IGN", "kind": "free_offer"}, [[0.0, 0.5, "free"]], 5.0)
        style = {line.split(",")[0]: line.split(",") for line in ass.splitlines() if line.startswith("Style: ")}
        badge, cap = style["Style: Kicker"], style["Style: Cap"]
        self.assertEqual((badge[3], badge[5], badge[6]), ("&H00141414&", "&H0000D4FF&", "&H0000D4FF&"))
        self.assertEqual((badge[15], badge[17]), ("3", "0"))                 # opaque box, no shadow
        self.assertEqual(cap[1:], ["Impact", "100", "&H00FFFFFF&", "&H000000FF", "&H00101010&", "&H00000000",
                                   "0", "0", "0", "0", "100", "100", "1", "0", "1", "9", "6", "5", "90", "90", "0", "1"])


if __name__ == "__main__":
    unittest.main()
