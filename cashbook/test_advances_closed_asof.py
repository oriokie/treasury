"""Closing a staff advance is a dated event, and the point-in-time advance
totals have to be read as at their own date.

`outstanding_bank_advances_total`, `outstanding_petty_advances_total` and
`outstanding_advances_total` each asked "is this advance closed *now*?" while
every other part of the same calculation — the top-ups counted, the settling
expenses counted, the cash returned to the tin — was asked as at `as_of`. So a
closed advance vanished from every historical figure the moment it was closed,
retrospectively, back through dates on which the money was demonstrably still
out.

That is not merely a report reading low. `_sync_managed_recon_items_inner`
recomputes a reconciliation worksheet's managed items on every ordinary page
load, and deletes an item whose recomputed amount is zero or less. A worksheet
prepared and balanced on 31 July, carrying a 5,000 advance, therefore lost that
line the first time anyone opened it after the advance was closed in August —
and a worksheet that had balanced was suddenly out by 5,000, with nothing on it
to say what had changed or when.

These tests pin the totals to `settled_on`, the date `AdvanceClose` stamps when
it sets the status to CLOSED, in the same way `_open_obligation_total` has
always judged payables and accruals.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from departments.models import Department

from .models import StaffAdvance
from .services.treasury_position import (outstanding_advances_total,
                                         outstanding_bank_advances_total,
                                         outstanding_petty_advances_total)

# The worksheet's date, and the date the advance was closed a fortnight later.
JULY_END = dt.date(2026, 7, 31)
CLOSED_IN_AUGUST = dt.date(2026, 8, 10)
AUGUST_END = dt.date(2026, 8, 31)


class AdvancesClosedLaterStillCountOnTheEarlierDateTests(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("tr_advasof", password="x")
        self.fund = Department.objects.create(
            name="AdvAsOfFund", fund_type="LOCAL", category="MINISTRY")

    def _advance(self, *, from_petty_cash, amount="5000"):
        return StaffAdvance.objects.create(
            staff_name="Trip holder", department=self.fund,
            amount=Decimal(amount), date_issued=dt.date(2026, 7, 10),
            purpose="camp meeting", method="CASH",
            from_petty_cash=from_petty_cash, issued_by=self.treasurer,
            status=StaffAdvance.Status.ISSUED)

    def _close(self, adv, on=CLOSED_IN_AUGUST):
        """Exactly what AdvanceClose.post() writes: the status and the date."""
        adv.status = StaffAdvance.Status.CLOSED
        adv.settled_on = on
        adv.save(update_fields=["status", "settled_on"])

    def test_a_bank_advance_closed_in_august_was_still_outstanding_in_july(self):
        adv = self._advance(from_petty_cash=False)
        self.assertEqual(outstanding_bank_advances_total(JULY_END), Decimal("5000"))
        self._close(adv)
        self.assertEqual(
            outstanding_bank_advances_total(JULY_END), Decimal("5000"),
            "Closing the advance in August rewrote the 31 July figure, and the "
            "reconciliation worksheet dated 31 July deleted its own line.")

    def test_that_same_advance_is_gone_once_the_date_reaches_the_closure(self):
        """The fix must not keep a closed advance alive for ever — only until
        the date it was actually closed on."""
        adv = self._advance(from_petty_cash=False)
        self._close(adv)
        self.assertEqual(outstanding_bank_advances_total(AUGUST_END), Decimal("0"))
        self.assertEqual(outstanding_advances_total(AUGUST_END), Decimal("0"))

    def test_a_petty_advance_closed_in_august_was_still_out_of_the_tin_in_july(self):
        adv = self._advance(from_petty_cash=True)
        self.assertEqual(outstanding_petty_advances_total(JULY_END), Decimal("5000"))
        self._close(adv)
        self.assertEqual(outstanding_petty_advances_total(JULY_END), Decimal("5000"))
        self.assertEqual(outstanding_petty_advances_total(AUGUST_END), Decimal("0"))

    def test_the_combined_total_reads_the_same_date_as_its_two_halves(self):
        """`outstanding_advances_total` is the receivable on the balance sheet
        and the dashboard, and it covers both halves — it must not disagree
        with them about which advances were open."""
        bank = self._advance(from_petty_cash=False, amount="5000")
        petty = self._advance(from_petty_cash=True, amount="1500")
        self._close(bank)
        self._close(petty)
        self.assertEqual(outstanding_advances_total(JULY_END), Decimal("6500"))
        self.assertEqual(
            outstanding_advances_total(JULY_END),
            outstanding_bank_advances_total(JULY_END)
            + outstanding_petty_advances_total(JULY_END))

    def test_cash_returned_to_the_tin_on_closure_is_not_backdated_either(self):
        """The return is credited to the box on `settled_on`, so on the earlier
        date the whole advance is still out — the same date the closure itself
        is judged on. If the two were read differently the figure would be
        neither the July balance nor the August one."""
        adv = self._advance(from_petty_cash=True, amount="5000")
        adv.returned_to_petty = Decimal("2000")
        adv.save(update_fields=["returned_to_petty"])
        self._close(adv)
        self.assertEqual(outstanding_petty_advances_total(JULY_END), Decimal("5000"))

    def test_an_advance_closed_with_no_settlement_date_is_closed_at_every_date(self):
        """A deliberate choice, not an oversight: a CLOSED row with no
        `settled_on` has no date to test, and reading it as still open would
        resurrect a receivable the treasurer has already retired — on every
        report, for ever. The flag is the only evidence there is, so it is
        trusted, exactly as `_open_obligation_total` trusts a settled payable
        with no payments recorded against it."""
        adv = self._advance(from_petty_cash=False)
        adv.status = StaffAdvance.Status.CLOSED
        adv.save(update_fields=["status"])
        self.assertIsNone(adv.settled_on)
        self.assertEqual(outstanding_bank_advances_total(JULY_END), Decimal("0"))
        self.assertEqual(outstanding_advances_total(JULY_END), Decimal("0"))
        petty = self._advance(from_petty_cash=True)
        petty.status = StaffAdvance.Status.CLOSED
        petty.save(update_fields=["status"])
        self.assertEqual(outstanding_petty_advances_total(JULY_END), Decimal("0"))

    def test_asking_for_today_still_means_today(self):
        """Called with no date the three totals answer "right now", and an
        advance closed today is closed."""
        today = dt.date.today()
        adv = StaffAdvance.objects.create(
            staff_name="Closed today", department=self.fund,
            amount=Decimal("800"), date_issued=today - dt.timedelta(days=3),
            purpose="stationery", method="CASH", from_petty_cash=False,
            issued_by=self.treasurer, status=StaffAdvance.Status.ISSUED)
        self.assertEqual(outstanding_advances_total(), Decimal("800"))
        self._close(adv, on=today)
        self.assertEqual(outstanding_advances_total(), Decimal("0"))
        self.assertEqual(outstanding_bank_advances_total(), Decimal("0"))
