"""Run:  python -m unittest discover tests -v   (from the project root, in the venv)"""
import json
import tempfile
import unittest
from pathlib import Path

from clipper import render
from clipper.config import load_config

WORDS = [[10.0, 10.3, "this"], [10.3, 10.6, "is"], [10.6, 11.0, "shit"], [11.1, 11.4, "so"],
         [11.4, 11.8, "good"], [11.8, 12.0, "man"], [30.0, 30.2, "late"], [2.0, 2.2, "early"]]
ANALYSIS = {"hook": "no way fuck", "meme_caption": ["Line one", "Line two"]}


class RenderTests(unittest.TestCase):
    def test_captions_three_words_masked_relative_and_clipped_to_cut(self):
        groups = render.caption_groups(WORDS, 10.0, 20.0)
        self.assertEqual([g[2] for g in groups], ["THIS IS S***", "SO GOOD MAN"])
        self.assertAlmostEqual(groups[0][0], 0.0)
        self.assertTrue(all(0 <= a < b <= 10.0 for a, b, _ in groups))

    def test_ass_has_uppercase_masked_hook_and_safe_zone_captions(self):
        for layout in ("stack", "blur"):
            ass = render.build_ass(ANALYSIS, WORDS, 10.0, 20.0, layout)
            self.assertIn("NO WAY F***", ass)
            self.assertNotIn("fuck", ass.lower())
            self.assertIn(f"\pos(540,{render.CAPTION_Y})", ass)
            self.assertEqual("Line one" in ass, layout == "blur")
        self.assertLess(render.CAPTION_Y, render.SAFE_BOTTOM)

    def test_facecam_crop_stays_inside_frame_with_panel_aspect(self):
        for face in [(74, 357, 56, 56), (1091, 286, 72, 72), (0, 0, 40, 40), (1240, 680, 40, 40)]:
            cw, ch, cx, cy = render.facecam_crop({"face": face, "frame": (1280, 720)})
            self.assertTrue(cx >= 0 and cy >= 0 and cx + cw <= 1280 and cy + ch <= 720)
            self.assertAlmostEqual(cw / ch, render.W / render.TOP_H, delta=0.05)

    def test_gameplay_crop_matches_bottom_panel(self):
        gw, gh, gx, gy = render.gameplay_crop(1280, 720)
        self.assertAlmostEqual(gw / gh, render.W / render.BOTTOM_H, delta=0.01)
        self.assertEqual(render.TOP_H + render.BOTTOM_H, render.H)

    def test_filters_end_in_1080x1920_pipeline(self):
        ass = Path("x.ass")
        stack = render.build_filter("stack", (1280, 720), {"face": (74, 357, 56, 56), "frame": (1280, 720)}, ass)
        blur = render.build_filter("blur", (1280, 720), None, ass)
        self.assertIn("vstack", stack)
        self.assertIn("gblur", blur)
        self.assertTrue(stack.endswith("[v]") and blur.endswith("[v]"))

    def test_audio_targets_minus_14(self):
        self.assertIn("volume=4.00dB", render.audio_filter({"input_i": -18.0}))
        self.assertIn("I=-14", render.audio_filter(None))

    def test_render_clip_skips_unapproved_and_fails_without_credit(self):
        cfg = load_config()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp = Path(tmp)
            cfg.database = tmp / "c.sqlite"
            folder = tmp / "f"
            folder.mkdir()
            (folder / "analysis.json").write_text(json.dumps({"status": "skipped", "post": False}), encoding="utf-8")
            self.assertEqual(render.render_clip(cfg, folder), "skipped")
            (folder / "analysis.json").write_text(json.dumps({"status": "edited", "post": True}), encoding="utf-8")
            (folder / "payload.json").write_text("{}", encoding="utf-8")
            self.assertEqual(render.render_clip(cfg, folder), "failed")
            self.assertFalse((folder / "clip.mp4").exists())

    def test_config_defaults(self):
        cfg = load_config()
        self.assertEqual(cfg.render["max_renders"], 1)
        self.assertEqual(cfg.ai["launch_min_score"], 4)


if __name__ == "__main__":
    unittest.main()
