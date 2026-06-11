"""Render smoke tests for the cashbook and core pages a treasurer reaches with no
URL arguments. Guards against template/context regressions (500s) and lifts view
coverage on the GET paths."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User, Group
from django.test import TestCase
from django.urls import reverse

from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from cashbook.models import Expense


def _user(name, role):
    u = User.objects.create_user(name, password="x")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


CASHBOOK_PAGES = [
    "expense_list", "expense_create", "transfer_list", "transfer_create",
    "recurring_list", "recurring_create", "accruals", "expense_categories",
    "advance_list", "advance_new", "petty_cash",
]
CORE_PAGES = ["settings", "notifications", "controls", "executive"]


class CashbookCorePagesRenderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.treasurer = _user("pg_tr", TREASURER)
        cls.fund = Department.objects.create(name="LCB", fund_type="LOCAL",
            category="MINISTRY", opening_balance=Decimal("1000"))
        Transaction.objects.create(date=dt.date.today(), channel="BANK",
            direction="CREDIT", amount=Decimal("500"), department=cls.fund,
            allocation_status="AUTO", confirmed=True, core_ref="PG1")
        cls.expense = Expense.objects.create(date=dt.date.today(), department=cls.fund,
            description="Tea", amount=Decimal("100"), category="REFRESHMENTS",
            status="PENDING", recorded_by=cls.treasurer)

    def setUp(self):
        self.client.force_login(self.treasurer)

    def test_cashbook_pages_render(self):
        failures = []
        for name in CASHBOOK_PAGES:
            try:
                url = reverse(name)
            except Exception:
                continue
            code = self.client.get(url).status_code
            if code != 200:
                failures.append((name, code))
        self.assertEqual(failures, [], f"cashbook pages not 200: {failures}")

    def test_core_pages_render(self):
        failures = []
        for name in CORE_PAGES:
            try:
                url = reverse(name)
            except Exception:
                continue
            code = self.client.get(url).status_code
            if code != 200:
                failures.append((name, code))
        self.assertEqual(failures, [], f"core pages not 200: {failures}")

    def test_expense_detail_and_edit_render(self):
        self.assertEqual(self.client.get(
            reverse("expense_detail", args=[self.expense.pk])).status_code, 200)
        self.assertEqual(self.client.get(
            reverse("expense_edit", args=[self.expense.pk])).status_code, 200)

    def test_dashboard_renders_with_data(self):
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
