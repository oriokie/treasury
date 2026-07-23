"""EAM Phase 2b — acquisition intake, the capital-expense bridge, the
capitalisation threshold, and the mandatory disposal fund.

The accounting rule under test throughout: a purchase is paid by an Expense,
which already carries the cash side, so capitalising it must MOVE the debit
(capital work-in-progress -> fixed assets) rather than add a second entry. Only
a donation, which involves no cash, posts a journal of its own.
"""
import calendar
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User, Group
from django.test import TestCase
from django.urls import reverse

from assets.models import FixedAsset, Acquisition, DepreciationRun
from assets.services import runs
from cashbook.models import Expense
from core.metrics import metrics
from core.models import SiteConfig
from core.roles import TREASURER
from departments.models import Department
from ledger.models import JournalLine
from ledger.services import posting


def _bal(system_key):
    from django.db.models import Sum
    agg = (JournalLine.objects.filter(account__system_key=system_key)
           .aggregate(d=Sum("debit"), c=Sum("credit")))
    return (agg["d"] or Decimal(0)) - (agg["c"] or Decimal(0))


class AcquisitionBase(TestCase):
    def setUp(self):
        self.fund = Department.objects.create(name="General", fund_type=Department.FundType.LOCAL)
        self.tr = User.objects.create_user("acq_tr", password="x")
        self.tr.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.tr)

    def _reconcile(self):
        today = dt.date.today()
        DepreciationRun.objects.all().delete()
        posting.rebuild()
        for m in range(1, today.month + 1):
            runs.post_run(runs.generate_run(today.year, m))
        end = dt.date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
        return metrics.register_vs_ledger(end)

    def _capital_expense(self, amount="180000", **kw):
        return Expense.objects.create(
            date=kw.get("date", dt.date(dt.date.today().year, 3, 4)),
            amount=Decimal(amount), description=kw.get("description", "Projector"),
            category="EQUIPMENT", expenditure_type=Expense.ExpenditureType.CAPITAL,
            department=self.fund, status=Expense.Status.PAID, recorded_by=self.tr)


class DonatedAssetTests(AcquisitionBase):
    def _donate(self, amount="250000"):
        asset = FixedAsset.objects.create(
            name="Donated organ", category="EQUIPMENT", cost=Decimal(amount),
            salvage_value=Decimal(0), acquired_on=dt.date(dt.date.today().year, 3, 1),
            in_service_on=dt.date(dt.date.today().year, 3, 1),
            method="STRAIGHT", rate=Decimal("10"), department=self.fund)
        Acquisition.objects.create(
            asset=asset, source=Acquisition.Source.DONATION, date=asset.acquired_on,
            amount=asset.cost, fund=self.fund, donor_name="Anonymous member")
        return asset

    def test_donation_is_credited_to_net_assets_not_income(self):
        """A gift in kind is not cash income; it increases net assets (EAM 9.4)."""
        self._donate("250000")
        self._reconcile()
        self.assertEqual(_bal("CAPITAL_FUND"), Decimal("-250000"))  # equity = credit
        from ledger.models import Account
        self.assertFalse(Account.objects.filter(system_key="DONATED_ASSET_INCOME").exists(),
                         "the superseded donated-asset income account must be retired")

    def test_donation_to_a_restricted_fund_credits_restricted_equity(self):
        trust = Department.objects.create(name="Tithe", fund_type=Department.FundType.TRUST)
        asset = FixedAsset.objects.create(
            name="Donated land", category="LAND", cost=Decimal("400000"),
            salvage_value=Decimal(0), acquired_on=dt.date(dt.date.today().year, 2, 1),
            in_service_on=dt.date(dt.date.today().year, 2, 1), department=trust)
        Acquisition.objects.create(asset=asset, source=Acquisition.Source.DONATION,
                                   date=asset.acquired_on, amount=asset.cost, fund=trust)
        self._reconcile()
        self.assertEqual(_bal("DESIGNATED_FUNDS"), Decimal("-400000"))

    def test_donation_does_not_touch_income(self):
        self._donate("250000")
        self._reconcile()
        for key in ("INC_DONATIONS", "INC_OTHER", "INC_OFFERINGS"):
            self.assertEqual(_bal(key), Decimal("0"), f"{key} must be untouched")

    def test_donation_reconciles_and_balances(self):
        self._donate()
        rec = self._reconcile()
        self.assertTrue(all(v["diff"] == Decimal("0") for v in rec.values()),
                        f"donated asset broke the reconciliation: {rec}")
        from django.db.models import Sum
        agg = JournalLine.objects.aggregate(d=Sum("debit"), c=Sum("credit"))
        self.assertEqual(agg["d"], agg["c"])

    def test_donation_posting_is_idempotent(self):
        self._donate()
        self._reconcile()
        first = JournalLine.objects.filter(entry__source_type="asset_acq").count()
        self._reconcile()
        self.assertEqual(JournalLine.objects.filter(entry__source_type="asset_acq").count(),
                         first, "reposting a donation must replace, not duplicate")

    def test_purchase_acquisition_posts_no_journal_of_its_own(self):
        """The expense carries the cash side; a second entry would double-count."""
        exp = self._capital_expense()
        asset = FixedAsset.objects.create(
            name="Projector", category="EQUIPMENT", cost=exp.amount,
            salvage_value=Decimal(0), acquired_on=exp.date, in_service_on=exp.date,
            method="NONE", rate=Decimal(0), department=self.fund)
        exp.capitalized_asset = asset
        exp.save()
        Acquisition.objects.create(asset=asset, source=Acquisition.Source.PURCHASE,
                                   date=exp.date, amount=exp.amount, expense=exp,
                                   fund=self.fund)
        self._reconcile()
        self.assertEqual(JournalLine.objects.filter(entry__source_type="asset_acq").count(), 0)


class CapitaliseExpenseTests(AcquisitionBase):
    def test_the_capitalise_page_opens(self):
        """Regression: the page used the money filter without loading the tag
        library, so it raised on load — the earlier tests only ever POSTed."""
        exp = self._capital_expense("180000")
        r = self.client.get(reverse("expense_capitalise", args=[exp.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Projector")

    def test_capitalising_creates_the_asset_and_links_the_payment(self):
        exp = self._capital_expense("180000")
        r = self.client.post(reverse("expense_capitalise", args=[exp.pk]),
                             {"name": "Projector", "category": "EQUIPMENT"})
        self.assertEqual(r.status_code, 302)
        asset = FixedAsset.objects.get(name="Projector")
        exp.refresh_from_db()
        self.assertEqual(asset.cost, Decimal("180000"))
        self.assertEqual(exp.capitalized_asset_id, asset.pk)
        self.assertEqual(asset.acquisition.source, Acquisition.Source.PURCHASE)
        self.assertEqual(asset.acquisition.expense_id, exp.pk)

    def test_capitalising_moves_the_debit_out_of_cwip_without_double_counting(self):
        exp = self._capital_expense("180000")
        self._reconcile()
        self.assertEqual(_bal("CWIP"), Decimal("180000"), "unlinked capital spend sits in CWIP")
        self.client.post(reverse("expense_capitalise", args=[exp.pk]),
                         {"name": "Projector", "category": "EQUIPMENT"})
        rec = self._reconcile()
        self.assertEqual(_bal("CWIP"), Decimal("0"), "capitalising must clear CWIP")
        self.assertTrue(all(v["diff"] == Decimal("0") for v in rec.values()),
                        f"capitalised purchase broke the reconciliation: {rec}")

    def test_adding_a_payment_to_an_existing_asset_accumulates_cost(self):
        asset = FixedAsset.objects.create(
            name="Church hall", category="BUILDING", cost=Decimal("1000000"),
            salvage_value=Decimal(0), acquired_on=dt.date(dt.date.today().year, 1, 1),
            method="NONE", rate=Decimal(0), department=self.fund)
        exp = self._capital_expense("400000", description="Roofing")
        self.client.post(reverse("expense_capitalise", args=[exp.pk]),
                         {"existing": str(asset.pk)})
        asset.refresh_from_db(); exp.refresh_from_db()
        self.assertEqual(asset.cost, Decimal("1400000"))
        self.assertEqual(exp.capitalized_asset_id, asset.pk)

    def test_an_already_capitalised_payment_is_not_counted_twice(self):
        exp = self._capital_expense("180000")
        self.client.post(reverse("expense_capitalise", args=[exp.pk]),
                         {"name": "Projector", "category": "EQUIPMENT"})
        self.client.post(reverse("expense_capitalise", args=[exp.pk]),
                         {"name": "Projector again", "category": "EQUIPMENT"})
        self.assertEqual(FixedAsset.objects.filter(name__startswith="Projector").count(), 1)

    def test_recurrent_expense_cannot_be_capitalised(self):
        exp = self._capital_expense("180000")
        exp.expenditure_type = Expense.ExpenditureType.RECURRENT
        exp.save()
        self.client.post(reverse("expense_capitalise", args=[exp.pk]),
                         {"name": "Nope", "category": "EQUIPMENT"})
        self.assertFalse(FixedAsset.objects.filter(name="Nope").exists())


class CapitalisationThresholdTests(AcquisitionBase):
    def test_small_payment_is_kept_off_the_register(self):
        cfg = SiteConfig.get()
        cfg.capitalisation_threshold = Decimal("10000")
        cfg.save()
        exp = self._capital_expense("2500", description="Stapler")
        self.client.post(reverse("expense_capitalise", args=[exp.pk]),
                         {"name": "Stapler", "category": "EQUIPMENT"})
        self.assertFalse(FixedAsset.objects.filter(name="Stapler").exists())

    def test_zero_threshold_keeps_the_previous_behaviour(self):
        cfg = SiteConfig.get()
        self.assertEqual(cfg.capitalisation_threshold, Decimal("0"))
        exp = self._capital_expense("2500", description="Stapler")
        self.client.post(reverse("expense_capitalise", args=[exp.pk]),
                         {"name": "Stapler", "category": "EQUIPMENT"})
        self.assertTrue(FixedAsset.objects.filter(name="Stapler").exists())


class DisposalFundRequiredTests(AcquisitionBase):
    def _asset(self):
        return FixedAsset.objects.create(
            name="Old van", category="VEHICLE", cost=Decimal("100000"),
            salvage_value=Decimal(0), acquired_on=dt.date(dt.date.today().year - 1, 1, 1),
            method="NONE", rate=Decimal(0), department=self.fund)

    def test_disposal_without_a_fund_is_refused(self):
        a = self._asset()
        self.client.post(reverse("asset_dispose", args=[a.pk]),
                         {"disposed_on": dt.date.today().isoformat(),
                          "proceeds": "120000", "method": "SOLD", "fund": ""})
        a.refresh_from_db()
        self.assertFalse(a.disposed, "a disposal with no fund must not be recorded")

    def test_disposal_with_a_fund_is_recorded(self):
        a = self._asset()
        self.client.post(reverse("asset_dispose", args=[a.pk]),
                         {"disposed_on": dt.date.today().isoformat(),
                          "proceeds": "120000", "method": "SOLD",
                          "fund": str(self.fund.pk)})
        a.refresh_from_db()
        self.assertTrue(a.disposed)
        self.assertEqual(a.disposal_fund_id, self.fund.pk)
        self.assertEqual(a.disposal_gain_loss, Decimal("20000"))


class NonCashContributionsReportTests(AcquisitionBase):
    """Donated assets are reported in their own section of the Income &
    Expenditure statement, sourced from the register — the statement itself
    stays transaction-based, and the cash income total is unaffected."""

    def _donate(self, amount="250000", when=None):
        when = when or dt.date(dt.date.today().year, 3, 1)
        asset = FixedAsset.objects.create(
            name="Donated organ", category="EQUIPMENT", cost=Decimal(amount),
            salvage_value=Decimal(0), acquired_on=when, in_service_on=when,
            department=self.fund)
        return Acquisition.objects.create(
            asset=asset, source=Acquisition.Source.DONATION, date=when,
            amount=Decimal(amount), fund=self.fund, donor_name="Anonymous member")

    def test_metric_totals_donations_in_the_period(self):
        year = dt.date.today().year
        self._donate("250000", dt.date(year, 3, 1))
        self._donate("50000", dt.date(year, 6, 1))
        self.assertEqual(metrics.donated_assets(dt.date(year, 1, 1), dt.date(year, 12, 31)),
                         Decimal("300000"))
        self.assertEqual(metrics.donated_assets(dt.date(year, 1, 1), dt.date(year, 4, 30)),
                         Decimal("250000"))

    def test_statement_shows_the_section_without_changing_income(self):
        year = dt.date.today().year
        self._donate("250000", dt.date(year, 3, 1))
        r = self.client.get(reverse("report_ie"), {"period": "ANNUAL", "year": year})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["donated_assets"], Decimal("250000"))
        self.assertContains(r, "Non-cash items")
        self.assertContains(r, "Donated assets received")
        # the cash income total and the net result must be untouched by a gift in kind
        self.assertEqual(r.context["income"], Decimal("0"))
        self.assertEqual(r.context["net"], Decimal("0"))

    def test_section_is_absent_when_there_are_no_donations(self):
        year = dt.date.today().year
        r = self.client.get(reverse("report_ie"), {"period": "ANNUAL", "year": year})
        self.assertNotContains(r, "Donated assets received")
