"""Run:  python -m unittest discover tests -v   (from the project root, in the venv)"""
import tempfile
import unittest
from pathlib import Path

from clipper import db
from clipper.clean import run_clean
from clipper.config import load_config


class CleanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.cfg = load_config()
        self.cfg.database = root / "c.sqlite"
        self.cfg.clips_dir = root / "clips"
        db.init_db(self.cfg.database)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, name, status, days_old, folder=True):
        if folder:
            (self.cfg.clips_dir / name).mkdir(parents=True)
            (self.cfg.clips_dir / name / "clip.mp4").write_bytes(b"x" * 1000)
        db.add_clip(self.cfg.database, "twitch", "lacy", name)
        col = db.CLEAN_AGE_COLUMN.get(status, "updated_at")
        with db.connect(self.cfg.database) as conn:
            conn.execute(f"UPDATE clips SET status = ?, {col} = datetime('now', ?), created_at = datetime('now', ?)"
                         " WHERE folder = ?", (status, f"-{days_old} days", f"-{days_old} days", name))

    def folders(self):
        return {c["folder"] for c in db.list_clips(self.cfg.database)}

    def test_deletes_only_what_is_old_enough(self):
        self.make("skip-old", "skipped", 3)
        self.make("skip-new", "skipped", 1)
        self.make("rej-old", "rejected", 3)
        self.make("rej-new", "rejected", 1)
        self.make("post-old", "posted", 8)
        self.make("post-mid", "posted", 5)          # posted clips live 7 days, not 2
        self.make("appr-old", "approved", 30)       # never touched
        self.make("rend-old", "rendered", 30)
        self.make("fail-old", "failed", 30)
        res = run_clean(self.cfg)
        self.assertEqual(sorted(c["folder"] for c in res.deleted), ["post-old", "rej-old", "skip-old"])
        self.assertEqual(res.freed_bytes, 3000)
        self.assertEqual(self.folders(), {"skip-new", "rej-new", "post-mid", "appr-old", "rend-old", "fail-old"})
        for gone in ("skip-old", "rej-old", "post-old"):
            self.assertFalse((self.cfg.clips_dir / gone).exists())
        for kept in ("skip-new", "post-mid", "appr-old"):
            self.assertTrue((self.cfg.clips_dir / kept).is_dir())

    def test_dry_run_deletes_nothing(self):
        self.make("skip-old", "skipped", 3)
        res = run_clean(self.cfg, dry_run=True)
        self.assertEqual(len(res.deleted), 1)
        self.assertTrue((self.cfg.clips_dir / "skip-old").is_dir())
        self.assertEqual(self.folders(), {"skip-old"})

    def test_missing_folder_still_removes_row(self):
        self.make("ghost", "rejected", 3, folder=False)
        self.assertEqual(len(run_clean(self.cfg).deleted), 1)
        self.assertEqual(self.folders(), set())

    def test_never_deletes_outside_clips_dir(self):
        outside = Path(self.tmp.name) / "precious"
        outside.mkdir()
        (outside / "keep.txt").write_text("x")
        self.make("evil", "skipped", 3, folder=False)
        with db.connect(self.cfg.database) as conn:
            conn.execute("UPDATE clips SET folder = ? WHERE folder = 'evil'", ("..\\precious",))
        res = run_clean(self.cfg)
        self.assertEqual((len(res.deleted), len(res.failed)), (0, 1))
        self.assertTrue((outside / "keep.txt").is_file())


if __name__ == "__main__":
    unittest.main()
