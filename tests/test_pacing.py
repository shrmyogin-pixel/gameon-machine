import unittest

from clipper import pacing
from clipper.buckets import balance, classify, enforce_mix
from clipper.hookscore import closer_banned, restates_hook, score_hook
from clipper.render import SAFE_BOTTOM, pop_caption_events, word_lines


def W(*items):
    return [[a, b, w] for a, b, w in items]


class PacingTests(unittest.TestCase):
    words = W((0.1, 0.5, "One"), (0.5, 0.9, "streamer"), (2.0, 2.4, "broke"), (2.4, 2.8, "it."), (3.0, 3.4, "Wow"))

    def test_only_long_gaps_are_cut_and_a_breath_is_kept(self):
        cuts = pacing.plan_trim(self.words)
        self.assertEqual(cuts, [(1.05, 1.85)])            # the 1.1 s gap keeps 0.3 s; the 0.2 s gap is untouched

    def test_words_move_earlier_after_the_cut(self):
        cuts = pacing.plan_trim(self.words)
        out = pacing.shift_words(self.words, cuts)
        self.assertEqual(out[0], [0.1, 0.5, "One"])
        self.assertAlmostEqual(out[2][0], 2.0 - 0.8)
        gaps = [b[0] - a[1] for a, b in zip(out, out[1:])]
        self.assertLessEqual(max(gaps), pacing.MAX_GAP)

    def test_no_long_gaps_no_trim(self):
        tight = W((0, 0.4, "a"), (0.5, 0.9, "b"))
        self.assertEqual(pacing.plan_trim(tight), [])
        self.assertEqual(pacing.shift_words(tight, []), tight)


class CaptionTests(unittest.TestCase):
    words = W((0.1, 0.4, "RuneScape"), (0.4, 0.8, "players"), (0.8, 1.0, "just"), (1.0, 1.4, "removed"),
              (1.4, 2.5, "1.56"), (2.5, 3.0, "billion."), (3.5, 3.8, "Fuck!"))

    def test_two_or_three_words_per_line_never_across_a_sentence(self):
        lines = word_lines(self.words, 0.0, 10.0)
        self.assertEqual([len(l) for l in lines], [3, 3, 1])
        for line in lines:
            self.assertLessEqual(len(line), 3)
        flat = [[w for _, _, w in l] for l in lines]
        self.assertEqual(flat[1][-1], "billion.")           # the sentence ends a line

    def test_one_event_per_word_with_active_word_colour_and_pop(self):
        ev = pop_caption_events(self.words, 0.0, 10.0)
        self.assertEqual(len(ev), len(self.words))
        self.assertIn("&H00FFF000&", ev[0])                  # electric cyan on the spoken word
        self.assertIn(r"\fscx118", ev[0])                   # the pop
        self.assertIn("&H0000F2FF&", ev[4])                  # neon yellow for the number
        self.assertTrue(all("\pos(540," in e for e in ev))

    def test_profanity_masked_and_inside_safe_zone(self):
        text = "\n".join(pop_caption_events(self.words, 0.0, 10.0))
        self.assertNotIn("FUCK", text)
        self.assertIn("F***", text)
        y = int(next(e for e in pop_caption_events(self.words, 0.0, 10.0, y=1700).__iter__()).split("\pos(540,")[1].split(")")[0])
        self.assertLess(y, SAFE_BOTTOM)

    def test_hindi_uses_devanagari_font_style_size(self):
        from clipper.render import build_ass
        ass = build_ass({"hook": "x", "meme_caption": []}, self.words, 0.0, 10.0, "stack", "hi")
        self.assertRegex(ass, r"Style: Cap,(Nirmala UI|Mangal),")


class HookCloseTests(unittest.TestCase):
    def test_generic_outros_are_banned_but_twists_pass(self):
        self.assertTrue(closer_banned("Facts here. Thanks for watching!"))
        self.assertTrue(closer_banned("Facts here. Follow Game On for more."))
        self.assertTrue(closer_banned("Facts here. Watch this space."))
        self.assertFalse(closer_banned("Facts here. Would you pay for that?"))

    def test_greetings_and_filler_openers_are_invalid_hooks(self):
        self.assertFalse(score_hook("Hey gamers, RuneScape just changed").valid)
        self.assertFalse(score_hook("Breaking news today in RuneScape").valid)
        self.assertTrue(score_hook("Stop scrolling if you play RuneScape", ["RuneScape"]).valid)

    def test_restating_the_hook_is_detected(self):
        hook = "RuneScape players just removed 1.56 billion premium items"
        self.assertTrue(restates_hook(hook, "Jagex just watched 1.56 billion bought items vanish from RuneScape. More."))
        self.assertFalse(restates_hook(hook, "Anyone holding bought bonds can swap them for gold. More."))


class S:
    def __init__(self, title, summary="", minutes=0):
        from datetime import datetime, timedelta, timezone
        self.title, self.summary = title, summary
        self.published = datetime(2026, 1, 1, tzinfo=timezone.utc) - timedelta(minutes=minutes)
        self.score = 5
        self.story = self
        self.bucket = ""


class BucketTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify("Epic Games Store gives away Alan Wake 2 for free"), "giveaway")
        self.assertEqual(classify("Kai Cenat breaks Twitch subscriber record"), "streamer")
        self.assertEqual(classify("Team Liquid upset NaVi in the grand final"), "achievement")
        self.assertEqual(classify("Patch 1.2 adds a hidden mechanic"), "whats_new")
        self.assertIsNone(classify("A quiet Tuesday"))

    def test_balance_mixes_buckets_instead_of_taking_the_newest_forty(self):
        stories = [S(f"Patch {i} update adds changes", minutes=i) for i in range(10)]
        stories += [S("Free game giveaway on Epic", minutes=50), S("Streamer breaks viewers record", minutes=60),
                    S("Team wins world first final", minutes=70)]
        got = balance(stories, 6)
        kinds = {classify(f"{s.title} {s.summary}") for s in got}
        self.assertTrue({"giveaway", "streamer", "achievement", "whats_new"} <= kinds)

    def test_enforce_mix_caps_one_bucket(self):
        picks = []
        for i in range(4):
            p = S(f"story {i}")
            p.bucket, p.score = "whats_new", 9 - i
            picks.append(p)
        other = S("other")
        other.bucket, other.score = "giveaway", 1
        kept, dropped = enforce_mix(picks + [other], 2)
        self.assertEqual(len(kept), 3)
        self.assertEqual(len(dropped), 2)


if __name__ == "__main__":
    unittest.main()
