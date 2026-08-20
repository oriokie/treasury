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

    def test_a_rejected_token_is_retried_without_auth(self):
        """A classic token one character too long made GitHub reject every
        authenticated call — including for a public repo where no token was
        needed. The checker must fall back unauthenticated or the release
        page reports '(none)' while v3.47.0 is sitting in plain sight."""
        import io
        import json
        import urllib.error

        calls = []

        def fake_urlopen(req, timeout=4):
            calls.append(req.headers.get("Authorization")
                         or req.get_header("Authorization"))
            auth = calls[-1]
            if auth:
                raise urllib.error.HTTPError(
                    req.full_url, 401, "Unauthorized", {},
                    io.BytesIO(json.dumps({"message": "Bad credentials"}).encode()))
            body = json.dumps({"tag_name": "v3.47.0", "html_url": "https://x",
                               "body": ""}).encode()
            resp = mock.Mock()
            resp.read.return_value = body
            resp.__enter__ = lambda s: s
            resp.__exit__ = mock.Mock(return_value=False)
            return resp

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            data = up._fetch_json(
                "https://api.github.com/repos/oriokie/treasury/releases/latest",
                token="ghp_" + "a" * 36 + "X")
        self.assertEqual(data["tag_name"], "v3.47.0")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0])       # first attempt sent the bad token
        self.assertFalse(calls[1])      # retry went public

    def test_a_401_without_a_token_is_not_retried(self):
        import io
        import json
        import urllib.error

        calls = []

        def fake_urlopen(req, timeout=4):
            calls.append(1)
            raise urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized", {},
                io.BytesIO(json.dumps({"message": "Bad credentials"}).encode()))

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(urllib.error.HTTPError):
                up._fetch_json("https://api.github.com/repos/o/r/releases/latest",
                               token="")
        self.assertEqual(len(calls), 1)
