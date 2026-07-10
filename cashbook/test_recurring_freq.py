"""Recurring expenses support monthly, quarterly and yearly cadences (#2)."""
import datetime as dt
from decimal import Decimal

from django.test import TestCase
from django.contrib.auth.models import User

from departments.models import Department
from cashbook.models import RecurringExpense
from cashbook.services.recurring import due_dates


class RecurringFrequencyTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("rf", password="x")
        self.d = Department.objects.create(name="LCB", fund_type="LOCAL",
                                           category="OFFERING", show_in_expenses=True)

    def _sched(self, freq, start, dom=15):
        return RecurringExpense(description="x", department=self.d, amount=Decimal("100"),
            frequency=freq, day_of_month=dom, start_date=start, created_by=self.u)

    def test_quarterly_every_three_months(self):
        dd = due_dates(self._sched("QUARTERLY", dt.date(2026, 2, 1)), dt.date(2026, 12, 31))
        self.assertEqual([(d.month, d.day) for d in dd], [(2,15),(5,15),(8,15),(11,15)])

    def test_yearly_same_month(self):
        dd = due_dates(self._sched("YEARLY", dt.date(2026, 3, 1)), dt.date(2028, 12, 31))
        self.assertEqual([d.isoformat() for d in dd],
                         ["2026-03-15", "2027-03-15", "2028-03-15"])

    def test_monthly_still_works(self):
        dd = due_dates(self._sched("MONTHLY", dt.date(2026, 1, 1)), dt.date(2026, 4, 30))
        self.assertEqual(len(dd), 4)

    def test_choices_available(self):
        codes = {c[0] for c in RecurringExpense.Frequency.choices}
        self.assertTrue({"MONTHLY", "QUARTERLY", "YEARLY"}.issubset(codes))
