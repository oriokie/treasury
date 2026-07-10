"""CRUD on payables/accruals/prepayments (safe for settled) + settle against an
already-entered expense (#1)."""
import datetime as dt
from decimal import Decimal

from django.test import TestCase, Client
from django.contrib.auth.models import User, Group

from departments.models import Department
from cashbook.models import Payable, Accrual, Prepayment, Expense


class ObligationCrudTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("oc", password="x", is_superuser=True)
        self.u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(self.u)
        self.fund = Department.objects.create(name="LCB", fund_type="LOCAL",
            category="OFFERING", show_in_expenses=True, is_trust=False, active=True)

    def _payable(self, **kw):
        d = dict(date=dt.date(2026, 6, 1), vendor="Acme", description="Chairs",
                 amount=Decimal("5000"), department=self.fund, category="MATERIALS",
                 recorded_by=self.u)
        d.update(kw); return Payable.objects.create(**d)

    def test_edit_payable(self):
        p = self._payable()
        self.assertEqual(self.c.get(f"/payables/payable/{p.id}/edit/").status_code, 200)
        self.c.post(f"/payables/payable/{p.id}/edit/", {
            "date": "2026-06-02", "vendor": "Acme Ltd", "description": "Chairs x10",
            "amount": "5500", "department": str(self.fund.id), "category": "MATERIALS"})
        p.refresh_from_db()
        self.assertEqual(p.vendor, "Acme Ltd"); self.assertEqual(p.amount, Decimal("5500"))

    def test_delete_unsettled_payable(self):
        p = self._payable()
        self.c.post(f"/payables/payable/{p.id}/delete/")
        self.assertFalse(Payable.objects.filter(pk=p.id).exists())

    def test_settled_is_readonly(self):
        e = Expense.objects.create(date=dt.date(2026, 6, 3), department=self.fund,
            description="paid", amount=Decimal("5000"), category="MATERIALS",
            status="PAID", recorded_by=self.u)
        p = self._payable(settled=True, settled_expense=e)
        self.assertEqual(self.c.get(f"/payables/payable/{p.id}/edit/").status_code, 302)
        self.c.post(f"/payables/payable/{p.id}/delete/")
        self.assertTrue(Payable.objects.filter(pk=p.id).exists())   # not deleted

    def test_settle_against_existing_expense(self):
        p = self._payable()
        e = Expense.objects.create(date=dt.date(2026, 6, 3), department=self.fund,
            description="Chairs paid", amount=Decimal("5000"), category="MATERIALS",
            status="PAID", recorded_by=self.u)
        r = self.c.get(f"/payables/payable/{p.id}/settle-existing/")
        self.assertIn("Chairs paid", r.content.decode())
        self.c.post(f"/payables/payable/{p.id}/settle-existing/", {"expense": str(e.id)})
        p.refresh_from_db()
        self.assertTrue(p.settled); self.assertEqual(p.settled_expense_id, e.id)
        # the expense was not duplicated
        self.assertEqual(Expense.objects.filter(description="Chairs paid").count(), 1)

    def test_delete_accrual(self):
        a = Accrual.objects.create(date=dt.date(2026, 6, 1), description="Power",
            amount=Decimal("800"), department=self.fund, category="UTILITIES",
            recorded_by=self.u)
        self.c.post(f"/payables/accrual/{a.id}/delete/")
        self.assertFalse(Accrual.objects.filter(pk=a.id).exists())
