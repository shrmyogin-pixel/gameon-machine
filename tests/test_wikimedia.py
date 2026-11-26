"""Wikimedia robot policy: honest User-Agent, spaced requests, Retry-After respected. No network."""
import io
import unittest
import urllib.error
from unittest import mock

from clipper import imagesearch, newsimage

API = "https://commons.wikimedia.org/w/api.php?action=query"


class WikimediaPolicyTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(newsimage._wikimedia.update, dict(newsimage._wikimedia))
        newsimage._wikimedia.update(contact="", next=0.0, blocked_until=0.0)
        self.sent, self.slept, self.clock = [], [], 1000.0

        def sleep(seconds):
            self.slept.append(seconds)
            self.clock += seconds

        def fetch(req, limit, timeout):
            self.sent.append(req)
            return b"{}", "application/json"
        for patcher in (mock.patch.object(newsimage, "public_host", return_value=True),
                        mock.patch.object(newsimage.time, "monotonic", lambda: self.clock),
                        mock.patch.object(newsimage.time, "sleep", sleep),
                        mock.patch.object(newsimage, "_fetch", fetch)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def agent(self, i=-1):
        return self.sent[i].get_header("User-agent")

    def test_it_names_itself_to_wikimedia_and_only_there(self):
        newsimage.download(API, 100, 5)
        self.assertEqual(self.agent(), "gameon-clipper-bot/1.0")
        newsimage.set_wikimedia_contact("https://example.org/about\r\n")
        newsimage.download("https://upload.wikimedia.org/a.jpg", 100, 5)
        self.assertEqual(self.agent(), "gameon-clipper-bot/1.0 (https://example.org/about)")
        newsimage.download("https://cdn.example.com/a.jpg", 100, 5)
        self.assertNotIn("example.org", self.agent())
        self.assertFalse(newsimage.is_wikimedia("https://notwikimedia.org/a.jpg"))

    def test_requests_are_spaced_more_widely_without_contact_details(self):
        newsimage.download(API, 100, 5)
        newsimage.download(API, 100, 5)
        self.assertEqual(self.slept, [newsimage.GAP_ANONYMOUS])
        newsimage.set_wikimedia_contact("https://example.org")
        newsimage.download(API, 100, 5)                       # still waits out the gap set by the call before
        newsimage.download(API, 100, 5)
        self.assertEqual(self.slept[-1], newsimage.GAP_WITH_CONTACT)

    def test_a_429_is_not_retried_and_retry_after_is_honoured(self):
        calls = []

        def refuse(req, limit, timeout):
            calls.append(req)
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"Retry-After": "120"}, io.BytesIO())
        with mock.patch.object(newsimage, "_fetch", refuse):
            with self.assertRaises(urllib.error.HTTPError):
                newsimage.download_retry(API, 100, 5, tries=3)
            self.clock += 119
            with self.assertRaises(newsimage.WikimediaBusy):
                newsimage.download_retry(API, 100, 5, tries=3)
            self.assertEqual(imagesearch.search_commons("x"), [])          # the search just goes on without Commons
        self.assertEqual(len(calls), 1)
        self.clock += 2
        newsimage.download(API, 100, 5)
        self.assertEqual(len(self.sent), 1)

    def test_thumbnails_use_a_standard_width(self):
        seen = []
        with mock.patch.object(newsimage, "download_retry", lambda url, *a, **k: (seen.append(url), (b"{}", ""))[1]):
            imagesearch.search_commons("x")
        self.assertIn("iiurlwidth=1920", seen[0])


if __name__ == "__main__":
    unittest.main()
