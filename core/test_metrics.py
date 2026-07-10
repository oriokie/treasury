"""Financial Metrics Registry tests.

The registry is a facade over the existing canonical services, so the tests
that matter most are EQUALITY tests: each consolidated metric must return
exactly what the legacy idiom it replaced returned, on the same data. If any
of these ever diverge, a consolidation has silently changed behaviour — which
this phase explicitly forbids.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.db.models import Sum
from django.test import TestCase
from django.urls import reverse

from core.metrics import income_credit_filter, income_credits, metrics
from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from ledger.services import posting
from reports.services import balances


class RegistryResolutionTests(TestCase):
    def test_every_metric_resolves_and_has_metadata(self):
        self.assertGreaterEqual(len(metrics.registry), 15)
        for m, impl in metrics.all():
            self.assertTrue(callable(impl), m.key)
            self.assertTrue(m.definition, m.key)
            self.assertTrue(m.authoritative, m.key)

    def test_unknown_metric_raises(self):
        with self.assertRaises(AttributeError):
            metrics.does_not_exist  # noqa: B018

    def test_by_category_groups(self):
        cats = metrics.by_category()
        self.assertIn("Income", cats)
        self.assertIn("Trust", cats)


class ConsolidationEqualityTests(TestCase):
    """Each consolidated metric == the legacy idiom it replaced."""

    def setUp(self):
        posting.ensure_chart()
        self.tithe = Department.objects.create(name="Tithe", fund_type="TRUST")
        self.dev = Department.objects.create(name="Development", fund_type="LOCAL")
        for amt, dept in [("1000", self.tithe), ("500", self.tithe),
                          ("2000", self.dev)]:
            Transaction.objects.create(
                date=dt.date(2026, 3, 1), channel="BANK", direction="CREDIT",
                amount=Decimal(amt), department=dept, allocation_status="AUTO",
                confirmed=True)
        # a reversed row and an excluded (loan) row that must NOT be counted
        Transaction.objects.create(
            date=dt.date(2026, 3, 1), channel="BANK", direction="CREDIT",
            amount=Decimal("9999"), department=self.dev, allocation_status="AUTO",
            confirmed=True, is_reversed=True)
        Transaction.objects.create(
            date=dt.date(2026, 3, 1), channel="BANK", direction="CREDIT",
            amount=Decimal("8888"), department=self.dev, allocation_status="AUTO",
            confirmed=True, excluded_from_income=True)

    def test_tithe_equals_canonical(self):
        self.assertEqual(metrics.tithe(None, None),
                         balances.tithe_total(None, None))
        self.assertEqual(metrics.tithe(None, None), Decimal("1500"))

    def test_total_income_matches_income_credit_basis(self):
        legacy = Transaction.objects.filter(
            direction="CREDIT", confirmed=True, is_reversal=False,
            is_reversed=False, excluded_from_income=False).aggregate(
            t=Sum("amount"))["t"] or Decimal(0)
        self.assertEqual(metrics.total_income(None, None), legacy)
        # reversed + excluded rows are not counted
        self.assertEqual(metrics.total_income(None, None), Decimal("3500"))

    def test_income_credit_filter_matches_assistant_and_dashboard(self):
        from core.services import assistant, dashboard
        reg = set(Transaction.objects.filter(
            income_credit_filter(None, None)).values_list("id", flat=True))
        a = set(Transaction.objects.filter(
            assistant._credit_filter(None, None)).values_list("id", flat=True))
        d = set(dashboard._credits().values_list("id", flat=True))
        self.assertEqual(reg, a)
        self.assertEqual(reg, d)

    def test_trust_to_remit_equals_manual_sum(self):
        manual = sum((r["to_remit"] for r in balances.trust_summary(None, None)),
                     Decimal(0))
        self.assertEqual(metrics.trust_to_remit(), manual)

    def test_fund_balance_equals_ledger(self):
        posting.rebuild()
        self.assertEqual(metrics.fund_balance(self.dev),
                         metrics.fund_balance_ledger(self.dev))

    def test_receipts_includes_loan_income_excludes(self):
        """The intentional distinction: receipts_by_department counts the
        excluded (loan) row as fund cash; total_income does not."""
        rcv = metrics.receipts_by_department(None, None).get(self.dev.id, Decimal(0))
        # dev has 2000 income + 8888 excluded loan cash = 10888 received
        self.assertEqual(rcv, Decimal("10888"))
        # but income excludes the loan cash
        self.assertEqual(metrics.total_income(None, None), Decimal("3500"))


class CatalogueViewTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("mc_tr", password="x")
        u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(u)

    def test_catalogue_renders_all_metrics(self):
        r = self.client.get(reverse("metrics_catalogue"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "tithe")
        self.assertContains(r, "reports.services.balances")
