import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from clipper import db, imagesearch, news, topics, trends
from clipper.config import load_config

NOW = datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc)


def story(i, title, trend=None):
    return news.Story(i, "IGN", title, "summary", f"https://ign.com/{i}", NOW, None, [], trend)


def reply(sid, subjects, **extra):
    d = {"id": sid, "kind": "news", "score": 8, "subjects": subjects,
         "hooks": [f"{subjects[0]} officially lands on PC today", f"{subjects[0]} leak confirms a huge change now",
                   f"{subjects[0]} just got a surprise update today", f"{subjects[0]} is free right now for everyone",
                   f"{subjects[0]} delayed again, officially confirmed today"],
         "script_body": " ".join(["Players get a new update with plenty of changes to try today."] * 6) + " Check it out.",
         "headline": "A solid headline for the story", "title": f"{subjects[0]} gets a big update today",
         "description": "A short description line", "tags": [f"t{i}" for i in range(9)], "hashtags": ["#gaming"],
         "sensitive": False, "sensitive_reason": "", "reason": "good"}
    d.update(extra)
    return d


class TopicTests(unittest.TestCase):
    def test_same_franchise_different_spellings(self):
        self.assertEqual(topics.subject_key("GTA 6"), topics.subject_key("Grand Theft Auto VI"))
        self.assertEqual(topics.subject_key("PlayStation"), "")                    # a platform is not a game
        self.assertTrue(topics.share_topic(topics.pick_topics(["GTA 6", "Rockstar"]), topics.pick_topics(["Grand Theft Auto 6"])))
        self.assertFalse(topics.share_topic(topics.pick_topics(["Elden Ring"]), topics.pick_topics(["Hades 2"])))

    def test_hot_topic_cap(self):
        stories = [story(i, f"GTA 6 story number {i}") for i in range(1, 6)] + [story(6, "Zelda gets a new patch")]
        kept = topics.cap_hot_topics(stories)
        self.assertEqual(sum("GTA" in s.title for s in kept), 2)
        self.assertIn("Zelda gets a new patch", [s.title for s in kept])

    def test_one_story_per_game_per_run_and_24h_memory(self):
        cfg = load_config()
        stories = [story(1, "GTA 6 trailer 3"), story(2, "GTA VI map leak"), story(3, "Hades 2 patch"), story(4, "Halo update")]
        text = json.dumps({"stories": [reply(1, ["GTA 6"], score=9), reply(2, ["Grand Theft Auto VI"], score=8),
                                       reply(3, ["Hades 2"], score=7), reply(4, ["Halo"], score=6)]})
        cfg.raw = {**cfg.raw, "news": {**cfg.raw["news"], "stories": 4}}
        picks = news.validate_reply(text, stories, cfg)
        kept, dropped = topics.enforce(picks, [])
        self.assertEqual([p.story.id for p in kept], [1, 3, 4])                    # only one GTA story
        self.assertEqual(len(dropped), 1)
        kept, dropped = topics.enforce(picks, ["halo"])                            # Halo was made 3 hours ago
        self.assertEqual([p.story.id for p in kept], [1, 3])
        picks[3].major_new = True                                                  # ...unless a major new announcement
        kept, _ = topics.enforce(picks, ["halo"])
        self.assertEqual([p.story.id for p in kept], [1, 3, 4])

    def test_topics_are_remembered_in_the_database(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "c.sqlite"
            db.init_db(path)
            db.add_topics(path, ["gta"], "GTA 6 thing")
            self.assertEqual(db.recent_topics(path, "2000-01-01 00:00:00"), ["gta"])
            self.assertEqual(db.recent_topics(path, "2999-01-01 00:00:00"), [])


class TrendTests(unittest.TestCase):
    def test_unverified_trend_is_dropped_and_verified_is_kept(self):
        cfg = load_config()
        stories = [story(1, "Secret in Zelda", {"source": "youtube", "signal": "1,000,000 views"}),
                   story(2, "Hidden Hades room", {"source": "youtube", "signal": "900,000 views"})]
        text = json.dumps({"stories": [reply(1, ["Zelda"], kind="trend", verified=False, core_fact="x"),
                                       reply(2, ["Hades"], kind="trend", verified=True, core_fact="A hidden room exists.")]})
        cfg.raw = {**cfg.raw, "news": {**cfg.raw["news"], "stories": 2}}
        picks = news.validate_reply(text, stories, cfg)
        self.assertEqual([p.story.id for p in picks], [2])
        self.assertEqual(picks[0].core_fact, "A hidden room exists.")

    def test_trend_prompt_demands_original_rewrite_and_verification(self):
        cfg = load_config()
        prompt = news.build_prompt(cfg, [story(1, "Secret in Zelda", {"source": "youtube", "signal": "1,000,000 views"})], 1, NOW, trend=True)
        self.assertIn("VERIFY", prompt)
        self.assertIn("COMPLETELY", prompt)
        self.assertIn("1,000,000 views", prompt)

    def test_youtube_metadata_is_filtered(self):
        cfg = load_config()
        data = {"entries": [
            {"url": "https://www.youtube.com/watch?v=aaa", "title": "Zelda secret room", "view_count": 500000, "duration": 40, "channel": "Chan"},
            {"url": "https://www.youtube.com/watch?v=bbb", "title": "Too few views", "view_count": 10, "duration": 40, "channel": "Chan"},
            {"url": "https://www.youtube.com/watch?v=ccc", "title": "A two hour stream", "view_count": 900000, "duration": 7200, "channel": "Chan"},
            {"url": "https://www.youtube.com/watch?v=ddd", "title": "Best casino bonuses in games", "view_count": 900000, "duration": 40, "channel": "Chan"}]}
        cfg.raw = {**cfg.raw, "trends": {"queries": ["q"]}}
        with mock.patch.object(trends, "_json_from", return_value=data):
            found = trends.youtube_trends(cfg, NOW)
        self.assertEqual([s.title for s in found], ["Zelda secret room"])
        self.assertEqual(found[0].trend["source"], "youtube")
        self.assertEqual(found[0].link, "https://www.youtube.com/watch?v=aaa")

    def test_trend_visuals_never_reuse_a_youtube_thumbnail(self):
        cfg = load_config()
        s = story(1, "Zelda secret", {"source": "youtube", "signal": "x"})
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp, \
                mock.patch.object(news, "fetch_news_images") as fetch, \
                mock.patch.object(imagesearch, "fetch_topic_images", side_effect=lambda t, f, c, need, h, n, to: [
                    {"file": Path(f) / f"news_image_{n + i}.jpg", "url": "u", "source": "commons", "width": 900, "height": 600}
                    for i in range(need)]) as topic:
            vis = news.collect_visuals(cfg, s, Path(tmp), "Zelda")
        fetch.assert_not_called()
        self.assertEqual(topic.call_args.args[0], "Zelda")
        self.assertTrue(vis and all(v["kind"] == "topic" for v in vis))


if __name__ == "__main__":
    unittest.main()
