"""Envelope-receipted bank credits aren't counted as 'pending allocation' (#4)."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase
from giving.models import Transaction
from reports.services.balances import pending_receipts_total


class PendingReceiptsTests(TestCase):
    def test_genuine_pending_counts(self):
        base = pending_receipts_total()
        Transaction.objects.create(date=dt.date(2026, 6, 1), channel="BANK",
            direction="CREDIT", amount=Decimal("1000"), allocation_status="REVIEW",
            confirmed=True, department=None, core_ref="P1")
        self.assertEqual(pending_receipts_total() - base, Decimal("1000"))

    def test_envelope_receipted_not_pending(self):
        base = pending_receipts_total()
        Transaction.objects.create(date=dt.date(2026, 6, 2), channel="BANK",
            direction="CREDIT", amount=Decimal("5000"), allocation_status="MANUAL",
            confirmed=True, department=None, processed_via_envelope=True,
            excluded_from_income=True, core_ref="E1")
        self.assertEqual(pending_receipts_total(), base)

    def test_manual_receipt_not_pending(self):
        base = pending_receipts_total()
        Transaction.objects.create(date=dt.date(2026, 6, 3), channel="BANK",
            direction="CREDIT", amount=Decimal("700"), allocation_status="MANUAL",
            confirmed=True, department=None, manual_receipt=True, core_ref="M1")
        self.assertEqual(pending_receipts_total(), base)
