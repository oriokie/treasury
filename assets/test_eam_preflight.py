"""EAM — pre-flight for acquisition-date temporal costing.

The check must be exactly right or it is worse than useless: if it says "safe"
and the switch then breaks the reconciliation, the books disagree in production.
So the load-bearing test here simulates the change and asserts the predicted
difference equals the real one.
"""
import calendar
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User, Group
from django.db.models import Q, Sum
from django.test import TestCase
from django.urls import reverse

from assets.models import FixedAsset, Acquisition, DepreciationRun
from assets.services import preflight, runs
from cashbook.models import Expense
from core.metrics import metrics
from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from ledger.models import JournalEntry, JournalLine
from ledger.services import posting


class PreflightBase(TestCase):
    def setUp(self):
        self.fund = Department.objects.create(name="General", fund_type=Department.FundType.LOCAL)
        self.tr = User.objects.create_user("pf_tr", password="x")
        self.tr.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.tr)
        self.year = dt.date.today().year
        # anchors the asset opening date at 1 Jan of this year
        Transaction.objects.create(date=dt.date(self.year, 1, 5), amount=Decimal("1000"),
                                   direction="CREDIT", channel="CASH", confirmed=True,
                                   allocation_status="MANUAL", department=self.fund,
                                   reference="ANCHOR")

    def _asset(self, name, cost, acquired, **kw):
        return FixedAsset.objects.create(
            name=name, category="EQUIPMENT", cost=Decimal(cost), salvage_value=Decimal(0),
            acquired_on=acquired, in_service_on=acquired, method="NONE", rate=Decimal(0),
            department=self.fund, **kw)

    def _capital_expense(self, amount, when, asset=None):
        e = Expense.objects.create(
            date=when, amount=Decimal(amount), description="Capital purchase",
            category="EQUIPMENT", expenditure_type=Expense.ExpenditureType.CAPITAL,
            department=self.fund, status=Expense.Status.PAID, recorded_by=self.tr)
        if asset:
            e.capitalized_asset = asset
            e.save()
        return e


class CoverageTests(PreflightBase):
    def test_a_register_with_nothing_new_is_ready(self):
        self._asset("Old pews", "50000", dt.date(self.year - 3, 1, 1))
        r = preflight.acquisition_coverage()
        self.assertTrue(r["ready"])
        self.assertEqual(r["totals"]["predicted_diff"], Decimal("0"))

    def test_an_asset_typed_straight_in_is_flagged(self):
        self._asset("Mystery generator", "90000", dt.date(self.year, 5, 1))
        r = preflight.acquisition_coverage()
        self.assertFalse(r["ready"])
        self.assertEqual(r["totals"]["shortfall"], Decimal("90000"))
        self.assertIn("no payment linked", r["unbacked"][0]["reason"])

    def test_a_purchase_backed_by_its_payment_is_clean(self):
        a = self._asset("Projector", "180000", dt.date(self.year, 3, 4))
        self._capital_expense("180000", dt.date(self.year, 3, 4), asset=a)
        r = preflight.acquisition_coverage()
        self.assertTrue(r["ready"], r["unbacked"])
        self.assertEqual(r["totals"]["covered"], Decimal("180000"))

    def test_a_donation_counts_as_backed(self):
        a = self._asset("Donated organ", "250000", dt.date(self.year, 3, 1))
        Acquisition.objects.create(asset=a, source=Acquisition.Source.DONATION,
                                   date=a.acquired_on, amount=a.cost, fund=self.fund)
        r = preflight.acquisition_coverage()
        self.assertTrue(r["ready"])

    def test_cost_above_the_payments_is_flagged_as_partial(self):
        a = self._asset("Borehole", "100000", dt.date(self.year, 4, 2))
        self._capital_expense("60000", dt.date(self.year, 4, 2), asset=a)
        r = preflight.acquisition_coverage()
        self.assertEqual(r["totals"]["shortfall"], Decimal("40000"))
        self.assertIn("higher than the payments", r["unbacked"][0]["reason"])

    def test_a_late_payment_on_an_opening_asset_would_double_count(self):
        a = self._asset("PA system", "420000", dt.date(self.year - 3, 6, 1))
        self._capital_expense("420000", dt.date(self.year, 2, 1), asset=a)
        r = preflight.acquisition_coverage()
        self.assertFalse(r["ready"])
        self.assertEqual(r["totals"]["double_counted"], Decimal("420000"))
        self.assertEqual(r["totals"]["predicted_diff"], Decimal("-420000"))

    def test_it_reports_as_at_the_date_asked_for(self):
        """Both sides of the reconciliation are as at a date, so the check must
        be too: an asset acquired after that date is not on the register yet and
        cannot be causing a difference. Dates are pinned so the result does not
        depend on what month the test is run in."""
        self._asset("Later generator", "90000", dt.date(self.year, 9, 1))
        early = preflight.acquisition_coverage(as_of=dt.date(self.year, 6, 30))
        self.assertTrue(early["ready"], "an asset not yet acquired must not be flagged")
        self.assertEqual(early["totals"]["shortfall"], Decimal("0"))
        later = preflight.acquisition_coverage(as_of=dt.date(self.year, 12, 31))
        self.assertEqual(later["totals"]["shortfall"], Decimal("90000"))

    def test_a_payment_after_the_date_asked_for_is_not_counted_yet(self):
        a = self._asset("Projector", "180000", dt.date(self.year, 3, 4))
        self._capital_expense("180000", dt.date(self.year, 8, 1), asset=a)
        at_june = preflight.acquisition_coverage(as_of=dt.date(self.year, 6, 30))
        self.assertEqual(at_june["totals"]["shortfall"], Decimal("180000"),
                         "the payment has not been made yet at this date")
        at_year_end = preflight.acquisition_coverage(as_of=dt.date(self.year, 12, 31))
        self.assertEqual(at_year_end["totals"]["shortfall"], Decimal("0"))

    def test_it_reports_as_at_the_date_asked_for(self):
        """Both sides of the reconciliation are as at a date, so the check must
        be too: an asset acquired after that date is not on the register yet and
        cannot be causing a difference. Dates are pinned so the result does not
        depend on what month the test is run in."""
        self._asset("Later generator", "90000", dt.date(self.year, 9, 1))
        early = preflight.acquisition_coverage(as_of=dt.date(self.year, 6, 30))
        self.assertTrue(early["ready"], "an asset not yet acquired must not be flagged")
        self.assertEqual(early["totals"]["shortfall"], Decimal("0"))
        later = preflight.acquisition_coverage(as_of=dt.date(self.year, 12, 31))
        self.assertEqual(later["totals"]["shortfall"], Decimal("90000"))

    def test_a_payment_after_the_date_asked_for_is_not_counted_yet(self):
        a = self._asset("Projector", "180000", dt.date(self.year, 3, 4))
        self._capital_expense("180000", dt.date(self.year, 8, 1), asset=a)
        at_june = preflight.acquisition_coverage(as_of=dt.date(self.year, 6, 30))
        self.assertEqual(at_june["totals"]["shortfall"], Decimal("180000"),
                         "the payment has not been made yet at this date")
        at_year_end = preflight.acquisition_coverage(as_of=dt.date(self.year, 12, 31))
        self.assertEqual(at_year_end["totals"]["shortfall"], Decimal("0"))

    def test_a_disposed_asset_is_left_out(self):
        self._asset("Sold van", "90000", dt.date(self.year, 2, 1),
                    disposed=True, disposed_on=dt.date(self.year, 3, 1))
        r = preflight.acquisition_coverage()
        self.assertTrue(r["ready"])

    def test_the_check_writes_nothing(self):
        a = self._asset("Mystery generator", "90000", dt.date(self.year, 5, 1))
        posting.rebuild()
        before_entries = JournalEntry.objects.count()
        before_cost = FixedAsset.objects.get(pk=a.pk).cost
        preflight.acquisition_coverage()
        self.assertEqual(JournalEntry.objects.count(), before_entries)
        self.assertEqual(FixedAsset.objects.get(pk=a.pk).cost, before_cost)

    def test_it_is_reachable_through_the_metrics_registry(self):
        self._asset("Mystery generator", "90000", dt.date(self.year, 5, 1))
        self.assertEqual(metrics.acquisition_coverage()["totals"]["shortfall"],
                         Decimal("90000"))


class PredictionAccuracyTests(PreflightBase):
    """The check must name the real number: cost is now recognised from each
    asset's acquisition date, so anything it reports as unbacked or
    double-counted is a live difference between the register and the ledger,
    not a hypothetical one."""

    def _actual_cost_diff(self):
        today = dt.date.today()
        end = dt.date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
        DepreciationRun.objects.all().delete()
        posting.rebuild()
        for m in range(1, today.month + 1):
            runs.post_run(runs.generate_run(today.year, m))
        return metrics.register_vs_ledger(end)["cost"]["diff"]

    def _build_mixed_estate(self):
        self._asset("Old pews", "50000", dt.date(self.year - 3, 1, 1))          # opening, clean
        backed = self._asset("Projector", "180000", dt.date(self.year, 3, 4))   # backed purchase
        self._capital_expense("180000", dt.date(self.year, 3, 4), asset=backed)
        donated = self._asset("Donated organ", "250000", dt.date(self.year, 3, 1))
        Acquisition.objects.create(asset=donated, source=Acquisition.Source.DONATION,
                                   date=donated.acquired_on, amount=donated.cost, fund=self.fund)
        self._asset("Mystery generator", "90000", dt.date(self.year, 5, 1))     # unbacked
        partial = self._asset("Borehole", "100000", dt.date(self.year, 4, 2))   # partial
        self._capital_expense("60000", dt.date(self.year, 4, 2), asset=partial)
        late = self._asset("PA system", "420000", dt.date(self.year - 3, 6, 1))  # double count
        self._capital_expense("420000", dt.date(self.year, 2, 1), asset=late)

    def test_the_reported_difference_is_the_real_one(self):
        self._build_mixed_estate()
        predicted = preflight.acquisition_coverage()["totals"]["predicted_diff"]
        self.assertEqual(predicted, Decimal("130000") - Decimal("420000"))
        self.assertEqual(self._actual_cost_diff(), predicted,
                         "the check must explain the register/ledger difference exactly")

    def test_a_clean_estate_reconciles_exactly(self):
        self._asset("Old pews", "50000", dt.date(self.year - 3, 1, 1))
        backed = self._asset("Projector", "180000", dt.date(self.year, 3, 4))
        self._capital_expense("180000", dt.date(self.year, 3, 4), asset=backed)
        r = preflight.acquisition_coverage()
        self.assertTrue(r["ready"])
        self.assertEqual(self._actual_cost_diff(), Decimal("0"),
                         "a register the check calls ready must reconcile")

    def test_a_donated_asset_alone_reconciles(self):
        donated = self._asset("Donated organ", "250000", dt.date(self.year, 3, 1))
        Acquisition.objects.create(asset=donated, source=Acquisition.Source.DONATION,
                                   date=donated.acquired_on, amount=donated.cost, fund=self.fund)
        self.assertTrue(preflight.acquisition_coverage()["ready"])
        self.assertEqual(self._actual_cost_diff(), Decimal("0"))


class PreflightPageTests(PreflightBase):
    def test_the_page_lists_both_kinds_of_problem(self):
        self._asset("Mystery generator", "90000", dt.date(self.year, 5, 1))
        late = self._asset("PA system", "420000", dt.date(self.year - 3, 6, 1))
        self._capital_expense("420000", dt.date(self.year, 2, 1), asset=late)
        r = self.client.get(reverse("asset_preflight"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Mystery generator")
        self.assertContains(r, "counted twice")

    def test_an_auditor_may_read_it(self):
        from core.roles import AUDITOR
        aud = User.objects.create_user("pf_aud", password="x")
        aud.groups.add(Group.objects.get_or_create(name=AUDITOR)[0])
        self.client.force_login(aud)
        self.assertEqual(self.client.get(reverse("asset_preflight")).status_code, 200)
