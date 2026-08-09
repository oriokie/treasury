"""A month-end checklist must be judged on the month, not on today.

The advances item in `period_close_checklist` was a FOURTH private copy of
"which staff advances were still open" — after the three in
`cashbook.services.treasury_position`, which were fixed to read the closure
DATE rather than today's status. This copy was missed, and carried the identical
fault twice over: `.exclude(status=CLOSED)` reads the status the row has now,
and `StaffAdvance.balance` is a property over current totals with no as-of at
all. So an advance genuinely outstanding on 31 July vanished from July's
checklist the moment it was closed in August, and July could pass its own close
review on the strength of something done after July ended.

Both halves are tested here, because they fail independently: one is the status,
the other is the money.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from cashbook.models import Expense, StaffAdvance
from core.services.period_close import period_close_checklist
from departments.models import Department

JUL31 = dt.date(2026, 7, 31)


def _advances_item(year, month):
    for item in period_close_checklist(year, month):
        if item["key"] == "advances":
            return item
    raise AssertionError("the checklist has no 'advances' item")


class AdvancesAreJudgedAsAtTheMonthEndTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user("pcadv", password="pc-adv-pass-1")
        self.fund = Department.objects.create(
            name="Building", slug="pc-building",
            fund_type=Department.FundType.LOCAL)
        self.advance = StaffAdvance.objects.create(
            staff_name="J. Mwangi", amount=Decimal("5000"),
            date_issued=dt.date(2026, 7, 15), department=self.fund,
            purpose="Conference travel", issued_by=self.user)

    def test_an_advance_open_at_month_end_is_flagged_for_that_month(self):
        self.assertFalse(_advances_item(2026, 7)["ok"])

    def test_closing_it_in_august_does_not_clear_it_from_july(self):
        """The regression. Closing is a DATED event, so it belongs to the month
        it happened in — clearing July retrospectively would let a month pass
        its close review because of something done after the month ended."""
        self.advance.status = StaffAdvance.Status.CLOSED
        self.advance.settled_on = dt.date(2026, 8, 10)
        self.advance.save(update_fields=["status", "settled_on"])

        self.assertFalse(_advances_item(2026, 7)["ok"],
                         "July's checklist cleared itself because the advance "
                         "was closed in August")
        self.assertTrue(_advances_item(2026, 8)["ok"],
                        "August, where the closure actually happened, should be "
                        "clear")

    def test_a_receipt_dated_in_august_does_not_settle_july_either(self):
        """The second half, which fails on its own: the advance stays OPEN, but
        the money accounting for it arrives later. `balance` is a property over
        current totals, so an August receipt would have retired the July
        balance and cleared July's checklist by the back door."""
        Expense.objects.create(
            date=dt.date(2026, 8, 5), department=self.fund,
            description="Conference travel receipts", amount=Decimal("5000"),
            status=Expense.Status.PAID, recorded_by=self.user,
            advance=self.advance)

        self.assertFalse(_advances_item(2026, 7)["ok"],
                         "an August receipt settled the advance as at 31 July")

    def test_an_advance_genuinely_settled_within_the_month_does_clear_it(self):
        """The rule must still be able to say yes, or it is not a check."""
        Expense.objects.create(
            date=dt.date(2026, 7, 20), department=self.fund,
            description="Conference travel receipts", amount=Decimal("5000"),
            status=Expense.Status.PAID, recorded_by=self.user,
            advance=self.advance)

        self.assertTrue(_advances_item(2026, 7)["ok"])

    def test_the_checklist_reads_the_one_shared_rule(self):
        """A guard on the generalisation, not the behaviour. This item was a
        fourth copy of a rule that already existed in one correct place; the
        copies are what let three of them be fixed and this one missed."""
        import inspect
        from core.services import period_close
        source = inspect.getsource(period_close.period_close_checklist)
        self.assertIn("advances_open_asof", source,
                      "the advances item must call the shared date-aware rule "
                      "in cashbook.services.treasury_position, not re-derive it")
        self.assertNotIn("exclude(status=StaffAdvance.Status.CLOSED)", source,
                         "reading today's status is the fault this closed")
