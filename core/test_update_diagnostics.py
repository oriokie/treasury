"""Why the update check found nothing — said, rather than guessed at.

Both GitHub lookups in `latest_release` swallowed their exception entirely, so a
rejected token, a renamed repository, an hourly rate limit and a server with no
internet all produced one sentence: "Couldn't read releases or tags… if the
repository is private, set GITHUB_TOKEN". A church that HAD set a token, and was
having it rejected, was told to set the token.

The answer was in the response every time and nothing looked at it.

The distinction that matters most is 401 against 404. GitHub answers "not found"
for a private repository the caller cannot see, so "wrong name" and "no access"
arrive as the same status and can only be told apart by whether a token was
sent — which is the whole difference between "check the name" and "check the
token's scope".
"""
import io
import json
import urllib.error

from django.test import SimpleTestCase

from core.services.updates import _explain_failure


def _http(code, message="", body=None):
    payload = json.dumps(body if body is not None else {"message": message})
    return urllib.error.HTTPError("https://api.github.com", code, "err", {},
                                  io.BytesIO(payload.encode()))


class TokenRejectedTests(SimpleTestCase):
    def test_401_blames_the_token_and_not_the_name(self):
        msg = _explain_failure(_http(401, "Bad credentials"), "o/r", "tok")
        self.assertIn("rejected the access token", msg)
        self.assertIn("Bad credentials", msg)

    def test_401_says_what_to_do(self):
        msg = _explain_failure(_http(401, "Bad credentials"), "o/r", "tok")
        self.assertIn("GITHUB_TOKEN", msg)
        self.assertIn("expired", msg)


class NotFoundTests(SimpleTestCase):
    def test_404_without_a_token_suggests_the_repo_is_private(self):
        msg = _explain_failure(_http(404, "Not Found"), "o/r", "")
        self.assertIn("PRIVATE", msg)
        self.assertIn("GITHUB_TOKEN", msg)

    def test_404_with_a_token_talks_about_scope_instead(self):
        """Telling someone who already set a token to set a token is the least
        useful sentence available at that moment."""
        msg = _explain_failure(_http(404, "Not Found"), "o/r", "tok")
        self.assertIn("cannot see", msg)
        self.assertIn("Contents: Read", msg)
        self.assertIn("repo", msg)

    def test_the_two_404s_are_not_the_same_message(self):
        self.assertNotEqual(_explain_failure(_http(404), "o/r", ""),
                            _explain_failure(_http(404), "o/r", "tok"))

    def test_it_names_the_repository_being_looked_for(self):
        self.assertIn("owner/name-here",
                      _explain_failure(_http(404), "owner/name-here", ""))


class OtherFailureTests(SimpleTestCase):
    def test_403_is_explained_as_a_rate_limit(self):
        msg = _explain_failure(_http(403, "API rate limit exceeded"), "o/r", "t")
        self.assertIn("rate limit", msg)

    def test_an_unexpected_status_is_still_reported(self):
        self.assertIn("HTTP 500", _explain_failure(_http(500, "boom"), "o/r", "t"))

    def test_no_network_is_not_reported_as_a_github_problem(self):
        msg = _explain_failure(OSError("dns"), "o/r", "t")
        self.assertIn("Could not reach GitHub", msg)
        self.assertIn("outbound internet", msg)

    def test_an_unreadable_body_does_not_take_the_page_down(self):
        """This runs to EXPLAIN a failure; it must not add one of its own."""
        broken = urllib.error.HTTPError("u", 401, "e", {}, io.BytesIO(b"not json"))
        self.assertIn("rejected the access token",
                      _explain_failure(broken, "o/r", "t"))

    def test_every_explanation_is_a_sentence_not_a_status_code(self):
        for exc in (_http(401), _http(404), _http(403), _http(500), OSError("x")):
            with self.subTest(exc=exc):
                msg = _explain_failure(exc, "o/r", "t")
                self.assertGreater(len(msg.split()), 8)
                self.assertTrue(msg.endswith("."))


class ReasonIsSurfacedTests(SimpleTestCase):
    def test_the_reason_is_readable_after_a_lookup(self):
        """The update page asks for this after calling latest_release."""
        from core.services import updates
        updates._release_cache["reason"] = "because of a thing."
        self.assertEqual(updates.last_failure_reason(), "because of a thing.")

    def test_it_is_empty_when_nothing_failed(self):
        from core.services import updates
        updates._release_cache["reason"] = ""
        self.assertEqual(updates.last_failure_reason(), "")
