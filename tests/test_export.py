"""Run:  python -m unittest discover tests -v   (from the project root, in the venv)"""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from clipper import db, export
from clipper.config import load_config

CREDIT = "Sources & Credits: Twitch - lacy (https://twitch.tv/lacy)"
MP4 = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 20
DAY = date(2026, 11, 24)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.cfg = load_config()
        self.cfg.database = root / "c.sqlite"
        self.cfg.clips_dir = root / "clips"
        self.cfg.raw = {**self.cfg.raw, "paths": {**self.cfg.raw.get("paths", {}), "exports_dir": str(root / "exports")}}
        self.out = self.cfg.exports_dir / "2026-11-24"

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, name="twitch-lacy-1", status="approved", title="Insane clutch: shit just got real!",
             description=None, handle="lacy", thumb=True, video=True, hashtags=("#lacy", "fortnite", "#f u c k"),
             tags=("fortnite", "clutch", "lacy clips"), sensitive=False):
        folder = self.cfg.clips_dir / name
        folder.mkdir(parents=True)
        if video:
            (folder / "clip.mp4").write_bytes(MP4)
        if thumb:
            (folder / "thumb.jpg").write_bytes(b"\xff\xd8thumb")
        (folder / "analysis.json").write_text(json.dumps({"hashtags": list(hashtags)}), encoding="utf-8")
        db.add_clip(self.cfg.database, "twitch", handle, name, stream="https://twitch.tv/lacy")
        db.record_analysis(self.cfg.database, name, {
            "score": 8, "banger": True, "sensitive": sensitive, "sensitive_reason": "mild" if sensitive else "",
            "title": title, "hook": "NO WAY", "meme_caption": ["a", "b"], "tags": list(tags),
            "description_full": CREDIT if description is None else description, "credit_line": CREDIT,
            "start": 1, "end": 31})
        db.record_files(self.cfg.database, name, folder)
        db.set_clip_status(self.cfg.database, name, status)
        return self.by_folder(name)

    def by_folder(self, name):
        return next(c for c in db.list_clips(self.cfg.database, limit=100) if c["folder"] == name)

    def export(self, **kw):
        return export.run_export(self.cfg, today=DAY, **kw)

    # ---------------------------------------------------------------- names
    def test_slug_and_base_name(self):
        self.assertEqual(export.slugify("Insane clutch: shit!! 🔥 Ünïcode / ..\\evil"), "insane-clutch-shit-unicode-evil")
        clip = {"id": 7, "platform": "youtube", "handle": "@CarryMinati", "title": "गजब का मोमेंट"}
        self.assertEqual(export.base_name(clip), "youtube-carryminati-clip-7")      # no ASCII title -> id
        clip["title"] = "CON"
        self.assertEqual(export.base_name(clip), "youtube-carryminati-con")        # prefixed, so never a reserved name
        clip["title"] = "x" * 300
        self.assertLessEqual(len(export.base_name(clip)), export.MAX_NAME)
        for bad in ('a<b>c:"d"/e\\f|g?h*', "..", "   "):
            slug = export.slugify(bad)
            self.assertRegex(slug, r"^[a-z0-9-]*$")

    # ---------------------------------------------------------------- happy path
    def test_exports_package_and_marks_exported(self):
        clip = self.make(description=f"Lacy clutches.\nWatch lacy live: u\n{CREDIT}")
        res = self.export()
        self.assertTrue(res.ok)
        self.assertEqual(len(res.exported), 1)
        stem = export.base_name(clip)
        self.assertTrue(stem.startswith("twitch-lacy-insane-clutch-s"))           # masked title -> "s***" -> "s"
        for ext in (".mp4", ".jpg", ".txt"):
            self.assertTrue((self.out / (stem + ext)).is_file(), ext)
        self.assertEqual((self.out / (stem + ".mp4")).read_bytes(), MP4)
        self.assertEqual((self.out / (stem + ".jpg")).read_bytes(), b"\xff\xd8thumb")
        self.assertEqual(sorted(p.suffix for p in self.out.iterdir()), [".jpg", ".mp4", ".txt"])   # no .part leftovers
        after = self.by_folder("twitch-lacy-1")
        self.assertIsNotNone(after["exported_at"])
        self.assertEqual(after["status"], "approved")                              # posting is the human's step
        self.assertTrue(after["export_path"].endswith(stem + ".mp4"))
        self.assertEqual(self.export().exported, [])                                # not exported twice

    def test_sheet_contents(self):
        self.make(description=f"Lacy clutches.\nWatch lacy live: u\n{CREDIT}")
        self.export()
        sheet = next(self.out.glob("*.txt")).read_text(encoding="utf-8-sig")
        title = sheet.split("TITLE\n" + export.SHEET_RULE + "\n")[1].split("\n")[0]
        self.assertEqual(title, "Insane clutch: s*** just got real!")             # profanity masked
        desc = sheet.split("DESCRIPTION")[1].split("HASHTAGS")[0]
        desc_lines = [l for l in desc.splitlines() if l.strip() and not set(l) <= {"="} and "paste as is" not in l]
        self.assertEqual(desc_lines[-1], CREDIT)                                   # description ends with the credit
        self.assertIn("Lacy clutches.", desc)
        hashtag_block = sheet.split("HASHTAGS\n" + export.SHEET_RULE + "\n")[1].split("\n")[0]
        self.assertEqual(hashtag_block, "#lacy #fortnite #f***")                   # '#'-prefixed, spaces gone, masked
        self.assertIn("#lacy #fortnite #f***", desc)                                # hashtags sit above the credit line
        self.assertLess(desc.index("#lacy"), desc.index("Sources & Credits:"))
        self.assertIn("fortnite, clutch, lacy clips", sheet)
        self.assertNotIn("shit", sheet.lower())
        self.assertNotIn("fuck", sheet.lower())
        self.assertIn("NOT FOR PASTING", sheet)
        self.assertIn("(banger)", sheet)

    def test_sensitive_note_and_hindi_roundtrip(self):
        self.make(title="गजब का मोमेंट", description=f"क्या खेल है\n{CREDIT}", hashtags=("हिंदी", "#गेमिंग"),
                  tags=("हिंदी", "गेमिंग"), sensitive=True)
        self.export()
        txt = next(self.out.glob("*.txt"))
        self.assertTrue(txt.name.startswith("twitch-lacy-clip-"))
        raw = txt.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))                            # BOM for Notepad
        sheet = raw.decode("utf-8-sig")
        self.assertIn("गजब का मोमेंट", sheet)
        self.assertIn("#हिंदी #गेमिंग", sheet)                                      # matras are kept
        self.assertIn("REVIEW NOTE", sheet)

    # ---------------------------------------------------------------- refusals
    def test_refuses_missing_credit_line(self):
        for bad in ("Just text, no credit", "", f"{CREDIT}\nstuff after it", "Sources & Credits:",
                    "Sources & Credits:   "):
            with self.subTest(desc=bad[:25]):
                self.setUp()
                clip = self.make(description=bad)
                res = self.export()
                self.assertEqual(res.exported, [])
                self.assertEqual(len(res.refused), 1, res.refused)
                self.assertIn("Sources & Credits", res.refused[0][1])
                self.assertFalse(self.out.exists() and any(self.out.iterdir()))     # nothing was copied at all
                self.assertIsNone(self.by_folder("twitch-lacy-1")["exported_at"])
                self.tearDown()
        self.setUp()

    def test_refuses_missing_video_and_wrong_status_and_unknown_id(self):
        self.make("a", video=False)
        self.make("b", status="rendered")
        self.make("c", status="posted")
        res = self.export(ids=[self.by_folder("a")["id"], self.by_folder("b")["id"],
                               self.by_folder("c")["id"], 999])
        self.assertEqual(res.exported, [])
        reasons = {c["id"]: r for c, r in res.refused}
        self.assertIn("clip.mp4 is missing", reasons[self.by_folder("a")["id"]])
        self.assertIn("only approved", reasons[self.by_folder("b")["id"]])
        self.assertIn("only approved", reasons[self.by_folder("c")["id"]])
        self.assertIn("no clip with that id", reasons[999])
        self.assertFalse(self.out.exists() and any(self.out.iterdir()))

    def test_one_bad_clip_does_not_stop_the_others(self):
        self.make("bad", description="no credit here")
        good = self.make("good", title="Good one")
        res = self.export()
        self.assertEqual([c["folder"] for c, _ in res.exported], ["good"])
        self.assertEqual([c["folder"] for c, _ in res.refused], ["bad"])
        self.assertFalse(res.ok)

    def test_clip_folder_outside_clips_dir_is_refused(self):
        clip = self.make("x")
        with db.connect(self.cfg.database) as conn:
            conn.execute("UPDATE clips SET folder = ? WHERE id = ?", ("..\\..\\elsewhere", clip["id"]))
        res = self.export()
        self.assertEqual(res.exported, [])
        self.assertEqual(len(res.refused), 1)

    # ---------------------------------------------------------------- selection, re-export, collisions
    def test_all_and_id_reexport_replace_instead_of_duplicate(self):
        clip = self.make()
        self.export()
        before = sorted(p.name for p in self.out.iterdir())
        self.assertEqual(self.export().exported, [])
        self.assertEqual(len(self.export(include_all=True).exported), 1)
        self.assertEqual(len(self.export(ids=[clip["id"]]).exported), 1)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), before)      # same names, no '-2' copies

    def test_same_title_different_clips_get_distinct_names(self):
        self.make("one", title="Same title")
        self.make("two", title="Same title")
        res = self.export()
        names = sorted(p.stem for p in self.out.glob("*.mp4"))
        self.assertEqual(len(res.exported), 2)
        self.assertEqual(names, ["twitch-lacy-same-title", "twitch-lacy-same-title-2"])

    def test_dry_run_copies_nothing(self):
        self.make()
        res = self.export(dry_run=True)
        self.assertEqual(len(res.exported), 1)
        self.assertFalse(self.cfg.exports_dir.exists())
        self.assertIsNone(self.by_folder("twitch-lacy-1")["exported_at"])

    def test_missing_thumbnail_still_exports(self):
        self.make(thumb=False)
        self.assertEqual(len(self.export().exported), 1)
        self.assertEqual(sorted(p.suffix for p in self.out.iterdir()), [".mp4", ".txt"])
        self.assertIn("Thumbnail:   (none)", next(self.out.glob("*.txt")).read_text(encoding="utf-8-sig"))

    def test_tag_and_hashtag_limits(self):
        tags = [f"tag-number-{i:03d}-xxxxxxxx" for i in range(60)]
        fitted = export.fit_tags(tags)
        self.assertLessEqual(len(", ".join(fitted)), export.MAX_TAGS_CHARS)
        self.assertEqual(fitted, tags[:len(fitted)])                                # whole tags, in order
        clip = self.make(hashtags=[f"h{i}" for i in range(40)])
        self.assertEqual(len(export.hashtags_for(clip, self.cfg.clips_dir / "twitch-lacy-1")), export.MAX_HASHTAGS)

    def test_hashtags_fall_back_to_tags_without_analysis(self):
        clip = self.make()
        (self.cfg.clips_dir / "twitch-lacy-1" / "analysis.json").unlink()
        self.assertEqual(export.hashtags_for(clip, self.cfg.clips_dir / "twitch-lacy-1"),
                         ["#fortnite", "#clutch", "#lacyclips"])


if __name__ == "__main__":
    unittest.main()
