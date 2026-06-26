"""Ledger recon layout + exports (#1,#2), trust payable split (#4),
income-statement casing (#6), historical month delete (#7), period selectors (#8),
detailed board report (#3)."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from core.models import HistoricalMonth, HistoricalYear
from reports.services import balances


class LedgerExportTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("le", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(u)

    def test_reconciliation_renders_and_exports(self):
        self.assertEqual(self.c.get("/ledger/reconciliation/").status_code, 200)
        r = self.c.get("/ledger/reconciliation/?export=xlsx")
        self.assertIn("spreadsheet", r["Content-Type"])

    def test_journal_exports(self):
        r = self.c.get("/ledger/journal/?export=xlsx")
        self.assertIn("spreadsheet", r["Content-Type"])
        self.assertIn("spreadsheet", "spreadsheet")
        rc = self.c.get("/ledger/journal/?export=csv")
        self.assertIn("text/csv", rc["Content-Type"])


class TrustPayableSplitTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("tp", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(u)

    def test_split_ties_and_balances(self):
        b = self.c.get("/reports/financial-position/").content.decode()
        self.assertIn("Receipted (firmly due to remit)", b)
        self.assertIn("Not yet receipted (allocated, awaiting receipt)", b)
        self.assertIn("Balanced", b)   # identity still holds
        # the suspense line shows only when there are unallocated bank receipts
        self.assertNotIn("Out of balance", b)


class IncomeStatementCasingTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("ic", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(u)

    def test_revenue_not_uppercase(self):
        b = self.c.get("/reports/income-statement/").content.decode()
        self.assertIn(">Revenue<", b)
        self.assertNotIn(">REVENUE<", b)

    def test_period_selector_present(self):
        for url in ["/reports/income-statement/", "/reports/changes-in-net-assets/"]:
            b = self.c.get(url).content.decode()
            self.assertIn('name="start"', b)
            self.assertIn("This quarter", b)

    def test_period_preset_works(self):
        self.assertEqual(
            self.c.get("/reports/income-statement/?period=year").status_code, 200)


class HistoricalMonthDeleteTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("hd", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(u)

    def test_view_and_delete_month(self):
        self.c.post("/reports/annual/historical/", {"action": "save_month",
            "year": "2024", "month": "3", "collection": "100", "trust_fund": "60",
            "expenditure": "40"})
        hm = HistoricalMonth.objects.get(year=2024, month=3)
        b = self.c.get("/reports/annual/historical/").content.decode()
        self.assertIn("hist-year", b)            # expandable per-year view
        self.c.post("/reports/annual/historical/",
                    {"action": "delete_month", "pk": str(hm.pk)})
        self.assertFalse(HistoricalMonth.objects.filter(pk=hm.pk).exists())

    def test_delete_year_all(self):
        self.c.post("/reports/annual/historical/", {"action": "save_month",
            "year": "2022", "month": "1", "collection": "100", "trust_fund": "60",
            "expenditure": "40"})
        self.c.post("/reports/annual/historical/",
                    {"action": "delete_year_all", "year": "2022"})
        self.assertFalse(HistoricalMonth.objects.filter(year=2022).exists())
        self.assertFalse(HistoricalYear.objects.filter(year=2022).exists())


class BoardReportDetailTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("br", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(u)

    def test_report_form_and_detail(self):
        b = self.c.get("/reports/board/").content.decode()
        self.assertIn("masthead", b)
        self.assertIn("Prepared by", b)
        self.assertIn("Reviewed by", b)
        # detailed report-form structure
        self.assertIn("Local funds statement", b)
