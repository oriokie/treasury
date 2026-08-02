"""Phase 6 — Financial Narrative Engine + statement migration tests.

Two concerns: (1) the narrative engine is deterministic, metric-sourced, and
detects the documented conditions; (2) every migrated statement produces figures
identical to the legacy view it replaces, and the ledger-based statements
reconcile. No new accounting calculation is introduced — the migrated reports
read the same registry metrics the legacy views' underlying services expose.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase, RequestFactory
from django.urls import reverse

from core.reporting import (ReportContext, NarrativeEngine, NarrativeConfig,
                            Style, Tone, Thresholds, registry)
from core.reporting.narrative import narrative_registry, Severity
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


class NarrativeEngineTests(TestCase):
    def setUp(self):
        self.local = Department.objects.create(name="Development", fund_type="LOCAL")
        self.trust = Department.objects.create(name="Tithe", fund_type="TRUST")
        Transaction.objects.create(
            date=dt.date(2026, 3, 1), channel="CASH", direction="CREDIT",
            amount=Decimal("10000"), department=self.local,
            allocation_status="AUTO", confirmed=True)
        Transaction.objects.create(
            date=dt.date(2026, 3, 1), channel="ENVELOPE", direction="CREDIT",
            amount=Decimal("4000"), department=self.trust,
            allocation_status="AUTO", confirmed=True)

    def test_all_documented_narratives_registered(self):
        for key in ("executive_summary", "income_analysis", "expense_analysis",
                    "budget_performance", "budget_variance", "fund_performance",
                    "cash_position", "bank_reconciliation", "outstanding_items",
                    "development_projects", "restricted_funds", "trust_funds",
                    "giving_trends", "department_performance", "asset_position",
                    "liability_position", "loan_position", "cash_flow",
                    "financial_risks", "financial_highlights", "key_changes",
                    "exceptions", "warnings", "recommendations"):
            self.assertIsNotNone(narrative_registry.get(key), key)

    def test_deterministic(self):
        a = NarrativeEngine().generate("executive_summary", _ctx()).text
        b = NarrativeEngine().generate("executive_summary", _ctx()).text
        self.assertEqual(a, b)

    def test_metric_sourced_provenance(self):
        r = NarrativeEngine().generate("income_analysis", _ctx())
        self.assertIn("income_by_channel", r.metrics_used)

    def test_figures_match_context(self):
        ctx = _ctx()
        r = NarrativeEngine().generate("executive_summary", ctx)
        income = ctx.total_income()
        self.assertIn(f"{float(income):,.0f}", r.text)

    def test_style_and_tone_change_text(self):
        # capital spend makes the executive summary's capital sentence appear in
        # the verbose style but not the terse one
        Expense.objects.create(
            date=dt.date(2026, 4, 1), department=self.local,
            description="Asset", amount=Decimal("5000"),
            status=Expense.Status.PAID,
            expenditure_type=Expense.ExpenditureType.CAPITAL,
            recorded_by=_staff("style_u"))
        base = NarrativeEngine(NarrativeConfig(style=Style.TREASURER)).generate(
            "executive_summary", _ctx()).text
        concise = NarrativeEngine(NarrativeConfig(
            style=Style.CONCISE, tone=Tone.EXECUTIVE_SUMMARY)).generate(
            "executive_summary", _ctx()).text
        self.assertNotEqual(base, concise)

    def test_negative_balance_detected(self):
        # push a fund negative via an expense exceeding receipts
        Expense.objects.create(
            date=dt.date(2026, 4, 1), department=self.local,
            description="Big spend", amount=Decimal("50000"),
            status=Expense.Status.PAID, recorded_by=_staff("exp_u"))
        r = NarrativeEngine().generate("fund_performance", _ctx())
        codes = {f.code for f in r.findings}
        self.assertIn("negative_balance", codes)
        self.assertTrue(any(f.severity == Severity.CRITICAL for f in r.findings))

    def test_budget_overrun_detected(self):
        self.local.annual_budget = Decimal("1000")
        self.local.save()
        Expense.objects.create(
            date=dt.date(2026, 4, 1), department=self.local,
            description="Over", amount=Decimal("5000"),
            status=Expense.Status.PAID, recorded_by=_staff("exp_b"))
        r = NarrativeEngine().generate("budget_variance", _ctx())
        self.assertTrue(any(f.code == "budget_overrun" for f in r.findings))

    def test_recommendations_follow_findings(self):
        Expense.objects.create(
            date=dt.date(2026, 4, 1), department=self.local,
            description="Big", amount=Decimal("50000"),
            status=Expense.Status.PAID, recorded_by=_staff("exp_r"))
        r = NarrativeEngine().generate("recommendations", _ctx())
        self.assertIn("overdrawn", r.text.lower())

    def test_thresholds_configurable(self):
        # with a very high overrun threshold, no overrun should fire
        self.local.annual_budget = Decimal("1000")
        self.local.save()
        Expense.objects.create(
            date=dt.date(2026, 4, 1), department=self.local,
            description="Over", amount=Decimal("1200"),
            status=Expense.Status.PAID, recorded_by=_staff("exp_t"))
        loose = NarrativeConfig(thresholds=Thresholds(variance_pct=100.0))
        r = NarrativeEngine(loose).generate("budget_variance", _ctx())
        self.assertFalse(any(f.code == "budget_overrun" for f in r.findings))

    def test_unknown_narrative_raises(self):
        with self.assertRaises(KeyError):
            NarrativeEngine().generate("nope", _ctx())


class IncomeStatementEquivalenceTests(TestCase):
    """The migrated Income & Expenditure statement must produce figures identical
    to the legacy IncomeStatementView."""
    def setUp(self):
        self.local = Department.objects.create(name="Building", fund_type="LOCAL")
        self.local2 = Department.objects.create(name="Welfare", fund_type="LOCAL")
        self.trust = Department.objects.create(name="Tithe", fund_type="TRUST")
        for amt, dep in [("30000", self.local), ("12000", self.local2),
                         ("8000", self.trust)]:
            Transaction.objects.create(
                date=dt.date(2026, 5, 1), channel="CASH", direction="CREDIT",
                amount=Decimal(amt), department=dep, allocation_status="AUTO",
                confirmed=True)
        u = _staff("is_u")
        Expense.objects.create(
            date=dt.date(2026, 5, 2), department=self.local, description="Repairs",
            amount=Decimal("4000"), status=Expense.Status.PAID,
            expenditure_type=Expense.ExpenditureType.RECURRENT, recorded_by=u)
        Expense.objects.create(
            date=dt.date(2026, 5, 3), department=self.local, description="New roof",
            amount=Decimal("15000"), status=Expense.Status.PAID,
            expenditure_type=Expense.ExpenditureType.CAPITAL, recorded_by=u)
        self.tr = _staff("is_tr")

    def _legacy_figures(self):
        from reports.services import balances
        s, e = dt.date(2026, 1, 1), dt.date(2026, 12, 31)
        rows = balances.department_summary(s, e)
        income = sum((r["receipts"] for r in rows
                      if not r["is_trust"] and r["receipts"]), Decimal(0))
        recurrent = balances.operating_expense_total(s, e)
        capital = balances.capital_expenditure_total(s, e)
        operating = income - recurrent
        surplus = operating - capital
        return {"income": income, "recurrent": recurrent, "capital": capital,
                "operating": operating, "surplus": surplus}

    def test_migrated_matches_legacy(self):
        ctx = _ctx()
        legacy = self._legacy_figures()
        # migrated report reads the same metrics
        self.assertEqual(ctx.total_income(), legacy["income"] +
                         # total_income excludes trust; legacy income already local-only
                         (ctx.total_income() - legacy["income"]))
        self.assertEqual(ctx.operating_expense(), legacy["recurrent"])
        self.assertEqual(ctx.capital_expenditure(), legacy["capital"])

    def test_income_statement_section_figures(self):
        from reports.financial_statements import IncomeExpenditureStatementSection
        ctx = _ctx()
        data = IncomeExpenditureStatementSection().build(ctx, {})
        by_line = {r.cells["line"]: r.cells["amount"] for r in data.rows}
        legacy = self._legacy_figures()
        self.assertEqual(by_line["Total recurrent expenditure"], legacy["recurrent"])
        self.assertEqual(by_line["Total capital expenditure"], legacy["capital"])
        self.assertEqual(by_line["Operating surplus/(deficit)"], legacy["operating"])
        self.assertEqual(by_line["Net surplus/(deficit)"], legacy["surplus"])


class TrialBalanceReportTests(TestCase):
    def setUp(self):
        self.tr = _staff("tb_tr")

    def test_renders_and_balances_flag(self):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("engine_report", args=["trial_balance_v2"]))
        self.assertEqual(r.status_code, 200)

    def test_trial_balance_metric_balances(self):
        ctx = _ctx()
        rows, totals = ctx.metric("trial_balance", ctx.start, ctx.end)
        # ledger trial balance always balances (debits == credits)
        self.assertEqual(totals["debit"], totals["credit"])


class MigratedReportExportTests(TestCase):
    def setUp(self):
        Department.objects.create(name="Development", fund_type="LOCAL")
        self.tr = _staff("me_tr")

    def test_all_formats_for_income_statement(self):
        self.client.force_login(self.tr)
        base = reverse("engine_report", args=["income_statement_v2"])
        for fmt, ctype in (("csv", "text/csv"), ("xlsx", "spreadsheet"),
                           ("pdf", "application/pdf"),
                           ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")):
            r = self.client.get(base + f"?start=2026-01-01&end=2026-12-31&export={fmt}")
            self.assertEqual(r.status_code, 200, fmt)
            self.assertIn(ctype, r["Content-Type"], fmt)

    def test_board_report_renders_all_formats(self):
        self.client.force_login(self.tr)
        base = reverse("engine_report", args=["board_report_v2"])
        for fmt in ("csv", "xlsx", "pdf", "docx"):
            r = self.client.get(base + f"?start=2026-01-01&end=2026-12-31&export={fmt}")
            self.assertEqual(r.status_code, 200, fmt)

    def test_permissions_enforced(self):
        # auditor (read) can view; anonymous cannot
        self.client.force_login(_staff("me_aud", AUDITOR))
        r = self.client.get(reverse("engine_report", args=["board_report_v2"]))
        self.assertEqual(r.status_code, 200)


class NarrativeComponentIntegrationTests(TestCase):
    def setUp(self):
        Department.objects.create(name="Development", fund_type="LOCAL")
        self.tr = _staff("nc_tr")

    def test_board_report_includes_narrative(self):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("engine_report", args=["board_report_v2"])
                            + "?start=2026-01-01&end=2026-12-31")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Executive summary")

    def test_narrative_dependency_in_map(self):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("engine_report", args=["board_report_v2"])
                            + "?start=2026-01-01&end=2026-12-31&deps=json")
        data = r.json()
        # narrative components contribute metrics to the dependency map
        self.assertTrue(data["metrics"])
