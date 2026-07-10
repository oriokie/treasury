"""New feature: a downloadable JPEG of the "Budget vs actual by item" table
on a fund's budget page, matching the on-screen table exactly (Budget item /
Budget / Actual / Variance / Used, plus the totals row) — generated
server-side with Pillow, the same established approach as the existing
Group Contribution Goals JPEG, so it renders identically wherever it's
downloaded rather than depending on a browser screenshot."""
import datetime as dt
import io
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from cashbook.models import BudgetLine, Expense


def _tr():
    u = User.objects.create_user("tr_budgjpeg", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class BudgetItemsJpegTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.d = Department.objects.create(name="BudgJpegTestFund", fund_type="LOCAL",
            category="MINISTRY")
        self.c = Client(); self.c.force_login(self.tr)

    def test_returns_valid_jpeg(self):
        b1 = BudgetLine.objects.create(department=self.d, year=2026, name="Accommodation",
            amount=Decimal("50000"), category="OTHER")
        Expense.objects.create(date=dt.date(2026, 6, 1), department=self.d, budget_line=b1,
            description="hotel", amount=Decimal("30000"), category="OTHER",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        r = self.c.get(f"/reports/fund/{self.d.id}/budget/items.jpg?year=2026")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "image/jpeg")
        from PIL import Image
        img = Image.open(io.BytesIO(r.content))
        self.assertEqual(img.format, "JPEG")

    def test_empty_budget_still_produces_valid_image(self):
        r = self.c.get(f"/reports/fund/{self.d.id}/budget/items.jpg?year=2026")
        self.assertEqual(r.status_code, 200)
        from PIL import Image
        img = Image.open(io.BytesIO(r.content))
        self.assertEqual(img.format, "JPEG")

    def test_download_link_present_on_budget_page(self):
        b = self.c.get(f"/reports/fund/{self.d.id}/budget/?year=2026").content.decode()
        self.assertIn("items.jpg", b)
        self.assertIn("Download JPEG", b)

    def test_content_disposition_filename(self):
        r = self.c.get(f"/reports/fund/{self.d.id}/budget/items.jpg?year=2026")
        self.assertIn("budget-vs-actual-", r["Content-Disposition"])
        self.assertIn(".jpg", r["Content-Disposition"])

    def test_multiple_budget_lines_all_included(self):
        for i, (name, amt) in enumerate([("Item A", "10000"), ("Item B", "20000"),
                                          ("Item C", "5000")]):
            BudgetLine.objects.create(department=self.d, year=2026, name=name,
                amount=Decimal(amt), category="OTHER")
        from cashbook.views import FundBudgetView
        from django.test import RequestFactory
        rf = RequestFactory()
        req = rf.get(f"/reports/fund/{self.d.id}/budget/?year=2026")
        req.user = self.tr
        ctx = FundBudgetView()._ctx(req, self.d)
        self.assertEqual(len(ctx["rows"]), 3)
        r = self.c.get(f"/reports/fund/{self.d.id}/budget/items.jpg?year=2026")
        self.assertEqual(r.status_code, 200)

    def test_direct_function_call_matches_context_shape(self):
        from cashbook.services.goal_chart import build_budget_items_jpeg
        BudgetLine.objects.create(department=self.d, year=2026, name="Test Item",
            amount=Decimal("1000"), category="OTHER")
        from cashbook.views import FundBudgetView
        from django.test import RequestFactory
        rf = RequestFactory()
        req = rf.get(f"/reports/fund/{self.d.id}/budget/?year=2026")
        req.user = self.tr
        ctx = FundBudgetView()._ctx(req, self.d)
        data = build_budget_items_jpeg(dept_name=self.d.name, year=ctx["year"],
            rows=ctx["rows"], tot_budget=ctx["tot_budget"], tot_actual=ctx["tot_actual"],
            tot_variance=ctx["tot_variance"], church_name="Test Church")
        self.assertIsInstance(data, bytes)
        self.assertGreater(len(data), 100)
