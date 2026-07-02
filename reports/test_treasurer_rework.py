"""Monthly treasurer report rework (#6): 3-month trends, all LCB accounts,
LCB expenditure (fixed), local-funds statement, chart, full SoFP/cashflow/recon."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from cashbook.models import Expense
from giving.models import Transaction
from reports.services import treasurer as T


class TreasurerServiceTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("ts", password="x", is_superuser=True)
        self.lcb = Department.objects.create(name="LCB – Local Church Budget",
            fund_type="LOCAL", category="OFFERING", show_in_expenses=True)
        self.lcb2 = Department.objects.create(name="LCB Departments",
            fund_type="LOCAL", category="OFFERING", show_in_expenses=True)

    def test_lcb_expenditure_matches_all_lcb_depts(self):
        # expense charged to the LCB fund must appear (the #f bug)
        Expense.objects.create(date=dt.date(2026, 6, 10), department=self.lcb,
            description="x", amount=Decimal("5000"), category="UTILITIES",
            status="PAID", recorded_by=self.u)
        out = T.lcb_expenditure(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        self.assertEqual(out["total"], Decimal("5000"))
        self.assertTrue(out["rows"])

    def test_trends_three_months(self):
        tt = T.trust_receipted_trend(dt.date(2026, 6, 30), months=3)
        self.assertEqual(len(tt["columns"]), 3)
        lt = T.lcb_subaccount_trend(dt.date(2026, 6, 30), months=3)
        self.assertEqual(len(lt["columns"]), 3)

    def test_all_lcb_accounts_listed(self):
        # both LCB depts listed even with no activity (dynamic, #d)
        lt = T.lcb_subaccount_trend(dt.date(2026, 6, 30), months=3)
        names = {r["dept"].name for r in lt["rows"]}
        self.assertIn("LCB – Local Church Budget", names)
        self.assertIn("LCB Departments", names)

    def test_local_funds_statement_columns(self):
        Transaction.objects.create(date=dt.date(2026, 6, 5), channel="CASH",
            direction="CREDIT", amount=Decimal("9000"), department=self.lcb,
            allocation_status="MANUAL", confirmed=True)
        out = T.local_funds_statement(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        for k in ("opening", "receipts", "expenses", "closing"):
            self.assertIn(k, out["totals"])


class TreasurerReportTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("tr", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(u)

    def test_report_renders_with_reworked_sections(self):
        import datetime as dt
        from decimal import Decimal
        from statements.models import BankReconciliation
        from django.contrib.auth.models import User
        BankReconciliation.objects.create(statement_date=dt.date(2026, 6, 30),
            bank_balance=Decimal("1000"),
            created_by=User.objects.get(username="tr"))
        b = self.c.get("/reports/board/?as_of=2026-06-15").content.decode()
        self.assertIn("yearlyChart", b)                      # e: chart
        self.assertIn("Expenditure summary", b)        # f
        self.assertIn("Local fund performance", b)            # h
        self.assertIn("Trust payable — receipted", b)        # i: full SoFP
        self.assertIn("Operating activities", b)             # i: full cashflow
        self.assertIn("Balance per cash book", b)            # i: full recon
        self.assertNotIn("Income &amp; expenditure statement", b)  # g
        self.assertNotIn("Local funds (with activity)", b)   # g
