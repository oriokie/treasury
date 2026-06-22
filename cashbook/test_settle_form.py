"""Settling a payable/accrual opens the editable expense form, capturing payment
method, claimant and any charge (#5)."""
import datetime as dt
from decimal import Decimal

from django.test import TestCase, Client
from django.contrib.auth.models import User, Group

from departments.models import Department, expense_departments
from cashbook.models import Payable, Accrual, Expense
from core.models import SiteConfig


class SettleFormTests(TestCase):
    def setUp(self):
        cfg = SiteConfig.get()
        cfg.require_expense_approval = False; cfg.enforce_fund_balance = False; cfg.save()
        self.u = User.objects.create_user("sf", password="x", is_superuser=True)
        self.u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(self.u)
        self.fund = Department.objects.create(name="LCB", fund_type="LOCAL",
                                              category="OFFERING", show_in_expenses=True)

    def test_settle_payable_prefills_form(self):
        p = Payable.objects.create(date=dt.date(2026, 6, 1), vendor="Acme",
            description="Tent hire", amount=Decimal("15000"), department=self.fund,
            category="MATERIALS", recorded_by=self.u)
        b = self.c.get(f"/expenses/new/?settle=payable:{p.id}").content.decode()
        self.assertIn("15000", b)
        self.assertIn("Tent hire", b)
        self.assertIn('name="settle"', b)

    def test_settle_payable_records_and_links(self):
        p = Payable.objects.create(date=dt.date(2026, 6, 1), vendor="Acme",
            description="Tent hire", amount=Decimal("15000"), department=self.fund,
            category="MATERIALS", recorded_by=self.u)
        self.c.post("/expenses/new/", {
            "date": "2026-06-12", "department": str(self.fund.id), "description": "Tent hire",
            "amount": "15000", "category": "MATERIALS", "method": "MPESA",
            "claimant": "Acme", "expenditure_type": "RECURRENT", "charge": "100",
            "settle": f"payable:{p.id}", "override_balance": "1"})
        p.refresh_from_db()
        self.assertTrue(p.settled)
        self.assertIsNotNone(p.settled_expense)
        self.assertEqual(p.settled_expense.method, "MPESA")
        self.assertEqual(p.settled_expense.claimant, "Acme")
        self.assertTrue(Expense.objects.filter(charge_for=p.settled_expense).exists())

    def test_settle_accrual(self):
        a = Accrual.objects.create(date=dt.date(2026, 6, 1), description="Utilities",
            amount=Decimal("3000"), department=self.fund, category="UTILITIES",
            recorded_by=self.u)
        self.c.post("/expenses/new/", {
            "date": "2026-06-12", "department": str(self.fund.id), "description": "Utilities",
            "amount": "3000", "category": "UTILITIES", "method": "BANK",
            "expenditure_type": "RECURRENT", "settle": f"accrual:{a.id}",
            "override_balance": "1"})
        a.refresh_from_db()
        self.assertTrue(a.settled)
        self.assertIsNotNone(a.settled_expense)
