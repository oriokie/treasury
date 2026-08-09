"""A closed fund that still holds money must keep its line on the fund summary.

`Department.status` is a gate on NEW money, not on reporting: the close screen
tells the treasurer in as many words that a closed account "stays in historical
reports but won't accept new transactions". `_department_summary_impl` used to
build its rows from `Department.objects.filter(active=True)`, which is exactly
the opposite — a closed fund and every shilling in it vanished from the master
report, with no row and not even a zero.

That stayed invisible because the close gate refuses a fund with a balance, so
a closed fund normally holds nothing. Issue #63's fix opens the one door
through which it can: an APPROVED envelope batch is deliberately allowed to
post into a fund closed after approval, because the money was given while the
fund was open and the cash is already counted and receipted. A fund closed in
that window ends the day holding money the Collections Summary reports and the
fund summary did not — 23,700 on one, 22,950 on the other, of one Sabbath.

These tests pin both halves of the rule: a closed fund with something to show
gets its row, and a closed fund with nothing to show still does not. Without
the second half the fix would trade one wrong report for a hundred cluttered
ones, which is what the `active` filter was really guarding against.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from cashbook.models import Expense, FundTransfer
from departments.models import Department
from giving.models import Transaction
from reports.services import balances

D = Decimal
JULY_START = dt.date(2026, 7, 1)
JULY_END = dt.date(2026, 7, 31)
SABBATH = dt.date(2026, 7, 11)


def _rows(start=JULY_START, end=JULY_END, consolidated=True):
    return balances.department_summary(start, end, consolidated)


def _by_id(start=JULY_START, end=JULY_END, consolidated=True):
    return {r["department"].id: r for r in _rows(start, end, consolidated)}


def _treasurer(username):
    u = User.objects.create_user(username, password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


def _receipt(dept, amount, date=SABBATH):
    """A confirmed envelope credit — the shape envelope posting writes."""
    return Transaction.objects.create(
        date=date, amount=D(amount), direction="CREDIT", confirmed=True,
        channel="ENVELOPE", allocation_status="MANUAL", department=dept)


def _close(dept):
    """Close a fund the way `CloseAccountView` does: `status` is authoritative
    and `Department.save` derives `active` from it. Setting `active` directly
    would test a state the application cannot actually produce."""
    dept.status = Department.Status.CLOSED
    dept.save()
    dept.refresh_from_db()
    assert dept.active is False
    return dept


class ClosedFundWithMoneyTests(TestCase):
    """The #63 window: money reaches a fund that is already closed."""

    def setUp(self):
        self.camp = Department.objects.create(
            name="Camp Meeting", slug="cf-camp",
            fund_type=Department.FundType.TRUST,
            category=Department.Category.TRUST)
        self.tithe = Department.objects.create(
            name="Tithe (closed-fund tests)", slug="cf-tithe",
            fund_type=Department.FundType.TRUST,
            category=Department.Category.TRUST)
        _receipt(self.camp, "750")
        _receipt(self.tithe, "20500")
        _close(self.camp)

    def test_a_closed_fund_holding_money_keeps_its_row(self):
        row = _by_id().get(self.camp.id)
        self.assertIsNotNone(
            row, "a closed fund holding 750 has no row on the fund summary — "
                 "money in one report and absent from another is the defect")
        self.assertEqual(row["receipts"], D("750"))
        self.assertEqual(row["closing"], D("750"))

    def test_it_keeps_its_row_unconsolidated_too(self):
        row = _by_id(consolidated=False).get(self.camp.id)
        self.assertIsNotNone(row, "the unconsolidated fund summary dropped a "
                                  "closed fund that is holding money")
        self.assertEqual(row["closing"], D("750"))

    def test_the_period_receipts_total_carries_the_closed_fund(self):
        """The figure the Collections Summary is checked against. It read
        20,500 against a true 21,250 while the closed fund had no row."""
        self.assertEqual(balances.totals(_rows())["receipts"], D("21250"))

    def test_the_closing_total_carries_it_too(self):
        """`current_cash_position` and the Statement of Financial Position are
        both the sum of these closings; cash sitting in a closed fund is still
        cash, and the bank statement will say so."""
        self.assertEqual(
            balances.totals(_rows(None, JULY_END))["closing"], D("21250"))

    def test_a_balance_carried_in_with_no_movement_still_shows(self):
        """August: nothing happens to the fund at all, but it is still holding
        July's 750 and a position statement that omits it is short by 750."""
        row = _by_id(dt.date(2026, 8, 1), dt.date(2026, 8, 31)).get(self.camp.id)
        self.assertIsNotNone(row, "a closed fund with a brought-forward "
                                  "balance and no movement lost its row")
        self.assertEqual(row["opening"], D("750"))
        self.assertEqual(row["receipts"], D("0"))
        self.assertEqual(row["closing"], D("750"))

    def test_movement_that_nets_to_nothing_still_shows(self):
        """Received and then transferred out inside the same period: closing is
        zero, but 750 moved through the fund and this row is the only place the
        report says where it went."""
        FundTransfer.objects.create(
            date=dt.date(2026, 7, 20), source=self.camp, destination=self.tithe,
            amount=D("750"), reason="Camp meeting is over for the year.",
            recorded_by=_treasurer("cf_tr_move"))
        row = _by_id().get(self.camp.id)
        self.assertIsNotNone(row, "a closed fund whose money moved out in the "
                                  "period lost the row that shows it moving")
        self.assertEqual(row["closing"], D("0"))
        self.assertEqual(row["transfers_out"], D("750"))

    def test_ordering_is_still_the_register_ordering(self):
        """Rows come back in `Department.Meta.ordering` (fund type, then name).
        A closed fund takes its place in that order rather than being tacked on
        the end, so every report and export keeps the shape treasurers read."""
        rows = _rows()
        ids = [r["department"].id for r in rows]
        self.assertEqual(
            ids, list(Department.objects.filter(id__in=ids)
                      .values_list("id", flat=True)))
        self.assertIn(self.camp.id, ids)


class ClosedFundWithNothingTests(TestCase):
    """The other half: closing a fund must still tidy it off the reports.

    The close gate only lets a fund close at zero, so this is the ordinary case
    by far — every fund a church has ever retired.
    """

    def setUp(self):
        self.live = Department.objects.create(
            name="Combined Offering (empty-close tests)", slug="cfe-live",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.OFFERING)
        _receipt(self.live, "2450")
        self.retired = _close(Department.objects.create(
            name="Youth Camp 2019", slug="cfe-retired",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.OFFERING))

    def test_a_closed_empty_fund_gets_no_row(self):
        self.assertNotIn(self.retired.id, _by_id())
        self.assertNotIn(self.retired.id, _by_id(consolidated=False))

    def test_a_closed_empty_fund_gets_no_row_over_all_time(self):
        self.assertNotIn(self.retired.id, _by_id(None, None))

    def test_the_open_fund_is_untouched(self):
        self.assertEqual(_by_id()[self.live.id]["closing"], D("2450"))

    def test_an_open_fund_with_nothing_in_it_still_gets_its_row(self):
        """Being open is reason enough on its own — a fund opened before its
        first Sabbath must appear, or the treasurer cannot see it exists."""
        fresh = Department.objects.create(
            name="Zzz New Fund", slug="cfe-new",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.OFFERING)
        self.assertIn(fresh.id, _by_id())


class ClosedSubAccountTests(TestCase):
    """Closing a parent closes its sub-accounts, so the #63 window lands on a
    sub just as readily — and a sub is only ever printed inside its parent's
    row. Dropping the empty parent takes the sub's money with it, hiding it as
    completely as filtering the sub out directly."""

    def setUp(self):
        self.parent = Department.objects.create(
            name="Youth", slug="cfs-parent",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.DEVELOPMENT)
        self.sub = Department.objects.create(
            name="Potluck", slug="cfs-sub", parent=self.parent,
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.DEVELOPMENT)
        _receipt(self.sub, "1200")
        _close(self.sub)
        _close(self.parent)

    def test_the_parent_row_carries_a_closed_subs_money(self):
        row = _by_id().get(self.parent.id)
        self.assertIsNotNone(
            row, "a closed sub-account's 1,200 has nowhere to be reported: "
                 "its parent, empty in its own right, was dropped too")
        self.assertEqual(row["closing"], D("1200"))
        self.assertEqual([c["department"].id for c in row["children"]],
                         [self.sub.id])

    def test_the_consolidated_total_carries_it(self):
        self.assertEqual(balances.totals(_rows())["closing"], D("1200"))

    def test_the_empty_parent_is_not_a_row_of_its_own_unconsolidated(self):
        """Unconsolidated, every fund prints its own line, so the parent is not
        needed to carry anything and an empty closed one would be pure noise."""
        rows = _by_id(consolidated=False)
        self.assertIn(self.sub.id, rows)
        self.assertNotIn(self.parent.id, rows)


class ClosedFundLeftOverdrawnTests(TestCase):
    """A closed fund can also be left on the wrong side of zero — a final
    invoice approved after the close. Same rule: it has something to say, so it
    says it, rather than the loss quietly leaving the report."""

    def setUp(self):
        self.user = _treasurer("cf_tr_exp")
        self.fund = _close(Department.objects.create(
            name="Building Project (wound up)", slug="cfx-fund",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.DEVELOPMENT))
        Expense.objects.create(
            date=SABBATH, department=self.fund, description="Final invoice",
            amount=D("300"), category="OTHER", status="PAID",
            recorded_by=self.user, approved_by=self.user)

    def test_an_overdrawn_closed_fund_is_reported(self):
        row = _by_id().get(self.fund.id)
        self.assertIsNotNone(row, "a closed fund carrying an approved expense "
                                  "is overdrawn and invisible")
        self.assertEqual(row["expenses"], D("300"))
        self.assertEqual(row["closing"], D("-300"))
