"""Fund ledger subgroups sort by closing balance (item 4); Camp Meeting
Offering goal lives in Settings, Camp Meeting Expense goal and every other
fund's own goal stay on the fund (item 5)."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import Transaction
from core.models import SiteConfig


def _tr():
    u = User.objects.create_user("tr_flsc", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class FundLedgerSortTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.parent = Department.objects.create(name="ParentSort", fund_type="LOCAL",
            category="DEVELOPMENT")
        self.small = Department.objects.create(name="SmallSub", fund_type="LOCAL",
            category="DEVELOPMENT", parent=self.parent, opening_balance=Decimal("100"))
        self.big = Department.objects.create(name="BigSub", fund_type="LOCAL",
            category="DEVELOPMENT", parent=self.parent, opening_balance=Decimal("100"))
        # small gets a big closing balance, big gets a small one — sort should
        # follow the closing balance, not receipts or creation order
        Transaction.objects.create(date=dt.date(2026, 6, 5), amount=Decimal("50000"),
            direction="CREDIT", confirmed=True, channel="CASH",
            allocation_status="MANUAL", department=self.small)
        Transaction.objects.create(date=dt.date(2026, 6, 5), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="CASH",
            allocation_status="MANUAL", department=self.big)
        self.c = Client(); self.c.force_login(self.tr)

    def test_sorted_by_closing_desc(self):
        from reports.views import FundLedgerView
        from django.test import RequestFactory
        rf = RequestFactory()
        req = rf.get(f"/reports/fund/{self.parent.id}/?start=2026-06-01&end=2026-06-30")
        req.user = self.tr
        view = FundLedgerView()
        view.request = req
        ctx = view.get_context_data(pk=self.parent.id)
        names = [r["sub"].name for r in ctx["subgroups"]]
        self.assertEqual(names[0], "SmallSub")   # 50,100 closing > 100,600... but name
        self.assertGreater(ctx["subgroups"][0]["closing"], ctx["subgroups"][1]["closing"])


class CampGoalSplitTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)
        self.camp_fund = Department.objects.create(name="Camp Meeting Expense",
            fund_type="LOCAL", category="MINISTRY", goal_type="CAMP_EXPENSE",
            year_goal=Decimal("500000"))
        self.trust = Department.objects.create(name="TrustCamp", fund_type="TRUST",
            category="OFFERING")
        cfg = SiteConfig.get()
        cfg.camp_offering_fund = self.trust
        cfg.camp_offering_goal = Decimal("300000")
        cfg.save()
        Transaction.objects.create(date=dt.date(2026, 6, 10), amount=Decimal("120000"),
            direction="CREDIT", confirmed=True, channel="CASH",
            allocation_status="MANUAL", department=self.camp_fund)
        Transaction.objects.create(date=dt.date(2026, 6, 12), amount=Decimal("80000"),
            direction="CREDIT", confirmed=True, channel="BANK",
            allocation_status="MANUAL", department=self.trust)

    def test_settings_goals_tab_present(self):
        b = self.c.get("/settings/?tab=goals").content.decode()
        self.assertIn('data-pane="goals"', b)
        self.assertIn("camp_offering_goal", b)

    def test_expense_goal_still_on_fund(self):
        from reports.views import _camp_goal_records
        rows = _camp_goal_records(2026)
        expense = next(r for r in rows if "Expense" in r["name"])
        self.assertEqual(expense["goal"], Decimal("500000"))
        self.assertEqual(expense["collected"], Decimal("120000"))

    def test_offering_goal_from_settings(self):
        from reports.views import _camp_goal_records
        rows = _camp_goal_records(2026)
        offering = next(r for r in rows if "Offering" in r["name"])
        self.assertEqual(offering["goal"], Decimal("300000"))

    def test_board_classic_shows_both_no_duplicates(self):
        b = self.c.get("/reports/board-classic/").content.decode()
        self.assertEqual(b.count("Camp Meeting Expense Goal"), 1)
        self.assertEqual(b.count("Camp Meeting Offering Goal"), 1)

    def test_settings_progress_shown(self):
        b = self.c.get("/settings/?tab=goals").content.decode()
        self.assertIn("Collected so far this year", b)
