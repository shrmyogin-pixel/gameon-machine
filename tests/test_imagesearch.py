"""Run:  python -m unittest tests.test_imagesearch -v

Topic-matched game pictures (ddgs + Wikimedia Commons), their validation and cache; the article scraper's <picture> /
srcset / data-src handling; download retries for hotlink blocks; the headline-repeat check. No real network.
"""
import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import cv2
import numpy as np
from PIL import Image

from clipper import imagesearch, news, newsimage


def photo(w=1280, h=720, seed=1, fmt="JPEG") -> bytes:
    rng = np.random.default_rng(seed)
    img = cv2.GaussianBlur(cv2.resize(rng.integers(0, 255, (6, 10, 3), dtype=np.uint8), (w, h), interpolation=cv2.INTER_CUBIC), (0, 0), 20)
    buf = io.BytesIO()
    Image.fromarray(img[..., ::-1]).save(buf, fmt)
    return buf.getvalue()


class ScraperTests(unittest.TestCase):
    PAGE = """<html><head><meta property="og:image" content="https://cdn.x/og.jpg"></head><body><article>
      <picture><source srcset="https://cdn.x/pic-800.webp 800w, https://cdn.x/pic-1600.webp 1600w" type="image/webp">
              <img src="https://cdn.x/pic-fallback.jpg"></picture>
      <img data-src="https://cdn.x/lazy.jpg" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">
      <img data-lazy-src="https://cdn.x/lazy2.jpg" width="1200">
      <img src="data:image/png;base64,AAAA">
      <img src="https://cdn.x/avatar-jane.jpg"><img src="https://cdn.x/share-icon.png"><img src="https://cdn.x/small.jpg" width="120" height="90">
    </article><footer><img src="https://cdn.x/footer-pic.jpg"></footer></body></html>"""

    def test_picture_srcset_data_src_and_og_are_all_found_junk_dropped(self):
        got = newsimage.article_images(self.PAGE, "https://ign.com/a")
        self.assertEqual(got[0], "https://cdn.x/og.jpg")
        for want in ("https://cdn.x/pic-1600.webp", "https://cdn.x/pic-fallback.jpg", "https://cdn.x/lazy.jpg", "https://cdn.x/lazy2.jpg"):
            self.assertIn(want, got)
        self.assertNotIn("https://cdn.x/pic-800.webp", got)                    # the widest srcset candidate only
        for bad in ("avatar", "share-icon", "small.jpg", "footer-pic", "data:"):
            self.assertFalse(any(bad in u for u in got), bad)


class RetryTests(unittest.TestCase):
    def test_a_403_hotlink_block_is_retried_with_a_referer_then_succeeds(self):
        calls = []

        def fake(url, limit, timeout, truncate=False, referer=None):
            calls.append(referer)
            if len(calls) == 1:
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
            return b"ok", "image/jpeg"
        with mock.patch.object(newsimage, "download", fake), mock.patch.object(newsimage.time, "sleep"):
            self.assertEqual(newsimage.download_retry("https://cdn.x/a.jpg", 10, 1, tries=3), (b"ok", "image/jpeg"))
        self.assertEqual(calls, [None, "https://cdn.x/"])

    def test_404_and_unsafe_hosts_are_not_retried(self):
        n = []

        def gone(url, *a, **k):
            n.append(1)
            raise urllib.error.HTTPError(url, 404, "nope", {}, None)
        with mock.patch.object(newsimage, "download", gone), mock.patch.object(newsimage.time, "sleep"):
            with self.assertRaises(urllib.error.HTTPError):
                newsimage.download_retry("https://cdn.x/a.jpg", 10, 1)
        self.assertEqual(len(n), 1)
        with self.assertRaises(ValueError):
            newsimage.download_retry("http://127.0.0.1/a.jpg", 10, 1)


class HelperTests(unittest.TestCase):
    def test_slug_and_relevance(self):
        self.assertEqual(imagesearch.topic_slug("Alan Wake 2!"), "alan-wake-2")
        self.assertEqual(imagesearch.topic_slug(""), "gaming")
        self.assertTrue(imagesearch.relevant("Alan Wake 2", "Alan Wake 2 - gameplay screenshot"))
        self.assertFalse(imagesearch.relevant("Alan Wake 2", "Alan Walker concert photos"))
        self.assertFalse(imagesearch.relevant("Halo Infinite", "Halo 3 multiplayer"))
        self.assertTrue(imagesearch.relevant("Xbox", "New Xbox console"))

    def test_validation_rejects_small_banner_corrupt_and_truncated(self):
        self.assertEqual(imagesearch.validate_image(photo(1280, 720)), (1280, 720))
        self.assertIsNone(imagesearch.validate_image(photo(400, 300)))               # too small
        self.assertIsNone(imagesearch.validate_image(photo(2400, 400)))              # banner shape
        self.assertIsNone(imagesearch.validate_image(b"<html>403</html>"))           # not a picture
        self.assertIsNone(imagesearch.validate_image(photo(1280, 720)[:3000]))       # truncated download

    def test_headline_repeat_is_detected(self):
        self.assertTrue(news.repeated_run("Alan Wake 2 is free on Epic Games this week", "Alan Wake 2 is free on Epic now"))
        self.assertEqual(news.repeated_run("Alan Wake 2 is free on Epic Games this week", "Epic hands out the horror hit for nothing."), "")


class TopicFetchTests(unittest.TestCase):
    def hits(self, n, source="ddgs", title="Alan Wake 2 gameplay screenshot"):
        return [{"url": f"https://img.x/{source}{i}.jpg", "title": title, "page": "https://site.x/p", "width": 1280, "height": 720,
                 "source": source} for i in range(n)]

    def run_fetch(self, tmp, ddgs=(), commons=(), need=4, known=(), data=None):
        folder, cache = Path(tmp) / "clip", Path(tmp) / "cache"
        folder.mkdir(exist_ok=True)

        def dl(url, limit, timeout, truncate=False, referer=None, tries=3):
            if data is not None and url in data:
                return data[url]
            return photo(1280, 720, seed=sum(map(ord, url)) % 997), "image/jpeg"
        with mock.patch.object(imagesearch, "search_ddgs", return_value=list(ddgs)), \
                mock.patch.object(imagesearch, "search_commons", return_value=list(commons)), \
                mock.patch.object(newsimage, "download_retry", dl):
            return imagesearch.fetch_topic_images("Alan Wake 2", folder, cache, need, list(known), 3), folder, cache

    def test_downloads_validates_numbers_and_caches(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            got, folder, cache = self.run_fetch(tmp, ddgs=self.hits(2), commons=self.hits(2, "commons"))
            self.assertEqual([g["source"] for g in got], ["ddgs", "ddgs", "commons", "commons"])
            self.assertEqual([g["file"].name for g in got], [f"news_image_{i}.jpg" for i in (3, 4, 5, 6)])
            self.assertTrue(all(g["file"].is_file() and g["width"] >= 600 for g in got))
            self.assertEqual(len(list((cache / "alan-wake-2").glob("*.jpg"))), 4)        # cached under the topic slug
            self.assertFalse((folder / "topic_probe.jpg").exists())

    def test_cached_pictures_are_reused_without_any_search(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            self.run_fetch(tmp, ddgs=self.hits(3))
            folder2 = Path(tmp) / "clip2"
            folder2.mkdir()
            with mock.patch.object(imagesearch, "search_ddgs", side_effect=AssertionError("searched")), \
                    mock.patch.object(imagesearch, "search_commons", side_effect=AssertionError("searched")):
                got = imagesearch.fetch_topic_images("Alan Wake 2", folder2, Path(tmp) / "cache", 3, [], 1)
            self.assertEqual([g["source"] for g in got], ["cache"] * 3)

    def test_wrong_game_small_corrupt_and_blocked_hits_are_skipped(self):
        bad = self.hits(1, title="Alan Walker live concert") + [{**self.hits(1)[0], "url": "https://img.x/small.jpg", "width": 300}]
        corrupt = {"url": "https://img.x/corrupt.jpg", "title": "Alan Wake 2 screenshot", "page": "", "width": 1280, "height": 720, "source": "ddgs"}
        blocked = {**corrupt, "url": "https://img.x/blocked.jpg"}
        ok = {**corrupt, "url": "https://img.x/ok.jpg"}

        def data():
            return {"https://img.x/corrupt.jpg": (b"GIF89a-not-really", "image/jpeg")}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            folder, cache = Path(tmp) / "clip", Path(tmp) / "cache"
            folder.mkdir()

            def dl(url, limit, timeout, truncate=False, referer=None, tries=3):
                if url.endswith("blocked.jpg"):
                    raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
                if url.endswith("corrupt.jpg"):
                    return b"not a picture", "image/jpeg"
                return photo(1280, 720, seed=5), "image/jpeg"
            with mock.patch.object(imagesearch, "search_ddgs", return_value=bad + [corrupt, blocked, ok]), \
                    mock.patch.object(imagesearch, "search_commons", return_value=[]), \
                    mock.patch.object(newsimage, "download_retry", dl):
                got = imagesearch.fetch_topic_images("Alan Wake 2", folder, cache, 4, [], 1)
            self.assertEqual([g["url"] for g in got], ["https://img.x/ok.jpg"])

    def test_duplicates_of_article_pictures_are_refused(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            same = Path(tmp) / "article.jpg"
            same.write_bytes(photo(1280, 720, seed=5))
            known = imagesearch.hashes_of([same])
            folder, cache = Path(tmp) / "clip", Path(tmp) / "cache"
            folder.mkdir()
            with mock.patch.object(imagesearch, "search_ddgs", return_value=self.hits(1)), \
                    mock.patch.object(imagesearch, "search_commons", return_value=[]), \
                    mock.patch.object(newsimage, "download_retry", lambda *a, **k: (photo(1280, 720, seed=5), "image/jpeg")):
                got = imagesearch.fetch_topic_images("Alan Wake 2", folder, cache, 3, known, 2)
            self.assertEqual(got, [])

    def test_search_failures_never_raise(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            with mock.patch.object(imagesearch, "search_ddgs", side_effect=RuntimeError("boom")):
                self.assertEqual(imagesearch.fetch_topic_images("Alan Wake 2", Path(tmp), Path(tmp) / "c", 3, [], 1), [])
            self.assertEqual(imagesearch.fetch_topic_images("", Path(tmp), Path(tmp) / "c", 3, [], 1), [])


if __name__ == "__main__":
    unittest.main()
