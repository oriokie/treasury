"""Camp/fund budgets: named budget items, expenses tagged to an item, actual
spend per item, plus contribution and yearly goals (#7, #1)."""
import datetime as dt
from decimal import Decimal

from django.test import TestCase, Client
from django.contrib.auth.models import User, Group

from departments.models import Department
from giving.models import Transaction
from cashbook.models import Expense, BudgetLine
from core.models import SiteConfig


class FundBudgetTests(TestCase):
    def setUp(self):
        cfg = SiteConfig.get()
        cfg.require_expense_approval = False; cfg.enforce_fund_balance = False; cfg.save()
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

    def _url(self):
        return f"/reports/fund/{self.camp.id}/budget/?year={self.yr}"

    def _add_item(self, name, amount, category=""):
        self.c.post(f"/reports/fund/{self.camp.id}/budget/",
            {"year": str(self.yr), "name": name, "amount": amount, "category": category})
        return BudgetLine.objects.get(department=self.camp, year=self.yr, name=name)

    def test_named_items_and_goals(self):
        self._add_item("Accommodation", "50000", "MATERIALS")
        b = self.c.get(self._url()).content.decode()
        self.assertIn("Accommodation", b)
        self.assertIn("Group Contribution Goal", b)
        self.assertIn("80,000", b)
        self.assertIn("200,000", b)
        self.assertIn("500,000", b)

    def test_budget_items_json_endpoint(self):
        self._add_item("Catering", "30000")
        import json
        r = self.c.get(f"/expenses/budget-items/?dept={self.camp.id}")
        items = json.loads(r.content)["items"]
        self.assertEqual([i["name"] for i in items], ["Catering"])

    def test_expense_tagged_to_item_shows_actual(self):
        acc = self._add_item("Accommodation", "50000", "MATERIALS")
        self.c.post("/expenses/new/", {
            "date": f"{self.yr}-06-10", "department": str(self.camp.id),
            "description": "Tents", "amount": "20000", "category": "MATERIALS",
            "method": "CASH", "expenditure_type": "RECURRENT",
            "budget_line": str(acc.id), "override_balance": "1"})
        e = Expense.objects.get(description="Tents")
        self.assertEqual(e.budget_line_id, acc.id)
        b = self.c.get(self._url()).content.decode()
        self.assertIn("20,000", b)      # actual against the item
        self.assertIn("50,000", b)      # its budget

    def test_form_has_budget_item_picker(self):
        b = self.c.get("/expenses/new/").content.decode()
        self.assertIn('id="id_budget_line"', b)
        self.assertIn("budgetItemRow", b)
        self.assertIn("budget-items", b)

    def test_item_update_is_idempotent(self):
        self._add_item("Catering", "30000")
        self._add_item("Catering", "45000")
        qs = BudgetLine.objects.filter(department=self.camp, year=self.yr, name="Catering")
        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.first().amount, Decimal("45000"))

    def test_save_goals(self):
        self.c.post(f"/reports/fund/{self.camp.id}/budget/",
            {"year": str(self.yr), "save_goals": "1",
             "contribution_goal": "250000", "expense_goal": "600000"})
        self.camp.refresh_from_db()
        self.assertEqual(self.camp.contribution_goal, Decimal("250000"))
        self.assertEqual(self.camp.year_goal, Decimal("600000"))

    def test_link_on_fund_report(self):
        b = self.c.get(f"/reports/fund/{self.camp.id}/").content.decode()
        self.assertIn("Budget &amp; goals", b)
