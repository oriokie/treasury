"""Performance regression guards.

These don't assert an exact query count (that's brittle); they assert the hot
pages stay UNDER a ceiling on a dataset large enough that a per-row N+1 would
blow straight past it. That's what catches the kind of regression we just fixed
(the expenses list at 66 queries, controls at 8,000+), without failing on a
±1 query change.
"""
import datetime as dt
from decimal import Decimal

from django.test import TestCase, Client
from django.test.utils import CaptureQueriesContext, override_settings
from django.db import connection
from django.contrib.auth.models import User, Group
from django.core.cache import cache

from departments.models import Department
from members.models import Member
from giving.models import Transaction
from cashbook.models import Expense


def _treasurer():
    u = User.objects.create_user("perf_treas", password="x", is_superuser=True, is_staff=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class QueryCeilingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.u = _treasurer()
        funds = [Department.objects.create(name=f"Fund {i}", fund_type="LOCAL",
                                           category="OFFERING") for i in range(6)]
        trust = Department.objects.create(name="Conference", fund_type="TRUST",
                                          category="TRUST")
        funds.append(trust)
        members = [Member.objects.create(name=f"Perf Member {i}",
                                         phone=f"25470000{i:05d}") for i in range(60)]
        base = dt.date.today() - dt.timedelta(days=120)
        # ~90 transactions across funds/members; an N+1 over rows would explode
        for i in range(90):
            Transaction.objects.create(
                date=base + dt.timedelta(days=i % 110),
                channel=["BANK", "CASH", "ENVELOPE"][i % 3], direction="CREDIT",
                amount=Decimal("100") + i, department=funds[i % len(funds)],
                member=members[i % len(members)], allocation_status="AUTO",
                confirmed=True, core_ref=f"PERFQ{i}")
        for i in range(70):
            Expense.objects.create(
                date=base + dt.timedelta(days=i % 110), department=funds[i % 6],
                description=f"Exp {i}", amount=Decimal("200") + i, category="OTHER",
                status=["PENDING", "APPROVED", "PAID"][i % 3], recorded_by=cls.u)

    def setUp(self):
        self.c = Client()
        self.c.force_login(self.u)

    def _ceiling(self, path, ceiling):
        self.c.get(path)  # warm caches/templates
        with CaptureQueriesContext(connection) as ctx:
            r = self.c.get(path)
        n = len(ctx.captured_queries)
        self.assertEqual(r.status_code, 200, f"{path} -> {r.status_code}")
        self.assertLess(n, ceiling,
                        f"{path} used {n} queries (ceiling {ceiling}) — possible N+1 regression")

    def test_expenses_list_no_n_plus_one(self):
        self._ceiling("/expenses/", 30)

    def test_transactions_list_no_n_plus_one(self):
        self._ceiling("/transactions/", 30)

    def test_members_list_no_n_plus_one(self):
        self._ceiling("/members/", 25)

    def test_dashboard_bounded(self):
        self._ceiling("/", 90)

    def test_executive_bounded(self):
        self._ceiling("/executive/", 320)

    def test_controls_bounded(self):
        self._ceiling("/controls/", 90)


@override_settings(DASHBOARD_CACHE_TTL=60)
class AggregateCacheTests(TestCase):
    def setUp(self):
        cache.clear()
        self.fund = Department.objects.create(name="Cache Fund", fund_type="LOCAL",
                                              category="OFFERING")
        Transaction.objects.create(date=dt.date.today(), channel="CASH", direction="CREDIT",
            amount=Decimal("500"), department=self.fund, allocation_status="MANUAL",
            confirmed=True, core_ref="CACHE1")

    def test_second_call_is_cached(self):
        from reports.services import balances
        balances.department_summary()  # populate
        with CaptureQueriesContext(connection) as ctx:
            balances.department_summary()  # should hit cache
        self.assertEqual(len(ctx.captured_queries), 0)

    def test_write_busts_cache(self):
        from reports.services import balances
        balances.department_summary()  # cached
        # a financial write must invalidate the cache
        Transaction.objects.create(date=dt.date.today(), channel="CASH", direction="CREDIT",
            amount=Decimal("999"), department=self.fund, allocation_status="MANUAL",
            confirmed=True, core_ref="CACHE2")
        with CaptureQueriesContext(connection) as ctx:
            balances.department_summary()
        self.assertGreater(len(ctx.captured_queries), 0)


class CacheOffByDefaultTests(TestCase):
    def test_no_caching_without_ttl(self):
        # default settings: DASHBOARD_CACHE_TTL = 0 -> always recompute
        from reports.services import balances
        Department.objects.create(name="NoCache Fund", fund_type="LOCAL", category="OFFERING")
        balances.department_summary()
        with CaptureQueriesContext(connection) as ctx:
            balances.department_summary()
        self.assertGreater(len(ctx.captured_queries), 0)
