"""Tests for the recommendations pass (v2.38):

* #2  — request-scoped SiteConfig memoization: one query per request instead
        of 7–11, no cross-request retention, save() invalidates mid-request,
        and direct get() outside a request still hits the database.
* #28 — server-side chart images: render_chart_config produces PNGs for
        bar/pie/line configs, tolerates junk, and the engine PDF/Word exports
        embed the images.
* #44c — the board pack marks collapsible sections and ships the toggle.
* #36 — the combined financial_statements_pack report is registered, renders,
        and its statements reconcile (one shared context).
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.models import SiteConfig, _siteconfig_local
from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction


def _treasurer(username):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
    return u


class SiteConfigCacheTests(TestCase):
    def setUp(self):
        SiteConfig.get()          # ensure the row exists

    def tearDown(self):
        _siteconfig_local.scope_open = False
        _siteconfig_local.obj = None

    def test_outside_request_uncached(self):
        # no middleware scope -> every call reads the DB (unchanged behaviour)
        with self.assertNumQueries(1):
            SiteConfig.get()
        with self.assertNumQueries(1):
            SiteConfig.get()

    def test_memoized_inside_scope(self):
        _siteconfig_local.scope_open = True
        _siteconfig_local.obj = None
        try:
            with self.assertNumQueries(1):
                first = SiteConfig.get()
            with self.assertNumQueries(0):
                second = SiteConfig.get()
            self.assertIs(first, second)
        finally:
            _siteconfig_local.scope_open = False
            _siteconfig_local.obj = None

    def test_save_invalidates_memo(self):
        _siteconfig_local.scope_open = True
        _siteconfig_local.obj = None
        try:
            cfg = SiteConfig.get()
            cfg.church_name = "Renamed Church"
            cfg.save()
            self.assertEqual(SiteConfig.get().church_name, "Renamed Church")
        finally:
            _siteconfig_local.scope_open = False
            _siteconfig_local.obj = None

    def test_middleware_closes_scope_after_request(self):
        u = _treasurer("sc_tr")
        self.client.force_login(u)
        r = self.client.get(reverse("transaction_list"))
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(getattr(_siteconfig_local, "obj", None))
        self.assertFalse(getattr(_siteconfig_local, "scope_open", False))


class ChartImageTests(TestCase):
    def _cfg(self, ctype, datasets, labels=("A", "B", "C")):
        return {"type": ctype,
                "data": {"labels": list(labels), "datasets": datasets}}

    def test_bar_config_renders_png(self):
        from reports.services.chart_image import render_chart_config
        uri, png = render_chart_config(
            self._cfg("bar", [{"label": "S", "data": [10, 20, 30],
                               "backgroundColor": "#1f5f4f"}]), "Bars")
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

    def test_doughnut_config_renders_png(self):
        from reports.services.chart_image import render_chart_config
        uri, png = render_chart_config(
            self._cfg("doughnut", [{"data": [60, 40],
                                    "backgroundColor": ["#1f5f4f", "#b08d57"]}],
                      labels=("Local", "Trust")), "Split")
        self.assertTrue(png)

    def test_line_config_renders_png(self):
        from reports.services.chart_image import render_chart_config
        uri, png = render_chart_config(
            self._cfg("line", [{"label": "Income", "data": [5, 9, 7],
                                "borderColor": "#1f5f4f"}]), "Trend")
        self.assertTrue(png)

    def test_junk_config_is_safe(self):
        from reports.services.chart_image import render_chart_config
        for junk in (None, {}, {"type": "bar", "data": {}},
                     {"type": "bar", "data": {"labels": [], "datasets": []}}):
            uri, png = render_chart_config(junk, "x")
            self.assertIsNone(uri)
            self.assertIsNone(png)


class _ReportSeed(TestCase):
    def setUp(self):
        self.tr = _treasurer("rp_tr")
        dept = Department.objects.create(name="Tithe", fund_type="TRUST")
        Transaction.objects.create(
            date=dt.date(2026, 5, 4), channel="CASH", direction="CREDIT",
            amount=Decimal("9000"), department=dept,
            allocation_status="AUTO", confirmed=True)
        self.client.force_login(self.tr)
        self.q = "?start=2026-01-01&end=2026-12-31"


class ChartExportTests(_ReportSeed):
    def test_pdf_export_embeds_chart_images(self):
        import io
        from pypdf import PdfReader
        r = self.client.get(reverse("engine_report", args=["treasurer_report"])
                            + self.q + "&export=pdf")
        self.assertEqual(r.status_code, 200)
        reader = PdfReader(io.BytesIO(r.content))
        images = sum(len(p.images) for p in reader.pages)
        self.assertGreaterEqual(images, 1)

    def test_word_export_embeds_chart_images(self):
        r = self.client.get(reverse("engine_report", args=["treasurer_report"])
                            + self.q + "&export=docx")
        self.assertEqual(r.status_code, 200)
        self.assertIn("data:image/png;base64",
                      r.content.decode("utf-8", "ignore"))


class CollapseToggleTests(_ReportSeed):
    def test_collapsible_sections_marked_and_toggle_shipped(self):
        html = self.client.get(reverse("engine_report",
                                       args=["treasurer_report"])
                               + self.q).content.decode()
        self.assertIn("bp-can-collapse", html)
        self.assertIn("bp-caret", html)
        # the executive snapshot is composed collapsible=False — its cell
        # must NOT carry the collapse class; verify at least one such cell
        self.assertIn('<div class="bp-cell" ', html)


class StatementsPackTests(_ReportSeed):
    def test_registered_and_renders_all_statements(self):
        from core.reporting import registry
        self.assertIsNotNone(registry.get("financial_statements_pack"))
        r = self.client.get(reverse("engine_report",
                                    args=["financial_statements_pack"]) + self.q)
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        for needle in ("Statement of income &amp; expenditure",
                       "Statement of financial position",
                       "Statement of cash flows",
                       "Statement of fund balances",
                       "Trial balance"):
            self.assertIn(needle.lower(), html.lower(), needle)

    def test_pack_statements_reconcile(self):
        # income & expenditure's income figure and the fund summary feeding
        # the position statement come from ONE context — assert the pack's
        # sections agree with direct metric reads
        from core.reporting import ReportContext
        from reports.financial_statements import (
            FinancialPositionSummarySection, FundBalancesStatementSection)
        ctx = ReportContext.for_period(dt.date(2026, 1, 1),
                                       dt.date(2026, 12, 31))
        pos = FinancialPositionSummarySection().render(ctx, {})
        # Group headings ("Assets", "Liabilities") carry no figure of their own,
        # so only the lines that state an amount are read here.
        by = {r.cells["label"]: Decimal(r.cells["value"])
              for r in pos.rows if r.cells["value"] is not None}
        cash = sum((r["closing"] or Decimal(0) for r in ctx.fund_summary()),
                   Decimal(0))
        # v2.42 split "Cash & bank" into Local/Trust; v2.44 reverted that back
        # to a single line, relabelled "Bank (funds on hand)" — petty cash
        # and staff advances are already itemised separately, so what
        # remains is genuinely bank-only.
        self.assertEqual(by["Bank (funds on hand)"]
                         + by["Petty cash float"]
                         + by["Staff advances (receivable)"], cash)

    def test_pack_exports(self):
        for fmt in ("pdf", "xlsx", "csv"):
            r = self.client.get(reverse("engine_report",
                                        args=["financial_statements_pack"])
                                + self.q + f"&export={fmt}")
            self.assertEqual(r.status_code, 200, fmt)
