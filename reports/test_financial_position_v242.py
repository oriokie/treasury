"""Tests for two fixes to the engine-based Financial Position summary
(reports.financial_statements.FinancialPositionSummarySection — used by the
Treasurer's Report board pack):

1. Payables, accruals and prepayments are now wired in (were previously
   excluded by explicit design, unlike the legacy full Statement of
   Financial Position which has always shown them) — through three newly
   registered Financial Metrics Registry entries
   (payables_outstanding/accruals_outstanding/prepayments_unexpired),
   relocated from cashbook/views.py to cashbook/services/treasury_position.py
   per the established "not view code" pattern, so both statements move
   together and can never silently diverge.
2. The lumped "Cash & bank (funds on hand)" line is replaced by a Local/
   Trust (unrestricted/restricted) split — a genuine fund-accounting
   improvement, not a value change: Total assets is unaffected.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from cashbook.models import Accrual, Payable, Prepayment
from core.metrics import metrics
from core.reporting import ReportContext
from core.roles import TREASURER
from departments.models import Department
from reports.financial_statements import FinancialPositionSummarySection


def _treasurer(username="fps_tr"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
    return u


class AccrualMetricsRegistrationTests(TestCase):
    """The three metrics exist, are self-consistent with validate_authoritative,
    and match the relocated service functions exactly (same object, not a
    reimplementation — see cashbook/services/treasury_position.py)."""

    def test_metrics_registered(self):
        for key in ("payables_outstanding", "accruals_outstanding",
                   "prepayments_unexpired"):
            self.assertIn(key, metrics.registry)

    def test_validate_authoritative_passes(self):
        metrics.validate_authoritative()

    def test_relocated_functions_are_the_same_object_views_reexports(self):
        from cashbook.services.treasury_position import (
            open_payables_total as svc_p, open_accruals_total as svc_a,
            unexpired_prepayments_total as svc_u)
        from cashbook.views import (
            open_payables_total as view_p, open_accruals_total as view_a,
            unexpired_prepayments_total as view_u)
        self.assertIs(svc_p, view_p)
        self.assertIs(svc_a, view_a)
        self.assertIs(svc_u, view_u)

    def test_metrics_return_correct_values(self):
        u = User.objects.create_superuser("amr_u", password="x")
        d = Department.objects.create(name="AMR Fund", fund_type="LOCAL")
        Payable.objects.create(date=dt.date(2026, 1, 1), department=d,
                               vendor="V", description="p", amount=Decimal("1000"),
                               recorded_by=u)
        Accrual.objects.create(date=dt.date(2026, 1, 1), department=d,
                               description="a", amount=Decimal("500"),
                               recorded_by=u)
        Prepayment.objects.create(date=dt.date(2026, 1, 1), department=d,
                                  description="pp", amount=Decimal("1200"),
                                  months=12, start_date=dt.date(2026, 1, 1),
                                  recorded_by=u)
        as_of = dt.date(2026, 6, 1)
        self.assertEqual(metrics.payables_outstanding(as_of), Decimal("1000"))
        self.assertEqual(metrics.accruals_outstanding(as_of), Decimal("500"))
        self.assertGreater(metrics.prepayments_unexpired(as_of), Decimal("0"))


class FinancialPositionSummaryTests(TestCase):
    """The engine-based summary used by the board pack."""

    def setUp(self):
        self.tr = _treasurer()
        self.client.force_login(self.tr)
        self.d = Department.objects.create(name="FPS Fund", fund_type="LOCAL")

    def test_cash_and_bank_label_removed(self):
        ctx = ReportContext.for_period(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        data = FinancialPositionSummarySection().render(ctx, {})
        labels = [r.cells["label"] for r in data.rows]
        self.assertNotIn("Cash & bank (funds on hand)", labels)

    def test_local_and_trust_cash_shown_separately(self):
        ctx = ReportContext.for_period(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        data = FinancialPositionSummarySection().render(ctx, {})
        labels = [r.cells["label"] for r in data.rows]
        self.assertIn("Local fund cash (unrestricted)", labels)
        self.assertIn("Trust fund cash (restricted)", labels)

    def test_payables_accruals_prepayments_included(self):
        Payable.objects.create(date=dt.date(2026, 3, 1), department=self.d,
                               vendor="V", description="p", amount=Decimal("2000"),
                               recorded_by=self.tr)
        Accrual.objects.create(date=dt.date(2026, 3, 1), department=self.d,
                               description="a", amount=Decimal("800"),
                               recorded_by=self.tr)
        Prepayment.objects.create(date=dt.date(2026, 1, 1), department=self.d,
                                  description="pp", amount=Decimal("1200"),
                                  months=12, start_date=dt.date(2026, 1, 1),
                                  recorded_by=self.tr)
        ctx = ReportContext.for_period(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        data = FinancialPositionSummarySection().render(ctx, {})
        by_label = {r.cells["label"]: r.cells["value"] for r in data.rows}
        self.assertIn("Accounts payable", by_label)
        self.assertIn("Accrued expenses", by_label)
        self.assertIn("Prepayments (unexpired)", by_label)
        self.assertGreaterEqual(by_label["Accounts payable"], Decimal("2000"))
        self.assertGreaterEqual(by_label["Accrued expenses"], Decimal("800"))

    def test_balance_sheet_identity_holds(self):
        Payable.objects.create(date=dt.date(2026, 3, 1), department=self.d,
                               vendor="V", description="p", amount=Decimal("2000"),
                               recorded_by=self.tr)
        Accrual.objects.create(date=dt.date(2026, 3, 1), department=self.d,
                               description="a", amount=Decimal("800"),
                               recorded_by=self.tr)
        Prepayment.objects.create(date=dt.date(2026, 1, 1), department=self.d,
                                  description="pp", amount=Decimal("1200"),
                                  months=12, start_date=dt.date(2026, 1, 1),
                                  recorded_by=self.tr)
        ctx = ReportContext.for_period(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        data = FinancialPositionSummarySection().render(ctx, {})
        by_label = {r.cells["label"]: r.cells["value"] for r in data.rows}
        self.assertEqual(by_label["Net assets"],
                         by_label["Total assets"] - by_label["Total liabilities"])

    def test_board_pack_renders_all_new_lines(self):
        r = self.client.get(reverse("engine_report", args=["treasurer_report"])
                            + "?start=2026-01-01&end=2026-12-31")
        html = r.content.decode()
        self.assertNotIn("Cash &amp; bank (funds on hand)", html)
        self.assertIn("Local fund cash", html)
        self.assertIn("Trust fund cash", html)
        self.assertIn("Accounts payable", html)
        self.assertIn("Accrued expenses", html)
        self.assertIn("Prepayments (unexpired)", html)

    def test_legacy_full_statement_unaffected(self):
        # the full SOFP already worked correctly and must keep working
        # unchanged — this fix only touched the engine-based summary
        r = self.client.get(f"/reports/financial-position/?as_of=2026-06-01")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("Cash &amp; bank (funds on hand)", html)
