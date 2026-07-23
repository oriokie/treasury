"""Asset transactions as they appear in the financial statements.

The invariant at the centre of this file: cost, accumulated depreciation and net
book value must all be drawn from the same population of assets. They were not —
net book value was summed over every asset while the other two were temporal, so
at any past date net book value was overstated by the cost of everything acquired
later. It reconciled today and was wrong yesterday, which is the worst way for an
accounting figure to be wrong.
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
from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from ledger.services import posting


class StatementBase(TestCase):
    def setUp(self):
        self.fund = Department.objects.create(name="General", fund_type=Department.FundType.LOCAL)
        self.tr = User.objects.create_user("st_tr", password="x")
        self.tr.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.tr)
        self.year = dt.date.today().year
        Transaction.objects.create(date=dt.date(self.year, 1, 5), amount=Decimal("1000"),
                                   direction="CREDIT", channel="CASH", confirmed=True,
                                   allocation_status="MANUAL", department=self.fund,
                                   reference="ANCHOR")

    def _asset(self, name, cost, acquired, method="STRAIGHT", rate="10"):
        return FixedAsset.objects.create(
            name=name, category="EQUIPMENT", cost=Decimal(cost), salvage_value=Decimal(0),
            acquired_on=acquired, in_service_on=acquired, method=method,
            rate=Decimal(rate), department=self.fund)

    def _post_everything(self):
        today = dt.date.today()
        DepreciationRun.objects.all().delete()
        posting.rebuild()
        for m in range(1, today.month + 1):
            runs.post_run(runs.generate_run(today.year, m))


class AssetFigureConsistencyTests(StatementBase):
    def test_net_book_value_equals_cost_less_depreciation_at_any_date(self):
        """The regression: an asset acquired mid-year must not appear in net book
        value at a date before the church owned it."""
        self._asset("Old pews", "500000", dt.date(self.year - 3, 1, 1))
        self._asset("New generator", "420000", dt.date(self.year, 6, 12))
        for when in (dt.date(self.year - 1, 12, 31),
                     dt.date(self.year, 3, 31),
                     dt.date(self.year, 12, 31)):
            cost = metrics.fixed_assets_cost(when)
            accdep = metrics.accumulated_depreciation(when)
            nbv = metrics.net_book_value(when)
            self.assertEqual(cost - accdep, nbv,
                             f"net book value disagrees with cost less depreciation at {when}")

    def test_an_asset_is_absent_before_it_was_acquired(self):
        self._asset("New generator", "420000", dt.date(self.year, 6, 12))
        self.assertEqual(metrics.net_book_value(dt.date(self.year, 1, 31)), Decimal("0"))
        self.assertGreater(metrics.net_book_value(dt.date(self.year, 6, 30)), Decimal("0"))

    def test_a_disposed_asset_leaves_all_three_figures_together(self):
        a = self._asset("Old van", "100000", dt.date(self.year - 2, 1, 1))
        a.disposed = True
        a.disposed_on = dt.date(self.year, 4, 15)
        a.disposal_proceeds = Decimal("50000")
        a.disposal_fund = self.fund
        a.save()
        after = dt.date(self.year, 5, 31)
        self.assertEqual(metrics.fixed_assets_cost(after), Decimal("0"))
        self.assertEqual(metrics.accumulated_depreciation(after), Decimal("0"))
        self.assertEqual(metrics.net_book_value(after), Decimal("0"))


class IncomeExpenditureAssetTests(StatementBase):
    def test_depreciation_is_reported_even_though_it_is_not_a_payment(self):
        self._asset("Church van", "1200000", dt.date(self.year - 1, 1, 1))
        self._post_everything()
        r = self.client.get(reverse("report_ie"), {"period": "ANNUAL", "year": self.year})
        self.assertGreater(r.context["depreciation"], Decimal("0"))
        self.assertContains(r, "Non-cash items")
        self.assertContains(r, "Depreciation")

    def test_depreciation_does_not_disturb_the_cash_figures(self):
        self._asset("Church van", "1200000", dt.date(self.year - 1, 1, 1))
        self._post_everything()
        r = self.client.get(reverse("report_ie"), {"period": "ANNUAL", "year": self.year})
        ctx = r.context
        self.assertEqual(ctx["net"], ctx["income"] - ctx["expense"] + ctx["disposal_gain_loss"])
        self.assertEqual(ctx["net_after_noncash"],
                         ctx["net"] + ctx["donated_assets"] - ctx["depreciation"])

    def test_depreciation_is_not_projected_past_today(self):
        """An annual period that has not finished must not show a full year of
        depreciation beside income that only runs to date."""
        self._asset("Church van", "1200000", dt.date(self.year - 1, 1, 1))
        self._post_everything()
        r = self.client.get(reverse("report_ie"), {"period": "ANNUAL", "year": self.year})
        to_date = metrics.depreciation_expense(dt.date(self.year, 1, 1), dt.date.today())
        self.assertEqual(r.context["depreciation"], to_date)


class ChangesInNetAssetsTests(StatementBase):
    def _movement(self):
        r = self.client.get(reverse("report_changes_net_assets"),
                            {"period": "ANNUAL", "year": self.year})
        return r.context["prop"]

    def test_the_movement_in_fixed_assets_ties(self):
        self._asset("Old pews", "500000", dt.date(self.year - 3, 1, 1))
        self._asset("New generator", "420000", dt.date(self.year, 3, 12))
        self._post_everything()
        p = self._movement()
        self.assertEqual(p["unexplained"], Decimal("0"),
                         "opening + additions - depreciation - disposals must equal closing")
        self.assertEqual(p["opening"] + p["additions"] - p["depr"] - p["disposals"],
                         p["closing"])

    def test_depreciation_is_the_posted_figure_not_a_balancing_number(self):
        self._asset("Old pews", "500000", dt.date(self.year - 3, 1, 1))
        self._post_everything()
        p = self._movement()
        expected = metrics.depreciation_expense(dt.date(self.year - 1, 12, 31), dt.date.today())
        self.assertEqual(p["depr"], expected)

    def test_a_donated_asset_is_an_addition_not_negative_depreciation(self):
        """The old balancing figure swept donations in, so a gift read as though
        the assets had appreciated."""
        donated = self._asset("Donated organ", "250000", dt.date(self.year, 3, 1))
        Acquisition.objects.create(asset=donated, source=Acquisition.Source.DONATION,
                                   date=donated.acquired_on, amount=donated.cost,
                                   fund=self.fund)
        self._post_everything()
        p = self._movement()
        self.assertEqual(p["donated"], Decimal("250000"))
        self.assertGreaterEqual(p["depr"], Decimal("0"), "depreciation must never be negative")
        self.assertEqual(p["unexplained"], Decimal("0"))

    def test_capital_spending_not_yet_on_the_register_is_not_an_addition(self):
        """Money paid towards an asset that is not linked to a register record is
        work in progress, not an addition to the register."""
        Expense.objects.create(
            date=dt.date(self.year, 2, 1), amount=Decimal("120000"),
            description="Building materials", category="EQUIPMENT",
            expenditure_type=Expense.ExpenditureType.CAPITAL, department=self.fund,
            status=Expense.Status.PAID, recorded_by=self.tr)
        self._post_everything()
        p = self._movement()
        self.assertEqual(p["additions"], Decimal("0"))
        self.assertEqual(p["unexplained"], Decimal("0"))

    def test_a_disposal_appears_at_its_carrying_value(self):
        a = self._asset("Old van", "100000", dt.date(self.year - 2, 1, 1))
        a.disposed = True
        a.disposed_on = dt.date(self.year, 4, 15)
        a.disposal_proceeds = Decimal("50000")
        a.disposal_fund = self.fund
        a.disposal_gain_loss = Decimal("50000") - a.net_book_value(a.disposed_on)
        a.save()
        self._post_everything()
        p = self._movement()
        self.assertGreater(p["disposals"], Decimal("0"))
        self.assertEqual(p["unexplained"], Decimal("0"))


class NetAssetsArticulationTests(StatementBase):
    """The Income Statement reports fund balances; the Statement of Financial
    Position reports net assets. They are different measures, and the bridge
    between them must tie exactly — it used to be out by the whole asset
    register, and later by the accrual adjustment, because each statement
    assembled its own total. Both now read one registered definition."""

    def test_the_bridge_ties_to_the_financial_position(self):
        self._asset("Church van", "1200000", dt.date(self.year - 1, 1, 1))
        self._post_everything()
        q = {"period": "ANNUAL", "year": self.year}
        inc = self.client.get(reverse("report_income_statement"), q).context
        pos = self.client.get(reverse("report_financial_position"), q).context
        self.assertEqual(inc["na_total"], pos["net_assets"],
                         "the income statement's bridge must reach the same net "
                         "assets the financial position reports")
        self.assertEqual(inc["na_unexplained"], Decimal("0"),
                         "the funds figure must match the one the metric used")

    def test_the_bridge_starts_from_the_funds_and_adds_the_asset_register(self):
        self._asset("Church van", "1200000", dt.date(self.year - 1, 1, 1))
        self._post_everything()
        q = {"period": "ANNUAL", "year": self.year}
        inc = self.client.get(reverse("report_income_statement"), q).context
        bridged = inc["na_close"] + sum(a for _l, a in inc["na_bridge"])
        self.assertEqual(bridged, inc["na_total"])

    def test_the_financial_position_reads_the_registered_definition(self):
        self._asset("Church van", "1200000", dt.date(self.year - 1, 1, 1))
        self._post_everything()
        q = {"period": "ANNUAL", "year": self.year}
        pos = self.client.get(reverse("report_financial_position"), q).context
        # compare at the date the statement itself used, not one we assumed
        self.assertEqual(pos["net_assets"], metrics.net_assets(pos["as_of"])["total"])
        self.assertTrue(pos["balanced"], "assets must still equal liabilities plus net assets")
