"""Two register fixes.

1. Debits duplicating on the second import. When the dedup-key formula changed
   (v2.69, folding amount + narration in so a bank can share one reference across
   distinct movements), lines already in the register still carried the OLD
   bare-reference keys. The next import computed a new-format key that did not
   match, so the same line — debits especially, since they lean on the reference
   rather than a unique M-Pesa receipt — re-imported as a duplicate. `import_file`
   now recognises either key form.

2. Purging a register import uploaded the same day, to undo a wrong upload.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.roles import TREASURER
from statements.models import BankAccount
from statements.models_register import (RegisterException, StatementLine,
                                        StatementRegisterImport)
from statements.services import register as reg


def _debit_row(ref, amount, narr, date=dt.date(2026, 4, 13)):
    return {"date": date, "credit": None, "debit": Decimal(amount),
            "core_ref": ref, "mpesa_ref": ref, "receipt": "",
            "reference": narr, "name": "", "phone": "",
            "raw_narration": narr, "balance": Decimal("1000")}


class LegacyKeyReimportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("t_reg", password="x")
        self.account = BankAccount.objects.create(
            name="Main", is_default=True, active=True)

    def _store_line_with_legacy_key(self, row):
        """Store a line exactly as a pre-v2.69 register would: bare-reference key."""
        imp = StatementRegisterImport.objects.create(
            account=self.account, uploaded_by=self.user, filename="old.xls")
        line = StatementLine.objects.create(
            account=self.account, imported_in=imp, date=row["date"],
            credit=row["credit"], debit=row["debit"],
            core_ref=row["core_ref"], mpesa_ref=row["mpesa_ref"],
            receipt=row["receipt"], reference=row["reference"],
            raw_narration=row["raw_narration"],
            dedup_key=reg.dedup_key_legacy(row))
        return line

    def test_debit_stored_under_legacy_key_is_not_reimported(self):
        row = _debit_row("SYBINSE00099", "950000", "EDWIN CHQ NO.0003")
        legacy_line = self._store_line_with_legacy_key(row)
        # sanity: the legacy key differs from the current key
        self.assertNotEqual(reg.dedup_key(row), legacy_line.dedup_key)

        # now import a file containing the SAME debit (current key formula)
        import io
        # drive import_file through the row path by monkeypatching read_rows
        from unittest import mock
        with mock.patch("statements.services.parser.read_rows",
                        return_value=[row]):
            before = StatementLine.objects.count()
            reg.import_file(self.account, path_or_bytes=b"x", filename="new.xls",
                            user=self.user)
            after = StatementLine.objects.count()

        self.assertEqual(after - before, 0,
                         "a debit already in the register under the old key was "
                         "re-imported as a duplicate")

    def test_dedup_keys_returns_both_forms_when_they_differ(self):
        row = _debit_row("SYBINSE00099", "950000", "EDWIN CHQ NO.0003")
        keys = reg.dedup_keys(row)
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0], reg.dedup_key(row))       # current first
        self.assertEqual(keys[1], reg.dedup_key_legacy(row))

    def test_dedup_keys_collapses_when_forms_match(self):
        # a genuine M-Pesa receipt keys identically in both forms
        row = {"date": dt.date(2026, 1, 1), "credit": Decimal("100"),
               "debit": None, "core_ref": "", "mpesa_ref": "",
               "receipt": "UATKR5A7M8", "raw_narration": "x"}
        self.assertEqual(len(reg.dedup_keys(row)), 1)


class RegisterPurgeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("t_pg", password="x")
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.account = BankAccount.objects.create(
            name="Main", is_default=True, active=True)

    def _import(self, rows):
        from unittest import mock
        with mock.patch("statements.services.parser.read_rows", return_value=rows):
            return reg.import_file(self.account, path_or_bytes=b"x",
                                   filename="f.xls", user=self.user)

    def test_purge_removes_the_lines_it_added(self):
        rows = [_debit_row(f"REF{i}", "100", f"CHQ {i}") for i in range(3)]
        imp = self._import(rows)
        self.assertEqual(StatementLine.objects.count(), 3)

        result = reg.purge_import(imp, user=self.user)
        self.assertEqual(result["lines_removed"], 3)
        self.assertEqual(StatementLine.objects.count(), 0)
        imp.refresh_from_db()
        self.assertTrue(imp.is_purged)

    def test_purge_closes_exceptions_on_removed_lines(self):
        rows = [_debit_row("REFX", "500", "CHQ X")]
        imp = self._import(rows)
        line = StatementLine.objects.get()
        RegisterException.objects.create(
            account=self.account, kind=RegisterException.Kind.MISSING_IN_LEDGER,
            line=line, date=line.date, amount=line.signed_amount)

        reg.purge_import(imp, user=self.user)
        # the exception's line is gone; the exception must not dangle open
        self.assertFalse(
            RegisterException.objects.filter(
                status=RegisterException.Status.OPEN).exists())

    def test_purge_refused_after_upload_day(self):
        rows = [_debit_row("REFY", "500", "CHQ Y")]
        imp = self._import(rows)
        # backdate the upload to yesterday
        StatementRegisterImport.objects.filter(pk=imp.pk).update(
            uploaded_at=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc))
        imp.refresh_from_db()
        with self.assertRaises(Exception):
            reg.purge_import(imp, user=self.user)

    def test_reimport_after_purge_reads_clean(self):
        rows = [_debit_row("REFZ", "500", "CHQ Z")]
        imp = self._import(rows)
        reg.purge_import(imp, user=self.user)
        # the same file can now be re-imported afresh
        imp2 = self._import(rows)
        self.assertEqual(imp2.lines_added, 1)


class RegisterPurgeViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("t_pv", password="x")
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.account = BankAccount.objects.create(
            name="Main", is_default=True, active=True)

    def test_purge_button_removes_lines_via_view(self):
        from unittest import mock
        rows = [_debit_row(f"R{i}", "100", f"CHQ {i}") for i in range(2)]
        with mock.patch("statements.services.parser.read_rows", return_value=rows):
            imp = reg.import_file(self.account, path_or_bytes=b"x",
                                  filename="f.xls", user=self.user)
        self.assertEqual(StatementLine.objects.count(), 2)

        self.client.force_login(self.user)
        resp = self.client.post(
            f"/bank-register/import/?account={self.account.pk}",
            {"purge": str(imp.pk)}, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(StatementLine.objects.count(), 0)
        imp.refresh_from_db()
        self.assertTrue(imp.is_purged)

    def test_import_page_shows_undo_for_todays_upload(self):
        from unittest import mock
        rows = [_debit_row("R9", "100", "CHQ 9")]
        with mock.patch("statements.services.parser.read_rows", return_value=rows):
            reg.import_file(self.account, path_or_bytes=b"x", filename="f.xls",
                            user=self.user)
        self.client.force_login(self.user)
        resp = self.client.get(f"/bank-register/import/?account={self.account.pk}")
        self.assertContains(resp, "Undo")
