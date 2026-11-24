"""Run:  python -m unittest discover tests -v   (from the project root, in the venv)"""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from clipper import db, notify, runner, status
from clipper.config import load_config


class FakeRec:
    """Stands in for Recorder: a thread that can be made to 'die'."""
    made = 0

    def __init__(self, stream, cfg=None, stop=None, pool=None):
        FakeRec.made += 1
        self.name_ = f"{stream['platform']}-{stream['handle']}"
        self.stream, self.stop, self.active_dir = stream, stop, None
        self.alive = False

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        pass


class SupervisorTests(unittest.TestCase):
    def test_backoff_grows_and_caps(self):
        self.assertEqual([runner.backoff(n) for n in range(7)], [5, 10, 20, 40, 80, 120, 120])

    def test_dead_recorder_is_restarted_after_backoff_and_others_untouched(self):
        stop = threading.Event()
        made = lambda s: FakeRec(s)
        a, b = FakeRec({"platform": "twitch", "handle": "a"}), FakeRec({"platform": "twitch", "handle": "b"})
        a.start(); b.start()
        slots = [runner.Slot({"platform": "twitch", "handle": "a"}, a), runner.Slot({"platform": "twitch", "handle": "b"}, b)]
        a.alive = False                                           # a's thread dies
        self.assertEqual(runner.supervise(slots, made, stop, now=100.0), 0)   # first pass only schedules
        self.assertEqual(slots[0].retry_at, 105.0)
        self.assertEqual(runner.supervise(slots, made, stop, now=104.0), 0)   # still backing off
        self.assertEqual(runner.supervise(slots, made, stop, now=105.0), 1)
        self.assertIsNot(slots[0].rec, a)
        self.assertTrue(slots[0].rec.is_alive())
        self.assertIs(slots[1].rec, b)                            # untouched
        self.assertEqual(slots[0].total_restarts, 1)
        slots[0].rec.alive = False                                # dies again: longer back-off
        runner.supervise(slots, made, stop, now=110.0)
        self.assertEqual(slots[0].retry_at, 120.0)

    def test_factory_failure_does_not_crash_supervisor(self):
        stop = threading.Event()
        dead = FakeRec({"platform": "twitch", "handle": "a"})
        slot = runner.Slot({"platform": "twitch", "handle": "a"}, dead)
        runner.supervise([slot], lambda s: 1 / 0, stop, now=0.0)
        self.assertEqual(runner.supervise([slot], lambda s: 1 / 0, stop, now=5.0), 0)
        self.assertEqual(slot.retry_at, 15.0)                     # scheduled a later retry instead of raising

    def test_no_restart_while_stopping(self):
        stop = threading.Event()
        stop.set()
        slot = runner.Slot({"platform": "twitch", "handle": "a"}, FakeRec({"platform": "twitch", "handle": "a"}))
        self.assertEqual(runner.supervise([slot], FakeRec, stop, now=1e6), 0)
        self.assertIsNone(slot.retry_at)

    def test_run_writes_heartbeat_then_cleans_up(self):
        cfg = load_config()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg.data_dir = Path(tmp)
            pool = mock.Mock()
            seen = {}

            def peek(*a, **k):
                seen["state"] = json.loads(runner.state_path(cfg).read_text(encoding="utf-8"))
            with mock.patch.object(runner, "TICK_SECONDS", 0.1), mock.patch.object(runner, "keep_awake", return_value=True) as ka:
                orig = runner.write_state

                def spy(*a, **k):
                    orig(*a, **k)
                    if not k.get("stopping"):
                        peek()
                with mock.patch.object(runner, "write_state", side_effect=spy):
                    rc = runner.run(cfg, [{"platform": "twitch", "handle": "x", "url": "u"}], seconds=0.5,
                                    recorder_cls=FakeRec, pool_cls=lambda c: pool)
            self.assertEqual(rc, 0)
            self.assertEqual(seen["state"]["pid"], os.getpid())
            self.assertEqual(seen["state"]["streams"][0]["name"], "twitch-x")
            self.assertEqual([c.args[0] for c in ka.call_args_list], [True, False])   # awake on, released on exit
            pool.close.assert_called_once()
            self.assertFalse(runner.state_path(cfg).exists())

    def test_keep_awake_returns_bool(self):
        self.assertIsInstance(runner.keep_awake(True), bool)
        self.assertIsInstance(runner.keep_awake(False), bool)


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.cfg = load_config()
        self.cfg.data_dir = root
        self.cfg.database = root / "clips.sqlite"
        self.cfg.sessions_dir = root / "sessions"
        self.cfg.log_file = root / "logs" / "clipper.log"

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_install_renders(self):
        text = status.render(self.cfg)
        for word in ("Runner: not running", "Clip queue:", "new", "skipped", "rendered", "approved", "rejected",
                     "posted", "failed", "Sessions", "Disk", "Recent errors", "none"):
            self.assertIn(word, text)

    def test_counts_sessions_disk_errors_and_failed_clips(self):
        for i, st in enumerate(["rendered", "rendered", "failed", "posted", "edited"]):
            db.add_clip(self.cfg.database, "twitch", "x", f"f{i}")
            db.set_clip_status(self.cfg.database, f"f{i}", st, "render: boom" if st == "failed" else None)
        now = time.time()
        live = self.cfg.sessions_dir / "twitch-x" / "20260101-000000"
        live.mkdir(parents=True)
        (live / "seg_00000.ts").write_bytes(b"x" * 2048)
        done = self.cfg.sessions_dir / "twitch-y" / "20260101-000001"
        done.mkdir(parents=True)
        (done / "seg_00000.ts").write_bytes(b"y")
        (done / "session.json").write_text(json.dumps({"chat_messages": 42, "end_reason": "stream ended"}), encoding="utf-8")
        crashed = self.cfg.sessions_dir / "twitch-z" / "20260101-000002"
        crashed.mkdir(parents=True)
        (crashed / "seg_00000.ts").write_bytes(b"z")
        os.utime(crashed / "seg_00000.ts", (now - 600, now - 600))
        os.utime(crashed, (now - 600, now - 600))
        (self.cfg.sessions_dir / "twitch-empty" / "20260101-000003").mkdir(parents=True)
        os.utime(self.cfg.sessions_dir / "twitch-empty" / "20260101-000003", (now - 600, now - 600))
        (self.cfg.sessions_dir / "_chat-tests" / "twitch-q").mkdir(parents=True)
        self.cfg.log_file.parent.mkdir()
        self.cfg.log_file.write_text("2026-01-01 10:00:00,123 INFO fine\n2026-01-01 10:00:01,000 ERROR [x] clip failed\n"
                                     "Traceback (most recent call last):\n  boom\n"
                                     "2026-01-01 10:00:02,000 WARNING careful\n", encoding="utf-8")
        text = status.render(self.cfg)
        self.assertRegex(text, r"rendered\s+2")
        self.assertRegex(text, r"failed\s+1")
        self.assertRegex(text, r"posted\s+1")
        self.assertRegex(text, r"in-progress\s+1")
        self.assertRegex(text, r"total\s+5")
        self.assertRegex(text, r"RECORDING\s+twitch-x")
        self.assertRegex(text, r"closed\s+twitch-y.*chat 42.*stream ended")
        self.assertRegex(text, r"interrupted\s+twitch-z")
        self.assertNotIn("twitch-empty", text)
        self.assertNotIn("_chat-tests", text)
        self.assertLess(text.index("RECORDING"), text.index("closed"))          # active first
        self.assertIn("[2026-01-01 10:00:01] [x] clip failed", text)
        self.assertNotIn("careful", text)                                        # warnings are not errors
        self.assertIn("f2: render: boom", text)
        self.assertIn("sessions", text)

    def test_runner_status_from_heartbeat(self):
        self.assertFalse(status.runner_info(self.cfg)["running"])
        beat = {"pid": os.getpid(), "started": "now", "heartbeat": time.time(), "streams": [
            {"name": "twitch-x", "alive": True, "recording": True, "restarts": 2}]}
        runner.state_path(self.cfg).write_text(json.dumps(beat), encoding="utf-8")
        text = status.render(self.cfg)
        self.assertIn("RUNNING", text)
        self.assertIn("twitch-x: recording, restarted 2x", text)
        beat["heartbeat"] = time.time() - 300                                    # stale heartbeat
        runner.state_path(self.cfg).write_text(json.dumps(beat), encoding="utf-8")
        self.assertIn("did not stop cleanly", status.render(self.cfg))
        beat.update(heartbeat=time.time(), pid=2 ** 22 + 12345)                  # fresh but process gone
        runner.state_path(self.cfg).write_text(json.dumps(beat), encoding="utf-8")
        self.assertIn("not running", status.render(self.cfg))

    def test_human_size(self):
        self.assertEqual(status.human_size(0), "0 B")
        self.assertEqual(status.human_size(1536), "1.5 KB")
        self.assertEqual(status.human_size(5 * 1024 ** 3), "5.0 GB")


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.clip = {"handle": "lacy", "platform": "twitch", "folder": "f", "title": "Big clutch", "ai_score": 7.0,
                     "banger": False, "sensitive": False}

    def test_threshold_and_banger(self):
        self.assertFalse(notify.should_notify(self.cfg, self.clip))
        self.assertTrue(notify.should_notify(self.cfg, {**self.clip, "ai_score": 8.0}))
        self.assertTrue(notify.should_notify(self.cfg, {**self.clip, "banger": True}))
        self.assertFalse(notify.should_notify(self.cfg, {**self.clip, "ai_score": None}))
        self.cfg.raw["notify"] = {"enabled": False}
        self.assertFalse(notify.should_notify(self.cfg, {**self.clip, "banger": True, "ai_score": 10}))

    def test_wording(self):
        title, body = notify.describe({**self.clip, "banger": True, "ai_score": 9.5, "sensitive": True})
        self.assertIn("Banger", title)
        self.assertIn("9.5", title)
        self.assertIn("lacy", body)
        self.assertIn("SENSITIVE", body)
        self.assertIn("High-score", notify.describe({**self.clip, "ai_score": 8})[0])

    def test_falls_back_when_toast_unavailable_or_errors(self):
        with mock.patch.object(notify, "send_toast", return_value=False):
            self.assertEqual(notify.notify("t", "b"), "fallback")
        with mock.patch.object(notify, "send_toast", side_effect=RuntimeError("boom")):
            self.assertEqual(notify.notify("t", "b"), "fallback")
        with mock.patch.object(notify, "_powershell", return_value=None):
            self.assertFalse(notify.send_toast("t", "b"))
        with mock.patch.object(notify, "_powershell", return_value="no-such-powershell.exe"):
            self.assertFalse(notify.send_toast("t", "b"))

    def test_text_goes_through_environment_not_the_script(self):
        evil = "x'; Remove-Item -Recurse C:\\ #"
        with mock.patch.object(notify.subprocess, "run") as run, mock.patch.object(notify, "_powershell", return_value="ps"), \
                mock.patch.object(notify.sys, "platform", "win32"):
            run.return_value = mock.Mock(returncode=0, stderr="")
            self.assertTrue(notify.send_toast(evil, "body"))
        args, kwargs = run.call_args
        self.assertNotIn(evil, " ".join(args[0]))
        self.assertEqual(kwargs["env"]["CLIPPER_TOAST_TITLE"], evil)

    def test_notify_rendered_only_fires_for_strong_clips(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            self.cfg.database = Path(tmp) / "c.sqlite"
            for name, score, banger in (("weak", 5, False), ("strong", 9, False), ("fire", 5, True)):
                db.add_clip(self.cfg.database, "twitch", "lacy", name)
                db.record_analysis(self.cfg.database, name, {"score": score, "banger": banger, "title": name})
            sent = []
            done = threading.Event()

            def fake(title, body):
                sent.append(body)
                if len(sent) == 2:
                    done.set()
            with mock.patch.object(notify, "notify", side_effect=fake):
                for name in ("weak", "strong", "fire", "missing"):
                    notify.notify_rendered(self.cfg, name)
                done.wait(5)
            self.assertEqual(sorted(b.split(": ")[1] for b in sent), ["fire", "strong"])


if __name__ == "__main__":
    unittest.main()
