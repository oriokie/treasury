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
        body = c.get(f"/leader/department/{dept.id}/").content.decode()
        self.assertNotIn("254712345678", body)        # full number never shown
        self.assertIn(mask_phone("254712345678"), body)  # masked form shown

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
        body = c.get(f"/leader/department/{dept.id}/").content.decode()
        self.assertNotIn("254700111222", body)
        self.assertIn("DON OR", body)                  # name shown (uppercased), phone masked


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
