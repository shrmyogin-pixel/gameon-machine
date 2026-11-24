"""Run:  python -m unittest discover tests -v   (from the project root, in the venv)"""
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2

from clipper import db, editor, render, routing, watermark
from clipper.config import PROJECT_ROOT, load_config

# 2026-11-24 12:00 UTC = 23:00 on 24 Nov in Melbourne (AEDT, UTC+11)
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def clip(i, score, platform="twitch", banger=False, sensitive=False, **extra):
    return {"id": i, "platform": platform, "ai_score": score, "banger": banger, "sensitive": sensitive,
            "pre_score": 0, **extra}


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def test_slot_times_are_the_next_melbourne_occurrence(self):
        tz = routing.ZoneInfo("Australia/Melbourne")
        got = {s["name"]: routing.next_slot_time(s, NOW, tz).isoformat(timespec="minutes") for s in self.cfg.slots}
        self.assertEqual(got["Slot 1 - US Peak"], "2026-11-25T10:00+11:00")
        self.assertEqual(got["Slot 2 - India Peak"], "2026-11-25T00:00+11:00")
        self.assertEqual(got["Slot 3 - Global Peak"], "2026-11-25T02:00+11:00")

    def test_top_three_go_to_youtube_one_per_slot_best_first_rest_round_robin(self):
        clips = [clip(1, 5), clip(2, 9), clip(3, 7), clip(4, 8), clip(5, 6), clip(6, 4), clip(7, 3), clip(8, 2)]
        plan = routing.plan(self.cfg, clips, NOW)
        yt = sorted((r["rank"], cid, r["slot"]) for cid, r in plan.items() if r["platform"] == "youtube")
        self.assertEqual(yt, [(1, 2, "Slot 1 - US Peak"), (2, 4, "Slot 2 - India Peak"), (3, 3, "Slot 3 - Global Peak")])
        others = [plan[cid]["platform"] for cid in (5, 1, 6, 7, 8)]               # ranks 4..8 in order
        self.assertEqual(others, ["tiktok", "instagram", "facebook", "tiktok", "instagram"])
        self.assertTrue(all(plan[c]["slot"] is None and plan[c]["at"] is None for c in (5, 1, 6, 7, 8)))
        self.assertEqual(plan[2]["at"], "2026-11-25T10:00+11:00")

    def test_banger_and_news_bonus_and_sensitive_penalty(self):
        self.assertGreater(routing.rank_key(clip(1, 7, banger=True)), routing.rank_key(clip(2, 8)))
        self.assertGreater(routing.rank_key(clip(1, 7.6, platform="news")), routing.rank_key(clip(2, 7.8)))
        plan = routing.plan(self.cfg, [clip(1, 10, sensitive=True), clip(2, 5), clip(3, 4), clip(4, 3)], NOW)
        self.assertNotEqual(plan[1]["platform"], "youtube")                         # sensitive never takes a headline slot
        self.assertEqual({plan[c]["platform"] for c in (2, 3, 4)}, {"youtube"})

    def test_fewer_clips_than_slots(self):
        plan = routing.plan(self.cfg, [clip(1, 6)], NOW)
        self.assertEqual(plan[1]["slot"], "Slot 1 - US Peak")
        self.assertEqual(routing.plan(self.cfg, [], NOW), {})

    def test_youtube_top_is_configurable(self):
        self.cfg.raw = {**self.cfg.raw, "routing": {"youtube_top": 2, "other_platforms": ["tiktok"]}}
        plan = routing.plan(self.cfg, [clip(i, 10 - i) for i in range(1, 6)], NOW)
        self.assertEqual(sum(r["platform"] == "youtube" for r in plan.values()), 2)
        self.assertEqual({r["platform"] for r in plan.values()} - {"youtube"}, {"tiktok"})

    def test_exported_clip_keeps_its_slot_so_a_better_clip_cannot_steal_it(self):
        locked = clip(1, 5, exported_at="2026-11-24 01:00:00", route_platform="youtube",
                      route_slot="Slot 1 - US Peak", route_at="2026-11-25T10:00+11:00", route_rank=1)
        plan = routing.plan(self.cfg, [locked, clip(2, 9), clip(3, 8), clip(4, 7)], NOW)
        self.assertEqual(plan[1]["slot"], "Slot 1 - US Peak")
        slots = {r["slot"] for cid, r in plan.items() if cid != 1 and r["platform"] == "youtube"}
        self.assertEqual(slots, {"Slot 2 - India Peak", "Slot 3 - Global Peak"})

    def test_assign_routes_saves_to_the_database_for_pending_clips_only(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            self.cfg.database = Path(tmp) / "c.sqlite"
            for i, (status, score) in enumerate([("rendered", 9), ("approved", 7), ("skipped", 10), ("posted", 10),
                                                 ("rendered", 5), ("rendered", 3)], start=1):
                db.add_clip(self.cfg.database, "twitch", "x", f"f{i}")
                db.record_analysis(self.cfg.database, f"f{i}", {"score": score, "title": "t"})
                db.set_clip_status(self.cfg.database, f"f{i}", status)
            result = routing.assign_routes(self.cfg, datetime.now(timezone.utc) + timedelta(seconds=5))
            self.assertEqual(sorted(result), [1, 2, 5, 6])                           # skipped / posted are left alone
            by_id = {c["id"]: c for c in db.list_clips(self.cfg.database)}
            self.assertEqual((by_id[1]["route_platform"], by_id[1]["route_slot"], by_id[1]["route_rank"]),
                             ("youtube", "Slot 1 - US Peak", 1))
            self.assertIsNone(by_id[3]["route_platform"])

    def test_day_start_is_melbourne_midnight_in_utc(self):
        self.assertEqual(routing.day_start_utc(self.cfg, NOW), "2026-11-23 13:00:00")


class VolumeCapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.cfg = load_config()
        self.cfg.database = Path(self.tmp.name) / "c.sqlite"
        self.cfg.raw = {**self.cfg.raw, "volume": {"live_max_per_day": 2, "live_cap_override_score": 9}}
        for i, status in enumerate(["rendered", "approved", "skipped", "failed"]):
            db.add_clip(self.cfg.database, "twitch", "x", f"f{i}")
            db.set_clip_status(self.cfg.database, f"f{i}", status)
        db.add_clip(self.cfg.database, "news", "IGN", "n1")
        db.set_clip_status(self.cfg.database, "n1", "rendered")

    def tearDown(self):
        self.tmp.cleanup()

    def result(self, score, banger=False):
        return {"post": True, "score": score, "banger": banger}

    def test_cap_counts_only_accepted_live_clips(self):
        self.assertTrue(editor.live_cap_reached(self.cfg, self.result(6)))         # 2 accepted live clips (news excluded)
        self.assertFalse(editor.live_cap_reached(self.cfg, self.result(9)))        # strong clips jump the queue
        self.assertFalse(editor.live_cap_reached(self.cfg, self.result(5, banger=True)))
        self.cfg.raw["volume"]["live_max_per_day"] = 3
        self.assertFalse(editor.live_cap_reached(self.cfg, self.result(6)))
        self.cfg.raw["volume"]["live_max_per_day"] = 0                              # 0 = no cap
        self.assertFalse(editor.live_cap_reached(self.cfg, self.result(1)))

    def test_decide_skips_when_capped(self):
        res = {**self.result(6), "reason": "ok"}
        self.assertEqual(editor.decide(self.cfg, res, capped=False), ("edited", None))
        status, why = editor.decide(self.cfg, res, capped=True)
        self.assertEqual(status, "skipped")
        self.assertIn("daily live quota", why)

    def test_shipped_defaults_match_the_15_a_day_plan(self):
        cfg = load_config()
        self.assertEqual(cfg.volume["daily_target"], 15)
        self.assertEqual(cfg.volume["news_per_day"] + cfg.volume["live_max_per_day"], 15)
        self.assertEqual(cfg.news["stories"], 4)
        # the scheduled runs can cover the daily news quota, and the quota (not the run count) is the cap
        self.assertGreaterEqual(cfg.news["stories"] * len(cfg.news["run_times"]), cfg.volume["news_per_day"])
        self.assertTrue(10 <= cfg.volume["daily_target"] <= 15)
        self.assertEqual(cfg.render["max_renders"], 1)
        self.assertTrue(cfg.raw["encoding"]["below_normal_priority"])
        self.assertFalse(cfg.raw["general"]["autopost"])
        self.assertTrue(2 <= cfg.raw["routing"]["youtube_top"] <= 3)


class WatermarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.cfg = load_config()
        self.cfg.raw = {**self.cfg.raw, "watermark": {**self.cfg.raw.get("watermark", {}), "file": str(self.root / "wm.png")}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_source_image_becomes_a_cropped_transparent_png(self):
        src = watermark.find_source(PROJECT_ROOT / "assets" / "watermark.jpg")      # finds watermark.JPG on any OS
        self.assertIsNotNone(src)
        out = watermark.build_watermark(src, self.root / "w.png")
        im = cv2.imread(str(out), cv2.IMREAD_UNCHANGED)
        self.assertEqual(im.shape[2], 4)
        a = im[..., 3]
        self.assertEqual([a[0, 0], a[0, -1], a[-1, 0], a[-1, -1]], [0, 0, 0, 0])    # corners are transparent
        self.assertEqual(a[a.shape[0] // 2, a.shape[1] // 2], 255)                  # the logo itself is solid
        self.assertTrue(0.3 < (a < 10).mean() < 0.5 and (a > 245).mean() > 0.4)

    def test_ensure_builds_once_and_degrades_gracefully(self):
        png = watermark.ensure_watermark(self.cfg)
        self.assertEqual(png, self.root / "wm.png")
        self.assertTrue(png.is_file())
        first = png.stat().st_mtime_ns
        self.assertEqual(watermark.ensure_watermark(self.cfg), png)
        self.assertEqual(png.stat().st_mtime_ns, first)                              # not rebuilt
        self.cfg.raw["watermark"] = {**self.cfg.raw["watermark"], "enabled": False}
        self.assertIsNone(watermark.ensure_watermark(self.cfg))
        self.cfg.raw["watermark"] = {"file": str(self.root / "none.png"), "source": str(self.root / "missing.jpg")}
        self.assertIsNone(watermark.ensure_watermark(self.cfg))                      # no image at all: render without it
        (self.root / "bad.jpg").write_bytes(b"not an image")
        self.cfg.raw["watermark"] = {"file": str(self.root / "bad.png"), "source": str(self.root / "bad.jpg")}
        self.assertIsNone(watermark.ensure_watermark(self.cfg))

    def test_placement_is_low_opacity_bottom_left_above_the_safe_zone(self):
        chain = watermark.overlay_filter(self.cfg)
        self.assertIn("colorchannelmixer=aa=0.4", chain)
        self.assertTrue(chain.startswith("[1:v]") and chain.endswith("[wm]"))
        self.assertTrue(watermark.overlay_filter(self.cfg, index=2).startswith("[2:v]"))
        pos = watermark.overlay_position(self.cfg, render.SAFE_BOTTOM, render.W)
        self.assertEqual(pos, f"overlay=x=36:y={render.SAFE_BOTTOM}-h-36")
        # the watermark's bottom edge is 36 px above the 1450 safe-zone line, and it sits under the captions
        s = watermark.settings(self.cfg)
        self.assertLess(render.SAFE_BOTTOM - 36, render.SAFE_BOTTOM)
        self.assertGreater(render.SAFE_BOTTOM - 36 - int(s["width"]), render.CAPTION_Y + 60)   # clear of caption text
        self.cfg.raw["watermark"] = {**self.cfg.raw["watermark"], "corner": "right"}
        self.assertEqual(watermark.overlay_position(self.cfg, render.SAFE_BOTTOM, render.W),
                         f"overlay=x={render.W}-w-36:y={render.SAFE_BOTTOM}-h-36")

    def test_filter_graph_puts_the_watermark_under_the_captions(self):
        wm = ("[1:v]format=rgba[wm]", "overlay=x=36:y=1450-h-36")
        for layout, cam in (("blur", None), ("stack", {"face": (74, 357, 56, 56), "frame": (1280, 720)})):
            g = render.build_filter(layout, (1280, 720), cam, Path("x.ass"), wm)
            self.assertLess(g.index("[wm]"), g.index("ass="))                         # overlay first, then captions
            self.assertTrue(g.endswith("format=yuv420p[v]"))
            self.assertEqual(len(re.findall(r"\[vs\]", g)), 2)
            plain = render.build_filter(layout, (1280, 720), cam, Path("x.ass"))
            self.assertNotIn("[wm]", plain)                                           # no watermark input -> unchanged graph


if __name__ == "__main__":
    unittest.main()
