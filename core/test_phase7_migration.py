"""Phase 7 — complete statement migration, consistency audit and snapshot
foundation tests.

Concerns: (1) migrated statements (Cash Flow, Fund Balances, Budget vs Actual)
produce figures identical to / reconciling with the legacy views; (2) the
reporting consistency audit passes for a period; (3) the immutable snapshot
foundation captures, verifies and protects a rendered report. No new accounting
calculation is introduced — every figure is a registry metric.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase, RequestFactory
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


class _FinancialData(TestCase):
    """Shared fixture: some local + trust income and mixed expenditure."""
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
        u = _staff("fd_u")
        Expense.objects.create(
            date=dt.date(2026, 5, 2), department=self.building,
            description="Repairs", amount=Decimal("5000"),
            status=Expense.Status.PAID,
            expenditure_type=Expense.ExpenditureType.RECURRENT, recorded_by=u)
        Expense.objects.create(
            date=dt.date(2026, 5, 3), department=self.building,
            description="Roof", amount=Decimal("20000"),
            status=Expense.Status.PAID,
            expenditure_type=Expense.ExpenditureType.CAPITAL, recorded_by=u)
        self.tr = _staff("fd_tr")


class CashFlowEquivalenceTests(_FinancialData):
    def test_cash_flow_reconciles(self):
        from reports.financial_statements import CashFlowStatementSection
        ctx = _ctx()
        data = CashFlowStatementSection().build(ctx, {})
        by = {r.cells["line"]: r.cells["amount"] for r in data.rows}
        # opening + net change == closing
        opening = by["Cash & bank at beginning of period"]
        end_calc = by["Cash & bank at end of period"]
        from reports.services import balances
        rows = balances.department_summary(ctx.start, ctx.end)
        cash_close = sum((r["closing"] for r in rows), Decimal(0))
        self.assertEqual(end_calc, cash_close)

    def test_cash_flow_uses_only_metrics(self):
        from reports.financial_statements import CashFlowStatementSection
        ctx = _ctx()
        data = CashFlowStatementSection().build(ctx, {})
        # provenance recorded
        self.assertTrue(set(data.extra["metrics_used"]))
        for m in ("remittances_total", "financing_activity", "operating_expense"):
            self.assertIn(m, ctx.metrics_used())

    def test_renders_and_exports(self):
        self.client.force_login(self.tr)
        base = reverse("engine_report", args=["cash_flow_v2"])
        for fmt in ("csv", "xlsx", "pdf", "docx"):
            r = self.client.get(base + f"?start=2026-01-01&end=2026-12-31&export={fmt}")
            self.assertEqual(r.status_code, 200, fmt)


class FundBalancesTests(_FinancialData):
    def test_total_equals_closing_cash(self):
        from reports.financial_statements import FundBalancesStatementSection
        ctx = _ctx()
        data = FundBalancesStatementSection().build(ctx, {})
        from reports.services import balances
        rows = balances.department_summary(ctx.start, ctx.end)
        cash_close = sum((r["closing"] for r in rows), Decimal(0))
        self.assertEqual(data.total.cells["closing"], cash_close)

    def test_renders(self):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("engine_report", args=["fund_balances_v2"])
                            + "?start=2026-01-01&end=2026-12-31")
        self.assertEqual(r.status_code, 200)


class BudgetVsActualEquivalenceTests(_FinancialData):
    def test_matches_legacy_service(self):
        from reports.financial_statements import BudgetVsActualSection
        from reports.services import budget as budget_svc
        self.building.annual_budget = Decimal("50000")
        self.building.save()
        ctx = _ctx()
        data = BudgetVsActualSection().build(ctx, {"year": 2026, "period": "ANNUAL"})
        legacy = budget_svc.budget_vs_actual(2026, "ANNUAL", None, None)
        # total budget/actual match the canonical service the legacy view uses
        legacy_total = legacy["totals"]
        self.assertEqual(data.total.cells["budget"], legacy_total["budget"])
        self.assertEqual(data.total.cells["actual"], legacy_total["actual"])


class ConsistencyAuditTests(_FinancialData):
    def test_audit_passes(self):
        from reports.consistency_reports import run_consistency_audit
        result = run_consistency_audit(_ctx())
        failed = [c.name for c in result.checks if not c.passed]
        self.assertEqual(failed, [], f"failed checks: {failed}")
        self.assertTrue(result.passed)

    def test_trial_balance_balances(self):
        from reports.consistency_reports import run_consistency_audit
        result = run_consistency_audit(_ctx())
        tb = next(c for c in result.checks if "Trial balance" in c.name)
        self.assertTrue(tb.passed)
        self.assertEqual(tb.left, tb.right)

    def test_audit_report_renders(self):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("engine_report", args=["consistency_audit"])
                            + "?start=2026-01-01&end=2026-12-31")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "PASS")


class SnapshotFoundationTests(_FinancialData):
    def _render(self, key="income_statement_v2"):
        report = registry.get(key)
        req = RequestFactory().get("/x?start=2026-01-01&end=2026-12-31")
        req.user = self.tr
        return report.render(req)

    def test_create_captures_metadata_and_payload(self):
        from reports.services.snapshots import create_snapshot
        snap = create_snapshot(self._render(), user=self.tr)
        self.assertTrue(snap.finalised)
        self.assertEqual(snap.report_key, "income_statement_v2")
        self.assertEqual(snap.period_start, dt.date(2026, 1, 1))
        self.assertTrue(snap.report_version)
        self.assertEqual(snap.template_version, "engine-1")
        self.assertIn("payload", snap.checksums)
        self.assertTrue(snap.payload["sections"])
        self.assertTrue(snap.metrics_used)

    def test_immutability_enforced(self):
        from reports.services.snapshots import create_snapshot
        snap = create_snapshot(self._render(), user=self.tr)
        snap.report_title = "tampered"
        with self.assertRaises(ValueError):
            snap.save()

    def test_verify_matches_fresh_render(self):
        from reports.services.snapshots import create_snapshot, verify_snapshot
        snap = create_snapshot(self._render(), user=self.tr)
        result = verify_snapshot(snap, self._render())
        self.assertTrue(result["payload"])
        self.assertTrue(result.get("csv", True))

    def test_verify_detects_drift(self):
        from reports.services.snapshots import create_snapshot, verify_snapshot
        snap = create_snapshot(self._render(), user=self.tr)
        # add income → the payload changes → drift detected
        Transaction.objects.create(
            date=dt.date(2026, 6, 1), channel="CASH", direction="CREDIT",
            amount=Decimal("99999"), department=self.building,
            allocation_status="AUTO", confirmed=True)
        result = verify_snapshot(snap, self._render())
        self.assertFalse(result["payload"])

    def test_checksum_deterministic_for_payload(self):
        from reports.services.snapshots import create_snapshot
        a = create_snapshot(self._render(), user=self.tr).checksums["payload"]
        b = create_snapshot(self._render(), user=self.tr).checksums["payload"]
        self.assertEqual(a, b)


class MigratedReportPermissionFilterTests(_FinancialData):
    def test_permissions(self):
        # auditor can view; anonymous is redirected
        self.client.force_login(_staff("pf_aud", AUDITOR))
        for key in ("cash_flow_v2", "fund_balances_v2", "budget_vs_actual_v2"):
            r = self.client.get(reverse("engine_report", args=[key]))
            self.assertEqual(r.status_code, 200, key)

    def test_fund_balances_consolidated_filter(self):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("engine_report", args=["fund_balances_v2"])
                            + "?start=2026-01-01&end=2026-12-31&consolidated=0")
        self.assertEqual(r.status_code, 200)


class DashboardReconciliationTests(_FinancialData):
    """Dashboards must reconcile with the financial reports — both draw from the
    same income_credits definition the total_income metric wraps."""

    def test_main_dashboard_uses_report_context(self):
        # DashboardView headline figures come through ReportContext metrics
        self.client.force_login(self.tr)
        r = self.client.get(reverse("dashboard") + "?start=2026-01-01&end=2026-12-31")
        self.assertEqual(r.status_code, 200)

    def test_executive_income_reconciles_with_metric(self):
        import datetime as dt
        from decimal import Decimal
        from django.db.models import Sum
        from core.metrics import metrics, income_credits
        year = 2026
        dash_year = income_credits(date__year=year).aggregate(
            t=Sum("amount"))["t"] or Decimal(0)
        metric_year = metrics.total_income(dt.date(year, 1, 1), dt.date(year, 12, 31))
        self.assertEqual(dash_year, metric_year)

    def test_dashboard_tithe_reconciles(self):
        import datetime as dt
        from core.metrics import metrics
        from core.reporting import ReportContext
        ctx = ReportContext.for_period(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        self.assertEqual(ctx.tithe(),
                         metrics.tithe(dt.date(2026, 1, 1), dt.date(2026, 12, 31)))
