"""The channel rules written down in .claude/skills/gameon-pipeline/SKILL.md, checked against the code and config."""
import unittest

from clipper import news, render
from clipper.buckets import BUCKETS, badge_label
from clipper.config import PROJECT_ROOT, load_config


class LockedPipelineRules(unittest.TestCase):
    def test_render_settings(self):
        cfg = load_config()
        self.assertEqual((render.W, render.H), (1080, 1920))
        self.assertIn("h264_qsv", cfg.raw["encoding"]["encoders"])
        self.assertTrue(cfg.raw["encoding"]["below_normal_priority"])
        self.assertEqual(cfg.render["max_renders"], 1)
        self.assertFalse(cfg.raw["general"]["autopost"])

    def test_video_comes_from_official_publishers_only(self):
        from clipper import videosearch as v
        cfg = load_config()
        self.assertTrue(cfg.news["official_only"])
        official = {v.channel_key(n) for n in cfg.news["official_channels"]}
        for channel in ("Rockstar Games", "PlayStation", "Nintendo of America", "Bethesda Softworks", "NVIDIA GeForce"):
            self.assertTrue(v.is_official({"channel": channel}, "Some Game", official), channel)
        for channel in ("IGN", "GameSpot", "GameTrailers", "Kai Cenat Live", "Indie Games Hub", "PlayStation Fan",
                        "GTA Games"):
            self.assertFalse(v.is_official({"channel": channel}, "GTA 6", official), channel)
        self.assertTrue(v.is_official({"channel": "Tin Coffins"}, "Tin Coffins", official))
        self.assertFalse(v.is_official({"channel": "Kai Cenat"}, "Kai Cenat", official, "creator"))

    def test_the_video_search_applies_it(self):
        from pathlib import Path
        from unittest import mock
        from clipper import videosearch as v
        cfg = load_config()
        asked = {}

        def hits(topic, kind, exclude="", official=None):
            asked["official"] = official
            return []
        with mock.patch.object(v, "youtube_sources", hits), mock.patch.object(v, "steam_sources", return_value=[]), \
                mock.patch.object(v, "steam_app", return_value=(1, "Hades 3", {"publishers": ["Tiny Publisher LLC"]})), \
                mock.patch.object(v, "commons_sources") as commons:
            v.fetch_clips(cfg, ["Hades 3"], Path("."), Path("no-recordings"), 5)
        self.assertIn("tiny publisher", asked["official"])
        commons.assert_not_called()

    def test_tech_footage_must_show_the_product_not_just_the_company(self):
        from types import SimpleNamespace as NS
        from clipper import videosearch as v
        for company in ("Lenovo", "Nvidia", "Steam", "PlayStation"):
            self.assertEqual(v.product_words(company), [], company)
        self.assertTrue(v.names_product("Lenovo Legion Go Beam", "Introducing the Legion Go Beam"))
        for title in ("Lenovo Yoga Book 9i", "Lenovo Center Meet-up", "Legion Go S trailer"):
            self.assertFalse(v.names_product("Legion Go Beam", title), title)
        self.assertTrue(v.names_product("Steam Deck", "Steam Deck: the new model"))
        self.assertEqual(news.picture_topic(NS(bucket="tech", subjects=["Lenovo", "Legion Go Beam"])), "Legion Go Beam")
        self.assertEqual(news.picture_topic(NS(bucket="update", subjects=["Warframe", "Tau"])), "Warframe")

    def test_captions_spell_the_script_not_the_transcription(self):
        script = "PCGamesN's writer sounds hopeful - about Tau."
        heard = [[0.0, 0.5, "Gamison's"], [0.5, 0.8, "writer"], [0.8, 1.1, "sounds"], [1.1, 1.5, "hopeful"],
                 [1.6, 1.9, "about"], [1.9, 2.3, "Tao."]]
        out = news.spell_from_script(heard, script)
        self.assertEqual(" ".join(w[2] for w in out), script)
        self.assertEqual(out[1][:2], [0.5, 0.8])
        for words in (heard[1:], heard[:2] + heard[3:], heard[:-1], [[0, 3, "x"]],
                      heard[:2] + [[0.8, 0.8, "um"]] + heard[2:]):
            out = news.spell_from_script(words, script)
            self.assertEqual(" ".join(w[2] for w in out), script, words)
            self.assertTrue(all(a <= b for a, b, _ in out))
            self.assertTrue(all(x[0] <= y[0] for x, y in zip(out, out[1:])), out)

    def test_each_bucket_has_its_pill(self):
        self.assertEqual([badge_label(b, subject="GTA 6") for b in BUCKETS],
                         ["GTA 6", "FREE GAME", "PATCH UPDATE", "WILD BUG", "CREATOR NEWS", "GAMING TECH"])
        self.assertEqual(badge_label("flagship", subject="Elden Ring"), "BIG GAME")
        self.assertEqual(badge_label("", "free_offer"), "FREE GAME")
        self.assertEqual(badge_label(""), "BREAKING NEWS")

    def test_the_pill_reaches_the_subtitle_file(self):
        from clipper.news_render import build_news_ass
        ass = build_news_ass({"hook": "Your car can fly now", "outlet": "IGN", "bucket": "glitch"}, [[0.0, 0.5, "fly"]], 5.0)
        self.assertTrue(any(",Kicker," in line and line.endswith("WILD BUG") for line in ass.splitlines()))

    def test_skill_file_is_there(self):
        text = (PROJECT_ROOT / ".claude" / "skills" / "gameon-pipeline" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: gameon-pipeline\n"))


class UnsafeStoryTests(unittest.TestCase):
    def test_piracy_cheat_scam_and_gambling_are_skipped_but_normal_news_is_not(self):
        for bad in ("GTA 6 torrent download is live", "Free V-Bucks generator no verification", "Best aimbot for Warzone",
                    "New mod menu gives unlimited cash", "Top casino sites for gamers", "FitGirl repack drops today"):
            self.assertTrue(news.HARD_SKIP.search(bad), bad)
        for good in ("GTA 6 money glitch found by players", "Anti-cheat update bans thousands", "Cheat codes return in GTA 6",
                     "A torrent of complaints hits the patch", "Free Steam game: claim it this week"):
            self.assertIsNone(news.HARD_SKIP.search(good), good)

    def test_betting_idioms_are_news_but_real_wagering_is_skipped(self):
        for idiom in ("\"I'm betting on the sickos\" - studio plans to expand the scope of RPGs", "Bungie's big gamble pays off",
                      "Sony is betting big on handhelds", "I bet you missed this secret"):
            self.assertIsNone(news.HARD_SKIP.search(idiom), idiom)
        for real in ("Best esports betting sites this week", "Team signs betting sponsor", "Pro banned for betting on his own matches",
                     "CS2 skin gambling crackdown", "New sportsbook offers a bonus", "Players placed bets on the final",
                     "Skin betting promo code inside"):
            self.assertTrue(news.HARD_SKIP.search(real), real)

    def test_a_hook_that_copies_the_headline_is_detected(self):
        title = "Exoprimal is shutting down in 2027, Capcom confirms"
        self.assertTrue(news.copies_headline(title, "Exoprimal is shutting down for good", ["Exoprimal"]))
        self.assertFalse(news.copies_headline(title, "Own Exoprimal? Your clock is ticking", ["Exoprimal"]))
        self.assertFalse(news.copies_headline("Call of Duty Black Ops gets a patch", "Your Call of Duty Black Ops loadout changed",
                                              ["Call of Duty Black Ops"]))

class StorySelectionTests(unittest.TestCase):
    def test_a_story_that_cannot_answer_its_hook_is_dropped(self):
        import json
        from tests.test_news import reply, story
        data = {"stories": [{**reply(1), "answers_hook": False}, {**reply(2), "answers_hook": True}]}
        picks = news.validate_reply(json.dumps(data), [story(1), story(2)], load_config())
        self.assertEqual([p.story.id for p in picks], [2])
        with self.assertRaisesRegex(news.NewsError, "cannot answer its own hook"):
            news.validate_reply(json.dumps({"stories": data["stories"][:1]}), [story(1)], load_config())

    def test_an_empty_story_list_means_nothing_today_not_a_failed_run(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from clipper import editor
        from tests.test_news import story
        cfg = load_config()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg.database = Path(tmp) / "c.sqlite"
            with mock.patch.object(editor, "call_claude", return_value='{"stories": []}') as call:
                self.assertEqual(news.pick_stories(cfg, [story(1)], 1, Path(tmp)), [])
        call.assert_called_once()

    def test_a_report_cannot_bring_a_covered_game_back(self):
        from types import SimpleNamespace as NS
        from clipper import topics

        def kept(title):
            pick = NS(score=8, subjects=["Ubisoft"], major_new=True, story=NS(title=title))
            return bool(topics.enforce([pick], ["assassin creed,ubisoft"])[0])
        self.assertFalse(kept("Report: Ubisoft CEO is demanding developers justify not using AI"))
        self.assertFalse(kept("Ubisoft rumoured to announce a delay"))
        self.assertFalse(kept("Ubisoft devs talk about crunch"))
        self.assertTrue(kept("Ubisoft announces a new release date for its next game"))

class GtaOnlyTests(unittest.TestCase):
    def test_only_real_gta_headlines_qualify(self):
        from clipper import gta
        for good in ("GTA 6 easter egg found in the second trailer", "GTA Online weekly update adds a new car",
                     "Grand Theft Auto VI delay rumour denied"):
            self.assertTrue(gta.is_gta_story(good), good)
        for bad in ("This GTA-like heist game is great", "Zelda patch out now", "GTA Online modded accounts for sale",
                    "GTA 6 beta download link appears"):
            self.assertFalse(gta.is_gta_story(bad), bad)
        self.assertFalse(gta.is_gta_story("Rockstar staff vote on a union", "The GTA maker held a ballot."))

    def test_several_angles_pass_but_the_same_event_does_not_repeat(self):
        from types import SimpleNamespace as NS
        from clipper import gta
        picks = [NS(score=s, story=NS(title=t)) for s, t in (
            (9, "GTA 6 breaks first weekend sales record"), (8, "GTA 6 first weekend sales record confirmed by Take-Two"),
            (7, "GTA 6 players find a hidden alligator easter egg"), (6, "GTA Online weekly bonus cars"))]
        kept, dropped = gta.enforce_angles(picks, [])
        self.assertEqual([p.score for p in kept], [9, 7, 6])
        self.assertEqual(len(dropped), 1)
        self.assertEqual(gta.enforce_angles(picks[2:3], ["Hidden alligator easter egg in GTA 6"])[0], [])

    def test_the_mode_filters_feeds_extends_the_window_and_is_undone_afterwards(self):
        import tempfile
        from datetime import datetime, timedelta, timezone
        from pathlib import Path
        from unittest import mock
        import feedparser
        from tests.test_news import rss
        cfg = load_config()
        now = datetime.now(timezone.utc)
        items = [(f"GTA 6 {word} spotted", f"https://x.com/{i}", "", now - timedelta(hours=30))
                 for i, word in enumerate(("alligator", "helicopter", "casino heist glitch", "jetski"))]
        items.append(("Zelda patch out now", "https://x.com/z", "", now - timedelta(hours=1)))
        seen = {}

        def fake_pick(c, stories, n, work):
            seen["titles"], seen["only"] = [s.title for s in stories], c.news.get("only")
            seen["prompt"] = news.build_prompt(c, stories, n)
            return []
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg.database = Path(tmp) / "c.sqlite"
            cfg.raw = {**cfg.raw, "news": {**cfg.raw["news"], "feeds": [{"name": "X", "url": "u"}]}}
            with mock.patch.object(feedparser, "parse", return_value=rss(items)), \
                    mock.patch.object(news, "pick_stories", fake_pick):
                result = news.run_news(cfg, dry_run=True, only="gta")
        self.assertEqual(len(seen["titles"]), 3)                  # 30 h old stories count; "casino" and Zelda do not
        self.assertIn("GTA-ONLY RUN", seen["prompt"])
        self.assertEqual(seen["only"], "gta")
        self.assertIsNone(cfg.news.get("only"))
        self.assertEqual(result.note, "no story worth making this time")

    def test_the_pill_says_gta_6_only_for_gta_6(self):
        self.assertEqual(badge_label("flagship", subject="GTA 6"), "GTA 6")
        self.assertEqual(badge_label("flagship", subject="GTA Online"), "GTA")
        self.assertEqual(badge_label("flagship", subject="GTA V"), "GTA")

    def test_the_command_and_batch_file(self):
        self.assertIn("-m clipper news --only-gta %*", (PROJECT_ROOT / "gta.bat").read_text(encoding="utf-8"))
        self.assertNotIn("gta", (PROJECT_ROOT / "news.bat").read_text(encoding="utf-8").lower())


class CreatorsOnlyTests(unittest.TestCase):
    def test_creator_news_qualifies_and_lookalikes_do_not(self):
        from clipper import creators
        for good in ("Kai Cenat finishes his month-long subathon", "Streamer completes Elden Ring no-hit run",
                     "Fnatic announce new Valorant roster", "Twitch viewership record falls to a Minecraft event",
                     "T1 wins the League of Legends world championship", "xQc reveals his own game"):
            self.assertTrue(creators.is_story(good), good)
        for bad in ("Marathon gets a release date", "Tekken adds to its character roster", "The creator of Final Fantasy speaks",
                    "Streamer arrested after police raid", "YouTuber accused in new allegations", "Zelda patch out now",
                    "Streamer starts slots stream"):
            self.assertFalse(creators.is_story(bad), bad)

    def test_one_short_per_creator_event_and_every_pill_is_creator_news(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        from clipper import editor
        from tests.test_news import reply, story
        cfg = load_config()
        cfg.raw = {**cfg.raw, "news": {**cfg.raw["news"], "stories": 3, "only": "creators"}}
        stories = [story(1), story(2), story(3)]
        stories[0].title = "Kai Cenat breaks the Twitch subscriber record"
        stories[1].title = "Kai Cenat sets new subscriber record on Twitch"
        stories[2].title = "Fnatic announce new Valorant roster"
        text = json.dumps({"stories": [{**reply(i), "bucket": "flagship"} for i in (1, 2, 3)]})
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg.database = Path(tmp) / "c.sqlite"
            with mock.patch.object(editor, "call_claude", return_value=text):
                picks = news.pick_stories(cfg, stories, 3, Path(tmp))
            self.assertIn("CREATORS-ONLY RUN", news.build_prompt(cfg, stories, 3))
        self.assertEqual([p.story.id for p in picks], [1, 3])
        self.assertEqual({badge_label(p.bucket) for p in picks}, {"CREATOR NEWS"})

    def test_the_command_reaches_the_creators_mode_and_other_launchers_are_unchanged(self):
        from unittest import mock
        from clipper import cli
        seen = []
        result = news.NewsResult()
        with mock.patch.object(news, "run_news", lambda *a, **k: seen.append(k.get("only")) or result):
            for args in (["news", "--creators"], ["news", "--topic", "creators"], ["news", "--only-gta"], ["news"]):
                cli.main(args)
        self.assertEqual(seen, ["creators", "creators", "gta", None])
        self.assertIn("-m clipper news --creators %*", (PROJECT_ROOT / "creators.bat").read_text(encoding="utf-8"))
        for name in ("news.bat", "export.bat"):
            self.assertNotIn("creators", (PROJECT_ROOT / name).read_text(encoding="utf-8").lower())


class PictureChoiceTests(unittest.TestCase):
    def test_promo_banners_are_not_article_pictures(self):
        from clipper import newsimage
        page = """<article>
          <img src="https://x.com/uploads/2024/05/YouTubeC2A.jpeg" width="1280">
          <a href="https://www.youtube.com/@outlet?sub_confirmation=1"><img src="https://x.com/banner-art.jpg" width="1280"></a>
          <a href="https://discord.gg/abc"><img src="https://x.com/community.jpg" width="1280"></a>
          <img src="https://x.com/join-us-newsletter.png" width="1280">
          <img src="https://x.com/GTA-6-YouTube-trailer-screenshot.jpg" width="1280">
          <img src="https://x.com/octane-cta-ctahonda-race.jpg" width="1280">
          <a href="https://x.com/full.jpg"><img src="https://x.com/GTA-6-Animal-Hunting.jpg" width="1280"></a>
        </article>"""
        got = [u.rsplit("/", 1)[1] for u in newsimage.article_images(page, "https://x.com/a")]
        self.assertEqual(got, ["GTA-6-YouTube-trailer-screenshot.jpg", "GTA-6-Animal-Hunting.jpg"])

    def test_video_thumbnails_are_never_used(self):
        from clipper import newsimage
        for url in ("https://i.ytimg.com/vi/abc/maxresdefault.jpg?v=1", "https://img.youtube.com/vi/abc/0.jpg",
                    "https://static-cdn.jtvnw.net/previews-ttv/live_user_x-1920x1080.jpg"):
            self.assertTrue(newsimage.is_video_thumbnail(url), url)
            self.assertFalse(newsimage._usable_url(url), url)
        self.assertTrue(newsimage._usable_url("https://cdn.example.com/gta-6-screen.jpg"))

    def test_betting_sites_are_never_picture_sources(self):
        from clipper import newsimage
        for url in ("https://www.betus.com.pa/wp-content/uploads/Screenshot_110.jpg", "https://thunderpick.io/a.jpg",
                    "https://cdn.x.com/uploads/esports-betting-guide.jpg", "https://stake.com/banner.jpg"):
            self.assertFalse(newsimage._usable_url(url), url)
        self.assertFalse(newsimage._usable_url("https://cdn.x.com/a.jpg", "Presented by Thunderpick"))
        for url in ("https://cdn.x.com/mistake.com-fix.jpg", "https://cdn.x.com/alphabet-soup.jpg", "https://cdn.x.com/lec-stage.jpg"):
            self.assertTrue(newsimage._usable_url(url), url)
        self.assertTrue(news.HARD_SKIP.search("Thunderpick World Championship returns"))

    def run_visuals(self, used_before: int, fresh_available: int):
        """collect_visuals with a fake search over pictures 0..N: -> the picture numbers the new Short got."""
        import tempfile
        from pathlib import Path
        from unittest import mock
        import cv2
        import numpy as np
        from clipper import db, imagesearch, newsimage
        from tests.test_imagesearch import photo
        from tests.test_news import story
        pool = [photo(1280, 720, seed=100 + i) for i in range(used_before + fresh_available)]

        def print_of(data):
            return newsimage.fingerprint(cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR))

        def search(topic, folder, cache, need, known, start_n, timeout=15.0):
            out = []
            for i, data in enumerate(pool):
                if len(out) >= need:
                    break
                f = Path(folder) / f"news_image_{start_n + len(out)}.jpg"
                if newsimage.save_picture(data, f, list(known) + [print_of(pool[o["n"]]) for o in out]):
                    out.append({"file": f, "url": None, "source": "cache", "n": i})
            return out
        cfg = load_config()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg.database = Path(tmp) / "c.sqlite"
            cfg.raw = {**cfg.raw, "news": {**cfg.raw["news"], "images_min": 4, "images_max": 4, "images_floor": 3}}
            db.add_used_pictures(cfg.database, [print_of(d) for d in pool[:used_before]])
            with mock.patch.object(news, "fetch_news_images", return_value=[]), \
                    mock.patch.object(imagesearch, "fetch_topic_images", search):
                return [v["n"] for v in news.collect_visuals(cfg, story(1), Path(tmp), "GTA 6")]

    def test_pictures_of_recent_shorts_are_left_out(self):
        self.assertEqual(self.run_visuals(used_before=3, fresh_available=4), [3, 4, 5, 6])

    def test_repeats_fill_in_only_when_there_are_not_enough_new_ones(self):
        self.assertEqual(self.run_visuals(used_before=4, fresh_available=2), [4, 5, 0, 1])


class RenderSlotTests(unittest.TestCase):
    def test_a_second_process_cannot_take_the_render_slot(self):
        import subprocess
        import sys
        import tempfile
        from pathlib import Path
        probe = ("import msvcrt, sys; h = open(sys.argv[1], 'a+b'); h.seek(0)\n"
                 "try: msvcrt.locking(h.fileno(), msvcrt.LK_NBLCK, 1); print('free')\n"
                 "except OSError: print('busy')")

        def state(path):
            return subprocess.run([sys.executable, "-c", probe, str(path)], capture_output=True, text=True).stdout.strip()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            with render._machine_slot(Path(tmp), 1):
                self.assertEqual(state(Path(tmp) / "render-0.lock"), "busy")
            self.assertEqual(state(Path(tmp) / "render-0.lock"), "free")


if __name__ == "__main__":
    unittest.main()
