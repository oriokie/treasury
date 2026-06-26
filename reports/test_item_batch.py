"""Monthly historical records + import (ITEM A), financial-position split (ITEM B),
monthly treasurer's report (ITEM C)."""
import io
import datetime as dt
from decimal import Decimal
import openpyxl
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from django.core.files.uploadedfile import SimpleUploadedFile
from core.models import HistoricalMonth, HistoricalYear
from departments.models import Department
from giving.models import Transaction


class HistoricalMonthTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("hm", password="x", is_superuser=True)
        self.u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(self.u)

    def test_sample_download(self):
        r = self.c.get("/reports/annual/historical/?sample=1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheet", r["Content-Type"])

    def test_save_month_recomputes_year(self):
        self.c.post("/reports/annual/historical/", {"action": "save_month",
            "year": "2024", "month": "1", "collection": "100000",
            "trust_fund": "60000", "expenditure": "40000"})
        self.c.post("/reports/annual/historical/", {"action": "save_month",
            "year": "2024", "month": "2", "collection": "50000",
            "trust_fund": "30000", "expenditure": "20000"})
        hy = HistoricalYear.objects.get(year=2024)
        self.assertEqual(hy.collection, Decimal("150000"))
        self.assertEqual(hy.trust_fund, Decimal("90000"))

    def test_excel_import(self):
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Monthly history"
        ws.append(["Year", "Month", "Collection", "Trust fund", "Expenditure"])
        ws.append([2023, 1, 80000, 50000, 30000])
        ws.append([2023, 2, 90000, 55000, 35000])
        buf = io.BytesIO(); wb.save(buf)
        up = SimpleUploadedFile("h.xlsx", buf.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.c.post("/reports/annual/historical/", {"action": "import", "file": up})
        self.assertEqual(HistoricalMonth.objects.filter(year=2023).count(), 2)
        self.assertEqual(HistoricalYear.objects.get(year=2023).collection, Decimal("170000"))


class FinancialPositionSplitTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("fp", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(u)

    def test_trust_split_and_explanations(self):
        b = self.c.get("/reports/financial-position/").content.decode()
        self.assertIn("Receipted (recorded against a trust fund)", b)
        self.assertIn("Not yet receipted", b)
        self.assertIn("accumulated local reserves", b)
        self.assertIn("set aside for specific purposes", b)


class MonthlyTreasurerReportTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("mt", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(u)

    def test_all_sections_render(self):
        b = self.c.get("/reports/board/?as_of=2026-06-15").content.decode()
        for s in ["1 · Collections summary", "2 · Trust funds",
                  "3 · LCB sub-accounts", "4 · Five-year trend",
                  "5 · LCB expenses", "6 · Local funds", "7 · Income",
                  "8 · Statement of financial position", "9 · Cash-flow statement",
                  "10 · Latest bank reconciliation"]:
            self.assertIn(s, b)

    def test_has_ai_or_fallback_summary(self):
        b = self.c.get("/reports/board/").content.decode()
        self.assertIn("✦", b)   # the headline summary (AI or rule-based)
