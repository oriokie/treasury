"""#4 Reconciliation auto-includes staff advances + unpresented cheques."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from cashbook.models import Expense, PaymentInstrument
from statements.models import BankReconciliation, ReconciliationItem
from ledger.services.posting import ensure_chart


def _treasurer():
    u = User.objects.create_user("tr", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class AutoReconTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.tr = _treasurer()
        self.c = Client(); self.c.force_login(self.tr)
        self.d = Department.objects.create(name="LCB", fund_type="LOCAL",
            category="OFFERING", show_in_expenses=True)
        self.e = Expense.objects.create(date=dt.date(2026, 6, 1), department=self.d,
            description="Rent", amount=Decimal("2500"), category="UTILITIES",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        PaymentInstrument.objects.create(method="CHEQUE", instrument_number="CHQ1",
            payee="Vendor", amount=Decimal("2500"), date_issued=dt.date(2026, 6, 1),
            status="OUTSTANDING", source_kind="EXPENSE", expense=self.e,
            recorded_by=self.tr)
        self.rec = BankReconciliation.objects.create(
            statement_date=dt.date(2026, 6, 30), bank_balance=Decimal("0"),
            created_by=self.tr)

    def test_unpresented_cheques_auto_added(self):
        self.c.get(f"/reconciliations/{self.rec.id}/")
        item = ReconciliationItem.objects.filter(reconciliation=self.rec,
            description__icontains="Unpresented cheques").first()
        self.assertIsNotNone(item)
        self.assertEqual(item.amount, Decimal("2500"))
        self.assertEqual(item.effect, "SUBTRACT")
        self.assertTrue(item.auto)

    def test_no_manual_buttons(self):
        body = self.c.get(f"/reconciliations/{self.rec.id}/").content.decode()
        self.assertNotIn("add_unpresented_cheques", body)
        self.assertNotIn("add_petty_cash", body)
        self.assertNotIn("add_advances", body)

    def test_cleared_cheque_removed_from_recon(self):
        self.c.get(f"/reconciliations/{self.rec.id}/")
        PaymentInstrument.objects.filter(instrument_number="CHQ1").update(status="CLEARED")
        self.c.get(f"/reconciliations/{self.rec.id}/")
        self.assertFalse(ReconciliationItem.objects.filter(reconciliation=self.rec,
            description__icontains="Unpresented cheques").exists())

    def test_advances_auto_added(self):
        from cashbook.models import StaffAdvance
        StaffAdvance.objects.create(staff_name="John", amount=Decimal("3000"),
            date_issued=dt.date(2026, 6, 1), from_petty_cash=False,
            department=self.d, purpose="travel", method="BANK",
            issued_by=self.tr)
        self.c.get(f"/reconciliations/{self.rec.id}/")
        self.assertTrue(ReconciliationItem.objects.filter(reconciliation=self.rec,
            description__icontains="Staff advances").exists())
