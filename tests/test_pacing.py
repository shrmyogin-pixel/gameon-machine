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
        self.assertTrue(all(r"\pos(540," in e for e in ev))

    def test_profanity_masked_and_inside_safe_zone(self):
        text = "\n".join(pop_caption_events(self.words, 0.0, 10.0))
        self.assertNotIn("FUCK", text)
        self.assertIn("F***", text)
        y = int(next(e for e in pop_caption_events(self.words, 0.0, 10.0, y=1700).__iter__()).split(r"\pos(540,")[1].split(")")[0])
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
    def test_classify_covers_all_six_buckets(self):
        for text, bucket in (("Epic Games Store gives away Alan Wake 2 for free", "freebie"),
                             ("GTA 6 easter egg hides in the second trailer", "flagship"),
                             ("Skate glitch launches players into orbit", "glitch"),
                             ("Team Liquid upset NaVi in the grand final", "creator"),
                             ("Nvidia brings DLSS to a new handheld", "tech"),
                             ("Patch 1.2 adds a photo mode", "update")):
            self.assertEqual(classify(text), bucket, text)
        self.assertEqual(classify("Patch notes: bug fixes and a free update"), "update")     # not a glitch, not a giveaway
        self.assertIsNone(classify("A quiet Tuesday"))

    def test_balance_mixes_buckets_instead_of_taking_the_newest_forty(self):
        stories = [S(f"Patch {i} update adds changes", minutes=i) for i in range(10)]
        others = ["Free game giveaway on Epic", "Zelda easter egg found", "A wild physics glitch", "Streamer hits a milestone",
                  "New GPU benchmarks land"]
        stories += [S(t, minutes=50 + i) for i, t in enumerate(others)]
        got = balance(stories, 6)
        self.assertEqual({classify(f"{s.title} {s.summary}") for s in got},
                         {"flagship", "freebie", "update", "glitch", "creator", "tech"})

    def test_enforce_mix_caps_one_bucket(self):
        picks = []
        for i in range(4):
            p = S(f"story {i}")
            p.bucket, p.score = "update", 9 - i
            picks.append(p)
        other = S("other")
        other.bucket, other.score = "freebie", 1
        kept, dropped = enforce_mix(picks + [other], 2)
        self.assertEqual(len(kept), 3)
        self.assertEqual(len(dropped), 2)


if __name__ == "__main__":
    unittest.main()


class PictureFilterTests(unittest.TestCase):
    def test_web_page_screenshots_are_rejected_but_art_is_kept(self):
        import cv2
        import numpy as np
        from clipper import newsimage, imagesearch
        page = np.full((900, 1600, 3), 245, np.uint8)
        cv2.putText(page, "Choose a subscription", (100, 200), cv2.FONT_HERSHEY_SIMPLEX, 3, (30, 30, 30), 6)
        art = np.zeros((900, 1600, 3), np.uint8)
        art[..., 0], art[..., 1], art[..., 2] = np.linspace(0, 255, 1600), 80, np.linspace(255, 0, 900)[:, None]
        bright_art = np.full((900, 1600, 3), (230, 160, 40), np.uint8)       # bright but colourful
        self.assertTrue(newsimage.looks_like_web_page(page))
        self.assertFalse(newsimage.looks_like_web_page(art))
        self.assertFalse(newsimage.looks_like_web_page(bright_art))
        data = cv2.imencode(".jpg", page)[1].tobytes()
        self.assertIsNone(newsimage.save_picture(data, __import__("pathlib").Path("unused.jpg")))
        self.assertTrue(imagesearch.looks_like_ui("PlayStation Plus subscription pricing"))
        self.assertTrue(imagesearch.looks_like_ui("PS Plus logo"))
        self.assertFalse(imagesearch.looks_like_ui("Nioh 3 screenshot boss fight"))


class SharpAndUniquePictureTests(unittest.TestCase):
    def test_low_res_blurry_and_regraded_duplicates_are_refused(self):
        import tempfile
        from pathlib import Path
        import cv2
        import numpy as np
        from clipper import newsimage
        from tests.test_imagesearch import photo

        def save(data, known=()):
            with tempfile.TemporaryDirectory() as tmp:
                return newsimage.save_picture(data, Path(tmp) / "p.jpg", list(known))

        def encode(picture):
            return cv2.imencode(".jpg", picture)[1].tobytes()

        sharp = photo(1280, 720, seed=3)
        img = cv2.imdecode(np.frombuffer(sharp, np.uint8), cv2.IMREAD_COLOR)
        first = save(sharp)
        self.assertIsNotNone(first)
        self.assertIsNone(save(photo(600, 338, seed=3)))                                  # a related-article thumbnail
        self.assertIsNone(save(encode(cv2.GaussianBlur(img, (0, 0), 8))))                 # out of focus
        self.assertIsNone(save(encode(cv2.convertScaleAbs(img, alpha=0.8, beta=30)), [first["hash"]]))   # same shot, regraded
        self.assertTrue(newsimage.big_enough(720, 1280))                                  # portrait pictures still count


class CuriosityHookTests(unittest.TestCase):
    def test_curiosity_hooks_outscore_plain_headline_hooks(self):
        plain = score_hook("Nioh 3 DLC 2 just got a new date", ["Nioh 3"])
        curious = score_hook("Stop scrolling if you play Nioh 3", ["Nioh 3"])
        self.assertEqual(plain.parts["curiosity"], 0.0)
        self.assertGreater(curious.total, plain.total)
        self.assertLessEqual(plain.total, 7.0)


class EightToTenPicturesTests(unittest.TestCase):
    def test_every_picture_shows_before_any_returns(self):
        from clipper import news_render
        for total, n in ((36.97, 10), (36.97, 9), (36.0, 8), (30.5, 8)):
            order = [i for i, s in news_render.plan_segments(total, n)]
            self.assertEqual(order[:n], list(range(n)), (total, n))
            self.assertTrue(all(2.0 - 1e-6 <= s <= 2.5 + 1e-6 for _, s in news_render.plan_segments(total, n)))

    def test_defaults_ask_for_eight_to_ten_pictures(self):
        from clipper.config import load_config
        n = load_config().news
        self.assertEqual((n["images_min"], n["images_max"]), (8, 10))


class DeferredNameHookTests(unittest.TestCase):
    subjects = ["Fortnite"]

    def test_curiosity_gap_without_the_name_beats_a_dry_headline(self):
        gap = score_hook("One streamer just broke a record nobody thought was possible", self.subjects)
        dry = score_hook("Fortnite just got a new update today", self.subjects)
        self.assertGreater(gap.total, dry.total)
        self.assertIn("withheld on purpose", " ".join(gap.reasons))

    def test_a_nameless_hook_without_a_real_gap_is_still_penalised(self):
        weak = score_hook("This just changed everything for gamers", self.subjects)
        self.assertEqual(weak.parts["name_early"], 0.0)

    def test_name_starting_in_the_first_six_words_counts_as_early(self):
        self.assertEqual(score_hook("Stop scrolling if you play Fortnite Chapter 7", ["Fortnite Chapter 7"]).parts["name_early"], 4.0)

    def test_names_subject(self):
        from clipper.hookscore import names_subject
        self.assertTrue(names_subject("Epic gave Fortnite players a record", self.subjects))
        self.assertFalse(names_subject("One streamer broke a record", self.subjects))
