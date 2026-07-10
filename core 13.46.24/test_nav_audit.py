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
