"""Tests for the redesigned Treasurer's Report board pack.

Covers the two new board-pack components (executive snapshot with period-on-
period movement, board action summary), the dedicated presentation template
(cover, sticky navigator, grouped/collapsible sections, print layout), the
grouped-context plumbing on the engine view, cross-statement reconciliation of
the headline figures, and that every export format still renders — while the
generic engine template and other reports are unchanged.

Every assertion that touches a figure checks it against the same registry
metric the rest of the report uses, so the redesign cannot silently diverge
from the Financial Metrics Registry / Semantic Reporting Layer.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from cashbook.models import Expense
from core.reporting import ReportContext, component_registry, registry
from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from reports.board_pack_components import (BoardActionSummaryComponent,
                                           ExecutiveSnapshotComponent)


def _treasurer(username="bp_tr"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
    return u


def _ctx(start=dt.date(2026, 1, 1), end=dt.date(2026, 12, 31)):
    return ReportContext.for_period(start, end)


class _Seed(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("bp_seed", password="x",
                                           is_superuser=True)
        self.local = Department.objects.create(name="Building", fund_type="LOCAL")
        self.trust = Department.objects.create(name="Tithe", fund_type="TRUST")
        for amt, dep in [("40000", self.local), ("18000", self.trust)]:
            Transaction.objects.create(
                date=dt.date(2026, 5, 4), channel="CASH", direction="CREDIT",
                amount=Decimal(amt), department=dep, allocation_status="AUTO",
                confirmed=True)
        Expense.objects.create(
            date=dt.date(2026, 5, 10), department=self.local, description="Repairs",
            amount=Decimal("6000"), category="MAINTENANCE", status="PAID",
            recorded_by=self.u)


class ComponentRegistrationTests(_Seed):
    def test_components_registered(self):
        self.assertTrue(component_registry.has("executive_snapshot"))
        self.assertTrue(component_registry.has("board_action_summary"))

    def test_declared_metrics_all_registered(self):
        from core.metrics import metrics
        for comp in (ExecutiveSnapshotComponent(), BoardActionSummaryComponent()):
            for m in comp.declared_metrics:
                self.assertIn(m, metrics.registry, m)


class ExecutiveSnapshotTests(_Seed):
    def test_snapshot_renders_expected_cards(self):
        data = ExecutiveSnapshotComponent().render(_ctx(), {})
        labels = [r.cells["label"] for r in data.rows]
        for expected in ("Total receipts", "Total payments",
                         "Net surplus / (deficit)", "Closing cash position",
                         "Trust still to remit", "Active funds"):
            self.assertIn(expected, labels)
        self.assertEqual(data.kind, "kpi")
        self.assertTrue(data.extra.get("snapshot"))

    def test_receipts_reconcile_with_fund_summary(self):
        ctx = _ctx()
        data = ExecutiveSnapshotComponent().render(ctx, {})
        by_label = {r.cells["label"]: r.cells["value"] for r in data.rows}
        expected_receipts = sum((r["receipts"] or 0 for r in ctx.fund_summary()),
                                Decimal(0))
        self.assertEqual(Decimal(by_label["Total receipts"]), expected_receipts)

    def test_net_equals_receipts_less_payments(self):
        data = ExecutiveSnapshotComponent().render(_ctx(), {})
        by_label = {r.cells["label"]: Decimal(r.cells["value"]) for r in data.rows}
        self.assertEqual(
            by_label["Net surplus / (deficit)"],
            by_label["Total receipts"] - by_label["Total payments"])

    def test_closing_matches_fund_summary_closing(self):
        ctx = _ctx()
        data = ExecutiveSnapshotComponent().render(ctx, {})
        by_label = {r.cells["label"]: Decimal(r.cells["value"]) for r in data.rows}
        expected = sum((r["closing"] or 0 for r in ctx.fund_summary()), Decimal(0))
        self.assertEqual(by_label["Closing cash position"], expected)

    def test_movement_present_with_period(self):
        # with a bounded period, movement metadata is attached
        data = ExecutiveSnapshotComponent().render(_ctx(), {})
        moved = [r for r in data.rows if "delta_abs" in r.meta]
        self.assertTrue(moved)


class BoardActionSummaryTests(_Seed):
    def test_renders_without_error(self):
        data = BoardActionSummaryComponent().render(_ctx(), {})
        self.assertIn(data.kind, ("table", "info"))

    def test_flags_trust_to_remit_followup(self):
        # when the registry reports trust still to remit, a follow-up appears
        ctx = _ctx()
        to_remit = ctx.trust_to_remit() or 0
        data = BoardActionSummaryComponent().render(ctx, {})
        if to_remit and data.kind == "table":
            actions = " ".join(r.cells.get("action", "") for r in data.rows)
            self.assertIn("Remit trust funds", actions)
        else:
            # nothing to remit -> the component still renders cleanly
            self.assertIn(data.kind, ("table", "info"))


class BoardPackReportTests(_Seed):
    def setUp(self):
        super().setUp()
        self.tr = _treasurer()
        self.client.force_login(self.tr)
        self.base = reverse("engine_report", args=["treasurer_report"])
        self.q = "?start=2026-01-01&end=2026-12-31"

    def test_uses_board_pack_template(self):
        report = registry.get("treasurer_report")
        self.assertEqual(report.html_template, "reports/treasurer_board_pack.html")

    def test_renders_cover_and_navigator(self):
        r = self.client.get(self.base + self.q)
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn("bp-cover", html)
        self.assertIn("bp-toc", html)               # sticky navigator
        self.assertIn("bp-snapshot", html)          # executive KPI cards
        self.assertIn("Financial health", html)     # cover health band

    def test_includes_all_statements_and_actions(self):
        html = self.client.get(self.base + self.q).content.decode()
        for needle in ("Statement of financial position",
                       "Statement of cash flows",
                       "Statement of fund balances",
                       "Statement of income &amp; expenditure",
                       "Board action summary"):
            self.assertIn(needle, html, needle)

    def test_ask_ai_removed(self):
        # v2.41: removed — see docs/recommendations.md #52.
        html = self.client.get(self.base + self.q).content.decode()
        self.assertNotIn("Ask AI", html)
        self.assertNotIn("ask-ai-link", html)

    def test_sections_are_grouped(self):
        r = self.client.get(self.base + self.q)
        groups = r.context["section_groups"]
        names = [g["name"] for g in groups]
        self.assertEqual(names[0], "Executive summary")
        self.assertIn("Financial statements", names)
        self.assertIn("Board actions", names)
        # every group has an anchor and at least one section
        for g in groups:
            self.assertTrue(g["anchor"])
            self.assertTrue(g["sections"])

    def test_all_exports_render(self):
        for fmt in ("csv", "xlsx", "pdf", "docx"):
            r = self.client.get(self.base + self.q + f"&export={fmt}")
            self.assertEqual(r.status_code, 200, fmt)

    def test_pdf_is_multipage_board_pack(self):
        import io
        from pypdf import PdfReader
        r = self.client.get(self.base + self.q + "&export=pdf")
        reader = PdfReader(io.BytesIO(r.content))
        self.assertGreater(len(reader.pages), 1)
        txt = "\n".join(p.extract_text() or "" for p in reader.pages)
        self.assertIn("Financial statements", txt)   # group heading in the PDF
        self.assertIn("Board action summary", txt)

    def test_print_uses_board_pack(self):
        r = self.client.get(self.base + self.q + "&print=1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("bp-cover", r.content.decode())


class BackwardCompatTests(_Seed):
    def test_generic_report_unchanged_template(self):
        # a report that does NOT opt into a template still uses the engine one
        report = registry.get("income_statement_v2")
        self.assertIsNone(getattr(report, "html_template", None))

    def test_generic_report_still_renders(self):
        self.client.force_login(_treasurer("bp_bc"))
        # board_report_v2 has since taken a presentation template of its own
        # (reports/board_pack_min.html); it is listed in the board-pack tests,
        # not here. These are the reports that still use the engine grid.
        for key in ("income_statement_v2", "cash_flow_v2"):
            r = self.client.get(reverse("engine_report", args=[key])
                                + "?start=2026-01-01&end=2026-12-31")
            self.assertEqual(r.status_code, 200, key)
            # generic reports render the flat engine grid, not a board pack
            self.assertNotIn("bp-cover", r.content.decode())
            self.assertNotIn("bpm-doc", r.content.decode())
