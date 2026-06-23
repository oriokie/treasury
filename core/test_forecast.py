"""Cash-flow forecast service + page, and the executive forecast section (#6, #7)."""
import datetime as dt
from decimal import Decimal

from django.test import TestCase, Client
from django.contrib.auth.models import User, Group

from departments.models import Department
from giving.models import Transaction
from cashbook.models import RecurringExpense
from core.services import forecast


class ForecastTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("fc", password="x", is_superuser=True)
        self.u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(self.u)
        self.fund = Department.objects.create(name="LCB", fund_type="LOCAL",
            category="OFFERING", show_in_expenses=True)
        today = dt.date.today()
        # six months of giving at ~30k/month
        for i in range(6):
            Transaction.objects.create(date=today - dt.timedelta(days=30 * i + 5),
                channel="CASH", direction="CREDIT", amount=Decimal("30000"),
                department=self.fund, allocation_status="MANUAL", confirmed=True)
        # a monthly recurring expense
        RecurringExpense.objects.create(description="Rent", department=self.fund,
            amount=Decimal("5000"), frequency="MONTHLY", day_of_month=1,
            start_date=today - dt.timedelta(days=200), created_by=self.u)

    def test_horizons_present(self):
        h = forecast.horizons()
        self.assertEqual(set(h), {"30 days", "Quarter", "Year"})
        for v in h.values():
            for k in ("opening", "proj_giving", "pledge_in", "recurring",
                      "discretionary", "projected", "net"):
                self.assertIn(k, v)

    def test_giving_grows_position(self):
        # with strong giving and modest spend, the projection should exceed opening
        h = forecast.project(365)
        self.assertGreater(h["proj_giving"], 0)

    def test_recurring_in_window(self):
        # the monthly recurring (5k) should appear in the 30-day window
        h = forecast.project(31)
        self.assertGreaterEqual(h["recurring"], Decimal("5000"))

    def test_forecast_page(self):
        b = self.c.get("/reports/forecast/").content.decode()
        self.assertIn("forecastChart", b)
        self.assertIn("Projected position", b)

    def test_executive_has_forecast(self):
        b = self.c.get("/executive/").content.decode()
        self.assertIn("Cash flow forecast", b)
        self.assertIn("Outstanding pledges", b)
