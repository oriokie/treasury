"""Fund budget pages (/reports/fund/<id>/budget/) are now viewable (read-only)
by a leader who has been granted the new, assignable view_fund_budget right
AND leads the specific fund in question — not bundled into the base Leader
role by default, so a treasurer opts leaders into it deliberately. Editing
(POST) always stays treasurer/assistant only. The leader dashboard shows a
"budget →" link only for funds a leader can actually view."""
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department, DepartmentLeadership
from accounts.models import Profile
from cashbook.models import BudgetLine


def _leader():
    u = User.objects.create_user("ld_budgetaccess", password="x")
    u.groups.add(Group.objects.get_or_create(name="Leader")[0])
    return u


def _tr():
    u = User.objects.create_user("tr_budgetaccess", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class FundBudgetLeaderAccessTests(TestCase):
    def setUp(self):
        self.ld = _leader()
        self.tr = _tr()
        self.led_dept = Department.objects.create(name="LedFundBudget", fund_type="LOCAL",
            category="DEVELOPMENT")
        self.other_dept = Department.objects.create(name="OtherFundBudget", fund_type="LOCAL",
            category="DEVELOPMENT")
        DepartmentLeadership.objects.create(user=self.ld, department=self.led_dept)
        self.c = Client(); self.c.force_login(self.ld)

    def test_leader_without_right_is_blocked(self):
        r = self.c.get(f"/reports/fund/{self.led_dept.id}/budget/")
        self.assertEqual(r.status_code, 302)

    def test_leader_with_right_can_view_own_fund(self):
        p = Profile.objects.create(name="BudgetRight1", rights=["view_fund_budget"])
        p.users.add(self.ld)
        r = self.c.get(f"/reports/fund/{self.led_dept.id}/budget/")
        self.assertEqual(r.status_code, 200)

    def test_leader_with_right_still_blocked_from_unled_fund(self):
        p = Profile.objects.create(name="BudgetRight2", rights=["view_fund_budget"])
        p.users.add(self.ld)
        r = self.c.get(f"/reports/fund/{self.other_dept.id}/budget/")
        self.assertEqual(r.status_code, 302)

    def test_leader_cannot_edit_even_with_right(self):
        p = Profile.objects.create(name="BudgetRight3", rights=["view_fund_budget"])
        p.users.add(self.ld)
        r = self.c.post(f"/reports/fund/{self.led_dept.id}/budget/",
            {"name": "Sneaky", "amount": "999", "year": "2026"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(BudgetLine.objects.filter(name="Sneaky").exists())

    def test_treasurer_unaffected_can_view_any_fund(self):
        c = Client(); c.force_login(self.tr)
        r = c.get(f"/reports/fund/{self.other_dept.id}/budget/")
        self.assertEqual(r.status_code, 200)

    def test_treasurer_unaffected_can_edit(self):
        c = Client(); c.force_login(self.tr)
        c.post(f"/reports/fund/{self.other_dept.id}/budget/",
            {"name": "RealItem", "amount": "500", "year": "2026"})
        self.assertTrue(BudgetLine.objects.filter(name="RealItem").exists())

    def test_right_not_granted_by_default_to_leader_role(self):
        from core.rights import GROUP_RIGHTS
        from core import roles
        self.assertNotIn("view_fund_budget", GROUP_RIGHTS.get(roles.LEADER, set()))

    def test_dashboard_hides_budget_link_without_right(self):
        b = self.c.get("/leader/?stay=1").content.decode()
        self.assertNotIn("budget →", b)

    def test_dashboard_shows_budget_link_with_right(self):
        p = Profile.objects.create(name="BudgetRight4", rights=["view_fund_budget"])
        p.users.add(self.ld)
        b = self.c.get("/leader/?stay=1").content.decode()
        self.assertIn("budget →", b)
        self.assertIn(f"/reports/fund/{self.led_dept.id}/budget/", b)

    def test_dashboard_budget_link_scoped_to_led_funds_only(self):
        # a leader with the right, leading only one of two funds shown, must
        # not get a budget link for the fund they don't lead
        other_leader = User.objects.create_user("ld_other_scope", password="x")
        other_leader.groups.add(Group.objects.get(name="Leader"))
        DepartmentLeadership.objects.create(user=other_leader, department=self.other_dept)
        p = Profile.objects.create(name="BudgetRight5", rights=["view_fund_budget"])
        p.users.add(self.ld)   # only self.ld gets the right, and only leads led_dept
        b = self.c.get("/leader/?stay=1").content.decode()
        self.assertIn(f"/reports/fund/{self.led_dept.id}/budget/", b)
        self.assertNotIn(f"/reports/fund/{self.other_dept.id}/budget/", b)
