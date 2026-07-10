"""Regression: cheque/transfer debits must not collide on a generic narration
word (the 'first debit imported, rest duplicated' bug), and cheque payments
should auto-reconcile to their expense and mark it cleared (PAID)."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase
from django.contrib.auth.models import User
from departments.models import Department
from cashbook.models import Expense
from giving.models import Transaction
from statements.models import StatementImport
from statements.services.importer import run_import
from statements.services.parser import parse_narration
from statements.services.reconcile import _cheque_numbers
from statements.services import reconcile


# a statement where every debit is a cheque whose narration begins "CHQ No."
CSV = b"""Posting Date,Narration,Core Ref,Channel REF,Debit Amount,Credit Amount,Running Balance
01-07-2026,CHQ No.000411,CB0001,SYB0001,15500.00,0.00,100000.00
02-07-2026,CHQ No.000410,CB0002,SYB0002,15500.00,0.00,84500.00
02-07-2026,CHQ No.000409,CB0003,SYB0003,36148.00,0.00,48352.00
02-07-2026,HENRY CHQ No.000408,CB0004,SYB0004,12000.00,0.00,36352.00
"""


class ChequeDebitImportTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("imp_chq", password="x")

    def _import(self, content):
        imp = StatementImport.objects.create(uploaded_by=self.u, filename="s.csv")
        run_import(imp, content, "s.csv")
        imp.refresh_from_db()
        return imp

    def test_all_cheque_debits_import(self):
        imp = self._import(CSV)
        self.assertEqual(imp.total_rows, 4)
        self.assertEqual(imp.duplicates_skipped, 0)  # was 3 before the fix
        self.assertEqual(Transaction.objects.filter(direction="DEBIT").count(), 4)

    def test_generic_word_not_used_as_receipt(self):
        # "CHQ" / "HENRY" must never become the bank_receipt dedup key
        self._import(CSV)
        for t in Transaction.objects.filter(direction="DEBIT"):
            self.assertIn(t.bank_receipt, (None, ""))

    def test_reimport_dedups_on_core_ref(self):
        self._import(CSV)
        n = Transaction.objects.count()
        imp2 = self._import(CSV)
        self.assertEqual(imp2.duplicates_skipped, 4)
        self.assertEqual(Transaction.objects.count(), n)

    def test_cheque_number_parsed(self):
        self.assertEqual(_cheque_numbers("CHQ No.000411"), {"411"})
        self.assertEqual(parse_narration("CHQ No.000411")["receipt"], "")


class ChequeReconcileTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("rec_chq", password="x", is_superuser=True)
        self.d = Department.objects.create(name="Maint", fund_type="LOCAL",
                                           category="MINISTRY")

    def _debit(self, amount, date, narration, ref):
        return Transaction.objects.create(date=date, amount=Decimal(amount),
            direction="DEBIT", channel="BANK", allocation_status="REVIEW",
            core_ref=ref, raw_narration=narration, confirmed=True)

    def test_cheque_auto_reconciles_and_clears(self):
        exp = Expense.objects.create(date=dt.date(2026, 7, 1), department=self.d,
            description="Venue", amount=Decimal("15500.00"), category="MAINTENANCE",
            method="CHEQUE", voucher_no="000411", status="APPROVED",
            recorded_by=self.u, approved_by=self.u, claimant="ABC Ltd")
        deb = self._debit("15500.00", dt.date(2026, 7, 4), "CHQ No.000411", "CBX411")
        res = reconcile.run_auto_reconcile(user=self.u)
        self.assertEqual(res["auto"], 1)
        exp.refresh_from_db()
        self.assertEqual(exp.bank_transaction_id, deb.id)
        self.assertEqual(exp.status, "PAID")
        self.assertEqual(exp.paid_date, dt.date(2026, 7, 4))

    def test_wrong_cheque_number_does_not_autolink(self):
        Expense.objects.create(date=dt.date(2026, 7, 1), department=self.d,
            description="Other", amount=Decimal("15500.00"), category="MAINTENANCE",
            method="CHEQUE", voucher_no="000999", status="APPROVED",
            recorded_by=self.u, approved_by=self.u)
        self._debit("15500.00", dt.date(2026, 7, 20), "CHQ No.000411", "CBX999")
        res = reconcile.run_auto_reconcile(user=self.u)
        # amount matches but 19 days apart and wrong cheque no -> not auto
        self.assertEqual(res["auto"], 0)
