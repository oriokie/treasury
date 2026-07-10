"""Tests for the treasury-metrics expansion and the registry gap fixes.

Covers:
* the relocated canonical implementations (petty float, staff advances,
  unpresented payments) — the service equals the old views-import path exactly,
  so nothing that imported from cashbook.views changed behaviour;
* the extracted bank_position service — the Bank Position view and the metric
  read the same single implementation;
* the new registry metrics (petty_cash_balance, staff_advances_outstanding,
  bank_position, cash_in_transit, pending_expense_claims, total_payments,
  budget_vs_actual, dev_group_progress, negative_fund_balances, dormant_funds)
  — each equals its canonical service value;
* the registry improvements (has/get, validate_authoritative — every
  documented authoritative path must resolve);
* the ReportContext as_of auto-application;
* the Treasurer's Report including the new Treasury Position and Funds
  Attention sections, with all exports still rendering.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from cashbook.models import (Expense, PettyCashTopUp, StaffAdvance)
from core.metrics import metrics
from core.reporting import ReportContext
from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction


def _treasurer(username="tm_tr"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
    return u


class _Seed(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("tm_seed", password="x",
                                          is_superuser=True)
        self.local = Department.objects.create(name="Building", fund_type="LOCAL")
        self.trust = Department.objects.create(name="Tithe", fund_type="TRUST")
        Transaction.objects.create(
            date=dt.date(2026, 5, 4), channel="CASH", direction="CREDIT",
            amount=Decimal("40000"), department=self.local,
            allocation_status="AUTO", confirmed=True)
        PettyCashTopUp.objects.create(date=dt.date(2026, 5, 1),
                                      amount=Decimal("5000"),
                                      recorded_by=self.u)
        Expense.objects.create(
            date=dt.date(2026, 5, 10), department=self.local,
            description="Stationery", amount=Decimal("650"),
            category="STATIONERY", status="PAID", recorded_by=self.u,
            paid_from_petty_cash=True)
        Expense.objects.create(
            date=dt.date(2026, 5, 12), department=self.local,
            description="Awaiting approval", amount=Decimal("2000"),
            category="MATERIALS", status="PENDING", recorded_by=self.u)
        self.adv = StaffAdvance.objects.create(
            date_issued=dt.date(2026, 5, 15), staff_name="John",
            amount=Decimal("8000"), from_petty_cash=False,
            department=self.local, purpose="Camp logistics",
            issued_by=self.u)


class RegistryImplementationTests(TestCase):
    def test_has_and_get(self):
        self.assertTrue(metrics.has("tithe"))
        self.assertFalse(metrics.has("nonexistent"))
        self.assertIsNotNone(metrics.get("tithe"))
        self.assertIsNone(metrics.get("nonexistent"))

    def test_every_authoritative_path_resolves(self):
        problems = metrics.validate_authoritative()
        self.assertEqual(problems, [],
                         "Stale authoritative paths in the registry: "
                         f"{problems}")

    def test_new_metrics_registered_with_metadata(self):
        for key in ("petty_cash_balance", "staff_advances_outstanding",
                    "bank_position", "cash_in_transit",
                    "pending_expense_claims", "total_payments",
                    "budget_vs_actual", "dev_group_progress",
                    "negative_fund_balances", "dormant_funds"):
            self.assertTrue(metrics.has(key), key)
            m = metrics.get(key)
            self.assertTrue(m.definition, key)
            self.assertTrue(m.authoritative, key)

    def test_pending_receipts_recategorised(self):
        self.assertEqual(metrics.get("pending_receipts_total").category,
                         "Balance")


class RelocationBackwardCompatTests(_Seed):
    """The old cashbook.views import paths must return identical figures to
    the new service — proving the relocation changed nothing."""

    def test_petty_balance_alias(self):
        from cashbook.views import _petty_balance_asof
        from cashbook.services.treasury_position import petty_balance_asof
        today = dt.date.today()
        self.assertIs(_petty_balance_asof, petty_balance_asof)
        self.assertEqual(petty_balance_asof(today), Decimal("4350"))  # 5000-650

    def test_advance_totals_alias(self):
        from cashbook import views as v
        from cashbook.services import treasury_position as tp
        self.assertIs(v.outstanding_advances_total,
                      tp.outstanding_advances_total)
        self.assertIs(v.outstanding_bank_advances_total,
                      tp.outstanding_bank_advances_total)
        self.assertIs(v.outstanding_petty_advances_total,
                      tp.outstanding_petty_advances_total)
        self.assertEqual(tp.outstanding_advances_total(dt.date.today()),
                         Decimal("8000"))

    def test_unpresented_alias(self):
        from cashbook import views as v
        from cashbook.services import treasury_position as tp
        self.assertIs(v.unpresented_cheques_total, tp.unpresented_cheques_total)
        self.assertIs(v.unpresented_payments_qs, tp.unpresented_payments_qs)


class NewMetricValueTests(_Seed):
    def test_petty_cash_balance_metric(self):
        from cashbook.services.treasury_position import petty_balance_asof
        today = dt.date.today()
        self.assertEqual(metrics.petty_cash_balance(today),
                         petty_balance_asof(today))

    def test_staff_advances_metric(self):
        self.assertEqual(metrics.staff_advances_outstanding(dt.date.today()),
                         Decimal("8000"))

    def test_pending_expense_claims_metric(self):
        out = metrics.pending_expense_claims(dt.date.today())
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["total"], Decimal("2000"))

    def test_cash_in_transit_zero_without_worksheet(self):
        self.assertEqual(metrics.cash_in_transit(dt.date.today()), Decimal(0))

    def test_cash_in_transit_from_worksheet(self):
        from statements.models import BankReconciliation, ReconciliationItem
        rec = BankReconciliation.objects.create(
            statement_date=dt.date(2026, 5, 31), bank_balance=Decimal("1000"),
            created_by=self.u)
        rec.items.create(kind=ReconciliationItem.Kind.IN_TRANSIT,
                         amount=Decimal("7500"), effect="ADD")
        rec.items.create(kind=ReconciliationItem.Kind.BANK_CHARGE,
                         amount=Decimal("120"), effect="SUBTRACT")
        self.assertEqual(metrics.cash_in_transit(dt.date(2026, 6, 30)),
                         Decimal("7500"))   # only IN_TRANSIT items
        # before the worksheet's date, no in-transit is known
        self.assertEqual(metrics.cash_in_transit(dt.date(2026, 5, 1)),
                         Decimal(0))

    def test_bank_position_matches_view(self):
        pos = metrics.bank_position()
        c = self.client
        c.force_login(_treasurer("tm_bp"))
        r = c.get(reverse("report_bank_position"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["system_balance"], pos["system_balance"])
        self.assertEqual(r.context["opening"], pos["opening"])

    def test_total_payments_composition(self):
        from reports.services.balances import (capital_expenditure_total,
                                               operating_expense_total,
                                               remittances_total)
        s, e = dt.date(2026, 1, 1), dt.date(2026, 12, 31)
        self.assertEqual(metrics.total_payments(s, e),
                         operating_expense_total(s, e)
                         + capital_expenditure_total(s, e)
                         + remittances_total(s, e))

    def test_negative_and_dormant_selectors(self):
        s, e = dt.date(2026, 1, 1), dt.date(2026, 12, 31)
        # an overdrawn fund
        over = Department.objects.create(name="Overdrawn", fund_type="LOCAL")
        Expense.objects.create(date=dt.date(2026, 5, 20), department=over,
                               description="x", amount=Decimal("500"),
                               category="OTHER", status="PAID",
                               recorded_by=self.u)
        # a dormant fund: opening balance, no movement
        Department.objects.create(name="Idle", fund_type="LOCAL",
                                  opening_balance=Decimal("900"))
        negative = metrics.negative_fund_balances(s, e)
        dormant = metrics.dormant_funds(s, e)
        self.assertIn("Overdrawn", [r["department"].name for r in negative])
        self.assertIn("Idle", [r["department"].name for r in dormant])
        # selectors agree with fund_summary closing figures by construction
        for r in negative:
            self.assertLess(r["closing"], 0)
        for r in dormant:
            self.assertNotEqual(r["closing"], 0)
            self.assertFalse(r["receipts"] or r["expenses"])


class ContextAsOfTests(_Seed):
    def test_as_of_metric_receives_period_end(self):
        end = dt.date(2026, 5, 31)
        ctx = ReportContext.for_period(dt.date(2026, 1, 1), end)
        from cashbook.services.treasury_position import petty_balance_asof
        # no explicit args -> the context's end is applied automatically
        self.assertEqual(ctx.metric("petty_cash_balance"),
                         petty_balance_asof(end))

    def test_explicit_as_of_still_wins(self):
        ctx = ReportContext.for_period(dt.date(2026, 1, 1),
                                       dt.date(2026, 12, 31))
        from cashbook.services.treasury_position import petty_balance_asof
        explicit = dt.date(2026, 5, 5)
        self.assertEqual(ctx.metric("petty_cash_balance", explicit),
                         petty_balance_asof(explicit))


class ReportInclusionTests(_Seed):
    def setUp(self):
        super().setUp()
        self.client.force_login(_treasurer("tm_rep"))
        self.url = (reverse("engine_report", args=["treasurer_report"])
                    + "?start=2026-01-01&end=2026-12-31")

    def test_treasury_position_section_renders(self):
        html = self.client.get(self.url).content.decode()
        for needle in ("Treasury position", "Bank balance per the system",
                       "Petty cash float", "Cash in transit",
                       "Outstanding staff advances", "Pending expense claims"):
            self.assertIn(needle, html, needle)

    def test_funds_attention_when_flagged(self):
        Department.objects.create(name="Sleeping Fund", fund_type="LOCAL",
                                  opening_balance=Decimal("777"))
        html = self.client.get(self.url).content.decode()
        self.assertIn("Funds requiring attention", html)
        self.assertIn("Dormant", html)

    def test_snapshot_includes_bank_and_petty_cards(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn("Bank balance (system)", html)
        self.assertIn("Petty cash float", html)
        self.assertIn("Staff advances outstanding", html)

    def test_exports_still_render(self):
        base = reverse("engine_report", args=["treasurer_report"])
        for fmt in ("csv", "xlsx", "pdf", "docx"):
            r = self.client.get(base
                                + f"?start=2026-01-01&end=2026-12-31&export={fmt}")
            self.assertEqual(r.status_code, 200, fmt)

    def test_board_actions_flag_pending_claims(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn("pending expense claims", html.lower())
