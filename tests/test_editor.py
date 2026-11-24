"""Run:  python -m unittest discover tests -v   (from the project root, in the venv)"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clipper import editor
from clipper.config import load_config
from clipper.db import add_clip
from clipper.editor import EditorError, validate

GOOD = {"post": True, "score": 7, "banger": False, "sensitive": False, "sensitive_reason": "",
        "start": 5.0, "end": 30.0, "title": "Fortnite with lacy: shit loot", "hook": "NO WAY",
        "meme_caption": ["One", "Two"], "description": "Lacy finds it.",
        "tags": [f"t{i}" for i in range(8)], "hashtags": ["#a", "b"], "reason": "fun"}
ARGS = (60.0, "Sources & Credits: Twitch - lacy (u)", "lacy", "u")


def v(**over):
    return validate(json.dumps({**GOOD, **over}), *ARGS)


class EditorTests(unittest.TestCase):
    def test_valid_masks_profanity_and_adds_credit(self):
        r = v()
        self.assertIn("s***", r["title"])
        self.assertNotIn("shit", r["title"])
        self.assertTrue(r["description_full"].splitlines()[-1].startswith("Sources & Credits:"))
        self.assertEqual(r["hashtags"], ["#a", "#b"])

    def test_invalid_replies_rejected(self):
        for bad in ["not json", "```json\n{}\n```", "[]", json.dumps({**GOOD, "score": "7"}),
                    json.dumps({**GOOD, "post": 1}), json.dumps({**GOOD, "tags": ["x"]})]:
            with self.subTest(bad=bad[:30]), self.assertRaises(EditorError):
                validate(bad, *ARGS)

    def test_range_checks(self):
        for over in ({"end": 4.0}, {"end": 200.0}, {"hook": "ONE"}, {"sensitive": True}, {"score": 11}):
            with self.subTest(over=over), self.assertRaises(EditorError):
                v(**over)

    def test_edit_clip_never_raises_and_marks_failed(self):
        cfg = load_config()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp = Path(tmp)
            cfg.database = tmp / "c.sqlite"
            folder = tmp / "twitch-x-1"
            folder.mkdir()
            add_clip(cfg.database, "twitch", "x", folder.name)
            (folder / "frames.jpg").write_bytes(b"x")
            (folder / "payload.json").write_text(json.dumps({
                "stream": {"platform": "twitch", "handle": "x", "url": "u", "credit_line": "c"},
                "moment": {"spike_at_s": 5}, "clip": {"duration_s": 60}, "timeline": [],
                "transcript": {"words": []}}), encoding="utf-8")
            with mock.patch.object(editor, "call_claude", return_value="prose, not json") as m:
                self.assertEqual(editor.edit_clip(cfg, folder), "failed")
            self.assertEqual(m.call_count, cfg.ai["retries"] + 1)
            self.assertFalse((folder / "analysis.json").exists())


if __name__ == "__main__":
    unittest.main()
