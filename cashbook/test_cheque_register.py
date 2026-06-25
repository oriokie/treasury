"""Cheque register: track issued cheques, clearing, sync, and reconciliation
wiring (#3)."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from cashbook.models import Expense, ChequeRegister
from cashbook.views import unpresented_cheques_total
from statements.models import BankReconciliation, ReconciliationItem


class ChequeRegisterTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("cr", password="x", is_superuser=True)
        self.u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(self.u)
        self.d = Department.objects.create(name="LCB", fund_type="LOCAL",
            category="OFFERING", show_in_expenses=True)

    def test_add_and_clear(self):
        self.c.post("/cheques/", {"action": "add", "cheque_number": "000123",
            "payee": "ACME", "amount": "5000", "date_issued": "2026-06-01"})
        chq = ChequeRegister.objects.get(cheque_number="000123")
        self.assertEqual(chq.status, "ISSUED")
        self.assertEqual(unpresented_cheques_total(), Decimal("5000"))
        self.c.post("/cheques/", {"action": "clear", "pk": str(chq.id)})
        chq.refresh_from_db()
        self.assertEqual(chq.status, "CLEARED")
        self.assertIsNotNone(chq.date_cleared)
        self.assertEqual(unpresented_cheques_total(), Decimal("0"))

    def test_bounce(self):
        self.c.post("/cheques/", {"action": "add", "cheque_number": "000200",
            "amount": "100", "date_issued": "2026-06-01"})
        chq = ChequeRegister.objects.get(cheque_number="000200")
        self.c.post("/cheques/", {"action": "bounce", "pk": str(chq.id)})
        chq.refresh_from_db()
        self.assertEqual(chq.status, "BOUNCED")

    def test_sync_from_expenses(self):
        Expense.objects.create(date=dt.date(2026, 6, 2), department=self.d,
            description="Rent", amount=Decimal("8000"), category="UTILITIES",
            method="CHEQUE", voucher_no="000456", status="PAID",
            paid_date=dt.date(2026, 6, 2), recorded_by=self.u)
        self.c.post("/cheques/", {"action": "sync"})
        self.assertTrue(ChequeRegister.objects.filter(cheque_number="000456").exists())

    def test_reconciliation_wiring(self):
        ChequeRegister.objects.create(cheque_number="000999", payee="Vendor",
            amount=Decimal("2500"), date_issued=dt.date(2026, 6, 1),
            status="ISSUED", recorded_by=self.u)
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
