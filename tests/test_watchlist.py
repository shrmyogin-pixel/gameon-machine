"""Watchlist research, hook bank, source check and post-render QA. No network: yt-dlp and article pages are mocked."""
import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from clipper import db, factcheck, hookbank, news, qa, watchlist
from clipper.config import load_config
from clipper.lifecycle import clean_link

NOW = datetime(2026, 11, 28, 1, 0, tzinfo=timezone.utc)
CREDIT = "Sources & Credits: IGN, https://ign.com/1"


def entry(n, title, views):
    return {"id": f"video{n:06d}", "title": title, "view_count": views, "channel": "Chan"}


LISTING = [entry(1, "Elden Ring Nightreign trailer breakdown", 900_000), entry(2, "Quiet weekly show", 40_000),
           entry(3, "Another quiet show", 50_000), entry(4, "Third quiet show", 45_000),
           entry(5, "Sponsored handheld, use code GAME", 5_000_000), entry(6, "Fourth quiet show", 42_000)]


def story(title, summary="", link="https://ign.com/1", sid=1, trend=None):
    return news.Story(sid, "IGN", title, summary, link, NOW, trend=trend)


def pick(source_title, script, summary="", headline="A headline", trend=None):
    from types import SimpleNamespace as NS
    return NS(story=story(source_title, summary, trend=trend), script=script, headline=headline, title=headline,
              description="", hook=NS(total=5.0))


class Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = load_config()
        self.cfg.data_dir, self.cfg.database = self.root, self.root / "c.sqlite"
        self.bank = self.root / "hook_bank.json"
        self.bank.write_text(json.dumps({"hooks": [
            {"id": "tr_1", "bucket": "trailers", "pattern": "The new [Game] trailer", "times_used": 0,
             "approved_count": 0, "rejected_count": 0, "performance_notes": "mine"},
            {"id": "gta_1", "bucket": "gta_6", "pattern": "GTA 6 [clue]", "times_used": 0}]}), encoding="utf-8")
        self.watch = self.root / "watchlist.json"
        self.watch.write_text(json.dumps({"scan_settings": {"request_delay_seconds": 0}, "buckets": {"trailers": [
            {"id": "a", "name": "A", "type": "channel_url", "url": "https://www.youtube.com/@a/videos"}]}}),
            encoding="utf-8")

    def entry_of(self, hook_id="tr_1"):
        return next(h for h in json.loads(self.bank.read_text(encoding="utf-8"))["hooks"] if h["id"] == hook_id)


class WatchlistTests(Temp):
    def scan(self, fetch, **kw):
        with mock.patch.object(watchlist, "fetch_metadata", fetch):
            return watchlist.scan(self.cfg, path=self.watch, **kw)

    def test_outliers_are_found_cached_and_promotional_items_dropped(self):
        calls = []
        fetch = lambda url, limit: calls.append(url) or LISTING
        found = self.scan(fetch, now=NOW)
        self.assertEqual([o["title"] for o in found], ["Elden Ring Nightreign trailer breakdown"])
        self.assertEqual(found[0]["url"], "https://www.youtube.com/watch?v=video000001")
        self.scan(fetch, now=NOW + timedelta(hours=1))
        self.assertEqual(len(calls), 1)                                   # cached
        self.scan(fetch, now=NOW + timedelta(hours=7))
        self.assertEqual(len(calls), 2)                                   # older than the cache life

    def test_recent_duplicates_are_left_out_and_a_failed_source_is_not_fatal(self):
        db.init_db(self.cfg.database)
        db.add_topics(self.cfg.database, ["elden ring"], "Elden Ring Nightreign gets new trailer")
        self.assertEqual(self.scan(lambda u, n: LISTING, now=datetime.now(timezone.utc)), [])

        def boom(url, limit):
            raise OSError("blocked")
        self.assertEqual(self.scan(boom, refresh=True), [])

    def test_metadata_only_is_enforced_and_listing_never_downloads(self):
        from clipper import videosearch
        ydl = mock.MagicMock()
        ydl.__enter__.return_value = ydl
        ydl.extract_info.return_value = {"entries": []}
        with mock.patch.object(videosearch, "_ydl", return_value=ydl) as make:
            watchlist.fetch_metadata("https://www.youtube.com/@a/videos", 12)
        self.assertTrue(make.call_args.args[0]["skip_download"])
        self.assertFalse(ydl.extract_info.call_args.kwargs["download"])
        self.watch.write_text(json.dumps({"scan_settings": {"mode": "download"}, "buckets": {}}), encoding="utf-8")
        self.assertIsNone(watchlist.load(self.watch))

    def test_the_shipped_watchlist_covers_every_bucket_without_promo(self):
        wl = watchlist.load()
        self.assertEqual(set(wl["buckets"]), set(watchlist.BUCKETS))
        self.assertTrue(all(watchlist.source_url(s) for sources in wl["buckets"].values() for s in sources))
        self.assertEqual(watchlist.MODE_BUCKETS["promo"], ())

    def test_stories_are_paired_and_the_outlier_title_never_reaches_the_writer(self):
        found = [{"title": "Elden Ring Nightreign SECRET ENDING", "ratio": 4.0, "bucket": "trailers"}]
        stories = [story("Elden Ring Nightreign shows a new trailer"), story("Zelda patch out now", sid=2)]
        watchlist.annotate(self.cfg, stories, found)
        self.assertEqual((stories[0].watch["hook_id"], stories[0].watch["ratio"]), ("tr_1", 4.0))
        self.assertIsNone(stories[1].watch)
        block = watchlist.prompt_block(stories)
        self.assertIn("pattern tr_1", block)
        self.assertNotIn("SECRET ENDING", block)

    def test_the_followed_pattern_is_recorded_on_the_pick(self):
        from tests.test_news import reply, story as news_story
        s = news_story(1)
        s.watch = {"bucket": "gta_6", "hook_id": "gta_1", "pattern": "p", "ratio": None}
        picks = news.validate_reply(json.dumps({"stories": [{**reply(1), "hook_pattern": "gta_1"}]}), [s], self.cfg)
        follows = picks[0].hook.hook == reply(1)["hooks"][0]
        self.assertEqual(picks[0].hook_pattern, "gta_1" if follows else "")
        picks = news.validate_reply(json.dumps({"stories": [{**reply(1), "hook_pattern": "made_up"}]}), [s], self.cfg)
        self.assertEqual(picks[0].hook_pattern, "")


class HookBankTests(Temp):
    def clip(self):
        db.init_db(self.cfg.database)
        db.add_clip(self.cfg.database, "news", "IGN", "n1")
        db.set_hook(self.cfg.database, "n1", "tr_1")
        return db.clip_by_folder(self.cfg.database, "n1")

    def act(self, action):
        hookbank.feedback(self.cfg, db.clip_by_folder(self.cfg.database, "n1"), action)
        e = self.entry_of()
        return e.get("approved_count"), e.get("rejected_count")

    def test_each_clip_counts_once_and_follows_the_last_decision(self):
        self.clip()
        self.assertEqual(self.act("approve"), (1, 0))
        self.assertEqual(self.act("uploaded"), (1, 0))
        self.assertEqual(self.act("undo"), (0, 0))
        self.assertEqual(self.act("reject"), (0, 1))
        notes = self.entry_of()["performance_notes"].splitlines()
        self.assertEqual(notes[0], "mine")
        self.assertEqual(len(notes), 2)
        self.assertIn("rejected", notes[1])

    def test_use_is_counted_and_a_missing_or_broken_bank_is_harmless(self):
        self.assertTrue(hookbank.record_use(self.cfg, "tr_1"))
        self.assertEqual(self.entry_of()["times_used"], 1)
        self.bank.write_text("{broken", encoding="utf-8")
        self.assertFalse(hookbank.record_use(self.cfg, "tr_1"))
        self.assertEqual(self.bank.read_text(encoding="utf-8"), "{broken")    # never overwritten
        self.bank.unlink()
        self.assertFalse(hookbank.record_use(self.cfg, "tr_1"))

    def test_dashboard_reject_counts_and_still_deletes_the_files(self):
        import http.client
        import threading
        from clipper import review
        self.cfg.clips_dir = self.root / "clips"
        self.clip()
        (self.cfg.clips_dir / "n1").mkdir(parents=True)
        db.set_clip_status(self.cfg.database, "n1", "rendered")
        cid = db.clip_by_folder(self.cfg.database, "n1")["id"]
        server = review.make_server(self.cfg, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("POST", f"/api/clips/{cid}/status", json.dumps({"action": "reject"}),
                         {"Content-Type": "application/json"})
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(self.entry_of()["rejected_count"], 1)
        self.assertFalse((self.cfg.clips_dir / "n1").exists())


class FactCheckTests(Temp):
    def check(self, p, page=""):
        with mock.patch.object(factcheck, "article_text", return_value=page):
            return factcheck.check(self.cfg, p)[0]

    def test_numbers_and_months_must_be_in_the_source(self):
        p = pick("Game costs $70 at launch", "It costs $70 and arrives March 12.")
        self.assertEqual(len(self.check(p)), 2)                           # the day and the month are not there
        self.assertEqual(self.check(p, page="It launches Mar 12."), [])
        self.assertTrue(self.check(pick("Game gets a price", "It costs $5 now.")))
        self.assertEqual(self.check(pick("Twelve maps arrive", "You get 12 maps and may love 1 of them.")), [])

    def test_a_rumour_must_stay_a_rumour(self):
        self.assertTrue(self.check(pick("Leak: new map for the game", "The new map is real.")))
        self.assertEqual(self.check(pick("Leak: new map for the game", "A leak shows a new map.")), [])

    def test_a_failed_check_stops_before_anything_is_made(self):
        p = pick("Game news", "It costs $999.")
        self.cfg.clips_dir = self.root / "clips"
        with mock.patch.object(factcheck, "article_text", return_value=""), \
                mock.patch.object(news, "synthesize") as voice, mock.patch.object(news, "collect_clips") as broll:
            with self.assertRaisesRegex(news.NewsError, "source check failed"):
                news.make_short(self.cfg, p)
        voice.assert_not_called()
        broll.assert_not_called()
        self.assertFalse(self.cfg.clips_dir.exists())

    def test_affiliate_parameters_leave_the_credit_link(self):
        self.assertEqual(clean_link("https://x.com/a?id=2&aff_id=9&utm_source=y"), "https://x.com/a?id=2")
        self.assertEqual(clean_link("https://x.com/a?id=2"), "https://x.com/a?id=2")


class QaTests(Temp):
    def video(self, size):
        out = self.root / "clip.mp4"
        subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"testsrc2=s={size}:r=30:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)], check=True)
        return out

    def test_a_good_short_passes_and_problems_are_named(self):
        self.assertEqual(qa.check(self.cfg, self.video("1080x1920"), f"x\n{CREDIT}"), [])
        warnings = qa.check(self.cfg, self.video("640x360"), "no credit here")
        self.assertIn("640x360", warnings[0])
        self.assertIn("Sources & Credits:", warnings[1])
        bad = self.root / "clip.mp4"
        bad.write_bytes(b"not a video")
        self.assertTrue(qa.check(self.cfg, bad, CREDIT))

    def test_warnings_reach_the_row_the_dashboard_reads(self):
        folder = self.root / "n1"
        folder.mkdir()
        db.add_clip(self.cfg.database, "news", "IGN", "n1")
        db.init_db(self.cfg.database)
        self.assertIsNone(db.clip_by_folder(self.cfg.database, "n1")["qa_warnings"])
        qa.record(self.cfg, folder, CREDIT)
        row = db.list_clips(self.cfg.database)[0]
        self.assertEqual(row["qa_warnings"], ["clip.mp4 is missing or empty"])


if __name__ == "__main__":
    unittest.main()
