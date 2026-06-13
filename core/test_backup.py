import base64
import glob
import os
import tempfile
from unittest import mock

from django.test import TestCase
from django.core.management import call_command


# A fake dump payload so the test exercises the command's encrypt/rotate/write
# logic without depending on snapshotting the (transactional) test database.
FAKE = (b"SQLite format 3\x00" + b"the-quick-brown-fox" * 50)


class BackupCommandTests(TestCase):
    def test_backup_writes_encrypted_and_rotates(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch("core.management.commands.backup_db.database_backup_bytes") as m:
            import itertools
            counter = itertools.count()
            # distinct filename each call so rotation has separate files
            m.side_effect = lambda: (f"treasury-backup-2026010{next(counter)}-000000.sqlite3", FAKE)
            for _ in range(3):
                call_command("backup_db", out=d, keep=2)
            files = sorted(glob.glob(os.path.join(d, "treasury-backup-*")))
            self.assertEqual(len(files), 2)                 # rotation kept 2
            self.assertTrue(all(f.endswith(".enc") for f in files))
            from core.fields import decrypt
            data = base64.b64decode(decrypt(open(files[-1]).read()))
            self.assertEqual(data, FAKE)                    # round-trips exactly

    def test_no_encrypt_flag_writes_raw(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch("core.management.commands.backup_db.database_backup_bytes") as m:
            m.return_value = ("treasury-backup-20260101-000000.sqlite3", FAKE)
            call_command("backup_db", out=d, keep=5, no_encrypt=True)
            files = glob.glob(os.path.join(d, "treasury-backup-*"))
            self.assertEqual(len(files), 1)
            self.assertFalse(files[0].endswith(".enc"))
            self.assertEqual(open(files[0], "rb").read(), FAKE)

    def test_backup_failure_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch("core.management.commands.backup_db.database_backup_bytes") as m:
            m.side_effect = RuntimeError("no db")
            # should not raise — just write nothing
            call_command("backup_db", out=d)
            self.assertEqual(glob.glob(os.path.join(d, "treasury-backup-*")), [])
