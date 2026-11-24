"""Run:  python -m unittest discover tests -v   (from the project root, in the venv)"""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from clipper import worker
from clipper.config import load_config
from clipper.detect import Moment
from clipper.profanity import mask

CFG = load_config()


def moment(ts):
    return Moment("twitch-x", "chat", ts, ts + 5, 100.0, 5.0, {})


class Words(unittest.TestCase):
    def write(self, data):
        f = Path(tempfile.mkdtemp()) / "t.json"
        f.write_text(json.dumps(data), encoding="utf-8")
        return f

    def tok(self, text, a, b):
        return {"text": text, "offsets": {"from": a, "to": b}}

    def test_tokens_merge_into_words(self):
        f = self.write({"result": {"language": "en"}, "transcription": [{
            "offsets": {"from": 0, "to": 2000}, "text": " Hello world,",
            "tokens": [self.tok("[_BEG_]", 0, 0), self.tok(" Hel", 0, 300), self.tok("lo", 300, 600),
                       self.tok(" world", 700, 1200), self.tok(",", 1200, 1250)]}]})
        lang, words = worker.parse_words(f)
        self.assertEqual(lang, "en")
        self.assertEqual(words, [[0.0, 0.6, "Hello"], [0.7, 1.25, "world,"]])

    def test_broken_bytes_fall_back_to_segment_split(self):
        f = self.write({"transcription": [{"offsets": {"from": 1000, "to": 3000}, "text": " नमस्ते दोस्तों",
                                           "tokens": [self.tok(" �", 1000, 1500), self.tok("�", 1500, 3000)]}]})
        _, words = worker.parse_words(f)
        self.assertEqual([w[2] for w in words], ["नमस्ते", "दोस्तों"])
        self.assertEqual(words[0][:2], [1.0, 2.0])

    def test_clamped_to_duration(self):
        f = self.write({"transcription": [{"offsets": {"from": 0, "to": 9000}, "text": " hi",
                                           "tokens": [self.tok(" hi", 8000, 9000)]}]})
        self.assertEqual(worker.parse_words(f, duration=5.0)[1], [[5.0, 5.0, "hi"]])


class Models(unittest.TestCase):
    def test_model_by_language(self):
        self.assertEqual(CFG.whisper_model_for("en"), CFG.whisper_model_en)
        self.assertEqual(CFG.whisper_model_for("en-US"), CFG.whisper_model_en)
        self.assertEqual(CFG.whisper_model_for("hi"), CFG.whisper_model_multi)
        self.assertEqual(CFG.whisper_model_for("pt"), CFG.whisper_model_multi)
        self.assertNotEqual(CFG.whisper_model_en, CFG.whisper_model_multi)

    def test_config(self):
        self.assertEqual((CFG.clip["context_before"], CFG.clip["context_after"], CFG.clip["workers"]), (75, 45, 2))


class Segments(unittest.TestCase):
    def test_spans_and_window(self):
        d = Path(tempfile.mkdtemp())
        base = 1_000_000.0
        for i in range(20):                       # seg i finished at base + 10*(i+1)
            f = d / f"seg_{i:05d}.ts"
            f.write_bytes(b"x")
            os.utime(f, (base + 10 * (i + 1),) * 2)
        spans = worker.segment_spans(d, 10)
        self.assertEqual((spans[0][1], spans[0][2]), (base, base + 10))
        self.assertEqual(spans[5][1], base + 50)
        # spike at +100s: window +25 .. +145 -> segments covering it (ends > 25, starts < 145)
        chosen = worker.pick_segments(spans, base + 25, base + 145)
        self.assertEqual([c[0].name for c in chosen][0], "seg_00002.ts")
        self.assertEqual(chosen[-1][0].name, "seg_00014.ts")
        self.assertEqual(worker.pick_segments(spans, base + 500, base + 600), [])


class Profanity(unittest.TestCase):
    def test_mask(self):
        self.assertEqual(mask("what the fuck, this is shit!"), "what the f***, this is s***!")
        self.assertEqual(mask("class assist"), "class assist")


class Pool(unittest.TestCase):
    def test_at_most_two_workers_and_waits_for_context_after(self):
        running, peak, done = [0], [0], []
        lock = threading.Lock()

        def fake_build(cfg, stream, session_dir, m, seg):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.3)
            with lock:
                running[0] -= 1
                done.append(time.time())

        cfg = load_config()
        cfg.raw["clip"] = {**cfg.clip, "context_after": 1}
        with mock.patch.object(worker, "build_clip", fake_build):
            pool = worker.ClipperPool(cfg)
            t0 = time.time()
            for _ in range(6):
                pool.submit({"platform": "twitch", "handle": "x"}, Path("."), moment(t0), 0)   # due at t0 + 1 + 0 + 1
            time.sleep(1.0)
            self.assertEqual(done, [])             # still waiting out context_after
            time.sleep(1.5)                        # now past due; close() would otherwise run them early
            pool.close()
        self.assertEqual(len(done), 6)
        self.assertEqual(peak[0], 2)
        self.assertGreaterEqual(min(done), t0 + 1.9)

    def test_close_runs_pending_moments_now(self):
        done = []
        cfg = load_config()
        with mock.patch.object(worker, "build_clip", lambda *a: done.append(1)):
            pool = worker.ClipperPool(cfg)
            pool.submit({"platform": "twitch", "handle": "x"}, Path("."), moment(time.time()), 10)  # due in ~56s
            pool.close()
        self.assertEqual(done, [1])


class Timeline(unittest.TestCase):
    def test_rows_filter_bots_and_cap(self):
        from datetime import datetime, timezone
        d = Path(tempfile.mkdtemp())
        t0 = 2_000_000.0
        lines = []
        for u, text, off in [("a", "lol", 1), ("a", "x", 2), ("a", "y", 3), ("b", "hi", 4), ("nightbot", "buy", 4),
                             ("c", "!cmd", 4), ("d", "pog", 7)]:
            ts = datetime.fromtimestamp(t0 + off, timezone.utc).isoformat(timespec="milliseconds")
            lines.append(json.dumps({"ts": ts, "username": u, "text": text}))
        (d / "chat.jsonl").write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
        with mock.patch.object(worker, "measure_loudness", lambda *a: ([-30.0] * 5 + [-10.0] * 5, 10.0)):
            rows = worker.build_timeline(d, d / "s.mp4", CFG, {"platform": "twitch", "handle": "x"}, t0, 10.0)
        self.assertEqual(rows[0], [0, 3, 2, 1, -30.0])   # a capped at 2, + b; one hype ("lol")
        self.assertEqual(rows[1], [5, 1, 1, 1, -10.0])


if __name__ == "__main__":
    unittest.main()
