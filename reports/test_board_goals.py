"""#3 Camp Meeting goals + board goal sections, #4 board settings, #5 chart."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import Transaction
from core.models import SiteConfig


def _treasurer():
    u = User.objects.create_user("tr", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class CampMeetingGoalTests(TestCase):
    def setUp(self):
        self.tr = _treasurer()
        self.c = Client(); self.c.force_login(self.tr)
        self.yr = dt.date.today().year
        self.exp = Department.objects.create(name="Camp Meeting Expense",
            fund_type="LOCAL", category="MINISTRY", show_in_expenses=True)
        self.s1 = Department.objects.create(name="Group A", fund_type="LOCAL",
            category="MINISTRY", parent=self.exp)
        self.s2 = Department.objects.create(name="Group B", fund_type="LOCAL",
            category="MINISTRY", parent=self.exp)
        self.off = Department.objects.create(name="Camp Meeting Offering",
            fund_type="TRUST", category="OFFERING")
        for d, amt in [(self.exp, 1000), (self.s1, 3000), (self.s2, 2000),
                       (self.off, 4500)]:
            Transaction.objects.create(date=dt.date(self.yr, 6, 1),
                amount=Decimal(amt), department=d, direction="CREDIT",
                confirmed=True, channel="BANK", allocation_status="MANUAL")

    def test_expense_goal_aggregates_subgroups_offering_separate(self):
        self.c.post(f"/reports/fund/{self.exp.id}/budget/", {"save_goals": "1",
            "year": str(self.yr), "expense_goal": "10000",
            "contribution_goal": "8000", "offering_goal": "5000",
            "goal_type": "CAMP_EXPENSE", "offering_fund": str(self.off.id)})
        body = self.c.get(f"/reports/fund/{self.exp.id}/budget/?year={self.yr}").content.decode()
        self.assertIn("Camp Meeting Expense Goal", body)
        self.assertIn("6,000", body)               # 1000+3000+2000 aggregated
        self.assertIn("Camp Meeting Offering Goal", body)
        self.assertIn("4,500", body)               # offering separate
        self.assertNotIn("10,500", body)           # never merged
        self.exp.refresh_from_db()
        self.assertEqual(self.exp.offering_fund_id, self.off.id)
        self.assertEqual(self.exp.year_goal, Decimal("10000"))
        self.assertEqual(self.exp.offering_goal, Decimal("5000"))

    def test_board_goals_section(self):
        self.exp.goal_type = "CAMP_EXPENSE"
        self.exp.year_goal = Decimal("10000")
        self.exp.offering_goal = Decimal("5000")
        self.exp.offering_fund = self.off
        self.exp.contribution_goal = Decimal("8000")
        self.exp.save()
        body = self.c.get("/reports/board-classic/").content.decode()
        self.assertIn("Goals and targets", body)
        self.assertIn("Camp Meeting Expense Goal", body)
        self.assertIn("Camp Meeting Offering Goal", body)


class BoardSettingsTests(TestCase):
    def setUp(self):
        self.tr = _treasurer()
        self.c = Client(); self.c.force_login(self.tr)

    def test_settings_reorder_and_hide(self):
        self.c.post("/reports/board-settings/", {
            "order": ["notes", "narrative", "funds", "trend"],
            "visible_notes": "on", "visible_narrative": "on",
            "visible_funds": "on", "notes": "A board note"})
        secs = SiteConfig.get().board_settings()["sections"]
        self.assertEqual(secs[0]["key"], "notes")
        self.assertTrue(any(s["key"] == "trend" and not s["visible"] for s in secs))
        body = self.c.get("/reports/board-classic/").content.decode()
        self.assertIn("A board note", body)
        self.assertNotIn("Multi-year trend", body)

    def test_sentence_case_headings(self):
        body = self.c.get("/reports/board-classic/").content.decode()
        self.assertIn("Income and expenditure", body)
        self.assertIn("Fund balances", body)


class ChartOfAccountsTests(TestCase):
    def test_expanded_chart(self):
        from ledger.services.posting import ensure_chart
        from ledger.models import Account
        ensure_chart()
        keys = set(Account.objects.values_list("system_key", flat=True))
        for k in ["PETTY_CASH", "MOBILE_MONEY", "STAFF_ADVANCES", "PREPAYMENTS",
                  "ACCRUALS", "PAYABLES", "STATUTORY_PAYABLE", "DESIGNATED_FUNDS",
                  "INC_INTEREST", "INC_FUNDRAISING", "INC_DONATIONS"]:
            self.assertIn(k, keys)
        self.assertEqual(Account.objects.get(system_key="PETTY_CASH").type, "ASSET")
        self.assertEqual(Account.objects.get(system_key="PAYABLES").type, "LIABILITY")
        self.assertEqual(Account.objects.get(system_key="INC_DONATIONS").type, "INCOME")

    def test_rebuild_still_balances(self):
        from ledger.services.posting import rebuild
        from ledger.models import JournalLine
        from django.db.models import Sum
        rebuild()
        d = JournalLine.objects.aggregate(t=Sum("debit"))["t"] or Decimal(0)
        c = JournalLine.objects.aggregate(t=Sum("credit"))["t"] or Decimal(0)
        self.assertEqual(d, c)
