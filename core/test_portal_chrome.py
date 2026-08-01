"""The shared header and menu, seen from a member's login.

`base.html` was written for the office and grew a portal underneath it. Three
things in the chrome had never been looked at as a member:

  * the role chip fell through its office-role chain to a catch-all, so a
    member of the congregation was told they were "Auditor / Board";
  * the bell linked to /notifications/, an office page the confinement
    middleware bounces a member away from, and badged it with a count from
    `unread_count()` — which includes broadcasts naming OTHER members'
    benevolent cases;
  * the user menu offered that same dead link plus Preferences, which bounces
    too.

None of it leaked a page — the middleware held — but a count of other people's
business was on screen, two menu items did nothing when clicked, and the app
told a member they were somebody else.
"""
from django.contrib.auth.models import Group, User
from django.test import TestCase

from benevolent.models_portal import MemberAccount
from core.models import Notification
from core.roles import MEMBER, TREASURER
from members.models import Member


class _Chrome(TestCase):
    def setUp(self):
        self.member_user = User.objects.create_user("chrome_member", password="x")
        self.member_user.groups.add(Group.objects.get_or_create(name=MEMBER)[0])
        MemberAccount.objects.create(
            user=self.member_user,
            member=Member.objects.create(name="CHROME MEMBER"),
            status=MemberAccount.Status.ACTIVE)

        self.office_user = User.objects.create_user("chrome_tr", password="x",
                                                    is_superuser=True)
        self.office_user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])

    def _as_member(self, path="/portal/"):
        self.client.force_login(self.member_user)
        return self.client.get(path).content.decode()

    def _as_office(self, path="/"):
        self.client.force_login(self.office_user)
        return self.client.get(path).content.decode()


class RoleChipTests(_Chrome):
    def test_a_member_is_called_a_member(self):
        body = self._as_member()
        self.assertIn('<span class="role">Member</span>', body)

    def test_a_member_is_not_called_an_auditor(self):
        self.assertNotIn("Auditor / Board", self._as_member())

    def test_the_office_chip_is_unchanged(self):
        self.assertIn("Administrator", self._as_office())


class BellTests(_Chrome):
    def test_the_bell_goes_somewhere_a_member_can_actually_reach(self):
        body = self._as_member()
        self.assertIn('href="/portal/notifications/" class="iconbtn"', body)
        self.assertNotIn('href="/notifications/" class="iconbtn"', body)

    def test_the_office_notifications_page_still_bounces_a_member(self):
        """The link is gone; the guard that made it a dead end stays."""
        self.client.force_login(self.member_user)
        r = self.client.get("/notifications/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], "/portal/")

    def test_a_broadcast_about_another_member_is_not_counted_at_a_member(self):
        """`unread_count()` counts recipient-is-NULL notifications, which is how
        the office sees church-wide notices. A member was being given the tally
        of a queue that is not theirs — seven of them, all naming other
        members' benevolent cases, on the demo data."""
        Notification.objects.create(
            kind="BENEVOLENT_CASE_SUBMITTED", recipient=None, read=False,
            message="Benevolent case BENC-2026-0004 submitted for SOMEONE ELSE")
        body = self._as_member()
        self.assertNotIn("BENC-2026-0004", body)
        self.assertNotIn('<span class="dot">1</span>', body)

    def test_the_office_still_gets_its_badge(self):
        Notification.objects.create(
            kind="REMITTANCE", recipient=None, read=False,
            message="Tithe remittance is due.")
        body = self._as_office()
        self.assertIn('href="/notifications/" class="iconbtn"', body)
        self.assertIn('<span class="dot">', body)


class UserMenuTests(_Chrome):
    def test_the_menu_offers_no_link_that_bounces(self):
        """Every href the member is offered has to be one they can open."""
        import re

        from django.test import Client
        body = self._as_member()
        menu = body.split('class="dd-menu', 1)[1].split("</div>", 1)[0]
        hrefs = set(re.findall(r'href="([^"]+)"', menu))

        c = Client(HTTP_HOST="testserver")
        c.force_login(self.member_user)
        for href in hrefs:
            if not href.startswith("/"):
                continue
            with self.subTest(href=href):
                r = c.get(href)
                self.assertNotEqual(
                    (r.status_code, r.get("Location")), (302, "/portal/"),
                    f"the user menu offers {href}, which bounces a member home")

    def test_the_office_menu_keeps_its_items(self):
        body = self._as_office()
        self.assertIn('href="/notifications/">Notifications</a>', body)
        self.assertIn("Appearance", body)


class BadgeQueryCostTests(_Chrome):
    def test_a_portal_render_does_not_pay_for_office_badges(self):
        """Six counts against Transaction, Expense and Notification, on every
        portal page, for numbers a member must never be shown."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.client.force_login(self.member_user)
        self.client.get("/portal/")                     # warm caches
        with CaptureQueriesContext(connection) as ctx:
            self.client.get("/portal/")
        sql = " ".join(q["sql"] for q in ctx.captured_queries)
        self.assertNotIn("core_notification", sql)
        self.assertNotIn("cashbook_expense", sql)
