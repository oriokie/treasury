"""Mark-processed apply path: bank memos must withdraw ledger entries (not
repost as INC_OTHER), and bulk apply must mark many ids without a per-id
SELECT round-trip pattern that times out large pastes.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from departments.models import Department
from giving.models import Transaction
from ledger.models import JournalEntry
from ledger.services import posting


class MarkProcessedMemoLedgerTests(TestCase):
    def setUp(self):
        posting.ensure_chart()
        self.user = User.objects.create_superuser("mpp", password="x")
        self.client.login(username="mpp", password="x")
        self.fund = Department.objects.create(
            name="MppFund", fund_type=Department.FundType.LOCAL)
        self.txn = Transaction.objects.create(
            date=dt.date(2026, 6, 1), channel="BANK", direction="CREDIT",
            amount=Decimal("1500"), department=self.fund, reference="MPP-REF-1",
            allocation_status="AUTO", confirmed=True)
        posting.post_transaction(self.txn)
        self.assertTrue(
            JournalEntry.objects.filter(
                source_type="transaction", source_id=self.txn.pk).exists())

    def test_mark_manual_receipt_withdraws_journal_not_reposts(self):
        self.txn.mark_manual_receipt(True)
        self.txn.refresh_from_db()
        self.assertTrue(self.txn.manual_receipt)
        self.assertTrue(self.txn.is_bank_memo)
        self.assertFalse(
            JournalEntry.objects.filter(
                source_type="transaction", source_id=self.txn.pk).exists(),
            "a bank memo must leave no journal — income lives on the envelope")

    def test_bulk_apply_marks_all_chunked_ids(self):
        extras = []
        for i in range(45):
            extras.append(Transaction.objects.create(
                date=dt.date(2026, 6, 1), channel="BANK", direction="CREDIT",
                amount=Decimal("100"), department=self.fund,
                reference=f"MPP-BULK-{i}", allocation_status="AUTO",
                confirmed=True))
        plan = [{
            "status": "ok", "reference": t.reference, "amount": None,
            "txn_ids": [t.id], "matches": [], "detail": "",
        } for t in [self.txn, *extras]]
        session = self.client.session
        session["mark_processed_plan"] = plan
        session.save()
        r = self.client.post(reverse("mark_processed_import"), {"apply": "1"})
        self.assertEqual(r.status_code, 302)
        marked = Transaction.objects.filter(
            reference__startswith="MPP-", manual_receipt=True).count()
        self.assertEqual(marked, 46)
