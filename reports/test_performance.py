"""Performance review: budget_vs_actual() and the expense-report's by-fund
breakdown both called budget_amount() once per top-level fund (an N+1 query
pattern — the Executive overview alone triggered ~45 identical Budget
queries). Verifies the bulk replacement produces identical results with a
small, constant number of queries."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.contrib.auth.models import User, Group
from departments.models import Department, Budget, BudgetLine
from reports.services.budget import budget_amount, budget_amounts_bulk, budget_vs_actual


def _tr():
    u = User.objects.create_user("tr_perf_rep", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class BulkBudgetAmountCorrectnessTests(TestCase):
    def setUp(self):
        self.d1 = Department.objects.create(name="PerfBudgetFund1", fund_type="LOCAL",
            category="MINISTRY", annual_budget=Decimal("10000"))
        self.d2 = Department.objects.create(name="PerfBudgetFund2", fund_type="LOCAL",
            category="MINISTRY")
        Budget.objects.create(year=2026, department=self.d2, amount=Decimal("50000"))
        self.d3 = Department.objects.create(name="PerfBudgetFund3", fund_type="LOCAL",
            category="MINISTRY")
        b3 = Budget.objects.create(year=2026, department=self.d3, amount=Decimal("1"))
        BudgetLine.objects.create(budget=b3, name="Line A", amount=Decimal("3000"))
        BudgetLine.objects.create(budget=b3, name="Line B", amount=Decimal("4000"))
        self.d4 = Department.objects.create(name="PerfBudgetFund4", fund_type="LOCAL",
            category="MINISTRY")   # no budget at all

    def test_bulk_matches_single_for_every_case(self):
        # legacy annual_budget fallback, plain Budget.amount, Budget with
        # lines (lines_total wins), and no budget at all
        depts = [self.d1, self.d2, self.d3, self.d4]
        bulk = budget_amounts_bulk(2026, depts)
        for d in depts:
            self.assertEqual(bulk.get(d.id), budget_amount(2026, d))
        self.assertEqual(bulk[self.d3.id], Decimal("7000"))   # lines win over Budget.amount


class BudgetVsActualQueryCountTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        for i in range(25):
            d = Department.objects.create(name=f"PerfVsActual{i}", fund_type="LOCAL",
                category="MINISTRY", is_trust=False)
            Budget.objects.create(year=2026, department=d, amount=Decimal("12000"))

    def test_query_count_bounded_not_per_fund(self):
        with CaptureQueriesContext(connection) as ctx:
            budget_vs_actual(2026, period="ANNUAL")
        # a handful of bulk queries, not ~1-2 per fund (25+)
        self.assertLess(len(ctx.captured_queries), 15)

    def test_executive_overview_renders(self):
        c = Client(); c.force_login(self.tr)
        r = c.get("/executive/")
        self.assertEqual(r.status_code, 200)

    def test_executive_query_count_reasonable(self):
        c = Client(); c.force_login(self.tr)
        with CaptureQueriesContext(connection) as ctx:
            c.get("/executive/")
        # well below the ~160+ queries seen before this fix, for 25 funds
        self.assertLess(len(ctx.captured_queries), 140)
