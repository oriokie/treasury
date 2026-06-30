"""Payment register (PaymentInstrument) — add/clear, sync, reconciliation wiring.
The register now treats cheques as payment instruments; these tests cover the
cheque-method path and its reconciliation integration."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from cashbook.models import Expense, PaymentInstrument
from cashbook.views import unpresented_cheques_total
from statements.models import BankReconciliation, ReconciliationItem
from ledger.services.posting import ensure_chart


class ChequeRegisterTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.u = User.objects.create_user("cr", password="x", is_superuser=True)
        self.u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(self.u)
        self.d = Department.objects.create(name="LCB", fund_type="LOCAL",
            category="OFFERING", show_in_expenses=True)
        self.e = Expense.objects.create(date=dt.date(2026, 6, 1), department=self.d,
            description="Rent", amount=Decimal("5000"), category="UTILITIES",
            status="PAID", recorded_by=self.u, approved_by=self.u)

    def _add(self, num, amount, **kw):
        data = {"action": "add", "method": "CHEQUE", "instrument_number": num,
                "source_kind": "EXPENSE", "source_id": str(self.e.id),
                "payee": "ACME", "amount": amount, "date_issued": "2026-06-01"}
        data.update(kw)
        return self.c.post("/payments/", data)

    def test_add_issue_and_clear(self):
        self._add("000123", "5000")
        chq = PaymentInstrument.objects.get(instrument_number="000123")
        self.assertEqual(chq.status, "DRAFT")
        chq.issue()
        self.assertEqual(unpresented_cheques_total(), Decimal("5000"))
        self.c.post("/payments/", {"action": "clear", "pk": str(chq.id)})
        chq.refresh_from_db()
        self.assertEqual(chq.status, "CLEARED")
        self.assertIsNotNone(chq.date_cleared)
        self.assertEqual(unpresented_cheques_total(), Decimal("0"))

    def test_void(self):
        self._add("000200", "100")
        chq = PaymentInstrument.objects.get(instrument_number="000200")
        self.c.post("/payments/", {"action": "void", "pk": str(chq.id)})
        chq.refresh_from_db()
        self.assertEqual(chq.status, "VOIDED")

    def test_sync_from_expenses(self):
        Expense.objects.create(date=dt.date(2026, 6, 2), department=self.d,
            description="Rent2", amount=Decimal("8000"), category="UTILITIES",
            method="CHEQUE", voucher_no="000456", status="PAID",
            paid_date=dt.date(2026, 6, 2), recorded_by=self.u)
        self.c.post("/payments/", {"action": "sync"})
        self.assertTrue(PaymentInstrument.objects.filter(
            instrument_number="000456").exists())

    def test_reconciliation_wiring(self):
        chq = PaymentInstrument.objects.create(method="CHEQUE",
            instrument_number="000999", payee="Vendor", amount=Decimal("2500"),
            date_issued=dt.date(2026, 6, 1), status="OUTSTANDING",
            source_kind="EXPENSE", expense=self.e, recorded_by=self.u)
        rec = BankReconciliation.objects.create(statement_date=dt.date(2026, 6, 30),
            bank_balance=Decimal("0"), created_by=self.u)
        b = self.c.get(f"/reconciliations/{rec.id}/").content.decode()
        self.assertIn("000999", b)
        self.c.post(f"/reconciliations/{rec.id}/", {"action": "add_unpresented_cheques"})
        items = ReconciliationItem.objects.filter(reconciliation=rec, kind="UNPRESENTED")
        self.assertEqual(items.count(), 1)
        self.assertEqual(items.first().amount, Decimal("2500"))
        # idempotent
        self.c.post(f"/reconciliations/{rec.id}/", {"action": "add_unpresented_cheques"})
        self.assertEqual(ReconciliationItem.objects.filter(
            reconciliation=rec, kind="UNPRESENTED").count(), 1)
