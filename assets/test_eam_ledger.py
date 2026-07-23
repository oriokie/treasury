"""EAM Phase 1 — ledger backbone contracts.

Assets now post to the general ledger: an opening balance brings the control
accounts up to the register, monthly depreciation runs charge Dr depreciation
expense / Cr accumulated depreciation, and capital spend is held in CWIP (not
expensed, not straight into fixed assets). The register↔ledger reconciliation
must be exact once the opening and every monthly run through a date are posted.
"""
import calendar
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from assets.models import AssetClass, DepreciationRun, FixedAsset
from assets.services import runs
from core.metrics import metrics
from ledger.services import posting


def _bal(key, as_of=None):
    from django.db.models import Sum
    from ledger.models import JournalLine
    qs = JournalLine.objects.filter(account__system_key=key)
    if as_of:
        qs = qs.filter(entry__date__lte=as_of)
    agg = qs.aggregate(d=Sum("debit"), c=Sum("credit"))
    return (agg["d"] or Decimal(0)) - (agg["c"] or Decimal(0))


class LedgerBackboneTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from departments.models import Department
        from giving.models import Transaction
        cls.fund = Department.objects.create(name="Building", fund_type=Department.FundType.LOCAL)
        # a transaction so the opening date resolves to this year
        Transaction.objects.create(date=dt.date(dt.date.today().year, 1, 5),
                                   amount=Decimal("1000"), direction="CREDIT",
                                   channel="CASH", allocation_status="MANUAL",
                                   confirmed=True, department=cls.fund, reference="SEEDTX")
        ac = AssetClass.objects.get(code="EQUIPMENT")
        FixedAsset.objects.create(
            name="Sound system", category="EQUIPMENT", asset_class=ac,
            acquired_on=dt.date(2023, 1, 1), in_service_on=dt.date(2023, 1, 1),
            cost=Decimal("600000"), method="STRAIGHT", rate=Decimal("10"),
            department=cls.fund, status="IN_SERVICE")

    def _post_year_to_date(self):
        posting.rebuild()
        today = dt.date.today()
        for m in range(1, today.month + 1):
            runs.post_run(runs.generate_run(today.year, m))
        return dt.date(today.year, today.month,
                       calendar.monthrange(today.year, today.month)[1])

    def test_opening_brings_control_accounts_to_register(self):
        posting.rebuild()   # posts asset opening
        d0 = posting._asset_opening_date()
        # cost control account equals register cost right after opening
        self.assertEqual(_bal("FIXED_ASSETS"),
                         Decimal(metrics.fixed_assets_cost(d0)))

    def test_monthly_run_posts_depreciation(self):
        posting.rebuild()
        today = dt.date.today()
        run = runs.post_run(runs.generate_run(today.year, today.month))
        self.assertEqual(run.status, DepreciationRun.Status.POSTED)
        self.assertGreater(run.total_charge, 0)
        # the run's charge is in depreciation expense and accumulated depreciation
        self.assertIsNotNone(run.journal)

    def test_register_reconciles_to_ledger(self):
        end = self._post_year_to_date()
        rec = metrics.register_vs_ledger(end)
        self.assertEqual(rec["cost"]["diff"], Decimal("0"))
        self.assertEqual(rec["accdep"]["diff"], Decimal("0"))
        self.assertEqual(rec["nbv"]["diff"], Decimal("0"))

    def test_reposting_a_run_is_idempotent(self):
        posting.rebuild()
        today = dt.date.today()
        run = runs.generate_run(today.year, today.month)
        posting.post_depreciation_run(run)
        accdep_once = _bal("ACCUM_DEPRECIATION")
        posting.post_depreciation_run(run)   # re-post
        self.assertEqual(_bal("ACCUM_DEPRECIATION"), accdep_once,
                         "re-posting a run must replace, not add")

    def test_generate_refuses_over_a_posted_run(self):
        posting.rebuild()
        today = dt.date.today()
        runs.post_run(runs.generate_run(today.year, today.month))
        with self.assertRaises(ValueError):
            runs.generate_run(today.year, today.month)

    def test_trial_balance_stays_balanced(self):
        self._post_year_to_date()
        from django.db.models import Sum
        from ledger.models import JournalLine
        agg = JournalLine.objects.aggregate(d=Sum("debit"), c=Sum("credit"))
        self.assertEqual(agg["d"], agg["c"])

    def test_a_capital_purchase_made_this_year_reconciles_exactly(self):
        """The modern flow: an asset bought during the year is recorded from its
        acquisition date and the payment that bought it carries its cost into the
        ledger. The opening must not also bring it in (it was not owned then)."""
        from cashbook.models import Expense
        from assets.models import Acquisition
        year = dt.date.today().year
        u = User.objects.create_user("capx", password="x")
        asset = FixedAsset.objects.create(
            name="Amplifier", category="EQUIPMENT", cost=Decimal("50000"),
            salvage_value=Decimal(0), acquired_on=dt.date(year, 2, 1),
            in_service_on=dt.date(year, 2, 1), method="NONE", rate=Decimal(0),
            department=self.fund)
        exp = Expense.objects.create(
            date=dt.date(year, 2, 1), amount=Decimal("50000"),
            description="Amplifier (capitalised)", category="EQUIPMENT",
            expenditure_type=Expense.ExpenditureType.CAPITAL,
            department=self.fund, status=Expense.Status.PAID, recorded_by=u,
            capitalized_asset=asset)
        Acquisition.objects.create(asset=asset, source=Acquisition.Source.PURCHASE,
                                   date=exp.date, amount=exp.amount, expense=exp,
                                   fund=self.fund)
        end = self._post_year_to_date()
        rec = metrics.register_vs_ledger(end)
        self.assertEqual(rec["cost"]["diff"], Decimal("0"),
                         "a linked capital payment must carry the cost exactly once")
        self.assertEqual(rec["nbv"]["diff"], Decimal("0"))

    def test_cost_is_recognised_from_the_acquisition_date_not_the_opening_date(self):
        """The point of temporal costing: an asset bought in February is not on
        the register in January."""
        year = dt.date.today().year
        FixedAsset.objects.create(
            name="Amplifier", category="EQUIPMENT", cost=Decimal("50000"),
            salvage_value=Decimal(0), acquired_on=dt.date(year, 2, 1),
            in_service_on=dt.date(year, 2, 1), method="NONE", rate=Decimal(0),
            department=self.fund)
        before = metrics.fixed_assets_cost(dt.date(year, 1, 31))
        after = metrics.fixed_assets_cost(dt.date(year, 3, 31))
        self.assertEqual(after - before, Decimal("50000"))


class DisposalLedgerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from departments.models import Department
        from giving.models import Transaction
        cls.fund = Department.objects.create(name="General", fund_type=Department.FundType.LOCAL)
        Transaction.objects.create(date=dt.date(dt.date.today().year, 1, 5),
                                   amount=Decimal("1000"), direction="CREDIT", channel="CASH",
                                   allocation_status="MANUAL", confirmed=True,
                                   department=cls.fund, reference="SEEDTX")

    def _make_asset(self, cost, method="NONE", rate="0"):
        return FixedAsset.objects.create(
            name="Van", category="VEHICLE", cost=Decimal(cost), salvage_value=Decimal("0"),
            acquired_on=dt.date(dt.date.today().year - 1, 1, 1),
            in_service_on=dt.date(dt.date.today().year - 1, 1, 1),
            method=method, rate=Decimal(rate), department=self.fund, status="IN_SERVICE")

    def _reconcile(self):
        from assets.models import DepreciationRun
        end = dt.date(dt.date.today().year, dt.date.today().month,
                      calendar.monthrange(dt.date.today().year, dt.date.today().month)[1])
        DepreciationRun.objects.all().delete()
        posting.rebuild()
        for m in range(1, dt.date.today().month + 1):
            runs.post_run(runs.generate_run(dt.date.today().year, m))
        return metrics.register_vs_ledger(end)

    def _dispose(self, asset, proceeds, on=None):
        on = on or dt.date(dt.date.today().year, 4, 15)
        asset.disposed = True
        asset.disposed_on = on
        asset.disposal_proceeds = Decimal(proceeds)
        asset.disposal_method = "SOLD"
        asset.disposal_fund = self.fund
        asset.disposal_gain_loss = Decimal(proceeds) - asset.net_book_value(on)
        asset.save()

    def test_disposal_reconciles_and_recognises_gain(self):
        a = self._make_asset("100000")   # NBV stays 100,000 (NONE method)
        self._dispose(a, "130000")        # gain 30,000
        rec = self._reconcile()
        self.assertTrue(all(v["diff"] == Decimal("0") for v in rec.values()),
                        f"disposal did not reconcile: {rec}")
        self.assertEqual(_bal("ASSET_DISPOSAL_GAIN"), Decimal("-30000"))  # income = credit

    def test_disposal_recognises_loss_and_balances(self):
        a = self._make_asset("100000")
        self._dispose(a, "70000")         # loss 30,000
        self._reconcile()
        self.assertEqual(_bal("ASSET_DISPOSAL_LOSS"), Decimal("30000"))   # expense = debit
        from django.db.models import Sum
        from ledger.models import JournalLine
        agg = JournalLine.objects.aggregate(d=Sum("debit"), c=Sum("credit"))
        self.assertEqual(agg["d"], agg["c"])

    def test_scrap_writes_off_whole_nbv_as_loss(self):
        a = self._make_asset("80000")
        self._dispose(a, "0")             # scrap, whole NBV is a loss
        self._reconcile()
        self.assertEqual(_bal("ASSET_DISPOSAL_LOSS"), Decimal("80000"))

    def test_disposed_asset_leaves_the_control_account(self):
        a = self._make_asset("100000")
        before = self._reconcile()
        self.assertEqual(before["cost"]["ledger"], Decimal("100000"))
        self._dispose(a, "100000")
        after = self._reconcile()
        self.assertEqual(after["cost"]["ledger"], Decimal("0"),
                         "disposed asset cost must leave the control account")


class CapitalToCwipTests(TestCase):
    def test_capital_expense_goes_to_cwip_not_expense_or_fixed_assets(self):
        from departments.models import Department
        from cashbook.models import Expense
        fund = Department.objects.create(name="Dev", fund_type=Department.FundType.LOCAL)
        u = User.objects.create_user("cap", password="x")
        exp = Expense.objects.create(
            date=dt.date(dt.date.today().year, 3, 1), amount=Decimal("200000"),
            description="Projector", category="EQUIPMENT",
            expenditure_type=Expense.ExpenditureType.CAPITAL,
            department=fund, status=Expense.Status.PAID, recorded_by=u)
        posting.rebuild()
        self.assertEqual(_bal("CWIP"), Decimal("200000"),
                         "capital spend should sit in CWIP")
        # and it is NOT an operating expense
        self.assertEqual(_bal("EXP_EQUIPMENT"), Decimal("0"))
