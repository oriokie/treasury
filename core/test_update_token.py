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


class WrongLengthTests(TestCase):
    """A token of the wrong length is not a token GitHub ever issued.

    The reported case: a classic token arrived 41 characters long. GitHub
    answers "Bad credentials", which reads exactly like expiry — so the fix
    that gets tried is issuing another token, which arrives 41 characters long
    too. The length is the one signal that tells the two apart.
    """
    GOOD = "ghp_" + "a" * 36
    FINE = "github_pat_" + "b" * 82

    def test_a_good_classic_token_is_forty_characters(self):
        self.assertEqual(len(self.GOOD), 40)
        with override_settings(GITHUB_TOKEN=self.GOOD):
            d = token_diagnosis()
        self.assertNotIn("expected", d["shape"])
        self.assertEqual(d["note"], "")

    def test_one_character_too_many_is_named_and_explained(self):
        with override_settings(GITHUB_TOKEN=self.GOOD + '"'):
            d = token_diagnosis()
        self.assertIn("41 characters", d["shape"])
        self.assertIn("1 too many", d["shape"])
        self.assertIn("too long", d["note"])
        self.assertIn("Issuing another token will not help", d["note"])

    def test_a_clipped_token_is_reported_as_short(self):
        with override_settings(GITHUB_TOKEN="ghp_" + "a" * 30):
            d = token_diagnosis()
        self.assertIn("6 too few", d["shape"])
        self.assertIn("clipped", d["note"])

    def test_a_good_fine_grained_token_is_ninety_three(self):
        self.assertEqual(len(self.FINE), 93)
        with override_settings(GITHUB_TOKEN=self.FINE):
            self.assertNotIn("expected", token_diagnosis()["shape"])

    def test_an_unrecognised_prefix_is_not_length_checked(self):
        """No expected length to compare against — say what IS known."""
        with override_settings(GITHUB_TOKEN="not-a-token-at-all"):
            d = token_diagnosis()
        self.assertNotIn("expected", d["shape"])
        self.assertIn("begin ghp_", d["note"])

    def test_the_length_warning_still_never_leaks_the_token(self):
        with override_settings(GITHUB_TOKEN=self.GOOD + ","):
            blob = repr(token_diagnosis())
        self.assertNotIn("a" * 36, blob)


class CleanerCoversEveryPasteTests(TestCase):
    """Every way the reported 41st character could have got there."""

    GOOD = "ghp_" + "a" * 36

    def test_each_stray_character_is_stripped(self):
        from config.settings import _clean_secret
        for label, raw in {
            "trailing newline": self.GOOD + "\n",
            "matched quotes": f'"{self.GOOD}"',
            "unbalanced leading quote": '"' + self.GOOD,
            "unbalanced trailing quote": self.GOOD + '"',
            "single quotes": f"'{self.GOOD}'",
            "trailing comma": self.GOOD + ",",
            "trailing semicolon": self.GOOD + ";",
            "zero-width space": self.GOOD + "\u200b",
            "byte-order mark": "\ufeff" + self.GOOD,
            "non-breaking space": self.GOOD + "\u00a0",
            "everything at once": f'  "{self.GOOD}",\n',
        }.items():
            self.assertEqual(_clean_secret(raw), self.GOOD, label)

    def test_the_interior_is_never_touched(self):
        """Junk in the MIDDLE is not something to guess at — a token that is
        wrong there should fail loudly, not be silently 'repaired'."""
        from config.settings import _clean_secret
        odd = "ghp_" + "a" * 17 + "-" + "a" * 18
        self.assertEqual(_clean_secret(odd), odd)
