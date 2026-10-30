"""Assistant cash-flow / recurring affordability answers."""
import calendar
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.roles import TREASURER
from core.services.assistant import _answer_rules as ask, parse_period
from departments.models import Department
from giving.models import Transaction


class ParseNextMonthTests(TestCase):
    def test_next_month_returns_following_calendar_month(self):
        today = dt.date.today()
        if today.month == 12:
            expect_y, expect_m = today.year + 1, 1
        else:
            expect_y, expect_m = today.year, today.month + 1
        start, end, label = parse_period("can we pay next month")
        self.assertEqual(start, dt.date(expect_y, expect_m, 1))
        last = calendar.monthrange(expect_y, expect_m)[1]
        self.assertEqual(end, dt.date(expect_y, expect_m, last))
        self.assertIn(str(expect_y), label)


class ForecastAnswerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("fc_tr", password="x", is_superuser=True)
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.local = Department.objects.create(
            name="Local Fc", fund_type="LOCAL", category="MINISTRY")
        today = dt.date.today()
        for i in range(3):
            when = today - dt.timedelta(days=30 * i + 5)
            Transaction.objects.create(
                date=when, channel="CASH", direction="CREDIT",
                amount=Decimal("50000"), department=self.local,
                confirmed=True, allocation_status="MANUAL",
                core_ref=f"FC-{i}")

    def test_can_we_cover_recurring_next_month(self):
        d = ask("Can we cover recurring expenses next month?")
        self.assertNotIn("_fallback", d)
        self.assertIn("outlook", d["text"].lower())
        self.assertEqual(d.get("link_label"), "Cash-flow forecast")
        labels = [r[0] for r in d.get("rows") or []]
        self.assertIn("Recurring expenses due", labels)
        self.assertIn("Projected closing", labels)

    def test_cash_flow_forecast_phrasing(self):
        d = ask("cash flow forecast")
        self.assertNotIn("_fallback", d)
        self.assertIn("Cash-flow forecast", d.get("link_label", ""))
