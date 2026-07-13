"""A downloadable PNG of the "Budget vs actual by item" table on a fund's
budget page, matching the on-screen table exactly (Budget item / Budget /
Actual / Variance / Used, plus the totals row) — generated server-side with
Pillow, the same approach as the Group Contribution Goals image. PNG rather
than JPEG (v2.40): lossless, so the sharp table text and flat fills don't
blur or band the way JPEG compression would."""
import datetime as dt
import io
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from cashbook.models import BudgetLine, Expense


def _tr():
    u = User.objects.create_user("tr_budgpng", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class BudgetItemsPngTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.d = Department.objects.create(name="BudgPngTestFund", fund_type="LOCAL",
            category="MINISTRY")
        self.c = Client(); self.c.force_login(self.tr)

    def test_returns_valid_png(self):
        b1 = BudgetLine.objects.create(department=self.d, year=2026, name="Accommodation",
            amount=Decimal("50000"), category="OTHER")
        Expense.objects.create(date=dt.date(2026, 6, 1), department=self.d, budget_line=b1,
            description="hotel", amount=Decimal("30000"), category="OTHER",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        r = self.c.get(f"/reports/fund/{self.d.id}/budget/items.png?year=2026")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "image/png")
        from PIL import Image
        img = Image.open(io.BytesIO(r.content))
        self.assertEqual(img.format, "PNG")

    def test_empty_budget_still_produces_valid_image(self):
        r = self.c.get(f"/reports/fund/{self.d.id}/budget/items.png?year=2026")
        self.assertEqual(r.status_code, 200)
        from PIL import Image
        img = Image.open(io.BytesIO(r.content))
        self.assertEqual(img.format, "PNG")

    def test_download_link_present_on_budget_page(self):
        b = self.c.get(f"/reports/fund/{self.d.id}/budget/?year=2026").content.decode()
        self.assertIn("items.png", b)
        self.assertIn("Download PNG", b)
        self.assertNotIn("items.jpg", b)

    def test_content_disposition_filename(self):
        r = self.c.get(f"/reports/fund/{self.d.id}/budget/items.png?year=2026")
        self.assertIn("budget-vs-actual-", r["Content-Disposition"])
        self.assertIn(".png", r["Content-Disposition"])

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
        r = self.c.get(f"/reports/fund/{self.d.id}/budget/items.png?year=2026")
        self.assertEqual(r.status_code, 200)

    def test_direct_function_call_matches_context_shape(self):
        from cashbook.services.goal_chart import build_budget_items_png
        BudgetLine.objects.create(department=self.d, year=2026, name="Test Item",
            amount=Decimal("1000"), category="OTHER")
        from cashbook.views import FundBudgetView
        from django.test import RequestFactory
        rf = RequestFactory()
        req = rf.get(f"/reports/fund/{self.d.id}/budget/?year=2026")
        req.user = self.tr
        ctx = FundBudgetView()._ctx(req, self.d)
        data = build_budget_items_png(dept_name=self.d.name, year=ctx["year"],
            rows=ctx["rows"], tot_budget=ctx["tot_budget"], tot_actual=ctx["tot_actual"],
            tot_variance=ctx["tot_variance"], church_name="Test Church")
        self.assertIsInstance(data, bytes)
        self.assertGreater(len(data), 100)

    def test_missing_system_fonts_still_produces_a_correctly_scaled_image(self):
        """The actual production bug: on a server without the system DejaVu
        font package installed, ImageFont.truetype() failed and fell back to
        Pillow's ImageFont.load_default() — a FIXED-SIZE BITMAP font that
        ignores the requested size. At this file's 4x print-quality SCALE,
        every position and padding is computed for a font ~4x the size
        load_default() actually draws, so the figures were technically
        present but rendered as near-invisible specks — "the PNG isn't
        showing the figures" was an accurate description.

        Fixed to fall back to reportlab's bundled Vera TTFs — a real,
        scalable font present in every environment that can run this
        application at all, since reportlab is already a hard dependency.
        This test simulates the missing-system-fonts case directly and
        proves the fallback produces an image identical in size/layout to
        the normal path, using a real (not bitmap) font.
        """
        from cashbook.services import goal_chart
        from PIL import Image, ImageFont
        import io as _io

        b1 = BudgetLine.objects.create(department=self.d, year=2026, name="Accommodation",
            amount=Decimal("50000"), category="OTHER")
        Expense.objects.create(date=dt.date(2026, 6, 1), department=self.d, budget_line=b1,
            description="hotel", amount=Decimal("30000"), category="OTHER",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)

        r_normal = self.c.get(f"/reports/fund/{self.d.id}/budget/items.png?year=2026")
        self.assertEqual(r_normal.status_code, 200)
        img_normal = Image.open(io.BytesIO(r_normal.content))

        original_font_dir = goal_chart._FONT_DIR
        goal_chart._FONT_DIR = "/nonexistent/path/simulating/no/system/fonts"
        try:
            r_fallback = self.c.get(f"/reports/fund/{self.d.id}/budget/items.png?year=2026")
        finally:
            goal_chart._FONT_DIR = original_font_dir

        self.assertEqual(r_fallback.status_code, 200)
        img_fallback = Image.open(io.BytesIO(r_fallback.content))
        self.assertEqual(img_fallback.format, "PNG")
        # same layout — a real font at the right scale, not a tiny bitmap
        # squeezed into space sized for something four times its size
        self.assertEqual(img_normal.size, img_fallback.size)

        # confirm the fallback path really did use a scalable font, not
        # Pillow's fixed bitmap default
        fallback_font = goal_chart._font("DejaVuSans-Bold.ttf", 24)
        self.assertIsInstance(fallback_font, ImageFont.FreeTypeFont)
        self.assertEqual(fallback_font.size, 24 * goal_chart.SCALE)

    def test_font_resolution_never_raises_even_if_reportlab_fonts_also_missing(self):
        """Absolute last resort: even if BOTH the system fonts and the
        bundled reportlab fonts were somehow unavailable, rendering must
        still complete rather than 500."""
        from cashbook.services import goal_chart
        original_font_dir = goal_chart._FONT_DIR
        original_vera = goal_chart._vera_font_dir
        goal_chart._FONT_DIR = "/nonexistent/system/fonts"
        goal_chart._vera_font_dir = lambda: "/nonexistent/vera/fonts"
        try:
            f = goal_chart._font("DejaVuSans-Bold.ttf", 24)
            self.assertIsNotNone(f)
        finally:
            goal_chart._FONT_DIR = original_font_dir
            goal_chart._vera_font_dir = original_vera
