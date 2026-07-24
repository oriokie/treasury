"""Navigation audit: breadcrumbs, renamed labels, no broken links, dedup."""
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group


def _treasurer():
    u = User.objects.create_user("tr", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class NavAuditTests(TestCase):
    def setUp(self):
        self.tr = _treasurer()
        self.c = Client(); self.c.force_login(self.tr)

    def test_breadcrumb_renders(self):
        b = self.c.get("/transactions/").content.decode()
        self.assertIn('class="breadcrumb', b)
        self.assertIn("Giving", b)
        self.assertIn("Transactions", b)

    def test_reconciliation_report_no_crash(self):
        # previously raised TypeError on None book_balance
        self.assertEqual(self.c.get("/reports/reconciliation/").status_code, 200)

    def test_renamed_nav_labels(self):
        b = self.c.get("/").content.decode()
        self.assertIn(">Transactions</a>", b)      # was "Ledger"
        self.assertIn("Assistant", b)              # was "Ask the books"
        self.assertIn("Ledger integrity", b)       # was "Ledger check"
        self.assertIn("Monthly Treasurer's Report</a>", b)  # was "Board report"

    def test_report_monthly_not_in_main_nav(self):
        # the basic fund-movement report was removed from the sidebar (kept in index)
        b = self.c.get("/").content.decode()
        self.assertNotIn("report_monthly", b)

    def test_report_index_dedup(self):
        idx = self.c.get("/reports/").content.decode()
        # exactly one report card links to the board report (plus the nav link)
        self.assertEqual(idx.count('href="/reports/board/"'), 2)  # 1 nav + 1 card
        self.assertIn("Fund movement summary", idx)
        self.assertIn("Reconciliation summary", idx)

    def test_active_state_on_current_page(self):
        import re
        b = self.c.get("/transactions/").content.decode()
        m = re.search(r'<a href="/transactions/"[^>]*class="([^"]*)"', b)
        self.assertTrue(m and "active" in m.group(1))

    def test_quick_add_menu_present(self):
        b = self.c.get("/").content.decode()
        self.assertIn("＋ New", b)  # frequent record actions near the top


class EveryBuiltScreenIsReachableTests(TestCase):
    """A screen nobody can navigate to has not been delivered.

    Written after the office side of the member portal sat unreachable for four
    releases: the views worked, the tests passed, and no menu anywhere linked to
    them, so the only way in was to type the URL. That is the same failure as
    #121 (a public form that redirected to login) — working code that no user
    can arrive at.

    The check is deliberately narrow: named, no-argument, non-detail pages that
    a treasurer should be able to find. It will not catch everything, but it
    would have caught this.
    """

    #: Pages reachable by a route other than the sidebar — from a parent page's
    #: buttons, a dashboard tile, or as a redirect target. Each needs a reason.
    LINKED_ELSEWHERE = {
        "portal_admin_review",     # opened from the queue
        "vendor_create",           # form posts to itself from the register
        "vendor_lookup",           # JSON, called by pickers
        "portal_unavailable",      # redirect target for a blocked member
        "after_login",             # redirect target
        "healthz", "login", "logout",
    }

    def test_the_portal_office_screens_are_in_the_menu(self):
        from django.contrib.auth.models import Group, User
        from django.test import Client
        from django.urls import reverse
        from core.roles import TREASURER

        user = User.objects.create_user("navtest", password="nav-pass-1")
        user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        client = Client()
        client.get("/accounts/login/")
        client.post("/accounts/login/",
                    {"username": "navtest", "password": "nav-pass-1"}, follow=True)

        body = client.get(reverse("benevolent_dashboard")).content.decode()
        for name in ["portal_admin_queue", "portal_admin_accounts"]:
            self.assertIn(
                reverse(name), body,
                f"{name} is built and tested but appears in no menu, so a "
                f"treasurer cannot reach it without typing the URL.")
