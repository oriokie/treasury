"""Camp/fund budgets and goals: per-category budget-vs-actual plus a contribution
goal and a yearly goal tracked against collections (#7)."""
import datetime as dt
from decimal import Decimal

from django.test import TestCase, Client
from django.contrib.auth.models import User, Group

from departments.models import Department
from giving.models import Transaction
from cashbook.models import Expense, BudgetLine


class FundBudgetTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("fb", password="x", is_superuser=True)
        self.u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(self.u)
        self.yr = dt.date.today().year
        self.camp = Department.objects.create(name="Camp Meeting", fund_type="LOCAL",
            category="MINISTRY", contribution_goal=Decimal("200000"),
            year_goal=Decimal("500000"), show_in_expenses=True)
        Transaction.objects.create(date=dt.date(self.yr, 3, 1), channel="CASH",
            direction="CREDIT", amount=Decimal("80000"), department=self.camp,
            allocation_status="MANUAL", confirmed=True)
        Expense.objects.create(date=dt.date(self.yr, 4, 1), department=self.camp,
            description="Catering", amount=Decimal("25000"), category="REFRESHMENTS",
            status="PAID", recorded_by=self.u)

    def _url(self):
        return f"/reports/fund/{self.camp.id}/budget/?year={self.yr}"

    def test_goals_tracked_against_collections(self):
        b = self.c.get(self._url()).content.decode()
        self.assertIn("Contribution goal", b)
        self.assertIn("80,000", b)       # collected
        self.assertIn("200,000", b)      # contribution goal
        self.assertIn("500,000", b)      # yearly goal

    def test_add_budget_line_and_actual(self):
        self.c.post(f"/reports/fund/{self.camp.id}/budget/",
            {"year": str(self.yr), "category": "REFRESHMENTS", "amount": "30000",
             "note": "catering"})
        bl = BudgetLine.objects.get(department=self.camp, year=self.yr, category="REFRESHMENTS")
        self.assertEqual(bl.amount, Decimal("30000"))
        b = self.c.get(self._url()).content.decode()
        self.assertIn("30,000", b)       # budget
        self.assertIn("25,000", b)       # actual spend

    def test_update_budget_line_is_idempotent(self):
        for amt in ("30000", "45000"):
            self.c.post(f"/reports/fund/{self.camp.id}/budget/",
                {"year": str(self.yr), "category": "REFRESHMENTS", "amount": amt})
        self.assertEqual(BudgetLine.objects.filter(department=self.camp,
                         year=self.yr, category="REFRESHMENTS").count(), 1)
        self.assertEqual(BudgetLine.objects.get(department=self.camp, year=self.yr,
                         category="REFRESHMENTS").amount, Decimal("45000"))

    def test_save_goals(self):
        self.c.post(f"/reports/fund/{self.camp.id}/budget/",
            {"year": str(self.yr), "save_goals": "1",
             "contribution_goal": "250000", "annual_goal": "600000"})
        self.camp.refresh_from_db()
        self.assertEqual(self.camp.contribution_goal, Decimal("250000"))
        self.assertEqual(self.camp.year_goal, Decimal("600000"))

    def test_link_on_fund_report(self):
        b = self.c.get(f"/reports/fund/{self.camp.id}/").content.decode()
        self.assertIn("Budget &amp; goals", b)
