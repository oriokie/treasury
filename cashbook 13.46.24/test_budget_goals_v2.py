"""#2 Offering goal only for camp funds; per-group contribution goals."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import Transaction


def _treasurer():
    u = User.objects.create_user("tr", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class BudgetGoalsV2Tests(TestCase):
    def setUp(self):
        self.tr = _treasurer()
        self.c = Client(); self.c.force_login(self.tr)
        self.yr = dt.date.today().year
        self.camp = Department.objects.create(name="Camp Expense", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=True, goal_type="CAMP_EXPENSE")
        self.g1 = Department.objects.create(name="Group A", fund_type="LOCAL",
            category="MINISTRY", parent=self.camp)
        self.g2 = Department.objects.create(name="Group B", fund_type="LOCAL",
            category="MINISTRY", parent=self.camp)
        Transaction.objects.create(date=dt.date(self.yr, 6, 1), amount=Decimal("3000"),
            department=self.g1, direction="CREDIT", confirmed=True, channel="BANK",
            allocation_status="MANUAL")

    def test_per_group_goals_saved(self):
        self.c.post(f"/reports/fund/{self.camp.id}/budget/", {"save_expense_goal": "1",
            "year": str(self.yr), "goal_type": "CAMP_EXPENSE", "expense_goal": "20000"})
        self.c.post(f"/reports/fund/{self.camp.id}/budget/", {"save_group_goals": "1",
            "year": str(self.yr),
            f"group_goal_{self.g1.id}": "5000", f"group_goal_{self.g2.id}": "4000"})
        self.g1.refresh_from_db(); self.g2.refresh_from_db()
        self.assertEqual(self.g1.contribution_goal, Decimal("5000"))
        self.assertEqual(self.g2.contribution_goal, Decimal("4000"))

    def test_group_goals_shown(self):
        self.g1.contribution_goal = Decimal("5000"); self.g1.save()
        body = self.c.get(f"/reports/fund/{self.camp.id}/budget/?year={self.yr}").content.decode()
        self.assertIn("Group Contribution Goals", body)
        self.assertIn("5,000", body)
        self.assertIn("3,000", body)   # g1 collected

    def test_offering_only_for_camp(self):
        camp_body = self.c.get(f"/reports/fund/{self.camp.id}/budget/").content.decode()
        self.assertIn('pill-amber u-xs">Trust fund', camp_body)
        plain = Department.objects.create(name="Plain", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=True, goal_type="NONE")
        plain_body = self.c.get(f"/reports/fund/{plain.id}/budget/").content.decode()
        self.assertNotIn('pill-amber u-xs">Trust fund', plain_body)

    def test_non_camp_offering_cleared_on_save(self):
        off = Department.objects.create(name="Off", fund_type="TRUST", category="OFFERING")
        plain = Department.objects.create(name="Plain2", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=True, goal_type="NONE")
        self.c.post(f"/reports/fund/{plain.id}/budget/", {"save_expense_goal": "1",
            "year": str(self.yr), "goal_type": "NONE", "expense_goal": "1000",
            "offering_goal": "9999", "offering_fund": str(off.id)})
        plain.refresh_from_db()
        self.assertIsNone(plain.offering_goal)   # offering not applied to non-camp
        self.assertIsNone(plain.offering_fund)


class AppearanceTests(TestCase):
    def test_font_and_sidebar_apply(self):
        u = User.objects.create_user("a", password="x")
        c = Client(); c.force_login(u)
        c.post("/preferences/update/", {"key": "font_family", "value": "SERIF"})
        c.post("/preferences/update/", {"key": "sidebar_style", "value": "MIDNIGHT"})
        u.refresh_from_db()
        self.assertEqual(u.preference.font_family, "SERIF")
        self.assertEqual(u.preference.sidebar_style, "MIDNIGHT")
        body = c.get("/").content.decode()
        self.assertIn('data-fontfamily="serif"', body)
        self.assertIn('data-sidebarstyle="midnight"', body)

    def test_display_font_variable_used(self):
        css = open("static/css/app.css").read()
        self.assertIn("var(--font-display", css)
        self.assertIn("--font-display:", css)


class FilterPersistenceTests(TestCase):
    def test_pagination_preserves_filters(self):
        u = User.objects.create_user("f", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        c = Client(); c.force_login(u)
        d = Department.objects.create(name="F", fund_type="LOCAL", category="OFFERING")
        for i in range(60):
            Transaction.objects.create(date=dt.date(2026, 6, 1), amount=Decimal("10"),
                department=d, direction="CREDIT", confirmed=True, channel="BANK",
                allocation_status="MANUAL")
        import re
        body = c.get("/transactions/?channel=BANK").content.decode()
        links = re.findall(r'href="([^"]*page=\d[^"]*)"', body)
        self.assertTrue(links)
        self.assertTrue(any("channel=BANK" in l for l in links))
