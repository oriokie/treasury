"""Petty cash and staff advances, report consolidation, and negative figures.

Three fixes that share a shape: a number or a setting that was right in one
place and wrong, or absent, in another.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase

from core import roles
from departments.models import Department

from .models import Expense, PettyCashTopUp, StaffAdvance


def _treasurer(name):
    user = User.objects.create_user(name, password="office-pass-1")
    user.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
    return user


class AdvanceCashOutIsDatedTests(TestCase):
    """Cash returned to the tin comes back on the day it comes back.

    `petty_cash_out_asof` date-gated the issue and each top-up, then subtracted
    `returned_to_petty` with no date test at all — the return has no date field
    of its own, and the date the cash actually came back is `settled_on`, which
    is what the petty cash register uses. So a returned advance showed as never
    having left the box, at *any* as-of date, including dates before the money
    went out. Where the whole advance came back the two cancelled exactly and
    the float card simply did not acknowledge the advance while the register
    did — which is how the two figures drifted apart.
    """

    def setUp(self):
        self.user = _treasurer("tess-adv")
        self.fund = Department.objects.create(
            name="Local Church Budget", slug="lcb-adv",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.today = dt.date.today()
        self.start = self.today - dt.timedelta(days=20)
        PettyCashTopUp.objects.create(date=self.start, amount=Decimal("20000"),
                                      recorded_by=self.user)
        self.issued = self.start + dt.timedelta(days=3)
        self.settled = self.start + dt.timedelta(days=9)
        self.advance = StaffAdvance.objects.create(
            staff_name="Peter Kamau", purpose="Camp supplies", department=self.fund,
            amount=Decimal("10000"), date_issued=self.issued,
            from_petty_cash=True, issued_by=self.user,
            status=StaffAdvance.Status.ISSUED)

    def _settle(self, returned):
        Expense.objects.create(
            date=self.issued + dt.timedelta(days=3), department=self.fund,
            description="Camp supplies receipt",
            amount=Decimal("10000") - returned,
            category=Expense.Category.MATERIALS, status=Expense.Status.PAID,
            recorded_by=self.user, advance=self.advance)
        self.advance.returned_to_petty = returned
        self.advance.settled_on = self.settled
        self.advance.status = StaffAdvance.Status.SETTLED
        self.advance.save()

    def test_the_whole_advance_is_out_of_the_box_before_it_is_returned(self):
        self._settle(Decimal("3000"))
        self.assertEqual(
            self.advance.petty_cash_out_asof(self.settled - dt.timedelta(days=1)),
            Decimal("10000"),
            "On a date before the cash came back, the full advance is still out "
            "of the box.")

    def test_the_return_reduces_the_cash_out_from_its_settlement_date(self):
        self._settle(Decimal("3000"))
        self.assertEqual(self.advance.petty_cash_out_asof(self.settled),
                         Decimal("7000"))

    def test_nothing_is_out_before_the_advance_was_issued(self):
        self._settle(Decimal("3000"))
        self.assertEqual(
            self.advance.petty_cash_out_asof(self.issued - dt.timedelta(days=1)),
            Decimal("0"),
            "An advance counted against the box before it was issued.")

    def test_an_unsettled_return_is_not_credited_early(self):
        """A figure typed in with no settlement date has not reached the tin."""
        self.advance.returned_to_petty = Decimal("3000")
        self.advance.settled_on = None
        self.advance.save()
        self.assertEqual(self.advance.petty_cash_out_asof(self.today),
                         Decimal("10000"))

    def test_an_over_return_is_carried_rather_than_hidden(self):
        """The result is no longer clamped at zero.

        More returned than issued is a data error. The register would carry it
        into the running balance, so clamping here would put the two figures
        back out of step and bury the error in the one place it is most visible.
        """
        self._settle(Decimal("12000"))
        self.assertEqual(self.advance.petty_cash_out_asof(self.today),
                         Decimal("-2000"))

    def test_a_bank_funded_advance_never_touches_the_box(self):
        self.advance.from_petty_cash = False
        self.advance.save(update_fields=["from_petty_cash"])
        self.assertEqual(self.advance.petty_cash_out_asof(self.today), Decimal("0"))


class PettyCashRegisterAndCardAgreeOnAdvancesTests(TestCase):
    """The register's closing balance and the float card are one number.

    Checked through the page rather than the helper, because the fault was a
    disagreement *between* two call sites that were each self-consistent.
    """

    def setUp(self):
        self.user = _treasurer("tess-agree")
        self.fund = Department.objects.create(
            name="Local Church Budget", slug="lcb-agree",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.today = dt.date.today()
        # Twenty days back rather than the 1st of the month — the same anchor the
        # fixture above uses. The report is asked for `start`..`today`, and the
        # advance below is dated start+2 with a settlement at start+5, so
        # anchoring on the 1st put both in the FUTURE whenever the suite ran in
        # the first days of a month: the advance fell outside the window and the
        # float never moved. Green on the 20th, red on the 1st.
        self.start = self.today - dt.timedelta(days=20)
        PettyCashTopUp.objects.create(date=self.start, amount=Decimal("20000"),
                                      recorded_by=self.user)
        self.client = Client()
        self.client.force_login(self.user)

    def _closing_and_card(self):
        response = self.client.get(
            f"/petty-cash/?start={self.start}&end={self.today}")
        self.assertEqual(response.status_code, 200)
        return response.context["closing"], response.context["balance_now"]

    def _advance(self, returned=Decimal("0"), settled=True):
        advance = StaffAdvance.objects.create(
            staff_name="Peter Kamau", purpose="Supplies", department=self.fund,
            amount=Decimal("10000"), date_issued=self.start + dt.timedelta(days=2),
            from_petty_cash=True, issued_by=self.user,
            status=StaffAdvance.Status.ISSUED)
        if returned:
            advance.returned_to_petty = returned
            if settled:
                advance.settled_on = self.start + dt.timedelta(days=5)
                advance.status = StaffAdvance.Status.SETTLED
            advance.save()
        return advance

    def test_they_agree_with_an_outstanding_advance(self):
        self._advance()
        closing, card = self._closing_and_card()
        self.assertEqual(closing, card)

    def test_they_agree_after_unspent_cash_is_returned(self):
        self._advance(returned=Decimal("3000"))
        closing, card = self._closing_and_card()
        self.assertEqual(
            closing, card,
            "The register and the float card disagree once an advance has been "
            "partly returned.")

    def test_they_agree_when_the_whole_advance_comes_back(self):
        self._advance(returned=Decimal("10000"))
        closing, card = self._closing_and_card()
        self.assertEqual(closing, card)

    def test_an_issued_advance_actually_reduces_the_float(self):
        """Agreement is not enough if both figures ignore the advance."""
        before, _ = self._closing_and_card()
        self._advance()
        after, card = self._closing_and_card()
        self.assertEqual(after, before - Decimal("10000"))
        self.assertEqual(after, card)


class ConsolidationAppliesToTheWholeReportTests(TestCase):
    """"Consolidate sub-accounts" has to mean the same thing in every section.

    The income & expenditure statement listed revenue per fund with
    consolidation pinned off, so a treasurer who asked for consolidation still
    got every sub-account itemised there while the rest of the report rolled
    them up — one report disagreeing with itself about what a fund is.
    """

    def setUp(self):
        self.parent = Department.objects.create(
            name="Local Church Budget", slug="lcb-con",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.child = Department.objects.create(
            name="LCB Departments", slug="lcb-dept-con", parent=self.parent,
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        # The income list only names funds that actually received something, so
        # both funds need giving or consolidation has nothing to fold.
        from giving.models import Transaction
        for fund, ref in ((self.parent, "CON-P"), (self.child, "CON-C")):
            Transaction.objects.create(
                date=dt.date.today(), channel="CASH", direction="CREDIT",
                amount=Decimal("1000"), department=fund,
                allocation_status="MANUAL", confirmed=True, core_ref=ref)

    def _rows(self, consolidated):
        from core.reporting.context import ReportContext
        return ReportContext(start=None, end=None).fund_summary(
            consolidated=consolidated)

    def test_sub_accounts_are_not_their_own_rows_when_consolidated(self):
        rows = self._rows(True)
        self.assertFalse(
            [r for r in rows if r["department"].parent_id],
            "A sub-account is still shown as its own fund under consolidation.")

    def test_sub_accounts_are_their_own_rows_when_not_consolidated(self):
        names = [r["department"].name for r in self._rows(False)]
        self.assertIn(self.child.name, names)

    def test_consolidation_does_not_change_the_totals(self):
        """It moves receipts onto the parent's line; it must not lose any.

        This is the assertion that makes the change safe: presentation may
        differ, the money may not.
        """
        def total(rows):
            return sum((r["receipts"] or Decimal(0)) for r in rows)
        self.assertEqual(total(self._rows(True)), total(self._rows(False)))

    def test_the_income_statement_honours_the_filter(self):
        from core.reporting.context import ReportContext
        from reports.financial_statements import IncomeExpenditureStatementSection
        section = IncomeExpenditureStatementSection()
        ctx = ReportContext(start=None, end=None)
        rolled = section.render(ctx, {"consolidated": True})
        split = section.render(ctx, {"consolidated": False})

        # Consolidation folds each sub-account's receipts onto its parent's
        # line, so the statement lists fewer funds while saying the same thing.
        self.assertLess(
            len(rolled.rows), len(split.rows),
            "The income statement listed the same number of funds either way, "
            "so it is ignoring the consolidation filter.")
        self.assertEqual(
            rolled.total.cells if rolled.total else None,
            split.total.cells if split.total else None,
            "Consolidating changed the statement's totals — it must only "
            "regroup them.")


class NegativeFigurePreferenceTests(TestCase):
    """A reader can choose how a negative is written, and it applies everywhere.

    There was no such setting. `money_acct` existed and put negatives in
    parentheses, but it was applied template by template — 88 uses against 433
    plain ones in the report templates alone — so the accounting convention was
    a matter of which template you happened to be looking at.
    """

    def setUp(self):
        self.user = _treasurer("tess-neg")
        self.client = Client()
        self.client.force_login(self.user)

    def _money(self, style, value=Decimal("-1234.5")):
        from core import numberstyle
        from core.templatetags.treasury_extras import money
        token = numberstyle.set_negatives_style(style)
        try:
            return money(value)
        finally:
            numberstyle.reset_negatives_style(token)

    def test_minus_style_writes_a_minus(self):
        self.assertEqual(self._money("MINUS"), "-1,234.50")

    def test_parentheses_style_writes_parentheses(self):
        self.assertEqual(self._money("PARENS"), "(1,234.50)")

    def test_positive_figures_are_untouched_either_way(self):
        for style in ("MINUS", "PARENS"):
            self.assertEqual(self._money(style, Decimal("1234.5")), "1,234.50")

    def test_outside_a_request_it_falls_back_to_a_minus(self):
        """Management commands and jobs must not need to know about this."""
        from core.templatetags.treasury_extras import money
        self.assertEqual(money(Decimal("-99.5")), "-99.50")

    def test_money_acct_and_money_plain_ignore_the_preference(self):
        """The escape hatches, for figures that must not vary by reader."""
        from core import numberstyle
        from core.templatetags.treasury_extras import money_acct, money_plain
        for style in ("MINUS", "PARENS"):
            token = numberstyle.set_negatives_style(style)
            try:
                self.assertEqual(money_acct(Decimal("-1234.5")), "(1,234.50)")
                self.assertEqual(money_plain(Decimal("-1234.5")), "-1,234.50")
            finally:
                numberstyle.reset_negatives_style(token)

    def test_the_preferences_page_offers_the_choice(self):
        body = self.client.get("/preferences/").content.decode()
        self.assertIn('data-pref="negatives"', body)

    def test_the_choice_is_saved_and_applied(self):
        from core.models import UserPreference
        pref = UserPreference.get_for(self.user)
        pref.negatives = UserPreference.Negatives.PARENS
        pref.save(update_fields=["negatives"])
        self.assertEqual(
            UserPreference.get_for(self.user).negatives, "PARENS")

    def test_the_middleware_publishes_the_readers_choice(self):
        from core.models import UserPreference
        from core.numberstyle import negatives_style
        pref = UserPreference.get_for(self.user)
        pref.negatives = UserPreference.Negatives.PARENS
        pref.save(update_fields=["negatives"])
        seen = {}

        def probe(request):
            seen["style"] = negatives_style()
            from django.http import HttpResponse
            return HttpResponse("ok")

        from core.numberstyle import NumberStyleMiddleware
        from django.test import RequestFactory
        request = RequestFactory().get("/")
        request.user = self.user
        NumberStyleMiddleware(probe)(request)
        self.assertEqual(seen["style"], "PARENS")
