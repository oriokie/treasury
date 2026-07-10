"""Accruals/payables are period-based for the SoFP: settling after the as-at date
still shows them as a liability on that date (#4)."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase
from django.contrib.auth.models import User
from departments.models import Department
from cashbook.models import Accrual, Payable
from cashbook.views import open_accruals_total, open_payables_total


class PeriodSettlementTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("ps", password="x")
        self.d = Department.objects.create(name="LCB", fund_type="LOCAL", category="OFFERING")

    def test_accrual_settled_after_asof_still_a_liability(self):
        Accrual.objects.create(date=dt.date(2026, 6, 1), description="Power",
            amount=Decimal("3000"), department=self.d, category="UTILITIES",
            recorded_by=self.u, settled=True, settled_on=dt.date(2026, 6, 15))
        self.assertEqual(open_accruals_total(dt.date(2026, 6, 14)), Decimal("3000"))
        self.assertEqual(open_accruals_total(dt.date(2026, 6, 15)), Decimal("0"))
        self.assertEqual(open_accruals_total(dt.date(2026, 6, 16)), Decimal("0"))

    def test_unsettled_always_outstanding(self):
        Accrual.objects.create(date=dt.date(2026, 6, 2), description="Water",
            amount=Decimal("500"), department=self.d, category="UTILITIES",
            recorded_by=self.u)
        self.assertEqual(open_accruals_total(dt.date(2026, 6, 20)), Decimal("500"))

    def test_payable_period_based(self):
        Payable.objects.create(date=dt.date(2026, 6, 1), vendor="Acme",
            description="Chairs", amount=Decimal("5000"), department=self.d,
            category="MATERIALS", recorded_by=self.u, settled=True,
            settled_on=dt.date(2026, 6, 20))
        self.assertEqual(open_payables_total(dt.date(2026, 6, 10)), Decimal("5000"))
        self.assertEqual(open_payables_total(dt.date(2026, 6, 20)), Decimal("0"))

    def test_incurred_after_asof_not_counted(self):
        Payable.objects.create(date=dt.date(2026, 6, 25), vendor="Late",
            description="x", amount=Decimal("100"), department=self.d,
            category="OTHER", recorded_by=self.u)
        self.assertEqual(open_payables_total(dt.date(2026, 6, 20)), Decimal("0"))
