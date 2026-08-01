"""Releases: the tag, the note, and the changelog that comes from them.

The pieces existed and were never joined up. VERSION holds the number,
WHATS_NEW holds the note the app shows under "What's new", and
`core.services.updates` asks GitHub for the newest tag so a hosted instance can
tell a treasurer an update is waiting.

That last one has never once found anything: the repository had NO tags. Its
own docstring claims "this project tags every version (vX.Y.Z)", which was not
true, so the update banner could not fire and the in-app updater had nothing to
offer. These tests hold the parts together now that they are.
"""
import subprocess

from django.test import SimpleTestCase, TestCase

from core.services import changelog as cl
from core.version import WHATS_NEW, VERSION_FILE, get_version


class VersionHasANoteTests(SimpleTestCase):
    """The ratchet. A version with no note is not a broken build — it is a
    release nobody can read: the app's panel is blank and so is GitHub's."""

    def test_the_current_version_has_a_release_note(self):
        missing = cl.missing_note()
        self.assertIsNone(
            missing,
            f"VERSION is {missing} but core.version.WHATS_NEW has no entry for "
            f"it. Add one before releasing — it is what the app's \"What's new\" "
            f"panel and the GitHub release both show.")

    def test_the_version_file_is_a_plain_semver(self):
        v = get_version()
        parts = v.split(".")
        self.assertEqual(len(parts), 3, f"VERSION is {v!r}; expected X.Y.Z")
        for p in parts:
            self.assertTrue(p.isdigit(), f"VERSION is {v!r}; expected X.Y.Z")

    def test_the_current_note_is_written_for_a_treasurer(self):
        """Not a commit subject. These are read by the person doing the books,
        so a one-line entry is a sign the wrong text was pasted in.

        The CURRENT version only, deliberately. Held against all 168 historical
        entries this would fail on "Initial release." — which is a perfectly
        good note for 1.0.0 — and rewriting history to satisfy a new rule is
        how a guard gets switched off instead of obeyed.
        """
        note = cl.notes_for(get_version())
        self.assertGreater(
            len(note.split()), 8,
            f"the note for {get_version()} is too short to tell anyone what "
            f"changed")

    def test_no_note_is_empty(self):
        for version, note in WHATS_NEW.items():
            with self.subTest(version=version):
                self.assertTrue(note.strip(), f"{version} has a blank note")


class VersionOrderingTests(SimpleTestCase):
    def test_versions_sort_as_numbers_not_as_text(self):
        """The bug that arrives with 3.10: a text sort puts it before 3.9, so
        the changelog silently reorders and the "latest" release is wrong."""
        self.assertGreater(cl.parse_version("3.10.0"), cl.parse_version("3.9.0"))
        self.assertGreater(cl.parse_version("v3.10.0"), cl.parse_version("3.9.9"))

    def test_a_leading_v_makes_no_difference(self):
        self.assertEqual(cl.parse_version("v1.2.3"), cl.parse_version("1.2.3"))

    def test_the_newest_release_comes_first(self):
        ordered = cl.released_versions()
        self.assertEqual(ordered, sorted(ordered, key=cl.parse_version,
                                         reverse=True))

    def test_a_ragged_version_does_not_raise(self):
        """Whatever is in the file, sorting it must not take a page down."""
        self.assertEqual(cl.parse_version("not-a-version"), (0, 0, 0))
        self.assertEqual(cl.parse_version("3.1"), (3, 1, 0))


class ChangelogTests(SimpleTestCase):
    def test_it_carries_every_version_that_has_a_note(self):
        body = cl.render()
        for v in WHATS_NEW:
            self.assertIn(f"## {v}", body)

    def test_the_newest_version_is_at_the_top(self):
        body = cl.render()
        newest, older = cl.released_versions()[0], cl.released_versions()[-1]
        self.assertLess(body.index(f"## {newest}"), body.index(f"## {older}"))

    def test_it_says_where_to_edit_instead(self):
        """A generated file someone hand-edits loses their change at the next
        release, so it has to say so on its face."""
        self.assertIn("WHATS_NEW", cl.render())

    def test_it_is_stable_between_runs(self):
        """Regenerated on every release; if the output wobbled, each release
        would carry a spurious diff of the whole file."""
        self.assertEqual(cl.render(), cl.render())


class ReleaseBodyTests(SimpleTestCase):
    def test_the_note_comes_before_the_commits(self):
        version = cl.released_versions()[0]
        body = cl.release_body(version, ["Fix a thing", "Fix another"])
        self.assertLess(body.index(cl.notes_for(version)[:30]),
                        body.index("Fix a thing"))

    def test_the_commits_are_folded_away(self):
        """The people who read a release notification are not the people who
        read a commit log."""
        body = cl.release_body(cl.released_versions()[0], ["Fix a thing"])
        self.assertIn("<details>", body)

    def test_a_release_with_no_commits_has_no_empty_section(self):
        body = cl.release_body(cl.released_versions()[0], [])
        self.assertNotIn("<details>", body)

    def test_an_unwritten_version_says_so_rather_than_being_blank(self):
        body = cl.release_body("99.99.99", [])
        self.assertIn("No release note", body)


class ReleaseCommandTests(TestCase):
    """The command's checks, exercised against the real repository."""

    def _run(self, *args):
        from io import StringIO

        from django.core.management import call_command
        out = StringIO()
        try:
            call_command("release", *args, stdout=out, stderr=out)
            return 0, out.getvalue()
        except SystemExit as exc:
            return exc.code, out.getvalue()
        except Exception as exc:            # CommandError and friends
            return 1, f"{out.getvalue()}{exc}"

    def test_check_writes_nothing_to_the_repository(self):
        """--check is what someone runs to find out whether they CAN release,
        so it must not change the repository in the asking.

        This caught a real one. The check used `git fetch` to compare against
        origin, and a fetch CREATES local refs — so once v3.39.0 existed on
        GitHub, merely checking whether it could be released brought that very
        tag into a checkout that did not have it. On CI, which clones without
        tags, that turned every --check into a repository mutation. It asks
        `ls-remote` now, which answers the same question and writes nothing.
        """
        before = self._tags()
        self._run("--check")
        self.assertEqual(self._tags(), before,
                         "--check changed the repository's tags")

    def test_the_remote_checks_can_be_skipped(self):
        """Tests and offline work must not depend on reaching GitHub."""
        from core.management.commands.release import Command
        problems = Command()._problems(get_version(), allow_dirty=True,
                                       remote=False)
        self.assertIsInstance(problems, list)

    def _tags(self):
        out = subprocess.run(["git", "tag"], capture_output=True, text=True,
                             cwd=str(VERSION_FILE.parent))
        return out.stdout

    def test_it_refuses_a_version_that_is_already_tagged(self):
        """Re-tagging an existing version would move what every hosted instance
        thinks that version IS."""
        from core.management.commands.release import Command
        problems = Command()._problems(get_version(), allow_dirty=True,
                                       remote=False)
        existing = subprocess.run(
            ["git", "tag", "-l", f"v{get_version()}"], capture_output=True,
            text=True, cwd=str(VERSION_FILE.parent)).stdout.strip()
        if existing:
            self.assertTrue(any("already exists" in p for p in problems))

    def test_it_lists_every_problem_at_once(self):
        """Someone about to tag wants the whole list, not to discover the next
        one each time they re-run."""
        from core.management.commands.release import Command
        problems = Command()._problems("0.0.1", allow_dirty=True, remote=False)
        self.assertIsInstance(problems, list)
        # 0.0.1 is older than the current VERSION, so at minimum that is caught.
        self.assertTrue(any("already released" in p or "WHATS_NEW" in p
                            for p in problems))
