"""Security + scoping tests for the departmental-leader area.

These lock in the guarantees that matter: a leader sees ONLY their own
department(s), cannot reach staff screens, cannot view another department's
detail, sees masked phone numbers, and has no write access.
"""
import datetime as dt
from decimal import Decimal

from django.test import TestCase, Client
from django.contrib.auth.models import User, Group

from departments.models import Department, DepartmentLeadership
from members.models import Member, mask_phone


def _make_leader(username, *departments):
    u = User.objects.create_user(username, password="x")
    g, _ = Group.objects.get_or_create(name="Leader")
    u.groups.add(g)
    for d in departments:
        DepartmentLeadership.objects.create(user=u, department=d)
    return u


class LeaderScopingTests(TestCase):
    def setUp(self):
        self.mine = Department.objects.create(name="Youth", fund_type="LOCAL",
                                              category="MINISTRY")
        self.other = Department.objects.create(name="Music", fund_type="LOCAL",
                                               category="MINISTRY")
        self.leader = _make_leader("ldr", self.mine)
        self.c = Client(); self.c.force_login(self.leader)

    def test_dashboard_loads(self):
        self.assertEqual(self.c.get("/leader/").status_code, 200)

    def test_can_view_own_department(self):
        r = self.c.get(f"/leader/department/{self.mine.id}/")
        self.assertEqual(r.status_code, 200)

    def test_cannot_view_other_department(self):
        r = self.c.get(f"/leader/department/{self.other.id}/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/leader/", r.url)        # bounced to own dashboard

    def test_main_dashboard_redirects_to_leader(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/leader/", r.url)

    def test_staff_views_are_blocked(self):
        for url in ["/expenses/", "/members/", "/pledges/list/", "/reports/",
                    "/transactions/", "/departments/", "/users/", "/settings/"]:
            r = self.c.get(url)
            self.assertEqual(r.status_code, 302, f"{url} should redirect a leader")
            self.assertIn("/leader/", r.url, f"{url} should bounce to leader dash")

    def test_subaccounts_included_in_scope(self):
        sub = Department.objects.create(name="Youth Choir", parent=self.mine,
                                        fund_type="LOCAL", category="MINISTRY")
        from departments.models import departments_led_by
        ids = set(departments_led_by(self.leader).values_list("id", flat=True))
        self.assertIn(self.mine.id, ids)
        self.assertIn(sub.id, ids)            # sub-account rolled into scope
        self.assertNotIn(self.other.id, ids)

    def test_leader_can_view_subaccount_detail(self):
        sub = Department.objects.create(name="Youth Choir", parent=self.mine,
                                        fund_type="LOCAL", category="MINISTRY")
        self.assertEqual(
            self.c.get(f"/leader/department/{sub.id}/").status_code, 200)


class LeaderPhoneMaskingTests(TestCase):
    def test_collections_phone_is_masked(self):
        from giving.models import Transaction
        dept = Department.objects.create(name="Youth", fund_type="LOCAL",
                                         category="MINISTRY")
        leader = _make_leader("ldr2", dept)
        m = Member.objects.create(name="Jane Giver", phone="254712345678")
        Transaction.objects.create(date=dt.date.today(), channel="CASH",
            direction="CREDIT", amount=Decimal("500"), department=dept, member=m,
            allocation_status="MANUAL", confirmed=True)
        c = Client(); c.force_login(leader)
        # the overview dashboard never shows the full number
        body = c.get(f"/leader/department/{dept.id}/").content.decode()
        self.assertNotIn("254712345678", body)
        # the dedicated collections page shows the masked form, never the full one
        page = c.get(f"/leader/department/{dept.id}/collections/").content.decode()
        self.assertNotIn("254712345678", page)
        self.assertIn(mask_phone("254712345678"), page)

    def test_pledge_phone_is_masked(self):
        from pledges.models import PledgeCampaign, Pledge
        dept = Department.objects.create(name="Building", fund_type="LOCAL",
                                         category="DEVELOPMENT")
        leader = _make_leader("ldr3", dept)
        m = Member.objects.create(name="Don Or", phone="254700111222")
        camp = PledgeCampaign.objects.create(name="Roof", target_department=dept,
                                             status="ACTIVE")
        Pledge.objects.create(campaign=camp, member=m, amount=Decimal("1000"),
                              status="ACTIVE")
        c = Client(); c.force_login(leader)
        # the dedicated pledges page shows the member, masks the phone
        page = c.get(f"/leader/department/{dept.id}/pledges/").content.decode()
        self.assertNotIn("254700111222", page)
        self.assertIn("DON OR", page)                  # name shown (uppercased), phone masked
        # the overview dashboard never leaks the full number either
        body = c.get(f"/leader/department/{dept.id}/").content.decode()
        self.assertNotIn("254700111222", body)


class LeaderReadOnlyTests(TestCase):
    def test_no_post_routes_for_leader(self):
        # a leader has no data-entry URLs; the staff create endpoints reject them
        dept = Department.objects.create(name="Youth", fund_type="LOCAL",
                                         category="MINISTRY")
        leader = _make_leader("ldr4", dept)
        c = Client(); c.force_login(leader)
        # attempting a write action on a staff endpoint is blocked (redirect)
        r = c.post("/cash/new/", {})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/leader/", r.url)


class MaskPhoneHelperTests(TestCase):
    def test_masking_rules(self):
        self.assertEqual(mask_phone("254712345678"), "*********678")
        self.assertEqual(mask_phone("123"), "***")
        self.assertEqual(mask_phone(""), "")
        self.assertEqual(mask_phone(None), "")
        self.assertEqual(mask_phone("254712345678", visible=4), "********5678")


class LeaderDetailPagesTests(TestCase):
    """Item 2: detailed, downloadable leader pages — collections, expenses, and
    development-group drill-down — all scoped and read-only."""

    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from giving.models import Transaction
        from departments.models import DevelopmentGroup
        self.dev = Department.objects.create(name="Dev Fund", fund_type="LOCAL",
                                             category="DEVELOPMENT")
        self.other = Department.objects.create(name="Not Mine", fund_type="LOCAL",
                                               category="MINISTRY")
        self.leader = _make_leader("devlead", self.dev)
        self.client.login(username="devlead", password="x")
        self.grp = DevelopmentGroup.objects.create(number=1, name="Group One",
                                                   target=Decimal("10000"))
        Transaction.objects.create(date=dt.date(2026, 3, 7), channel="CASH",
            direction="CREDIT", amount=Decimal("500"), department=self.dev,
            dev_group=self.grp, payer_name="Giver A", confirmed=True,
            allocation_status="MANUAL", core_ref="LD1")

    def test_collections_page_and_downloads(self):
        url = f"/leader/department/{self.dev.id}/collections/"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url + "?export=csv").status_code, 200)
        r = self.client.get(url + "?export=xlsx")
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheet", r["Content-Type"])

    def test_expenses_page_and_download(self):
        url = f"/leader/department/{self.dev.id}/expenses/"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url + "?export=csv").status_code, 200)

    def test_group_drilldown_and_download(self):
        url = f"/leader/group/{self.grp.id}/"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url + "?export=csv").status_code, 200)

    def test_cannot_access_other_department(self):
        # not led by this leader -> redirected away, no data leak
        r = self.client.get(f"/leader/department/{self.other.id}/collections/")
        self.assertEqual(r.status_code, 302)
        r = self.client.get(f"/leader/department/{self.other.id}/expenses/")
        self.assertEqual(r.status_code, 302)

    def test_non_development_leader_cannot_drilldown_groups(self):
        from departments.models import DevelopmentGroup
        # a leader of a ministry (non-dev) dept can't see group drill-downs
        ministry = Department.objects.create(name="Choir", fund_type="LOCAL",
                                             category="MINISTRY")
        _make_leader("minlead", ministry)
        self.client.logout(); self.client.login(username="minlead", password="x")
        r = self.client.get(f"/leader/group/{self.grp.id}/")
        self.assertEqual(r.status_code, 302)


class LeaderDashboardRevampTests(TestCase):
    """The revamped leader department page: insights dashboard + dedicated pages."""

    def setUp(self):
        self.dept = Department.objects.create(name="Camp Dev", fund_type="LOCAL",
                                              category="DEVELOPMENT", annual_budget=100000)
        self.leader = _make_leader("ldrev", self.dept)
        self.c = Client(); self.c.force_login(self.leader)

    def test_dashboard_renders_insights(self):
        r = self.c.get(f"/leader/department/{self.dept.id}/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Closing balance")
        self.assertContains(r, "Collections")
        self.assertContains(r, "Top contributors")
        self.assertContains(r, "Explore")
        # dedicated-page links are present
        self.assertContains(r, f"/leader/department/{self.dept.id}/collections/")
        self.assertContains(r, f"/leader/department/{self.dept.id}/expenses/")

    def test_kpis_reflect_giving_and_spend(self):
        import datetime as dt
        from decimal import Decimal
        from giving.models import Transaction
        from cashbook.models import Expense
        d = dt.date.today()
        Transaction.objects.create(date=d, channel="CASH", direction="CREDIT",
            amount=Decimal("1000"), department=self.dept, payer_name="Giver A",
            confirmed=True, allocation_status="MANUAL")
        Expense.objects.create(date=d, department=self.dept, description="Tents",
            amount=Decimal("300"), category="MATERIALS", status="APPROVED",
            recorded_by=self.leader)
        r = self.c.get(f"/leader/department/{self.dept.id}/")
        self.assertEqual(r.status_code, 200)
        ctx = r.context
        self.assertEqual(ctx["kpi"]["receipts"], Decimal("1000"))
        self.assertEqual(ctx["kpi"]["spend"], Decimal("300"))
        self.assertEqual(ctx["kpi"]["net"], Decimal("700"))
        self.assertEqual(ctx["top_givers"][0]["who"].upper(), "GIVER A")
        # budget card present (annual_budget set)
        self.assertIsNotNone(ctx["budget"])

    def test_pledges_page_and_export(self):
        r = self.c.get(f"/leader/department/{self.dept.id}/pledges/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Pledged")
        self.assertEqual(
            self.c.get(f"/leader/department/{self.dept.id}/pledges/?export=csv").status_code, 200)
        self.assertEqual(
            self.c.get(f"/leader/department/{self.dept.id}/pledges/?export=xlsx").status_code, 200)

    def test_pledges_page_guarded(self):
        other = Department.objects.create(name="Not Mine", fund_type="LOCAL",
                                          category="MINISTRY")
        r = self.c.get(f"/leader/department/{other.id}/pledges/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/leader/", r["Location"])
