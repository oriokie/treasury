"""Reporting review finding (Critical): three places computed "today's true
cash position" from SiteConfig.opening_bank_balance / opening_cash_on_hand /
opening_unremitted_trust — fields populated only by the legacy-spreadsheet
import tool as a labelled reference snapshot, left at zero for every normal
deployment. For a normal deployment (opening balances set per-fund via
Department.opening_balance, as this church's are) this meant "today's cash"
was silently understated by the entire true opening position — a discrepancy
in the millions of shillings, surfacing as: a wildly wrong Executive overview
KPI card, a wrong Cash Flow Forecast, and — most seriously — a bank
reconciliation book balance that could never tie to the actual bank
statement. All three now derive from Department.opening_balance, the same
authoritative source the ledger and every other report already use."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department, total_opening_cash_position
from giving.models import Transaction
from cashbook.models import Expense


def _tr():
    u = User.objects.create_user("tr_cashpos", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class TotalOpeningCashPositionTests(TestCase):
    def test_sums_every_fund_opening_balance(self):
        Department.objects.create(name="OpenA", fund_type="LOCAL",
            category="MINISTRY", opening_balance=Decimal("10000"))
        Department.objects.create(name="OpenB", fund_type="TRUST",
            category="OFFERING", opening_balance=Decimal("5000"))
        Department.objects.create(name="OpenC", fund_type="LOCAL",
            category="MINISTRY", opening_balance=Decimal("0"))
        self.assertEqual(total_opening_cash_position(), Decimal("15000"))

    def test_zero_when_no_departments(self):
        Department.objects.all().delete()
        self.assertEqual(total_opening_cash_position(), Decimal("0"))


class CashFigureReconciliationTests(TestCase):
    """The three fixed call sites must all agree with each other and with the
    Statement of Financial Position — the actual proof this bug is closed."""
    def setUp(self):
        self.tr = _tr()
        self.d = Department.objects.create(name="CashPosFund", fund_type="LOCAL",
            category="MINISTRY", opening_balance=Decimal("100000"))
        Transaction.objects.create(date=dt.date(2026, 6, 1), amount=Decimal("5000"),
            direction="CREDIT", confirmed=True, channel="CASH",
            allocation_status="MANUAL", department=self.d)
        Expense.objects.create(date=dt.date(2026, 6, 2), department=self.d,
            description="x", amount=Decimal("1500"), category="OTHER",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        self.c = Client(); self.c.force_login(self.tr)

    def _sofp_cash(self):
        from django.test import RequestFactory
        from reports.views import MonthlyTreasurerReportView
        rf = RequestFactory()
        today = dt.date.today()
        req = rf.get(f"/reports/board/?as_of={today:%Y-%m}")
        req.user = self.tr
        view = MonthlyTreasurerReportView(); view.request = req
        return view.get_context_data()["sofp"]["cash"]

    def test_executive_dashboard_cash_matches_sofp(self):
        from core.services import dashboard as dash_svc
        cards = dash_svc.cards()
        exec_cash = next(c["value"] for c in cards if "Cash" in c["label"])
        self.assertEqual(exec_cash, self._sofp_cash())

    def test_forecast_cash_now_matches_sofp(self):
        from core.services import forecast
        self.assertEqual(forecast.cash_now(), self._sofp_cash())

    def test_reconciliation_book_balance_matches_sofp(self):
        from statements.views import _ledger_bank_balance
        today = dt.date.today()
        self.assertEqual(_ledger_bank_balance(today), self._sofp_cash())

    def test_cash_position_not_understated_by_opening_balance(self):
        # the specific regression this closes: with SiteConfig's opening
        # fields at their normal zero default, cash must still reflect the
        # fund's true opening balance, not just income-minus-expenses
        from core.services import forecast
        cash = forecast.cash_now()
        self.assertGreaterEqual(cash, Decimal("100000"))   # the opening balance alone

    def test_recon_diagnostic_opening_consistent_with_book(self):
        from statements.views import _recon_diagnostic
        today = dt.date.today()
        result = _recon_diagnostic(today)
        # "book" already came from current_cash_position() (ties to the SOFP
        # by construction) even before this fix; "opening" is the specific
        # value this fix corrected — the two must now be mutually consistent
        # (opening + income - expenses + transfers == book) instead of
        # showing a confusing, self-contradictory diagnostic
        self.assertEqual(result["book"], self._sofp_cash())
        rebuilt = result["opening"] + result["income"] - result["expenses"] + result["transfers"]
        self.assertEqual(rebuilt, result["book"])
        self.assertGreaterEqual(result["opening"], Decimal("100000"))
