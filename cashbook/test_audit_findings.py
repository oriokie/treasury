"""Regression tests for two accounting-integrity findings from a full audit
review: (1) an envelope's linked transactions must stay in sync with the
general ledger when their date is corrected, and (2) a posted (approved/paid)
expense must not be hard-deletable — it has already reached the ledger and
must be reversed, not erased."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from cashbook.models import Expense
from giving.models import Transaction
from ledger.services.posting import ensure_chart
from ledger.models import JournalEntry


def _tr():
    u = User.objects.create_user("tr_audit1", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class EnvelopeDateLedgerSyncTests(TestCase):
    """A bulk .update() on linked transactions' dates bypasses the post_save
    signal that posts to the ledger, leaving journal entries dated under the
    old Sabbath after a correction. Moving an envelope must re-post them."""
    def setUp(self):
        ensure_chart()
        self.tr = _tr()
        self.d = Department.objects.create(name="EnvLedgerFund", fund_type="LOCAL",
            category="OFFERING")

    def test_ledger_entry_date_follows_envelope_move(self):
        from envelopes.models import Envelope, EnvelopeLine
        old_date = dt.date(2026, 6, 6)
        new_date = dt.date(2026, 6, 13)
        env = Envelope.objects.create(date=old_date, receipt_no="ENV-AUDIT-1",
            recorded_by=self.tr)
        txn = Transaction.objects.create(date=old_date, amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="ENVELOPE",
            allocation_status="MANUAL", department=self.d)
        EnvelopeLine.objects.create(envelope=env, department=self.d,
            amount=Decimal("500"), transaction=txn)

        c = Client(); c.force_login(self.tr)
        c.post(f"/envelopes/{env.id}/reassign/", {"sabbath": new_date.isoformat()})

        txn.refresh_from_db()
        self.assertEqual(txn.date, new_date)
        je = JournalEntry.objects.filter(source_type="transaction", source_id=txn.pk).first()
        self.assertIsNotNone(je)
        self.assertEqual(je.date, new_date,
            "journal entry must move with the corrected transaction date, "
            "not silently stay under the old Sabbath")


class PostedExpenseDeleteProtectionTests(TestCase):
    """Only a PENDING expense (no ledger effect yet) may be hard-deleted.
    An APPROVED or PAID expense is already posted — deleting it would erase
    it from the general ledger with no trace; a refund/reversal is required."""
    def setUp(self):
        ensure_chart()
        self.tr = _tr()
        self.d = Department.objects.create(name="DelProtFund", fund_type="LOCAL",
            category="MINISTRY")
        self.c = Client(); self.c.force_login(self.tr)

    def test_pending_expense_still_deletable(self):
        exp = Expense.objects.create(date=dt.date(2026, 6, 5), department=self.d,
            description="Pending item", amount=Decimal("500"), category="OTHER",
            status="PENDING", recorded_by=self.tr)
        self.c.post(f"/expenses/{exp.id}/delete/")
        self.assertFalse(Expense.objects.filter(id=exp.id).exists())

    def test_approved_expense_not_deletable(self):
        exp = Expense.objects.create(date=dt.date(2026, 6, 5), department=self.d,
            description="Approved item", amount=Decimal("500"), category="OTHER",
            status="APPROVED", recorded_by=self.tr, approved_by=self.tr)
        self.c.post(f"/expenses/{exp.id}/delete/")
        self.assertTrue(Expense.objects.filter(id=exp.id).exists())

    def test_paid_expense_not_deletable(self):
        exp = Expense.objects.create(date=dt.date(2026, 6, 5), department=self.d,
            description="Paid item", amount=Decimal("500"), category="OTHER",
            status="PAID", recorded_by=self.tr, approved_by=self.tr,
            paid_date=dt.date(2026, 6, 6))
        self.c.post(f"/expenses/{exp.id}/delete/")
        self.assertTrue(Expense.objects.filter(id=exp.id).exists())
        je = JournalEntry.objects.filter(source_type="expense", source_id=exp.pk).first()
        self.assertIsNotNone(je, "posted expense must remain in the ledger")

    def test_paid_expense_delete_shows_reversal_guidance(self):
        exp = Expense.objects.create(date=dt.date(2026, 6, 5), department=self.d,
            description="Paid item 2", amount=Decimal("500"), category="OTHER",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        r = self.c.post(f"/expenses/{exp.id}/delete/", follow=True)
        b = r.content.decode()
        self.assertIn("refund", b.lower())
