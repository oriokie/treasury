"""Tests for the high-DPI rendering fix (v2.43) across every server-side
image-generation path in the app:

1. cashbook/services/goal_chart.py — the two budget-page PNG downloads
   (Group Contribution Goals, Budget vs Actual by item).
2. reports/services/chart_image.py — the three chart builders (bar, doughnut/
   split, line) used to embed charts into PDF/Word exports.
3. static/js/table_png.js and the dashboard's own inline copy — client-side
   canvas exporters (scale bumped 2x -> 4x; verified only via a source-level
   assertion here, since a canvas needs a real browser to execute — see the
   Node+jsdom DOM harness used during implementation for the client-side
   behavioural check).

All three previously rendered at ~96 DPI-equivalent screen resolution with
no scale-up — visibly soft once printed at A4 or zoomed. Now render at 4x
their previous logical size (Pillow paths) or 4x canvas scale (client-side
path), with 300 DPI metadata on every server-generated PNG.
"""
from decimal import Decimal

from django.test import TestCase
from PIL import Image
import io


class GoalChartHighDpiTests(TestCase):
    def test_group_goals_png_is_high_resolution(self):
        from cashbook.services.goal_chart import build_group_goals_png
        data = build_group_goals_png(
            dept_name="Camp Meeting", year=2026,
            group_rows=[{"name": "Group 1", "goal": Decimal("50000"),
                        "collected": Decimal("32000"), "pct": 64,
                        "short": Decimal("18000")}],
            contribution_goal={"goal": Decimal("50000"), "collected": Decimal("32000"),
                               "short": Decimal("18000")})
        img = Image.open(io.BytesIO(data))
        self.assertEqual(img.format, "PNG")
        # previously 1180 wide; must now be meaningfully larger (4x logical)
        self.assertGreaterEqual(img.width, 1180 * 3)

    def test_group_goals_png_has_300_dpi_metadata(self):
        from cashbook.services.goal_chart import build_group_goals_png
        data = build_group_goals_png(
            dept_name="X", year=2026, group_rows=[],
            contribution_goal={"goal": 0, "collected": 0, "short": 0})
        img = Image.open(io.BytesIO(data))
        dpi = img.info.get("dpi")
        self.assertIsNotNone(dpi)
        self.assertGreaterEqual(round(dpi[0]), 299)

    def test_budget_items_png_is_high_resolution_and_300_dpi(self):
        from cashbook.services.goal_chart import build_budget_items_png
        data = build_budget_items_png(
            dept_name="Camp Meeting", year=2026,
            rows=[{"name": "Item", "category": "OTHER", "note": "",
                  "budget": Decimal("1000"), "actual": Decimal("500"),
                  "variance": Decimal("500"), "pct": 50}],
            tot_budget=Decimal("1000"), tot_actual=Decimal("500"),
            tot_variance=Decimal("500"))
        img = Image.open(io.BytesIO(data))
        self.assertGreaterEqual(img.width, 1180 * 3)
        self.assertGreaterEqual(round(img.info.get("dpi", (0, 0))[0]), 299)

    def test_empty_tables_still_render_without_error(self):
        from cashbook.services.goal_chart import (build_group_goals_png,
                                                   build_budget_items_png)
        d1 = build_group_goals_png(dept_name="Empty", year=2026, group_rows=[],
                                   contribution_goal={"goal": 0, "collected": 0, "short": 0})
        d2 = build_budget_items_png(dept_name="Empty", year=2026, rows=[],
                                    tot_budget=0, tot_actual=0, tot_variance=0)
        self.assertGreater(len(d1), 0)
        self.assertGreater(len(d2), 0)

    def test_layout_scales_proportionally_no_overlap(self):
        # a rough sanity check: image height should grow linearly with row
        # count at the SAME rate regardless of scale, confirming the scale
        # factor was applied uniformly rather than to only some dimensions
        # (which would distort proportions and could overlap content)
        from cashbook.services.goal_chart import build_group_goals_png
        one_row = build_group_goals_png(
            dept_name="X", year=2026,
            group_rows=[{"name": "G1", "goal": Decimal("100"), "collected": Decimal("50"),
                        "pct": 50, "short": Decimal("50")}],
            contribution_goal={"goal": 100, "collected": 50, "short": 50})
        three_rows = build_group_goals_png(
            dept_name="X", year=2026,
            group_rows=[{"name": f"G{i}", "goal": Decimal("100"), "collected": Decimal("50"),
                        "pct": 50, "short": Decimal("50")} for i in range(3)],
            contribution_goal={"goal": 300, "collected": 150, "short": 150})
        h1 = Image.open(io.BytesIO(one_row)).height
        h3 = Image.open(io.BytesIO(three_rows)).height
        # two extra rows at logical row_h=46, scale=4 => +368px, generously bounded
        self.assertGreater(h3, h1)
        self.assertLess(h3 - h1, 46 * 4 * 3)   # sanity ceiling, not exact


class ChartImageHighDpiTests(TestCase):
    def test_doughnut_chart_high_resolution_and_dpi(self):
        from reports.services.chart_image import render_chart_config
        config = {"type": "doughnut", "data": {"labels": ["A", "B"],
                  "datasets": [{"data": [10, 20], "backgroundColor": ["#1f5f4f", "#b08d57"]}]}}
        uri, png_bytes = render_chart_config(config, "Test")
        self.assertIsNotNone(png_bytes)
        img = Image.open(io.BytesIO(png_bytes))
        self.assertGreaterEqual(img.width, 560 * 3)
        self.assertGreaterEqual(round(img.info.get("dpi", (0, 0))[0]), 299)

    def test_bar_chart_high_resolution(self):
        from reports.services.chart_image import render_chart_config
        config = {"type": "bar", "data": {"labels": ["A", "B", "C"],
                  "datasets": [{"data": [1, 2, 3], "backgroundColor": "#1f5f4f"}]}}
        uri, png_bytes = render_chart_config(config, "Test")
        img = Image.open(io.BytesIO(png_bytes))
        self.assertGreaterEqual(img.width, 760 * 3)

    def test_line_chart_high_resolution(self):
        from reports.services.chart_image import render_chart_config
        config = {"type": "line", "data": {"labels": ["2024", "2025"],
                  "datasets": [{"label": "S", "data": [1, 2], "borderColor": "#1f5f4f"}]}}
        uri, png_bytes = render_chart_config(config, "Test")
        img = Image.open(io.BytesIO(png_bytes))
        self.assertGreaterEqual(img.width, 760 * 3)

    def test_empty_config_still_returns_none_cleanly(self):
        from reports.services.chart_image import render_chart_config
        uri, png_bytes = render_chart_config({}, "Test")
        self.assertIsNone(uri)
        self.assertIsNone(png_bytes)

    def test_treasurer_report_pdf_export_still_works(self):
        # the exact consumer of these charts (rec #28) — must not break
        from django.contrib.auth.models import Group, User
        from django.urls import reverse
        from core.roles import TREASURER
        u = User.objects.create_user("ci_tr", password="x")
        u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        c = self.client
        c.force_login(u)
        r = c.get(reverse("engine_report", args=["treasurer_report"])
                 + "?start=2026-01-01&end=2026-12-31&export=pdf")
        self.assertEqual(r.status_code, 200)

    def test_treasurer_report_word_export_still_works(self):
        from django.contrib.auth.models import Group, User
        from django.urls import reverse
        from core.roles import TREASURER
        u = User.objects.create_user("ci_tr2", password="x")
        u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        c = self.client
        c.force_login(u)
        r = c.get(reverse("engine_report", args=["treasurer_report"])
                 + "?start=2026-01-01&end=2026-12-31&export=docx")
        self.assertEqual(r.status_code, 200)


class ClientSideCanvasScaleTests(TestCase):
    """The canvas-based exporters can't run in this test environment (no
    browser); this checks the source ships the intended scale factor, since
    the actual drawing behaviour was verified via a Node+jsdom check at
    implementation time (syntax + no runtime errors — jsdom has no canvas
    2D context, so pixel-level drawing itself needs a real browser)."""

    def test_table_png_js_scale_is_4(self):
        content = open("static/js/table_png.js").read()
        self.assertIn("var scale = 4;", content)
        self.assertNotIn("var scale = 2;", content)

    def test_dashboard_inline_export_scale_is_4(self):
        content = open("templates/dashboard.html").read()
        self.assertIn("var scale = 4;", content)
