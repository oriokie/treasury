"""Update checker falls back to tags when no GitHub Release is published (#3)."""
from unittest import mock
from django.test import TestCase, override_settings
import core.services.updates as up


class UpdateCheckTests(TestCase):
    def setUp(self):
        up._release_cache["value"] = None; up._release_cache["at"] = 0

    @override_settings(GITHUB_REPO="oriokie/treasury", GITHUB_TOKEN="x")
    def test_tags_fallback_picks_highest(self):
        def fake(url, token, timeout=4):
            if "releases/latest" in url:
                raise Exception("404")
            if "/tags" in url:
                return [{"name": "v1.57.0"}, {"name": "v1.58.0"}, {"name": "v1.9.0"}]
            return {}
        with mock.patch.object(up, "_fetch_json", fake):
            rel = up.latest_release(force=True)
        self.assertEqual(rel["tag"], "v1.58.0")
        self.assertIn("releases/tag/v1.58.0", rel["url"])

    @override_settings(GITHUB_REPO="oriokie/treasury", GITHUB_TOKEN="x")
    def test_published_release_preferred(self):
        def fake(url, token, timeout=4):
            if "releases/latest" in url:
                return {"tag_name": "v2.0.0", "html_url": "https://x", "body": "n"}
            return []
        with mock.patch.object(up, "_fetch_json", fake):
            rel = up.latest_release(force=True)
        self.assertEqual(rel["tag"], "v2.0.0")

    @override_settings(GITHUB_REPO="oriokie/treasury", GITHUB_TOKEN="x")
    def test_no_releases_no_tags_returns_none(self):
        def fake(url, token, timeout=4):
            if "releases/latest" in url:
                raise Exception("404")
            return []
        with mock.patch.object(up, "_fetch_json", fake):
            rel = up.latest_release(force=True)
        self.assertIsNone(rel)
