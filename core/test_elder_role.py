"""New Elder role: a read-only, board-level role distinct from both staff
(Treasurer/Assistant/Auditor) and departmental Leader. Elders get their own
dashboard and the executive overview by default; "reports" access is a
separately assignable right (view_reports) a treasurer can grant to a
specific elder via a profile — not switched on for every elder automatically."""
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from accounts.models import Profile
from core import roles
from core.rights import has_right, GROUP_RIGHTS


def _elder():
    u = User.objects.create_user("test_elder_role", password="x")
    u.groups.add(Group.objects.get_or_create(name="Elder")[0])
    return u


class ElderRoleBasicsTests(TestCase):
    def test_elder_is_a_recognised_role(self):
        self.assertIn(roles.ELDER, roles.ALL_ROLES)

    def test_is_elder_true_for_elder_group_member(self):
        u = _elder()
        self.assertTrue(roles.is_elder(u))

    def test_is_elder_false_for_other_roles(self):
        tr = User.objects.create_user("tr_not_elder", password="x")
        tr.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.assertFalse(roles.is_elder(tr))

    def test_elder_is_not_a_staff_role(self):
        """Elders must not get the full staff nav / ReadAccessMixin universe —
        only the specifically curated pages."""
        u = _elder()
        self.assertFalse(roles.is_staff_role(u))

    def test_elder_gets_executive_dashboard_right_by_default(self):
        u = _elder()
        self.assertTrue(has_right(u, "view_executive_dashboard"))

    def test_elder_does_not_get_view_reports_by_default(self):
        u = _elder()
        self.assertFalse(has_right(u, "view_reports"))

    def test_view_reports_not_in_elder_default_group_rights(self):
        self.assertNotIn("view_reports", GROUP_RIGHTS.get(roles.ELDER, set()))

    def test_elder_default_profile_seeded(self):
        p = Profile.objects.filter(name="Elder (default)", is_system=True).first()
        self.assertIsNotNone(p)
        self.assertIn("view_executive_dashboard", p.rights)
        self.assertNotIn("view_reports", p.rights)

    def test_role_selectable_in_edit_role_form(self):
        from accounts.forms import EditRoleForm
        form = EditRoleForm()
        choices = [c[0] for c in form.fields["role"].choices]
        self.assertIn("Elder", choices)


class ElderDashboardAccessTests(TestCase):
    def setUp(self):
        self.elder = _elder()
        self.c = Client(); self.c.force_login(self.elder)

    def test_elder_dashboard_accessible(self):
        r = self.c.get("/elder/")
        self.assertEqual(r.status_code, 200)

    def test_executive_dashboard_accessible_by_default(self):
        r = self.c.get("/executive/")
        self.assertEqual(r.status_code, 200)

    def test_reports_index_blocked_without_right(self):
        r = self.c.get("/reports/")
        self.assertEqual(r.status_code, 302)

    def test_monthly_report_blocked_without_right(self):
        r = self.c.get("/reports/board/")
        self.assertEqual(r.status_code, 302)

    def test_general_staff_pages_blocked(self):
        for url in ("/transactions/", "/expenses/", "/members/"):
            r = self.c.get(url)
            self.assertEqual(r.status_code, 302, f"{url} should be blocked for an elder")

    def test_data_entry_pages_blocked(self):
        r = self.c.get("/expenses/new/")
        self.assertEqual(r.status_code, 302)

    def test_leader_only_pages_blocked(self):
        r = self.c.get("/leader/")
        self.assertEqual(r.status_code, 302)

    def test_dashboard_shows_no_reports_link_without_right(self):
        b = self.c.get("/elder/").content.decode()
        self.assertNotIn("Open reports", b)

    def test_dashboard_links_to_executive_overview(self):
        b = self.c.get("/elder/").content.decode()
        self.assertIn("/executive/", b)


class ElderAssignableReportsRightTests(TestCase):
    def setUp(self):
        self.elder = _elder()
        self.c = Client(); self.c.force_login(self.elder)
        p = Profile.objects.create(name="ElderReportsGrant", rights=["view_reports"])
        p.users.add(self.elder)

    def test_reports_index_accessible_once_granted(self):
        r = self.c.get("/reports/")
        self.assertEqual(r.status_code, 200)

    def test_monthly_report_accessible_once_granted(self):
        r = self.c.get("/reports/board/")
        self.assertEqual(r.status_code, 200)

    def test_annual_summary_accessible_once_granted(self):
        r = self.c.get("/reports/annual/")
        self.assertEqual(r.status_code, 200)

    def test_dashboard_shows_reports_link_once_granted(self):
        b = self.c.get("/elder/").content.decode()
        self.assertIn("Open reports", b)

    def test_still_no_write_access_anywhere(self):
        r = self.c.get("/expenses/new/")
        self.assertEqual(r.status_code, 302)
        r2 = self.c.get("/transactions/")
        self.assertEqual(r2.status_code, 302)


class ExistingStaffAccessUnaffectedTests(TestCase):
    """The bulk mixin swap across every report view must not change access
    for existing Treasurer/Assistant/Auditor users at all."""
    def test_treasurer_still_has_full_report_access(self):
        tr = User.objects.create_user("tr_elder_regress", password="x", is_superuser=True)
        tr.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        c = Client(); c.force_login(tr)
        for url in ("/reports/", "/reports/board/", "/reports/annual/", "/executive/"):
            r = c.get(url)
            self.assertEqual(r.status_code, 200, f"{url} should work for treasurer")

    def test_leader_still_excluded_from_reports_and_executive(self):
        ld = User.objects.create_user("ld_elder_regress", password="x")
        ld.groups.add(Group.objects.get_or_create(name="Leader")[0])
        c = Client(); c.force_login(ld)
        r = c.get("/reports/")
        self.assertEqual(r.status_code, 302)
        r2 = c.get("/executive/")
        self.assertEqual(r2.status_code, 302)

    def test_anonymous_still_blocked_everywhere(self):
        c = Client()
        for url in ("/elder/", "/executive/", "/reports/"):
            r = c.get(url)
            self.assertIn(r.status_code, (302, 403))
