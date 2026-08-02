"""Why the update check says "Bad credentials", and which token it used.

The message GitHub returns tells a treasurer the token is wrong. It cannot tell
them WHICH token the server sent — and that is the hard part, because the .env
reader defers to the real environment, so a new token written into .env while a
stale one is still exported is ignored in silence.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings

from core.services.updates import token_diagnosis


class TokenShapeTests(TestCase):
    @override_settings(GITHUB_TOKEN="")
    def test_no_token_says_a_private_repo_needs_one(self):
        d = token_diagnosis()
        self.assertFalse(d["set"])
        self.assertIn("private repository", d["note"])

    @override_settings(GITHUB_TOKEN="ghp_" + "a" * 36)
    def test_a_classic_token_is_recognised(self):
        d = token_diagnosis()
        self.assertTrue(d["set"])
        self.assertIn("classic", d["shape"])
        self.assertIn("40 characters", d["shape"])

    @override_settings(GITHUB_TOKEN="github_pat_" + "b" * 60)
    def test_a_fine_grained_token_is_recognised(self):
        self.assertIn("fine-grained", token_diagnosis()["shape"])

    @override_settings(GITHUB_TOKEN="not-a-github-token")
    def test_something_that_is_not_a_token_is_called_out(self):
        d = token_diagnosis()
        self.assertIn("unrecognised", d["shape"])
        self.assertIn("begin ghp_", d["note"])

    @override_settings(GITHUB_TOKEN="ghp_" + "a" * 36)
    def test_the_token_itself_is_never_returned(self):
        """A diagnostic that leaks the credential is worse than no diagnostic."""
        blob = repr(token_diagnosis())
        self.assertNotIn("a" * 36, blob)


class EnvOverrideTests(TestCase):
    """The trap: .env updated, a stale export still winning."""

    def _with_env_file(self, contents):
        import tempfile, pathlib
        d = tempfile.mkdtemp()
        (pathlib.Path(d) / ".env").write_text(contents, encoding="utf-8")
        return d

    @override_settings(GITHUB_TOKEN="ghp_" + "a" * 36)
    def test_a_stale_export_beating_dotenv_is_reported(self):
        base = self._with_env_file("GITHUB_TOKEN=ghp_" + "b" * 36 + "\n")
        with override_settings(BASE_DIR=base):
            d = token_diagnosis()
        self.assertTrue(d["overridden"])
        self.assertIn("overriding", d["note"])

    @override_settings(GITHUB_TOKEN="ghp_" + "a" * 36)
    def test_matching_values_are_not_reported_as_overridden(self):
        base = self._with_env_file("GITHUB_TOKEN=ghp_" + "a" * 36 + "\n")
        with override_settings(BASE_DIR=base):
            self.assertFalse(token_diagnosis()["overridden"])

    @override_settings(GITHUB_TOKEN="ghp_" + "a" * 36)
    def test_quotes_in_dotenv_do_not_look_like_a_mismatch(self):
        """The loader strips them, so the comparison must too."""
        base = self._with_env_file('GITHUB_TOKEN="ghp_' + "a" * 36 + '"\n')
        with override_settings(BASE_DIR=base):
            self.assertFalse(token_diagnosis()["overridden"])

    @override_settings(GITHUB_TOKEN="ghp_" + "a" * 36)
    def test_a_missing_dotenv_is_not_an_error(self):
        with override_settings(BASE_DIR="/nonexistent-path-for-tests"):
            self.assertFalse(token_diagnosis()["overridden"])


class SettingsCleanTests(TestCase):
    """A token pasted with a trailing newline or quotes reads as revoked."""

    def test_whitespace_and_quotes_are_stripped_on_read(self):
        from config.settings import _clean_secret
        good = "ghp_" + "a" * 36
        for raw in (good, good + "\n", " " + good + " ",
                    f'"{good}"', f"'{good}'", f' "{good}" '):
            self.assertEqual(_clean_secret(raw), good, repr(raw))

    def test_an_empty_value_stays_empty(self):
        from config.settings import _clean_secret
        for raw in ("", None, "   ", '""'):
            self.assertEqual(_clean_secret(raw), "")


class FailureWordingTests(TestCase):
    """What a 401 says depends on which kind of token was sent."""

    def _explain(self, token):
        import urllib.error
        from io import BytesIO
        from core.services.updates import _explain_failure
        exc = urllib.error.HTTPError(
            "u", 401, "Unauthorized", {},
            BytesIO(b'{"message": "Bad credentials"}'))
        return _explain_failure(exc, "owner/repo", token)

    def test_a_fine_grained_token_is_told_to_check_its_expiry_first(self):
        msg = self._explain("github_pat_" + "a" * 82)
        self.assertIn("always expire", msg)
        self.assertIn("expiry date", msg)

    def test_and_told_that_scopes_are_not_the_cause(self):
        """A 401 never reached the permission check, so sending someone to
        audit scopes wastes the one thing they have — patience."""
        self.assertIn("not the cause", self._explain("github_pat_" + "a" * 82))

    def test_a_classic_token_keeps_the_general_wording(self):
        msg = self._explain("ghp_" + "a" * 36)
        self.assertNotIn("always expire", msg)
        self.assertIn("invalid, expired, or was revoked", msg)

    def test_github_s_own_words_are_quoted_either_way(self):
        for token in ("github_pat_" + "a" * 82, "ghp_" + "a" * 36):
            self.assertIn("Bad credentials", self._explain(token))
