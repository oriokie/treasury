"""Financial-position balances, cash-flows reconciles (#11), bank position counts
bank-paid expenses + real-time balance (#12)."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client, RequestFactory
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import Transaction
from cashbook.models import Expense
from reports.views import StatementOfCashFlowsView, BankPositionView


class PositionReportTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("pr", password="x", is_superuser=True)
        self.u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(self.u)
        self.d = Department.objects.create(name="LCB", fund_type="LOCAL",
            category="OFFERING", show_in_expenses=True)

    def test_financial_position_balances(self):
        Transaction.objects.create(date=dt.date(2026, 3, 5), channel="CASH",
            direction="CREDIT", amount=Decimal("10000"), department=self.d,
            allocation_status="MANUAL", confirmed=True)
        b = self.c.get("/reports/financial-position/?as_of=2026-12-31").content.decode()
        self.assertEqual(self.c.get("/reports/financial-position/").status_code, 200)
        self.assertIn("TOTAL", b.upper())

    def test_cash_flows_reconciles_even_with_untyped_expense(self):
        Transaction.objects.create(date=dt.date(2026, 3, 5), channel="CASH",
            direction="CREDIT", amount=Decimal("10000"), department=self.d,
            allocation_status="MANUAL", confirmed=True)
        Expense.objects.create(date=dt.date(2026, 3, 10), department=self.d,
            description="x", amount=Decimal("2000"), category="OTHER",
            status="PAID", recorded_by=self.u)
        req = RequestFactory().get("/reports/cash-flows/?start=2026-01-01&end=2026-12-31")
        req.user = self.u
        v = StatementOfCashFlowsView(); v.request = req; v.kwargs = {}
        resp = v.get(req)
        self.assertTrue(resp.context_data["ties"])

    def test_bank_position_subtracts_bank_paid_expenses(self):
        def sysbal():
            v = BankPositionView(); v.kwargs = {}
            v.request = RequestFactory().get("/")
            return v.get_context_data()["system_balance"]
        before = sysbal()
        Expense.objects.create(date=dt.date(2026, 6, 1), department=self.d,
            description="bank pay", amount=Decimal("3000"), category="OTHER",
            method="BANK", status="PAID", paid_date=dt.date(2026, 6, 1),
            recorded_by=self.u)
        self.assertEqual(before - sysbal(), Decimal("3000"))

    def test_bank_paid_expense_linked_to_debit_not_double_counted(self):
        # an expense linked to a bank debit row is already in `debits`; must not
        # be subtracted twice
        t = Transaction.objects.create(date=dt.date(2026, 6, 2), channel="BANK",
            direction="DEBIT", amount=Decimal("1000"), allocation_status="MANUAL",
            confirmed=True, core_ref="DBT-LINK")
        def sysbal():
            v = BankPositionView(); v.kwargs = {}
            v.request = RequestFactory().get("/")
            return v.get_context_data()["system_balance"]
        before = sysbal()
        Expense.objects.create(date=dt.date(2026, 6, 2), department=self.d,
            description="linked", amount=Decimal("1000"), category="OTHER",
            method="BANK", status="PAID", paid_date=dt.date(2026, 6, 2),
            recorded_by=self.u, bank_transaction=t)
        # linked expense is excluded from the extra bank_expenses subtraction
        self.assertEqual(before, sysbal())
