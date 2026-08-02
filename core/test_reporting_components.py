"""Tests for the Report Component Library, Chart Engine, Rendering Framework and
Financial Dependency Map (the component/rendering phase, v2.29).

Targeted at the new modules. They assert: (1) every component is registered and
draws only from the ReportContext; (2) charts produce valid, metric-sourced
Chart.js configs; (3) each renderer produces its medium and honours layout
export/print visibility; (4) the dependency map traces components → metrics →
services and supports reverse impact analysis; (5) the component-demo report
renders end to end in every format.
"""
import datetime as dt
import json
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.test import RequestFactory
from django.urls import reverse

from core.reporting import (ReportContext, Report, SectionData,
                            component_registry, renderer_registry,
                            build_dependency_map, impact_of_metric, LayoutMeta)
from core.reporting.charts import ChartEngine, ChartSpec
from core.reporting.component_library import (
    FundSummaryComponent, KpiCardsComponent, IncomeSummaryComponent,
    ChartComponent, CashPositionComponent, OutstandingItemsComponent,
    CommentaryComponent, SignatureBlockComponent, InfoPanelComponent)
from core.roles import TREASURER, AUDITOR
from departments.models import Department
from giving.models import Transaction


def _staff(username, role=TREASURER):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


def _ctx(start=dt.date(2026, 1, 1), end=dt.date(2026, 12, 31)):
    return ReportContext.for_period(start, end)


class ComponentRegistryTests(TestCase):
    def test_library_registered(self):
        keys = {k for k, _ in component_registry.all()}
        for expected in ("kpi_cards", "executive_summary", "fund_summary",
                         "income_summary", "expense_summary", "budget_summary",
                         "cash_position", "bank_recon_summary",
                         "outstanding_items", "variance_analysis", "chart",
                         "commentary", "signature_block", "appendix",
                         "info_panel", "financial_statement"):
            self.assertIn(expected, keys)

    def test_create_by_key(self):
        comp = component_registry.create("kpi_cards")
        self.assertIsInstance(comp, KpiCardsComponent)

    def test_unknown_component_raises(self):
        with self.assertRaises(KeyError):
            component_registry.create("nope")

    def test_by_category(self):
        cats = component_registry.by_category()
        self.assertIn("Financial", cats)
        self.assertIn("Visual", cats)


class ComponentRenderTests(TestCase):
    def setUp(self):
        self.dev = Department.objects.create(name="Development", fund_type="LOCAL")
        self.tithe = Department.objects.create(name="Tithe", fund_type="TRUST")
        Transaction.objects.create(
            date=dt.date(2026, 3, 1), channel="BANK", direction="CREDIT",
            amount=Decimal("2500"), department=self.dev, allocation_status="AUTO",
            confirmed=True)
        Transaction.objects.create(
            date=dt.date(2026, 3, 1), channel="ENVELOPE", direction="CREDIT",
            amount=Decimal("1000"), department=self.tithe, allocation_status="AUTO",
            confirmed=True)

    def test_kpi_cards_from_metrics(self):
        data = KpiCardsComponent().build(_ctx(), {})
        self.assertEqual(data.kind, "kpi")
        labels = [r.cells["label"] for r in data.rows]
        self.assertIn("Total income", labels)
        # records provenance
        self.assertTrue(set(data.extra["metrics_used"]))

    def test_fund_summary_has_drilldown_and_total(self):
        data = FundSummaryComponent().build(_ctx(), {"consolidated": True})
        self.assertEqual(data.kind, "table")
        self.assertIsNotNone(data.total)
        self.assertTrue(any(r.url for r in data.rows))   # drill-down present

    def test_income_summary_total_matches_metric(self):
        ctx = _ctx()
        data = IncomeSummaryComponent().build(ctx, {})
        self.assertEqual(data.total.cells["total"], ctx.total_income())

    def test_component_records_layout_and_metrics(self):
        comp = FundSummaryComponent(layout=LayoutMeta(width=6, order=5))
        data = comp.build(_ctx(), {})
        self.assertEqual(data.extra["layout"]["width"], 6)
        self.assertIn("fund_summary", data.extra["metrics_used"])

    def test_cash_position_keyvalue(self):
        data = CashPositionComponent().build(_ctx(), {})
        self.assertEqual(data.kind, "keyvalue")

    def test_outstanding_items(self):
        data = OutstandingItemsComponent().build(_ctx(), {})
        items = [r.cells["item"] for r in data.rows]
        self.assertIn("Trust funds still to remit", items)

    def test_commentary_and_info_and_signature(self):
        self.assertEqual(CommentaryComponent("hi").build(_ctx(), {}).kind,
                         "commentary")
        self.assertEqual(InfoPanelComponent("note").build(_ctx(), {}).kind, "info")
        sig = SignatureBlockComponent().build(_ctx(), {})
        self.assertEqual(sig.kind, "signature")
        self.assertEqual(len(sig.rows), 3)

    def test_empty_commentary_hidden(self):
        self.assertIsNone(CommentaryComponent("").build(_ctx(), {}))


class ChartEngineTests(TestCase):
    def setUp(self):
        self.dev = Department.objects.create(name="Development", fund_type="LOCAL")
        Transaction.objects.create(
            date=dt.date(2026, 3, 1), channel="BANK", direction="CREDIT",
            amount=Decimal("2500"), department=self.dev, allocation_status="AUTO",
            confirmed=True)

    def test_all_chart_types_produce_valid_configs(self):
        specs = [
            ChartEngine.line("l", ["A", "B"], [("s", [1, 2])]),
            ChartEngine.bar("b", ["A"], [("s", [1])], stacked=True),
            ChartEngine.doughnut("d", ["A", "B"], [1, 2]),
            ChartEngine.waterfall("w", ["o", "+", "-", "c"], [100, 50, -30, 0]),
            ChartEngine.gauge("g", 60, 100),
            ChartEngine.comparison("c", ["A"], [("x", [1]), ("y", [2])]),
        ]
        for spec in specs:
            cfg = spec.to_config()
            json.dumps(cfg)          # must be JSON-serialisable
            self.assertIn("type", cfg)
            self.assertIn("data", cfg)

    def test_stacked_and_waterfall_set_stacked_scales(self):
        for spec in (ChartEngine.bar("b", ["A"], [("s", [1])], stacked=True),
                     ChartEngine.waterfall("w", ["a", "b"], [10, 5])):
            cfg = spec.to_config()
            self.assertTrue(cfg["options"]["scales"]["y"]["stacked"])

    def test_metric_driven_income_by_channel(self):
        ctx = _ctx()
        spec = ChartEngine.income_by_channel(ctx)
        self.assertEqual(spec.metrics_used, ["income_by_channel"])
        self.assertEqual(spec.chart_type, "doughnut")

    def test_fund_balances_chart_uses_fund_summary(self):
        spec = ChartEngine.fund_closing_balances(_ctx())
        self.assertIn("fund_summary", spec.metrics_used)


class RendererTests(TestCase):
    def setUp(self):
        self.dev = Department.objects.create(name="Development", fund_type="LOCAL")
        Transaction.objects.create(
            date=dt.date(2026, 3, 1), channel="BANK", direction="CREDIT",
            amount=Decimal("2500"), department=self.dev, allocation_status="AUTO",
            confirmed=True)
        self.user = _staff("rend_tr")

    def _rendered(self):
        report = Report(
            key="rtest", title="Renderer test",
            sections=[
                KpiCardsComponent(layout=LayoutMeta(export_visible=False)),
                FundSummaryComponent(),
                InfoPanelComponent("methodology"),   # export_visible False by default
            ])
        req = RequestFactory().get("/x?start=2026-01-01&end=2026-12-31")
        req.user = self.user
        return report.render(req)

    def test_all_formats_registered(self):
        for fmt in ("html", "csv", "xlsx", "pdf", "docx", "print"):
            self.assertIsNotNone(renderer_registry.get(fmt))

    def test_csv_excludes_export_hidden_components(self):
        rendered = self._rendered()
        resp = renderer_registry.get("csv").render(rendered, church="X")
        body = resp.content.decode()
        self.assertIn("Fund balances", body)          # export-visible
        self.assertNotIn("methodology", body)          # info panel hidden

    def test_xlsx_produces_spreadsheet(self):
        resp = renderer_registry.get("xlsx").render(self._rendered(), church="X")
        self.assertIn("spreadsheetml", resp["Content-Type"])

    def test_pdf_produces_pdf(self):
        resp = renderer_registry.get("pdf").render(self._rendered(), church="X")
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertTrue(resp.content[:4] == b"%PDF")

    def test_docx_carries_the_table_data_not_just_the_titles(self):
        """The regression that shipped in 3.45.0: ``is_empty`` is a method, so
        reading it as an attribute was always truthy and every table section
        printed "Nothing to report" instead of its rows. The tests passed
        because section TITLES are written before that branch — so this asserts
        a figure from inside a table, which is the only thing that proves the
        table is there."""
        from core.reporting.wordml import docx_text
        resp = renderer_registry.get("docx").render(self._rendered(), church="X")
        text = docx_text(resp.content)
        self.assertNotIn("Nothing to report", text)
        self.assertIn("Development", text)       # a row label
        self.assertIn("2,500.00", text)          # and its figure

    def test_docx_is_a_real_package(self):
        import io
        import zipfile
        from core.reporting.wordml import docx_text
        resp = renderer_registry.get("docx").render(self._rendered(), church="X")
        self.assertEqual(resp["Content-Type"],
                         "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            self.assertIsNone(z.testzip())
            self.assertIn("word/document.xml", z.namelist())
        self.assertIn("Fund balances", docx_text(resp.content))

    def test_print_renderer_drops_print_hidden(self):
        # a component explicitly marked print_visible=False drops out of print
        from core.reporting.component_library import CommentaryComponent
        report = Report(key="ptest", title="Print test",
                        sections=[
                            FundSummaryComponent(),
                            CommentaryComponent(
                                "screen only", key="screen_note",
                                title="Screen note",
                                layout=LayoutMeta(print_visible=False)),
                        ])
        req = RequestFactory().get("/x?start=2026-01-01&end=2026-12-31")
        req.user = self.user
        rendered = report.render(req)
        ctx = renderer_registry.get("print").render(rendered)
        titles = [s.title for s in ctx["sections"]]
        self.assertIn("Fund balances", titles)
        self.assertNotIn("Screen note", titles)
        self.assertTrue(ctx["print_mode"])


class DependencyMapTests(TestCase):
    def setUp(self):
        self.dev = Department.objects.create(name="Development", fund_type="LOCAL")
        self.user = _staff("dep_tr")

    def _rendered(self):
        report = Report(key="deptest", title="Dep test",
                        sections=[KpiCardsComponent(), FundSummaryComponent(),
                                  IncomeSummaryComponent()])
        req = RequestFactory().get("/x?start=2026-01-01&end=2026-12-31")
        req.user = self.user
        return report.render(req)

    def test_map_lists_metrics_and_services(self):
        dm = build_dependency_map(self._rendered())
        self.assertIn("fund_summary", dm.all_metrics())
        self.assertIn("total_income", dm.all_metrics())
        # services resolved from registry metadata
        self.assertTrue(any("balances" in s for s in dm.all_services()))

    def test_reverse_index_for_impact_analysis(self):
        dm = build_dependency_map(self._rendered())
        rev = dm.metric_to_components()
        # fund_summary is used by several components
        self.assertIn("fund_summary", rev)
        self.assertGreaterEqual(len(rev["fund_summary"]), 2)

    def test_as_dict_serialisable(self):
        dm = build_dependency_map(self._rendered())
        json.dumps(dm.as_dict())

    def test_impact_of_metric_static(self):
        report = Report(key="imp", title="Imp",
                        sections=[FundSummaryComponent()])
        hits = impact_of_metric("fund_summary", [report])
        self.assertEqual(hits, [("imp", "fund_summary")])


class ComponentDemoReportTests(TestCase):
    def setUp(self):
        self.dev = Department.objects.create(name="Development", fund_type="LOCAL")
        self.tithe = Department.objects.create(name="Tithe", fund_type="TRUST")
        Transaction.objects.create(
            date=dt.date(2026, 3, 1), channel="BANK", direction="CREDIT",
            amount=Decimal("5000"), department=self.dev, allocation_status="AUTO",
            confirmed=True)
        self.tr = _staff("cd_tr")
        self.aud = _staff("cd_aud", AUDITOR)

    def test_registered(self):
        from core.reporting import registry
        self.assertIsNotNone(registry.get("board_pack_demo"))

    def test_html_renders_all_component_kinds(self):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("engine_report", args=["board_pack_demo"])
                            + "?start=2026-01-01&end=2026-12-31")
        self.assertEqual(r.status_code, 200)
        for needle in ("Executive summary", "Key figures", "Fund balances",
                       "Signatures", "chart_chart_income_channel"):
            self.assertContains(r, needle)

    def test_every_export_format(self):
        self.client.force_login(self.tr)
        base = reverse("engine_report", args=["board_pack_demo"])
        for fmt, ctype in (("csv", "text/csv"),
                           ("xlsx", "spreadsheet"),
                           ("pdf", "application/pdf"),
                           ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")):
            r = self.client.get(base + f"?start=2026-01-01&end=2026-12-31&export={fmt}")
            self.assertEqual(r.status_code, 200, fmt)
            self.assertIn(ctype, r["Content-Type"], fmt)

    def test_dependency_map_endpoint(self):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("engine_report", args=["board_pack_demo"])
                            + "?start=2026-01-01&end=2026-12-31&deps=json")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("metric_to_components", data)
        self.assertTrue(data["metrics"])

    def test_auditor_can_view(self):
        self.client.force_login(self.aud)
        r = self.client.get(reverse("engine_report", args=["board_pack_demo"]))
        self.assertEqual(r.status_code, 200)
