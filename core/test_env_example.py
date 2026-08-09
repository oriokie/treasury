"""The shared template must stay a template.

`.env.example` is TRACKED, so it reaches every clone, fork and collaborator —
which is exactly what makes it the worst place in the repository to leave a real
value. It had accumulated three: a genuine 50-character Django SECRET_KEY (which
signs sessions and password-reset tokens, and stands in as the Fernet key for
encrypted settings and 2FA secrets whenever TREASURY_ENCRYPTION_KEY is unset),
the live production hostname, and a working MySQL username and password for the
production database.

Nothing about that was visible in review: the file looks like documentation, and
a diff to it reads like a settings tidy-up. So the guard is mechanical — the
values are named here, and the test fails if any of them comes back or if a new
credential line stops looking like a placeholder.

Also pinned: DJANGO_DEBUG=False. A file whose first line says "Copy to .env" has
to be safe on the machine someone copies it to; debugging left on serves a stack
trace carrying the settings and environment to whoever can provoke an error.
"""
import hashlib
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

#: The values that were genuinely committed here once, as SHA-256 digests.
#:
#: Digests, not the strings themselves, and the reason is the whole point of
#: this file: a guard that names the secret it is guarding against has put the
#: secret back. These are still live credentials sitting in git history, and
#: writing them into a test would return them to the WORKING TREE — where the
#: next person to grep the repository, or any secret scanner pointed at it,
#: would find them again. The label after each digest says what it was, so a
#: failure is readable without the value ever being written down.
LEAKED_DIGESTS = {
    "6d807b07e0f9ad93b9af7cc6e97c530f093d2dbe637924f00e5a9dca20ba0b58":
        "the production DJANGO_SECRET_KEY",
    "cfec7d62a2bfdf1f7aa9835a934afe43fa3eeb30125222abf01cfce4680a17bf":
        "the production MYSQL_PASSWORD",
    "d002a80f12bfe571902c998dcaab834c4d1296091d7add0fb35051e2d919735a":
        "the production MYSQL_USER",
    "42e9c8c0de56899c116dc4bb719d27ae85514b447623b00195f8e2eb70825035":
        "the production MYSQL_DB",
    "ec062502570be9c7d2c3bd83140ffa2745d81a1f344e1f5ac6eb7a879c5c9b6e":
        "the production hostname",
}


def _candidate_values(text):
    """Every string in the file that could be a credential coming back.

    A leaked value returns as the right-hand side of a setting, or as a bare
    word in a comment someone pasted. Both are covered: each line's RHS, the
    whole line, and every whitespace-separated token. Hashing candidates rather
    than searching for known strings is what lets the expected values stay
    digests.
    """
    for raw in text.splitlines():
        line = raw.strip().lstrip("#").strip()
        if not line:
            continue
        yield line
        for token in line.split():
            yield token.strip(",;\"'")
        if "=" in line:
            yield line.partition("=")[2].strip().strip("\"'")

#: Every setting whose value is a credential. Each must read as an obvious
#: placeholder, so that copying the file cannot silently produce a working
#: secret — and, for the secret key, so config.settings can refuse to boot on it.
CREDENTIAL_KEYS = ("DJANGO_SECRET_KEY", "MYSQL_PASSWORD", "POSTGRES_PASSWORD",
                   "DJANGO_EMAIL_PASSWORD", "GITHUB_TOKEN",
                   "TREASURY_ENCRYPTION_KEY")

PLACEHOLDER = "change-me"


class EnvExampleCarriesNoRealValuesTests(SimpleTestCase):

    def setUp(self):
        self.text = (Path(settings.BASE_DIR) / ".env.example").read_text()

    def test_no_previously_leaked_value_has_returned(self):
        for candidate in _candidate_values(self.text):
            digest = hashlib.sha256(candidate.encode()).hexdigest()
            self.assertNotIn(
                digest, LEAKED_DIGESTS,
                f"{LEAKED_DIGESTS.get(digest)} is back in .env.example. That is "
                "a real, live credential — it belongs in .env, which is "
                "gitignored, and this file is public in every clone.")

    def test_every_credential_line_is_an_obvious_placeholder(self):
        for raw in self.text.splitlines():
            line = raw.strip()
            if line.startswith("#") or "=" not in line:
                continue                      # comment, or an example left commented out
            key, _, value = line.partition("=")
            if key.strip() not in CREDENTIAL_KEYS:
                continue
            value = value.strip().strip("\"'")
            if not value:
                continue                      # empty is safe: nothing to copy by mistake
            self.assertTrue(
                value.startswith(PLACEHOLDER),
                f"{key.strip()} in .env.example reads {value!r}, which does not "
                f"look like a placeholder. Anything not beginning "
                f"{PLACEHOLDER!r} risks being a real credential, and this file "
                f"is public in every clone.")

    def test_debugging_is_off_in_the_template(self):
        """The file says "Copy to .env" at the top; the copy must be safe."""
        self.assertIn("DJANGO_DEBUG=False", self.text)
        self.assertNotIn("\nDJANGO_DEBUG=True", self.text)


class TheSecretKeyPlaceholderCannotReachProductionTests(SimpleTestCase):
    """The template is only half the guard. The other half is that the app
    refuses to serve on the value the template ships, so a copied-and-forgotten
    .env fails loudly at boot instead of signing real sessions with a key that
    is in every clone."""

    def test_settings_refuses_the_placeholder_when_debug_is_off(self):
        import config.settings as s
        self.assertTrue(
            s.SECRET_KEY.startswith("change-me") is False or s.DEBUG,
            "config.settings should raise ImproperlyConfigured rather than run "
            "with the placeholder key while DEBUG is off.")

    def test_the_check_is_actually_written_and_names_the_placeholder(self):
        source = (Path(settings.BASE_DIR) / "config" / "settings.py").read_text()
        self.assertIn('SECRET_KEY.startswith("change-me")', source,
                      "config/settings.py must reject the .env.example "
                      "placeholder, not only the built-in dev key.")
