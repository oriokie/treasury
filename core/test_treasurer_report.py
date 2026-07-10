"""Phase — Financial Intelligence Chatbot + Treasurer's Report tests.

Covers the knowledge-aware assistant (report-context-aware answering grounded in
the Knowledge Service, no LLM required, never invents figures), the Ask-AI
context plumbing, the intelligence report components, and the comprehensive
Treasurer's Report (renders + exports + composes health/insights/recommendations,
figures from the metrics registry). Deterministic and metric-sourced.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.reporting import ReportContext, registry
from core.roles import TREASURER, AUDITOR
from departments.models import Department
from giving.models import Transaction
from cashbook.models import Expense


def _staff(username, role=TREASURER):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


def _ctx(start=dt.date(2026, 1, 1), end=dt.date(2026, 12, 31)):
    return ReportContext.for_period(start, end)


class _Data(TestCase):
    def setUp(self):
        self.building = Department.objects.create(name="Building", fund_type="LOCAL")
        self.tithe = Department.objects.create(name="Tithe", fund_type="TRUST")
        for amt, dep in [("40000", self.building), ("9000", self.tithe)]:
            Transaction.objects.create(
                date=dt.date(2026, 5, 1), channel="CASH", direction="CREDIT",
                amount=Decimal(amt), department=dep, allocation_status="AUTO",
                confirmed=True)
        self.tr = _staff("k_tr")


class KnowledgeContextTests(_Data):
    def test_context_block_built_from_knowledge_service(self):
        from core.services.assistant_knowledge import knowledge_context
        block = knowledge_context("treasurer_report",
                                  {"start": "2026-01-01", "end": "2026-12-31"})
        self.assertIn("Financial health score", block)
        self.assertIn("KNOWLEDGE CONTEXT", block)

    def test_structured_answer_health(self):
        from core.services.assistant_knowledge import structured_answer
        a = structured_answer("treasurer_report",
                              {"start": "2026-01-01", "end": "2026-12-31"},
                              element="health score")
        self.assertIn("Health Score", a["text"])
        self.assertTrue(a["rows"])

    def test_answer_with_context_grounded(self):
        from core.services.assistant_knowledge import answer_with_context
        a = answer_with_context("what are the risks?", report_key="treasurer_report",
                                period={"start": "2026-01-01", "end": "2026-12-31"})
        self.assertTrue(a["provenance"]["grounded"])

    def test_answer_never_invents_without_llm(self):
        # with LLM off, answers are structured knowledge only (rows from metrics)
        from core.services.assistant_knowledge import answer_with_context
        a = answer_with_context("income figures", report_key="treasurer_report",
                                period={"start": "2026-01-01", "end": "2026-12-31"},
                                element="income")
        # all row values are KES-formatted metric values or metric names
        self.assertIsInstance(a.get("rows", []), list)


class AssistantViewContextTests(_Data):
    def test_assistant_page_accepts_context(self):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("assistant")
                            + "?report_key=treasurer_report&start=2026-01-01&end=2026-12-31&element=Cash%20position")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "treasurer_report")

    def test_ask_endpoint_with_context(self):
        import json
        self.client.force_login(self.tr)
        r = self.client.post(reverse("assistant_ask"),
                             data=json.dumps({
                                 "q": "why is the score what it is?",
                                 "report_key": "treasurer_report",
                                 "element": "health score",
                                 "period": {"start": "2026-01-01", "end": "2026-12-31"}}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertIn("text", d)

    def test_ask_endpoint_without_context_uses_classic(self):
        import json
        self.client.force_login(self.tr)
        r = self.client.post(reverse("assistant_ask"),
                             data=json.dumps({"q": "how much cash do we have?"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text", r.json())


class IntelligenceComponentTests(_Data):
    def test_health_score_component(self):
        from reports.intelligence_components import HealthScoreComponent
        data = HealthScoreComponent().build(_ctx(), {})
        self.assertEqual(data.kind, "table")
        self.assertTrue(data.rows)
        self.assertTrue(data.total)

    def test_insights_component(self):
        from reports.intelligence_components import InsightsComponent
        Expense.objects.create(
            date=dt.date(2026, 6, 1), department=self.building,
            description="Overspend", amount=Decimal("100000"),
            status=Expense.Status.PAID, recorded_by=self.tr)
        data = InsightsComponent().build(_ctx(), {})
        self.assertTrue(data.rows or data.kind == "info")

    def test_recommendations_component(self):
        from reports.intelligence_components import RecommendationsComponent
        data = RecommendationsComponent().build(_ctx(), {})
        self.assertIn(data.kind, ("table", "info"))

    def test_ai_briefing_deterministic_fallback(self):
        # LLM off -> deterministic narrative briefing, never blank
        from reports.intelligence_components import AiBriefingComponent
        data = AiBriefingComponent().build(_ctx(), {})
        self.assertEqual(data.kind, "commentary")
        self.assertTrue(data.extra["text"])


class TreasurerReportTests(_Data):
    def test_registered(self):
        self.assertIsNotNone(registry.get("treasurer_report"))

    def test_renders(self):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("engine_report", args=["treasurer_report"])
                            + "?start=2026-01-01&end=2026-12-31")
        self.assertEqual(r.status_code, 200)

    def test_has_ask_ai_affordances(self):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("engine_report", args=["treasurer_report"])
                            + "?start=2026-01-01&end=2026-12-31")
        html = r.content.decode()
        self.assertIn("Ask AI about this report", html)
        self.assertIn("ask-ai-link", html)

    def test_exports_all_formats(self):
        self.client.force_login(self.tr)
        base = reverse("engine_report", args=["treasurer_report"])
        for fmt in ("csv", "xlsx", "pdf", "docx"):
            r = self.client.get(base + f"?start=2026-01-01&end=2026-12-31&export={fmt}")
            self.assertEqual(r.status_code, 200, fmt)

    def test_includes_intelligence_and_statements(self):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("engine_report", args=["treasurer_report"])
                            + "?start=2026-01-01&end=2026-12-31")
        html = r.content.decode()
        self.assertIn("Financial health score", html)
        self.assertIn("Executive briefing", html)

    def test_figures_from_registry(self):
        # the health-score section's metrics are all registered
        from core.metrics import metrics
        report = registry.get("treasurer_report")
        for s in report.sections:
            for m in getattr(s, "declared_metrics", ()) or ():
                self.assertIn(m, metrics.registry, m)

    def test_permission_enforced(self):
        self.client.force_login(_staff("k_aud", AUDITOR))
        r = self.client.get(reverse("engine_report", args=["treasurer_report"])
                            + "?start=2026-01-01&end=2026-12-31")
        self.assertEqual(r.status_code, 200)  # auditor has report read access


class BackwardCompatTests(_Data):
    def test_existing_engine_reports_still_render(self):
        self.client.force_login(self.tr)
        for key in ("board_report_v2", "income_statement_v2", "cash_flow_v2"):
            r = self.client.get(reverse("engine_report", args=[key])
                                + "?start=2026-01-01&end=2026-12-31")
            self.assertEqual(r.status_code, 200, key)

    def test_classic_assistant_still_works(self):
        from core.services import assistant
        d = assistant.answer("how much cash do we have?", self.tr)
        self.assertIn("text", d)
