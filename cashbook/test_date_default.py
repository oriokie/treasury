"""ExpenseListView had the same shape of gap as the transaction ledger: no
default date bound on a bare visit, so every expense ever recorded loaded
on every page view. Fixed to default to the current month.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.roles import TREASURER
from cashbook.models import Expense
from departments.models import Department


class ExpenseListDateDefaultTests(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("tr_expdate", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.treasurer)
        self.dept = Department.objects.create(
            name="ExpDate Fund", slug="expdate-fund", fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.today = dt.date.today()

    def _exp(self, date, description, amount="500"):
        return Expense.objects.create(
            date=date, department=self.dept, description=description,
            amount=Decimal(amount), category="OTHER", status="PAID",
            recorded_by=self.treasurer, approved_by=self.treasurer)

    def test_a_bare_visit_shows_only_this_month(self):
        self._exp(self.today, "This month expense")
        self._exp(self.today.replace(day=1) - dt.timedelta(days=10), "Last month expense")
        r = self.client.get("/expenses/")
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("This month expense", body)
        self.assertNotIn("Last month expense", body)

    def test_the_date_inputs_are_prefilled_with_the_default(self):
        r = self.client.get("/expenses/")
        body = r.content.decode()
        first_of_month = self.today.replace(day=1).isoformat()
        self.assertIn(f'value="{first_of_month}"', body)

    def test_explicitly_clearing_the_dates_shows_everything(self):
        self._exp(self.today.replace(day=1) - dt.timedelta(days=400), "Very old expense")
        r = self.client.get("/expenses/?start=&end=")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Very old expense", r.content.decode())

    def test_an_explicit_range_is_respected_exactly_as_before(self):
        self._exp(dt.date(2025, 3, 15), "March expense")
        r = self.client.get("/expenses/?start=2025-03-01&end=2025-03-31")
        self.assertEqual(r.status_code, 200)
        self.assertIn("March expense", r.content.decode())
