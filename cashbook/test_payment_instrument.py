"""Payment-instrument framework: source-required, lifecycle, accounting integrity."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from cashbook.models import (Expense, RemittanceBatch, PaymentInstrument)
from ledger.models import JournalEntry
from ledger.services.posting import ensure_chart


def _treasurer():
    u = User.objects.create_user("tr", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class PaymentInstrumentTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.tr = _treasurer()
        self.c = Client(); self.c.force_login(self.tr)
        self.d = Department.objects.create(name="Build", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=True)
        self.e = Expense.objects.create(date=dt.date(2026, 6, 1), department=self.d,
            description="Chairs", amount=Decimal("8000"), category="MATERIALS",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)

    def _add(self, **kw):
        data = {"action": "add", "method": "CHEQUE", "instrument_number": "001234",
                "source_kind": "EXPENSE", "source_id": str(self.e.id),
                "payee": "ACME", "amount": "8000", "date_issued": "2026-06-02"}
        data.update(kw)
        return self.c.post("/payments/", data)

    def test_source_required(self):
        before = PaymentInstrument.objects.count()
        self._add(source_kind="EXPENSE", source_id="")
        self.assertEqual(PaymentInstrument.objects.count(), before)

    def test_instrument_posts_no_journal(self):
        j0 = JournalEntry.objects.count()
        self._add()
        self.assertEqual(JournalEntry.objects.count(), j0)

    def test_full_lifecycle(self):
        self._add()
        inst = PaymentInstrument.objects.get(instrument_number="001234")
        self.assertEqual(inst.status, "DRAFT")
        self.c.post("/payments/", {"action": "approve", "pk": inst.id})
        inst.refresh_from_db(); self.assertEqual(inst.status, "APPROVED")
        self.assertEqual(inst.approved_by_id, self.tr.id)
        self.c.post("/payments/", {"action": "issue", "pk": inst.id})
        inst.refresh_from_db()
        self.assertEqual(inst.status, "ISSUED"); self.assertTrue(inst.is_outstanding)
        self.c.post("/payments/", {"action": "clear", "pk": inst.id})
        inst.refresh_from_db()
        self.assertEqual(inst.status, "CLEARED"); self.assertTrue(inst.is_locked)
        self.assertIsNotNone(inst.date_cleared)

    def test_cleared_cannot_be_voided_or_deleted(self):
        self._add()
        inst = PaymentInstrument.objects.get(instrument_number="001234")
        inst.clear()
        self.c.post("/payments/", {"action": "void", "pk": inst.id})
        inst.refresh_from_db(); self.assertEqual(inst.status, "CLEARED")
        self.c.post("/payments/", {"action": "delete", "pk": inst.id})
        self.assertTrue(PaymentInstrument.objects.filter(pk=inst.id).exists())

    def test_clearing_posts_no_journal(self):
        self._add()
        inst = PaymentInstrument.objects.get(instrument_number="001234")
        j0 = JournalEntry.objects.count()
        inst.issue(); inst.clear()
        self.assertEqual(JournalEntry.objects.count(), j0)

    def test_remittance_settles_without_extra_journal(self):
        b = RemittanceBatch.objects.create(total_amount=Decimal("15000"),
            cheque_no="RMT1", cheque_date=dt.date(2026, 6, 3), status="DRAFT",
            created_by=self.tr)
        j0 = JournalEntry.objects.count()
        self.c.post("/payments/", {"action": "add", "method": "CHEQUE",
            "instrument_number": "RMT1", "source_kind": "REMITTANCE",
            "source_id": str(b.id), "payee": "Conference", "amount": "15000",
            "date_issued": "2026-06-03"})
        inst = PaymentInstrument.objects.get(instrument_number="RMT1")
        self.assertEqual(inst.remittance_batch_id, b.id)
        inst.issue(); inst.clear()
        self.assertEqual(JournalEntry.objects.count(), j0)

    def test_manual_payment_allowed_for_treasurer(self):
        self.c.post("/payments/", {"action": "add", "method": "EFT",
            "instrument_number": "EFT1", "source_kind": "MANUAL",
            "payee": "Supplier", "amount": "1200"})
        self.assertTrue(PaymentInstrument.objects.filter(instrument_number="EFT1").exists())

    def test_other_methods_supported(self):
        for m in ["EFT", "RTGS", "MPESA"]:
            self.c.post("/payments/", {"action": "add", "method": m,
                "instrument_number": f"{m}9", "source_kind": "EXPENSE",
                "source_id": str(self.e.id), "amount": "100"})
        self.assertEqual(PaymentInstrument.objects.exclude(method="CHEQUE").count(), 3)

    def test_print_and_outstanding_pages(self):
        self._add()
        inst = PaymentInstrument.objects.get(instrument_number="001234")
        self.assertEqual(self.c.get(f"/payments/{inst.id}/print/").status_code, 200)
        self.assertEqual(self.c.get("/payments/outstanding/").status_code, 200)
        self.assertEqual(self.c.get("/payments/outstanding/?export=csv").status_code, 200)

    def test_amount_in_words(self):
        from cashbook.views import _amount_in_words
        self.assertEqual(_amount_in_words(Decimal("8000")),
                         "Eight thousand shillings only")
        self.assertIn("cents", _amount_in_words(Decimal("1250.50")))


class NonTreasurerTests(TestCase):
    def test_non_treasurer_cannot_manage(self):
        ensure_chart()
        u = User.objects.create_user("assist", password="x")
        u.groups.add(Group.objects.get_or_create(name="Assistant")[0])
        c = Client(); c.force_login(u)
        before = PaymentInstrument.objects.count()
        c.post("/payments/", {"action": "add", "method": "CHEQUE",
            "source_kind": "MANUAL", "amount": "500"})
        self.assertEqual(PaymentInstrument.objects.count(), before)
