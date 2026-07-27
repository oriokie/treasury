"""A restore must not blow up on the way out.

Restoring replaces the database — and on a default Django install both the
session and the flash messages live in it.

The treasurer's session row is written when they sign in, which is necessarily
*after* the backup was taken, so it does not exist in the file that replaces the
live one. Django's session middleware then tries to save that session at the end
of the request, finds no row to update, and raises `SessionInterrupted`, which is
rendered as a 400.

The restore itself had already succeeded. So the treasurer saw a stack trace and
a half-familiar database, with nothing on the screen to say whether their data
had arrived or been destroyed — which is the worst possible moment to be
guessing.

Signing the user out is not a workaround for the exception. It is what actually
happened: the account that was signed in a moment ago may not exist in the
restored database, and if it does, its password is whatever it was when the
backup was taken.
"""
import datetime as dt
from unittest import mock

from django.contrib.auth.models import Group, User
from django.contrib.sessions.models import Session
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from core import roles
from core.services import backup as bk
from members.models import Member


class RestoreCompletesWithoutBreakingTheSessionTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user("tess-restore", password="office-pass-1")
        self.user.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.client = Client()
        self.client.force_login(self.user)

    #: What a real backup file looks like to the view. The test database runs
    #: in memory, so no genuine one can be taken here — and it is not needed:
    #: the fault being guarded is in what the VIEW does after a restore
    #: succeeds, not in the restore itself, which `core.test_backup_restore_export`
    #: covers against a file on disk.
    FAKE = b"SQLite format 3\x00" + b"\x00" * 8192

    def _backup(self):
        return SimpleUploadedFile("treasury-backup.sqlite3", self.FAKE)

    def _restore(self, upload, confirm="RESTORE", succeeds=True,
                 message="Database restored from backup. The previous database "
                         "was saved as db.sqlite3.pre-restore-20260727-120000."):
        with mock.patch("core.services.backup.database_restore",
                        return_value=(succeeds, message)), \
             mock.patch("core.views.database_restore",
                        return_value=(succeeds, message), create=True):
            return self.client.post(reverse("backup_restore"),
                                    {"backup_file": upload, "confirm": confirm})

    def test_a_successful_restore_does_not_raise(self):
        """The whole bug: this used to be a 400 with the data already restored."""
        response = self._restore(self._backup())
        self.assertEqual(
            response.status_code, 200,
            "Restore finished and then failed on the way out — the session "
            "middleware could not save a session whose row no longer exists.")

    def test_the_page_says_the_restore_finished(self):
        body = self._restore(self._backup()).content.decode()
        self.assertIn("Restore complete", body)

    def test_the_page_says_the_user_has_been_signed_out(self):
        """Otherwise being bounced to a login screen looks like a failure."""
        body = self._restore(self._backup()).content.decode()
        self.assertIn("signed out", body)

    def test_the_page_says_where_the_replaced_database_went(self):
        """The way back, if the restore was a mistake."""
        body = self._restore(self._backup()).content.decode()
        self.assertIn("pre-restore", body)

    def test_the_user_really_is_signed_out(self):
        self._restore(self._backup())
        response = self.client.get(reverse("settings"))
        self.assertNotEqual(response.status_code, 200)

    def test_the_session_row_is_cleared_rather_than_saved(self):
        """An empty session is what the middleware clears instead of saving."""
        self._restore(self._backup())
        self.assertFalse(self.client.session.items())

    def test_a_restore_that_fails_reports_it_and_keeps_the_session(self):
        """A failure must not sign anybody out — nothing was replaced."""
        response = self._restore(self._backup(), succeeds=False,
                                 message="That file is not a SQLite database.")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(reverse("settings")).status_code, 200)

    # -- the paths that must still behave -------------------------------------

    def test_an_unconfirmed_restore_changes_nothing_and_keeps_the_session(self):
        before = Member.objects.count()
        Member.objects.create(name="STILL HERE", phone="254700000111")
        response = self._restore(self._backup(), confirm="")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Member.objects.count(), before + 1)
        self.assertEqual(self.client.get(reverse("settings")).status_code, 200)

    def test_a_rejected_file_leaves_the_treasurer_signed_in(self):
        """A refusal is not a restore, so nothing about the session changed.

        This matters: being logged out by a file uploaded in error would suggest
        something drastic had happened when nothing had.
        """
        response = self.client.post(
            reverse("backup_restore"),
            {"backup_file": SimpleUploadedFile("notes.xlsx", b"PK\x03\x04 nope"),
             "confirm": "RESTORE"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(reverse("settings")).status_code, 200)

    def test_a_missing_file_is_reported_without_signing_out(self):
        response = self.client.post(reverse("backup_restore"),
                                    {"confirm": "RESTORE"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(reverse("settings")).status_code, 200)

    def test_only_a_treasurer_may_restore(self):
        assistant = User.objects.create_user("assist-restore", password="x")
        assistant.groups.add(Group.objects.get_or_create(name=roles.ASSISTANT)[0])
        other = Client()
        other.force_login(assistant)
        response = other.post(reverse("backup_restore"),
                              {"backup_file": self._backup(), "confirm": "RESTORE"})
        self.assertNotEqual(response.status_code, 200)
