"""Legacy 'Remit trust funds' now uses the unified PaymentInstrument workflow:
it creates a remittance batch settled by a single generic payment instrument."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import Transaction
from cashbook.models import RemittanceBatch, PaymentInstrument, Expense
from ledger.models import JournalEntry
from ledger.services.posting import ensure_chart


def _treasurer():
    u = User.objects.create_user("tr", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class LegacyRemitPaymentTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        ensure_chart()
        self.tr = _treasurer()
        self.c = Client(); self.c.force_login(self.tr)
        self.trust = Department.objects.create(name="ENF", fund_type="TRUST",
            category="TRUST", is_trust=True)
        Transaction.objects.create(date=dt.date(2026, 6, 1), amount=Decimal("9000"),
            department=self.trust, direction="CREDIT", confirmed=True,
            channel="BANK", allocation_status="MANUAL", manual_receipt=True)

    def _remit(self, **kw):
        data = {"start": "2026-06-01", "end": "2026-06-30", "method": "EFT",
                "instrument_number": "EFT-902", "date_issued": "2026-06-30"}
        data.update(kw)
        return self.c.post("/reports/remittance/remit/", data)

    def test_creates_batch_settled_by_payment(self):
        n0 = RemittanceBatch.objects.count()
        self._remit()
        self.assertEqual(RemittanceBatch.objects.count(), n0 + 1)
        b = RemittanceBatch.objects.order_by("-id").first()
        self.assertEqual(b.status, "REMITTED")
        self.assertIsNotNone(b.payment_id)
        self.assertEqual(b.payment.method, "EFT")
        self.assertEqual(b.payment.instrument_number, "EFT-902")
        self.assertTrue(b.is_settled)

    def test_expenses_linked_to_batch(self):
        self._remit()
        b = RemittanceBatch.objects.order_by("-id").first()
        ex = Expense.objects.filter(remittance_batch=b, category="REMITTANCE").first()
        self.assertIsNotNone(ex)
        self.assertEqual(ex.status, "PAID")
        self.assertEqual(ex.voucher_no, "EFT-902")

    def test_clearing_posts_no_journal(self):
        self._remit()
        b = RemittanceBatch.objects.order_by("-id").first()
        j0 = JournalEntry.objects.count()
        self.c.post("/payments/", {"action": "clear", "pk": b.payment.id})
        b.payment.refresh_from_db()
        self.assertEqual(b.payment.status, "CLEARED")
        self.assertEqual(JournalEntry.objects.count(), j0)

    def test_cheque_method_keeps_legacy_fields(self):
        self._remit(method="CHEQUE", instrument_number="00777")
        b = RemittanceBatch.objects.order_by("-id").first()
        self.assertEqual(b.payment.method, "CHEQUE")
        self.assertEqual(b.cheque_no, "00777")

    def test_nothing_outstanding(self):
        # remove all trust receipts so there is nothing to remit
        Transaction.objects.all().delete()
        from django.core.cache import cache
        cache.clear()
        n0 = RemittanceBatch.objects.count()
        self.c.post("/reports/remittance/remit/", {"start": "2030-01-01",
            "end": "2030-01-31", "method": "EFT"})
        self.assertEqual(RemittanceBatch.objects.count(), n0)  # no empty batch

    def test_form_has_generic_payment_fields(self):
        body = self.c.get("/reports/remittance/?start=2026-06-01&end=2026-06-30").content.decode()
        self.assertIn('name="method"', body)
        self.assertIn('name="instrument_number"', body)
        self.assertNotIn('placeholder="Cheque no."', body)
