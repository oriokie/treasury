"""#1 Monthly Treasurer's Report: camp goals + Word/Excel export."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import Transaction
from ledger.services.posting import ensure_chart


def _treasurer():
    u = User.objects.create_user("tr", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class BoardExportTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.tr = _treasurer()
        self.c = Client(); self.c.force_login(self.tr)
        yr = dt.date.today().year
        self.camp = Department.objects.create(name="Camp Expense", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=True, goal_type="CAMP_EXPENSE",
            year_goal=Decimal("50000"))
        self.off = Department.objects.create(name="Camp Offering", fund_type="TRUST",
            category="OFFERING")
        self.camp.offering_fund = self.off
        self.camp.offering_goal = Decimal("30000")
        self.camp.save()
        Transaction.objects.create(date=dt.date(yr, 6, 1), amount=Decimal("12000"),
            department=self.camp, direction="CREDIT", confirmed=True, channel="BANK",
            allocation_status="MANUAL")

    def test_board_page_shows_camp_goals(self):
        body = self.c.get("/reports/board/").content.decode()
        self.assertIn("Camp Meeting goals", body)
        self.assertIn("Camp Meeting Expense Goal", body)
        self.assertIn("Camp Meeting Offering Goal", body)

    def test_board_has_charts(self):
        body = self.c.get("/reports/board/").content.decode()
        self.assertIn("incomeExpChart", body)
        self.assertIn("fundMixChart", body)

    def test_excel_export(self):
        r = self.c.get("/reports/board/export/excel/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheetml", r["Content-Type"])
        self.assertIn("attachment", r["Content-Disposition"])
        self.assertGreater(len(r.content), 3000)

    def test_word_export(self):
        r = self.c.get("/reports/board/export/word/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/msword")
        self.assertIn(b"Monthly Treasurer", r.content)
        self.assertIn(b"financial position", r.content)
        self.assertIn(b"Camp Meeting", r.content)

    def test_camp_goals_exclude_groups(self):
        # a group (subgroup) with a contribution goal must NOT appear in camp_goals
        g = Department.objects.create(name="Group X", fund_type="LOCAL",
            category="MINISTRY", parent=self.camp, contribution_goal=Decimal("9999"))
        from reports.views import _camp_goal_records
        rows = _camp_goal_records(dt.date.today().year)
        names = [r["name"] for r in rows]
        self.assertIn("Camp Meeting Expense Goal", names)
        self.assertNotIn("Group X", names)
        self.assertFalse(any("contribution" in n.lower() for n in names))
