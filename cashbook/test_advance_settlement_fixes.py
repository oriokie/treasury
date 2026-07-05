"""Follow-up fixes to the staff-advance / cash-count reconciliation:

1. StaffAdvance.petty_outstanding_asof() never subtracted expenses already
   settled against the advance — it always returned the full amount ever
   disbursed, so the bank reconciliation's "Staff advances from petty cash
   (not yet accounted)" line never decreased no matter how much had actually
   been accounted for.
2. When someone accounts for a cash-tracked advance (settlement expense,
   Expense.advance set), the Sabbath cash count's "Cash Disbursed" wrongly
   counted it as a brand-new outflow from THIS float — even though the real
   cash movement happened back when the advance was issued, possibly from a
   different float entirely (petty cash), or was already the settlement of
   money that left this same float earlier, not today. Fixed by excluding
   any advance-settlement expense from "Cash Disbursed", and instead adding
   back the advance's own issuance (only when cash, only when NOT from petty
   cash — i.e. only when it really did come out of this float) at the point
   in time it actually happened."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase
from django.contrib.auth.models import User
from departments.models import Department
from cashbook.models import StaffAdvance
from cashbook.views import _record_advance_expense, outstanding_petty_advances_total
from envelopes.views import CountSessionCreate


class PettyAdvanceOutstandingSubtractsSettledTests(TestCase):
    def setUp(self):
        self.tr = User.objects.create_user("tr_advfix", password="x")
        self.d = Department.objects.create(name="AdvFixFund", fund_type="LOCAL",
            category="MINISTRY")

    def test_outstanding_decreases_as_expenses_are_settled(self):
        adv = StaffAdvance.objects.create(staff_name="Petty Settle", department=self.d,
            amount=Decimal("5000"), date_issued=dt.date(2026, 6, 1), purpose="trip",
            method="CASH", from_petty_cash=True, issued_by=self.tr, status="ISSUED")
        self.assertEqual(outstanding_petty_advances_total(dt.date(2026, 6, 30)),
                         Decimal("5000"))
        _record_advance_expense(adv, date=dt.date(2026, 6, 5), desc="spend1",
            amount=Decimal("2000"), category="OTHER", user=self.tr)
        self.assertEqual(outstanding_petty_advances_total(dt.date(2026, 6, 30)),
                         Decimal("3000"))
        _record_advance_expense(adv, date=dt.date(2026, 6, 10), desc="spend2",
            amount=Decimal("3000"), category="OTHER", user=self.tr)
        self.assertEqual(outstanding_petty_advances_total(dt.date(2026, 6, 30)),
                         Decimal("0"))

    def test_settlement_after_the_as_of_date_does_not_count_yet(self):
        adv = StaffAdvance.objects.create(staff_name="Late Settle", department=self.d,
            amount=Decimal("2000"), date_issued=dt.date(2026, 6, 1), purpose="trip",
            method="CASH", from_petty_cash=True, issued_by=self.tr, status="ISSUED")
        _record_advance_expense(adv, date=dt.date(2026, 7, 15), desc="late spend",
            amount=Decimal("2000"), category="OTHER", user=self.tr)
        self.assertEqual(outstanding_petty_advances_total(dt.date(2026, 6, 30)),
                         Decimal("2000"))
        self.assertEqual(outstanding_petty_advances_total(dt.date(2026, 7, 31)),
                         Decimal("0"))

    def test_closed_advances_excluded(self):
        adv = StaffAdvance.objects.create(staff_name="Closed Adv", department=self.d,
            amount=Decimal("1000"), date_issued=dt.date(2026, 6, 1), purpose="trip",
            method="CASH", from_petty_cash=True, issued_by=self.tr, status="CLOSED")
        self.assertEqual(outstanding_petty_advances_total(dt.date(2026, 6, 30)),
                         Decimal("0"))

    def test_settling_an_advance_does_not_change_the_petty_float_balance(self):
        """Regression guard: petty_outstanding_asof() (used for the "not yet
        accounted for" reconciliation figure) and petty_cash_out_asof() (used
        for the petty float's own running balance) must stay independent.
        Settling an advance via an expense is a paperwork reclassification,
        never a cash movement — the float's balance must not increase just
        because an advance was accounted for."""
        from cashbook.views import _petty_balance_asof
        adv = StaffAdvance.objects.create(staff_name="Float Stable", department=self.d,
            amount=Decimal("5000"), date_issued=dt.date(2026, 6, 1), purpose="trip",
            method="CASH", from_petty_cash=True, issued_by=self.tr, status="ISSUED")
        float_before = _petty_balance_asof(dt.date(2026, 6, 30))
        _record_advance_expense(adv, date=dt.date(2026, 6, 10), desc="settle",
            amount=Decimal("5000"), category="OTHER", user=self.tr)
        float_after = _petty_balance_asof(dt.date(2026, 6, 30))
        self.assertEqual(float_before, float_after)
        # but the "not yet accounted for" figure DOES correctly drop to zero
        self.assertEqual(outstanding_petty_advances_total(dt.date(2026, 6, 30)),
                         Decimal("0"))


class CashCountAdvanceSettlementExclusionTests(TestCase):
    def setUp(self):
        self.tr = User.objects.create_user("tr_cashadvfix", password="x")
        self.d = Department.objects.create(name="CashAdvFixFund", fund_type="LOCAL",
            category="MINISTRY")
        self.view = CountSessionCreate()

    def test_settlement_of_petty_cash_advance_not_double_counted(self):
        adv = StaffAdvance.objects.create(staff_name="Petty Given", department=self.d,
            amount=Decimal("4000"), date_issued=dt.date(2026, 5, 1), purpose="trip",
            method="CASH", from_petty_cash=True, issued_by=self.tr, status="ISSUED")
        _record_advance_expense(adv, date=dt.date(2026, 6, 5), desc="settle",
            amount=Decimal("2500"), category="OTHER", user=self.tr)
        b = self.view._breakdown(dt.date(2026, 6, 6))
        # the settlement must not appear as a fresh disbursement — it never
        # touched THIS float; the petty cash tin already accounted for it
        self.assertEqual(b["disbursed"], Decimal("0"))

    def test_advance_issued_directly_from_this_float_counted_at_issuance(self):
        adv = StaffAdvance.objects.create(staff_name="Float Given", department=self.d,
            amount=Decimal("3000"), date_issued=dt.date(2026, 6, 2), purpose="trip",
            method="CASH", from_petty_cash=False, issued_by=self.tr, status="ISSUED")
        b = self.view._breakdown(dt.date(2026, 6, 6))
        self.assertEqual(b["disbursed"], Decimal("3000"))

    def test_settlement_of_float_advance_not_counted_a_second_time(self):
        adv = StaffAdvance.objects.create(staff_name="Float Given2", department=self.d,
            amount=Decimal("3000"), date_issued=dt.date(2026, 6, 2), purpose="trip",
            method="CASH", from_petty_cash=False, issued_by=self.tr, status="ISSUED")
        _record_advance_expense(adv, date=dt.date(2026, 6, 5), desc="settle",
            amount=Decimal("1000"), category="OTHER", user=self.tr)
        b = self.view._breakdown(dt.date(2026, 6, 6))
        # counted once (3000, at issuance) — the 1000 settlement is not added again
        self.assertEqual(b["disbursed"], Decimal("3000"))

    def test_ordinary_cash_expense_unaffected(self):
        from cashbook.models import Expense
        Expense.objects.create(date=dt.date(2026, 6, 3), department=self.d,
            description="normal", amount=Decimal("500"), category="OTHER",
            method="CASH", status="PAID", recorded_by=self.tr, approved_by=self.tr)
        b = self.view._breakdown(dt.date(2026, 6, 6))
        self.assertEqual(b["disbursed"], Decimal("500"))
