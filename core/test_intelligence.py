"""Phase 9 — Financial Intelligence Platform tests.

Covers insight generation + explainability, recommendations, health scoring,
trend/forecast, the knowledge service, the workspace + analytics APIs, insight
status persistence, and accounting correctness (insights read only registry
metrics, never a fresh calculation). Deterministic and metric-sourced throughout.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.reporting import ReportContext
from core.intelligence import (IntelligenceEngine, IntelligenceConfig,
                              compute_health_score, recommendations_from_insights,
                              trends, knowledge, intelligence_registry, Severity)
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
        self.welfare = Department.objects.create(name="Welfare", fund_type="LOCAL")
        self.tithe = Department.objects.create(name="Tithe", fund_type="TRUST")
        for amt, dep in [("40000", self.building), ("15000", self.welfare),
                         ("9000", self.tithe)]:
            Transaction.objects.create(
                date=dt.date(2026, 5, 1), channel="CASH", direction="CREDIT",
                amount=Decimal(amt), department=dep, allocation_status="AUTO",
                confirmed=True)
        self.tr = _staff("i_tr")


class InsightGenerationTests(_Data):
    def test_engine_produces_insights(self):
        insights = IntelligenceEngine().analyse(_ctx())
        self.assertTrue(insights)

    def test_insights_sorted_by_priority(self):
        insights = IntelligenceEngine().analyse(_ctx())
        priorities = [i.priority for i in insights]
        self.assertEqual(priorities, sorted(priorities, reverse=True))

    def test_negative_balance_detected(self):
        Expense.objects.create(
            date=dt.date(2026, 6, 1), department=self.building,
            description="Overspend", amount=Decimal("100000"),
            status=Expense.Status.PAID, recorded_by=self.tr)
        insights = IntelligenceEngine().analyse(_ctx())
        codes = {i.code for i in insights}
        self.assertIn("negative_balance", codes)
        neg = next(i for i in insights if i.code == "negative_balance")
        self.assertEqual(neg.severity, Severity.CRITICAL)

    def test_budget_overrun_detected(self):
        self.building.annual_budget = Decimal("1000")
        self.building.save()
        Expense.objects.create(
            date=dt.date(2026, 6, 1), department=self.building,
            description="Over", amount=Decimal("5000"),
            status=Expense.Status.PAID, recorded_by=self.tr)
        insights = IntelligenceEngine().analyse(_ctx())
        self.assertIn("budget_overrun", {i.code for i in insights})

    def test_trust_remittance_insight(self):
        # receipted trust money creates a firm remittance liability
        from giving.models import Transaction as T
        t = T.objects.create(
            date=dt.date(2026, 5, 2), channel="ENVELOPE", direction="CREDIT",
            amount=Decimal("5000"), department=self.tithe,
            allocation_status="AUTO", confirmed=True)
        insights = IntelligenceEngine().analyse(_ctx())
        # trust_to_remit insight fires only if there is a material to-remit balance
        ctx = _ctx()
        if ctx.trust_to_remit() > IntelligenceConfig().material_amount:
            self.assertIn("trust_to_remit", {i.code for i in insights})
        else:
            self.skipTest("no material trust-to-remit balance in this fixture")

    def test_deterministic(self):
        a = [i.fingerprint for i in IntelligenceEngine().analyse(_ctx())]
        b = [i.fingerprint for i in IntelligenceEngine().analyse(_ctx())]
        self.assertEqual(a, b)


class ExplainabilityTests(_Data):
    def test_every_insight_has_explanation(self):
        for i in IntelligenceEngine().analyse(_ctx()):
            self.assertTrue(i.explanation.reason, i.code)
            # every insight names either metrics or the services behind it
            self.assertTrue(i.explanation.metrics or i.explanation.services, i.code)

    def test_thresholds_recorded(self):
        Expense.objects.create(
            date=dt.date(2026, 6, 1), department=self.building,
            description="Overspend", amount=Decimal("100000"),
            status=Expense.Status.PAID, recorded_by=self.tr)
        neg = next(i for i in IntelligenceEngine().analyse(_ctx())
                   if i.code == "negative_balance")
        self.assertIn("balance_floor", neg.explanation.thresholds)

    def test_supporting_metrics_are_registry_metrics(self):
        from core.metrics import metrics
        for i in IntelligenceEngine().analyse(_ctx()):
            for m in i.supporting_metrics:
                self.assertIn(m, metrics.registry, f"{i.code}:{m}")

    def test_config_thresholds_change_detection(self):
        # with a very high income-concentration threshold, that insight vanishes
        loose = IntelligenceConfig(income_concentration_pct=100.0)
        codes = {i.code for i in IntelligenceEngine(loose).analyse(_ctx())}
        self.assertNotIn("income_concentration", codes)


class RecommendationTests(_Data):
    def test_recommendations_from_insights(self):
        insights = IntelligenceEngine().analyse(_ctx())
        recs = recommendations_from_insights(insights)
        self.assertTrue(recs)
        # each rec traces to an insight fingerprint
        for r in recs:
            self.assertTrue(r.insight_fingerprint)

    def test_recommendations_prioritised(self):
        recs = recommendations_from_insights(IntelligenceEngine().analyse(_ctx()))
        pr = [r.priority for r in recs]
        self.assertEqual(pr, sorted(pr, reverse=True))


class HealthScoreTests(_Data):
    def test_score_in_range(self):
        hs = compute_health_score(_ctx())
        self.assertGreaterEqual(hs.overall, 0)
        self.assertLessEqual(hs.overall, 100)
        self.assertIn(hs.band, ("Strong", "Sound", "Watch", "At risk"))

    def test_indicators_transparent(self):
        hs = compute_health_score(_ctx())
        self.assertTrue(hs.indicators)
        for ind in hs.indicators:
            self.assertTrue(ind.detail)
            self.assertGreaterEqual(ind.score, 0)
            self.assertLessEqual(ind.score, 100)
            self.assertTrue(ind.weight > 0)

    def test_weighted_average(self):
        hs = compute_health_score(_ctx())
        tw = sum(i.weight for i in hs.indicators)
        expected = sum(i.score * i.weight for i in hs.indicators) / tw
        self.assertAlmostEqual(hs.overall, expected, places=4)


class TrendForecastTests(_Data):
    def test_trend_builds_series(self):
        t = trends.trend("total_income", end_date=dt.date(2026, 7, 1), months=6)
        self.assertEqual(len(t.points), 6)
        self.assertIn(t.direction, ("rising", "falling", "flat"))

    def test_forecast_is_labelled_projection(self):
        f = trends.forecast("total_income", end_date=dt.date(2026, 7, 1),
                            history_months=6, horizon_months=3)
        self.assertTrue(f.is_projection)
        # projection points appended beyond history
        self.assertTrue(any("proj" in p.label for p in f.points))

    def test_year_on_year(self):
        yoy = trends.year_on_year("total_income", end_date=dt.date(2026, 5, 15))
        self.assertIn("change_pct", yoy)
        self.assertIn(yoy["direction"], ("up", "down", "flat"))

    def test_forecast_deterministic(self):
        a = trends.forecast("total_income", end_date=dt.date(2026, 7, 1)).as_dict()
        b = trends.forecast("total_income", end_date=dt.date(2026, 7, 1)).as_dict()
        self.assertEqual(a, b)


class KnowledgeServiceTests(_Data):
    def test_knowledge_for_concept(self):
        k = knowledge.knowledge_for("income", _ctx())
        self.assertEqual(k["concept"], "income")
        self.assertIn("total_income", k["metrics"])
        self.assertIn("total_income", k["dependency_graph"])
        self.assertIsNotNone(k["narrative"])

    def test_unknown_concept_raises(self):
        with self.assertRaises(KeyError):
            knowledge.knowledge_for("nonsense", _ctx())

    def test_full_briefing(self):
        b = knowledge.full_briefing(_ctx())
        for key in ("health_score", "insights", "recommendations", "concepts",
                    "provenance", "disclaimer"):
            self.assertIn(key, b)

    def test_all_concepts_resolve(self):
        for c in knowledge.concepts():
            k = knowledge.knowledge_for(c, _ctx())
            self.assertEqual(k["concept"], c)


class WorkspaceAndApiTests(_Data):
    def test_workspace_renders(self):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("treasurer_workspace")
                            + "?start=2026-01-01&end=2026-12-31")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "health score")

    def test_analytics_apis(self):
        self.client.force_login(self.tr)
        qs = "?start=2026-01-01&end=2026-12-31"
        for name, extra in (("api_analytics_insights", ""),
                            ("api_analytics_health", ""),
                            ("api_analytics_trend", "&metric=total_income"),
                            ("api_analytics_knowledge", "&concept=income")):
            r = self.client.get(reverse(name) + qs + extra)
            self.assertEqual(r.status_code, 200, name)
            self.assertEqual(r["Content-Type"], "application/json")

    def test_knowledge_api_unknown_concept_404(self):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("api_analytics_knowledge")
                            + "?concept=nope&start=2026-01-01&end=2026-12-31")
        self.assertEqual(r.status_code, 404)


class InsightStatusTests(_Data):
    def test_dismiss_records_history(self):
        from core.models import InsightStatus, InsightStatusHistory
        self.client.force_login(self.tr)
        insights = IntelligenceEngine().analyse(_ctx())
        fp = insights[0].fingerprint
        r = self.client.post(reverse("insight_status"), {
            "fingerprint": fp, "state": "dismissed",
            "code": insights[0].code, "subject": insights[0].subject})
        self.assertEqual(r.status_code, 302)
        st = InsightStatus.objects.get(fingerprint=fp)
        self.assertEqual(st.state, "dismissed")
        self.assertEqual(st.history.count(), 1)

    def test_dismissed_insight_filtered_from_workspace(self):
        from core.intelligence_views import _apply_statuses
        from core.models import InsightStatus
        insights = IntelligenceEngine().analyse(_ctx())
        fp = insights[0].fingerprint
        InsightStatus.objects.create(fingerprint=fp, code=insights[0].code,
                                     state="dismissed")
        live = _apply_statuses(IntelligenceEngine().analyse(_ctx()))
        self.assertNotIn(fp, [i.fingerprint for i in live])


class AccountingCorrectnessTests(_Data):
    def test_insight_values_match_metrics(self):
        # the operating-deficit insight's figures equal the registry metrics
        Expense.objects.create(
            date=dt.date(2026, 6, 1), department=self.building,
            description="Big", amount=Decimal("200000"),
            status=Expense.Status.PAID,
            expenditure_type=Expense.ExpenditureType.RECURRENT,
            recorded_by=self.tr)
        ctx = _ctx()
        insights = IntelligenceEngine().analyse(ctx)
        deficit = next((i for i in insights if i.code == "operating_deficit"), None)
        self.assertIsNotNone(deficit)
        expected = ctx.total_income() - ctx.operating_expense()
        self.assertEqual(deficit.value, expected)

    def test_no_insight_without_registry_metric(self):
        from core.metrics import metrics
        for i in IntelligenceEngine().analyse(_ctx()):
            for m in i.explanation.metrics:
                self.assertIn(m, metrics.registry)


class ModuleRegistryTests(TestCase):
    def test_modules_registered(self):
        self.assertGreaterEqual(len(intelligence_registry.all()), 12)

    def test_module_keys_unique(self):
        keys = intelligence_registry.keys()
        self.assertEqual(len(keys), len(set(keys)))
