"""Tests for two fixes to the fund budget page (v2.44):

1. GroupGoalsPngView/BudgetItemsPngView required TreasurerRequiredMixin — a
   narrower permission than FundBudgetView's own can_view_fund_budget check
   (which also allows Assistants and leaders granted the view_fund_budget
   right for their own fund). Since the "Download PNG" links are ON the
   budget page, anyone who could see that page but wasn't a Treasurer would
   get a 403 clicking them, with no obvious reason why — now aligned to the
   same permission model.
2. fund_budget.html used class="ledger compact" on two tables but never
   defined the matching CSS rule anywhere (every other page in the app that
   uses this class also defines it locally) — so "compact" was a no-op and
   the tables rendered at full default padding, sprawling wider than
   necessary for a 4-5 column table that should comfortably fit a portrait
   viewport. The "Budget vs actual by item" table didn't even have the class
   at all.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from cashbook.models import BudgetLine, Expense
from core.roles import ASSISTANT, TREASURER, LEADER
from departments.models import Department


def _treasurer(username="fb_tr"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
    return u


def _assistant(username="fb_asst"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
    return u


class BudgetPngPermissionTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="FB Fund", fund_type="LOCAL")

    def test_treasurer_can_download_both_pngs(self):
        tr = _treasurer()
        self.client.force_login(tr)
        r1 = self.client.get(f"/reports/fund/{self.dept.id}/budget/items.png")
        r2 = self.client.get(f"/reports/fund/{self.dept.id}/budget/group-goals.png")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)

    def test_assistant_can_now_download_both_pngs(self):
        # the actual bug: assistant can view the HTML page but previously
        # got a 403 on the PNG links that page itself shows them
        asst = _assistant()
        self.client.force_login(asst)
        html_resp = self.client.get(f"/reports/fund/{self.dept.id}/budget/")
        self.assertEqual(html_resp.status_code, 200)
        r1 = self.client.get(f"/reports/fund/{self.dept.id}/budget/items.png")
        r2 = self.client.get(f"/reports/fund/{self.dept.id}/budget/group-goals.png")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)

    def test_unrelated_user_still_denied(self):
        u = User.objects.create_user("fb_norole", password="x")
        self.client.force_login(u)
        r1 = self.client.get(f"/reports/fund/{self.dept.id}/budget/items.png")
        r2 = self.client.get(f"/reports/fund/{self.dept.id}/budget/group-goals.png")
        self.assertEqual(r1.status_code, 403)
        self.assertEqual(r2.status_code, 403)

    def test_leader_without_right_still_denied(self):
        u = User.objects.create_user("fb_leader", password="x")
        u.groups.add(Group.objects.get_or_create(name=LEADER)[0])
        self.client.force_login(u)
        r = self.client.get(f"/reports/fund/{self.dept.id}/budget/items.png")
        self.assertEqual(r.status_code, 403)

    def test_permission_matches_html_page_exactly(self):
        # for every user type, the PNG endpoints and the HTML page must agree
        for user in (_treasurer("fb_tr2"), _assistant("fb_asst2"),
                    User.objects.create_user("fb_none2", password="x")):
            self.client.force_login(user)
            html_ok = self.client.get(
                f"/reports/fund/{self.dept.id}/budget/").status_code == 200
            png_ok = self.client.get(
                f"/reports/fund/{self.dept.id}/budget/items.png").status_code == 200
            # HTML page redirects (not 200) on denial, PNG returns 403 — but
            # "can view" must agree either way
            self.assertEqual(html_ok, png_ok, user.username)


class BudgetPageTableWidthTests(TestCase):
    def setUp(self):
        self.tr = _treasurer("fb_tr3")
        self.client.force_login(self.tr)
        self.dept = Department.objects.create(name="FB Width Fund", fund_type="LOCAL")
        BudgetLine.objects.create(department=self.dept, year=2026, name="Item",
                                  amount=Decimal("1000"), category="OTHER")

    def test_compact_css_rule_present(self):
        r = self.client.get(f"/reports/fund/{self.dept.id}/budget/?year=2026")
        html = r.content.decode()
        self.assertIn(".ledger.compact td,.ledger.compact th{padding:.3rem",
                      html)

    def test_budget_items_table_has_compact_class(self):
        r = self.client.get(f"/reports/fund/{self.dept.id}/budget/?year=2026")
        html = r.content.decode()
        self.assertIn('<table class="ledger compact" id="campBudget">', html)

    def test_budget_items_table_wrapped_for_scroll(self):
        r = self.client.get(f"/reports/fund/{self.dept.id}/budget/?year=2026")
        html = r.content.decode()
        self.assertIn('<div class="table-wrap">\n  <table class="ledger compact" id="campBudget">',
                      html)

    def test_page_still_renders_figures_correctly(self):
        r = self.client.get(f"/reports/fund/{self.dept.id}/budget/?year=2026")
        html = r.content.decode()
        self.assertIn("Item", html)
        self.assertIn("1,000", html)
