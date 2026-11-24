"""Run:  python -m unittest discover tests -v   (from the project root, in the venv)"""
import math
import random
import shutil
import struct
import subprocess
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from clipper.detect import (SAMPLE_RATE, StreamDetector, build_params, loudness_per_second,
                            measure_loudness, replay, SpikeMonitor)

T0 = 1_000_000.0


def det(has_chat=True, **over):
    p = build_params({}, over)
    return StreamDetector("test", p, T0, has_chat=has_chat)


def quiet_chat(d, buckets, per_bucket=3, start=0):
    """Steady background chat: `per_bucket` different users per 5s bucket."""
    for b in range(start, start + buckets):
        for i in range(per_bucket):
            d.add_chat(T0 + b * 5 + i, f"user{b}_{i}", "hello there")


def burst(d, bucket, users, text="LOL omg"):
    for i in range(users):
        d.add_chat(T0 + bucket * 5 + 1, f"fan{bucket}_{i}", text)


class ChatSpike(unittest.TestCase):
    def test_clear_spike_fires_with_numbers(self):
        d = det()
        quiet_chat(d, 30)
        burst(d, 30, 25)
        ms = d.advance(T0 + 31 * 5 + 5)
        self.assertEqual(len(ms), 1)
        m = ms[0]
        self.assertEqual(m.kind, "chat")
        self.assertEqual(m.numbers["count"], 25)
        self.assertEqual(m.numbers["unique"], 25)
        self.assertGreater(m.prescore, 4)
        self.assertEqual(m.numbers["word_msgs"], 25)
        self.assertIn("count=25", m.describe())

    def test_no_fire_in_first_12_buckets(self):
        d = det()
        quiet_chat(d, 5)
        burst(d, 5, 30)
        self.assertEqual(d.advance(T0 + 200), [])
        d = det()
        quiet_chat(d, 12)
        burst(d, 12, 30)          # 13th bucket: allowed
        self.assertEqual(len(d.advance(T0 + 200)), 1)

    def test_min_messages_blocks(self):
        d = det()
        quiet_chat(d, 30, per_bucket=1)
        burst(d, 30, 8)           # big jump but under 10
        self.assertEqual(d.advance(T0 + 300), [])
        d = det(min_messages=6)   # per-stream override lets it through
        quiet_chat(d, 30, per_bucket=1)
        burst(d, 30, 8)
        self.assertEqual(len(d.advance(T0 + 300)), 1)

    def test_per_user_cap_and_unique_ratio(self):
        d = det()
        quiet_chat(d, 30)
        for i in range(40):       # 3 people spamming: capped at 2 each = 6 messages
            d.add_chat(T0 + 151, f"spammer{i % 3}", "LOL")
        self.assertEqual(d.advance(T0 + 300), [])
        d = det()
        quiet_chat(d, 30)
        for i in range(40):       # 12 people x 2 = 24 counted msgs, ratio exactly 0.5 passes
            d.add_chat(T0 + 151, f"p{i % 12}", "LOL")
        ms = d.advance(T0 + 300)
        self.assertEqual(len(ms), 1)
        self.assertEqual(ms[0].numbers["count"], 24)
        self.assertAlmostEqual(ms[0].numbers["unique_ratio"], 0.5)
        d = det(min_unique_ratio=0.6)   # same chat, stricter ratio -> blocked
        quiet_chat(d, 30)
        for i in range(40):
            d.add_chat(T0 + 151, f"p{i % 12}", "LOL")
        self.assertEqual(d.advance(T0 + 300), [])

    def test_bots_and_commands_ignored(self):
        d = det()
        quiet_chat(d, 30)
        for i in range(30):
            d.add_chat(T0 + 151, "Nightbot", "buy stuff")
            d.add_chat(T0 + 151, f"u{i}", "!discord")
            d.add_chat(T0 + 151, "sery_bot", "hi")
        self.assertEqual(d.advance(T0 + 300), [])
        self.assertEqual(d.baseline[30], 0)

    def test_must_beat_sigma(self):
        d = det()
        rnd = random.Random(1)       # noisy chat: 8..16 per bucket
        for b in range(40):
            for i in range(rnd.randint(8, 16)):
                d.add_chat(T0 + b * 5, f"n{b}_{i}", "hey")
        burst(d, 40, 17)            # 17 >= min_messages but not mean + 2.5 sd
        self.assertEqual(d.advance(T0 + 400), [])

    def test_cooldown_90s(self):
        d = det()
        quiet_chat(d, 30)
        burst(d, 30, 25)            # fires at +150s
        burst(d, 32, 40)            # +160s: inside cooldown, suppressed
        burst(d, 49, 45)            # +245s: 95s later, allowed
        ms = d.advance(T0 + 1000)
        self.assertEqual([m.offset for m in ms], [150.0, 245.0])

    def test_no_chat_reader_means_no_chat_spike(self):
        d = det(has_chat=False)
        quiet_chat(d, 30)
        burst(d, 30, 30)
        self.assertEqual(d.advance(T0 + 400), [])

    def test_words_raise_score(self):
        def run(text):
            d = det()
            quiet_chat(d, 30)
            burst(d, 30, 25, text=text)
            return d.advance(T0 + 400)[0].prescore
        self.assertGreater(run("LMAO no way pog"), run("ok then"))


class Audio(unittest.TestCase):
    def feed(self, d, seconds, loud_at=(), base=-30.0, loud=-10.0):
        vals = [(loud if i in loud_at else base) + random.Random(i).uniform(-1, 1) for i in range(seconds)]
        d.add_audio(T0, vals)

    def test_audio_alone_needs_two_seconds(self):
        d = det(has_chat=False)
        self.feed(d, 120, loud_at={80})         # one loud second only
        self.assertEqual(d.advance(T0), [])
        d = det(has_chat=False)
        self.feed(d, 120, loud_at={80, 81})
        ms = d.advance(T0)
        self.assertEqual(len(ms), 1)
        self.assertEqual(ms[0].kind, "audio")
        self.assertEqual(ms[0].offset, 80.0)
        self.assertGreaterEqual(ms[0].prescore, 3.0)

    def test_audio_alone_not_when_chat_present(self):
        d = det(has_chat=True)
        self.feed(d, 120, loud_at={80, 81})
        self.assertEqual(d.advance(T0), [])

    def test_audio_alone_respects_cooldown(self):
        d = det(has_chat=False)
        self.feed(d, 200, loud_at={80, 81, 120, 121, 190, 191})
        self.assertEqual([m.offset for m in d.advance(T0)], [80.0, 190.0])

    def test_audio_boosts_chat_spike(self):
        def run(loud):
            d = det()
            quiet_chat(d, 30)
            self.feed(d, 160, loud_at=set(range(150, 155)) if loud else set())
            burst(d, 30, 25)
            return d.advance(T0 + 400)[0]
        plain, boosted = run(False), run(True)
        self.assertEqual(plain.kind, "chat")
        self.assertEqual(boosted.kind, "chat+audio")
        self.assertGreaterEqual(boosted.numbers["audio_z"], 1.5)
        self.assertGreater(boosted.prescore, plain.prescore)


class Params(unittest.TestCase):
    def test_stream_override_and_bad_values(self):
        p = build_params({"min_messages": 12}, {"min_messages": 4})
        self.assertEqual(p["min_messages"], 4)
        p = build_params({"min_messages": "lots", "score": {"weight_chat": 1.0}})
        self.assertEqual(p["min_messages"], 10)
        self.assertEqual(p["score"]["weight_chat"], 1.0)
        self.assertEqual(p["score"]["weight_audio"], 2.5)
        p = build_params({}, {"detect": {"cooldown_seconds": 30}})
        self.assertEqual(p["cooldown_seconds"], 30)


class RealAudio(unittest.TestCase):
    def test_loudness_pcm(self):
        quiet = struct.pack("<%dh" % SAMPLE_RATE, *([100] * SAMPLE_RATE))
        loud = struct.pack("<%dh" % SAMPLE_RATE, *([16000] * SAMPLE_RATE))
        db, dur = loudness_per_second(quiet + loud + b"\x00\x00" * 10)
        self.assertEqual(len(db), 2)
        self.assertLess(db[0], db[1])
        self.assertAlmostEqual(db[1], 20 * math.log10(16000 / 32768), places=1)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_segment_decode_and_replay(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            sess = tmp / "twitch-test" / "20260101-000000"
            sess.mkdir(parents=True)
            seg = sess / "seg_00000.ts"   # 3s quiet tone then 2s loud tone
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=5,volume='if(lt(t,3),0.02,0.8)':eval=frame",
                            "-f", "mpegts", str(seg)], check=True)
            res = measure_loudness("ffmpeg", seg)
            self.assertIsNotNone(res)
            db, dur = res
            self.assertAlmostEqual(dur, 5.0, delta=0.5)
            self.assertGreaterEqual(len(db), 4)
            self.assertGreater(db[-1] - db[0], 15)
            self.assertEqual(replay(sess, "twitch-test", build_params({}), "ffmpeg"), [])  # too short to warm up
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class MonitorThread(unittest.TestCase):
    def test_monitor_reads_chat_file_and_writes_moments(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            started = time.time() - 400
            lines = []
            def msg(t, u, text):
                ts = datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds")
                lines.append(json.dumps({"ts": ts, "username": u, "text": text}))
            for b in range(30):
                for i in range(3):
                    msg(started + b * 5 + i, f"u{b}_{i}", "hi")
            for i in range(25):
                msg(started + 30 * 5 + 1, f"fan{i}", "LOL pog")
            (tmp / "chat.jsonl").write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
            stop = threading.Event()
            handed = []
            mon = SpikeMonitor(tmp, "twitch-test", build_params({}), "ffmpeg", started, stop,
                               chat_alive=lambda: True, on_moment=handed.append)
            mon.start()
            time.sleep(1.5)
            stop.set()
            mon.join(10)
            self.assertFalse(mon.is_alive())
            rows = [json.loads(x) for x in (tmp / "moments.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["kind"], "chat")
            self.assertEqual(rows[0]["numbers"]["count"], 25)
            self.assertEqual([m.numbers["count"] for m in handed], [25])   # handed on to the clipper worker
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
