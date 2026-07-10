"""Performance review: the General Ledger Health Check was calling
fund_balance_from_ledger() once per department (an N+1 query pattern —
roughly 2 queries per fund, each in its own implicit transaction). Verifies
the bulk replacement produces identical results with a small, constant
number of queries regardless of how many funds exist."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.contrib.auth.models import User
from departments.models import Department
from giving.models import Transaction
from cashbook.models import Expense
from ledger.services.posting import (ensure_chart, fund_balance_from_ledger,
    fund_balances_from_ledger_bulk)
from ledger.services.health import funds_out_of_balance, run_health_check


class BulkFundBalanceCorrectnessTests(TestCase):
    """The bulk rewrite must produce exactly the same numbers as the original
    per-department function — a performance fix must never change a result."""
    def setUp(self):
        ensure_chart()
        self.tr = User.objects.create_user("tr_perf1", password="x")
        self.local = Department.objects.create(name="PerfLocalA", fund_type="LOCAL",
            category="MINISTRY")
        self.trust = Department.objects.create(name="PerfTrustA", fund_type="TRUST",
            category="OFFERING")
        self.empty = Department.objects.create(name="PerfEmptyA", fund_type="LOCAL",
            category="MINISTRY")
        Transaction.objects.create(date=dt.date(2026, 6, 1), amount=Decimal("5000"),
            direction="CREDIT", confirmed=True, channel="CASH",
            allocation_status="MANUAL", department=self.local)
        Expense.objects.create(date=dt.date(2026, 6, 2), department=self.local,
            description="x", amount=Decimal("1200"), category="OTHER",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        Transaction.objects.create(date=dt.date(2026, 6, 3), amount=Decimal("8000"),
            direction="CREDIT", confirmed=True, channel="BANK",
            allocation_status="MANUAL", department=self.trust)

    def test_bulk_matches_single_for_every_fund(self):
        depts = [self.local, self.trust, self.empty]
        bulk = fund_balances_from_ledger_bulk([d.id for d in depts])
        for d in depts:
            self.assertEqual(bulk.get(d.id, Decimal(0)), fund_balance_from_ledger(d))

    def test_empty_dept_list_returns_empty_dict(self):
        self.assertEqual(fund_balances_from_ledger_bulk([]), {})

    def test_fund_with_no_ledger_activity_returns_zero(self):
        bulk = fund_balances_from_ledger_bulk([self.empty.id])
        self.assertEqual(bulk[self.empty.id], Decimal(0))


class HealthCheckQueryCountTests(TestCase):
    """The health check page must scale with a small constant number of
    queries, not with the number of funds in the church."""
    def setUp(self):
        ensure_chart()
        # a realistic number of funds, matching real deployments
        for i in range(30):
            Department.objects.create(name=f"PerfScaleFund{i}", fund_type="LOCAL",
                category="MINISTRY")

    def test_funds_out_of_balance_query_count_bounded(self):
        with CaptureQueriesContext(connection) as ctx:
            funds_out_of_balance()
        # a handful of grouped-aggregate queries, not ~2 per fund (60+)
        self.assertLess(len(ctx.captured_queries), 20)

    def test_full_health_check_still_returns_all_clear_on_clean_ledger(self):
        result = run_health_check()
        self.assertTrue(result["trial_balance_balanced"])
        self.assertEqual(result["funds_out_of_balance"], [])
