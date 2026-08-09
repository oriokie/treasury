"""A staff advance, from the cash leaving the treasurer's hands to the day it is
closed — walked twice, once out of the bank and once out of the petty-cash tin.

An advance is the one thing in this book that is money the church still owns
while it is in somebody else's pocket. That makes it the process where the
seams matter most, and both of the seams the audit found live here:

* **An outstanding-advance figure is a POINT IN TIME, not a status.** The July
  worksheet says what was out on 31 July. Closing the advance on 10 August must
  not reach back and rewrite that. It did: the three totals asked "is this
  closed *now*?" while every other term in the same subtraction was asked as at
  the reporting date, and because the reconciliation worksheet re-syncs its
  managed lines on every ordinary page load, the first person to open the July
  worksheet in September silently deleted a line that had balanced. This suite
  walks that: issue in July, close today, re-open the July worksheet.
* **A petty-cash advance is a movement of physical cash.** The float has to
  drop when the money leaves the tin, must NOT move when a receipt is later
  filed against it (that is paperwork, not cash), must drop again on a top-up,
  and must rise only when notes physically come back. Three different parts of
  the app answer "what is in the box" — the float service, the petty-cash
  register page, and whoever counts it — and they must all say the same number
  at every stage.

Dates here are anchored to the real `date.today()` rather than to the harness's
fixed TODAY, because `AdvanceClose` stamps `settled_on = date.today()` itself.
The closure date is therefore not ours to choose, and the only way to write "the
advance was open at the reporting date and was closed after it" without pinning
the system clock is to place the reporting date in the past relative to today.
`MONTH_END` below is that worksheet date — "31 July" in the story above.
"""
import datetime as dt
from decimal import Decimal

from django.urls import reverse

from cashbook.models import AdvanceTopUp, Expense, StaffAdvance
from cashbook.services import treasury_position as tp

from .base import BusinessWorkflowTest

#: The day the treasurer is sitting at the desk closing things off — and, because
#: `AdvanceClose` stamps it itself, the date every closure in this file carries.
CLOSING_DAY = dt.date.today()
#: The reporting date of a worksheet that was prepared, and balanced, before any
#: of that happened. "31 July" in the module docstring.
MONTH_END = CLOSING_DAY - dt.timedelta(days=9)
#: The advance's own timeline, all of it inside the period the worksheet covers.
FLOAT_FUNDED = MONTH_END - dt.timedelta(days=26)
OPENING_GIFT = MONTH_END - dt.timedelta(days=27)
ISSUED_ON = MONTH_END - dt.timedelta(days=21)
FIRST_RECEIPTS = MONTH_END - dt.timedelta(days=14)
TOPPED_UP = MONTH_END - dt.timedelta(days=10)
LAST_RECEIPTS = MONTH_END - dt.timedelta(days=3)
PERIOD_START = FLOAT_FUNDED - dt.timedelta(days=2)


def money(value):
    """A figure at two decimal places.

    `assert_agree` compares `str(Decimal(v))`, so an aggregate that came back as
    `60000` and a model field that came back as `60000.00` are reported as a
    disagreement when they are the same money. Every figure this file hands to
    it goes through here first, so a failure means the AMOUNTS differ and not
    merely the scale they were stored at.
    """
    return Decimal(value).quantize(Decimal("0.01"))


class StaffAdvanceLifecycle(BusinessWorkflowTest):
    """Everything below starts from a church that already has money and a
    treasurer, and NOTHING else — no advance, no petty float, no expenses. Each
    of those is created by the workflow, through the app, in the order a human
    would do it.
    """

    def setUp(self):
        super().setUp()
        self.office = self.acting_as(self.treasurer)

        # The fund has to hold something before an advance drawn on it can be
        # accounted for. It gets there the way it really does — a receipt.
        # (Fixture setup for state the workflow is NOT under test to create.)
        from giving.models import Transaction
        Transaction.objects.create(
            date=OPENING_GIFT, amount=Decimal("300000"), direction="CREDIT",
            channel="BANK", confirmed=True, allocation_status="MANUAL",
            department=self.local_fund, payer_name="OPENING GIFT")
        # A second fund with real movement on it, so no figure in this file is
        # ever the only row in its table (failure #125: the page that rendered
        # only on an empty record).
        Transaction.objects.create(
            date=OPENING_GIFT, amount=Decimal("64000"), direction="CREDIT",
            channel="BANK", confirmed=True, allocation_status="MANUAL",
            department=self.trust_fund, payer_name="TITHE — SABBATH 1")

    # -- private helpers (this file only; nothing here belongs in base.py yet) --

    def _issue(self, client, **over):
        """Fill in the 'New advance' form the way the treasurer does."""
        data = {
            "staff_name": "Pastor Otieno", "department": self.local_fund.id,
            "amount": "40000", "date_issued": ISSUED_ON.isoformat(),
            "purpose": "Camp meeting — venue, transport and per diem",
            "method": "BANK", "reference": "FT-77120", "bank_charge": "0",
        }
        data.update(over)
        self.submit(client, "advance_new", data)
        return StaffAdvance.objects.get(staff_name=data["staff_name"])

    def _account_for(self, client, adv, *, date, desc, amount, category="OTHER",
                     charge="0"):
        """Hand in a receipt against the advance, through the form on its page."""
        return self.submit(client, "advance_add_expense", {
            "date": date.isoformat(), "description": desc, "amount": str(amount),
            "category": category, "charge": str(charge)}, args=[adv.pk])

    def _petty_float(self, as_of):
        """What the float service says is in the tin as at a date."""
        return tp.petty_balance_asof(as_of)

    def _register_closing(self, client, start, end):
        """What the petty-cash REGISTER page says the tin closed at — a second,
        independently-assembled answer to the same question, built by running a
        balance forward through the movements it lists rather than by the
        aggregate the float card reads."""
        page = self.visit(client, "petty_cash",
                          query=f"?start={start.isoformat()}&end={end.isoformat()}")
        return Decimal(page.context["closing"])

    def _advance_page_still_to_account(self, client, adv):
        """The bottom line of the running statement on the advance's own page —
        the figure a treasurer reads, assembled event-by-event rather than from
        the model's `balance` property."""
        page = self.visit(client, "advance_detail", args=[adv.pk])
        return Decimal(page.context["to_account"])

    # =====================================================================
    # 1. THE BANK-FUNDED ADVANCE, ISSUE TO CLOSE
    # =====================================================================

    def test_a_bank_advance_is_issued_accounted_topped_up_and_closed(self):
        # 1. The treasurer issues 40,000 to a pastor for camp meeting, by bank
        #    transfer, and the bank takes 150 to send it.
        adv = self._issue(self.office, bank_charge="150")
        self.assertEqual(adv.status, StaffAdvance.Status.ISSUED)
        self.assertEqual(adv.amount, Decimal("40000"))

        # 2. Issuing an advance is NOT spending. The 40,000 is still the
        #    church's money — it has only moved from the bank into a receivable
        #    — so the fund must be lighter by the 150 sending charge and by
        #    nothing else. This is the assertion that separates "advanced" from
        #    "spent", and getting it wrong writes off the money on day one.
        self.assert_fund_balance(self.local_fund, Decimal("299850"), as_of=ISSUED_ON)
        self.assertEqual(tp.outstanding_advances_total(ISSUED_ON), Decimal("40000"))
        self.assertEqual(tp.outstanding_bank_advances_total(ISSUED_ON),
                         Decimal("40000"))
        self.assertEqual(tp.outstanding_petty_advances_total(ISSUED_ON), Decimal("0"))

        # 3. The pastor comes back with receipts. One clean, one that cost him a
        #    60/= M-Pesa fee to pay — the fee came out of the advance too.
        self._account_for(self.office, adv, date=FIRST_RECEIPTS,
                          desc="Venue hire — camp ground", amount="12000")
        self._account_for(self.office, adv, date=FIRST_RECEIPTS,
                          desc="Bus hire to the camp", amount="8000",
                          category="TRANSPORT", charge="60")
        adv.refresh_from_db()
        self.assertEqual(adv.status, StaffAdvance.Status.PARTLY)
        self.assertEqual(adv.settled_total, Decimal("20060"))
        self.assertEqual(adv.balance, Decimal("19940"))
        self.assertEqual(tp.outstanding_advances_total(FIRST_RECEIPTS),
                         Decimal("19940"))

        # 4. The camp runs long and he needs more. The treasurer tops the same
        #    advance up rather than issuing a second one, and the bank takes
        #    another 100 to send it.
        self.submit(self.office, "advance_topup", {
            "date": TOPPED_UP.isoformat(), "amount": "15000", "charge": "100",
            "note": "extra nights"}, args=[adv.pk])
        adv.refresh_from_db()
        self.assertEqual(adv.amount, Decimal("55000"),
                         "the top-up must join the advance, not start a new one")
        # The 100 is the CHURCH's cost of sending the money, not the pastor's
        # spending. It must hit the fund and must NOT be added to what he has to
        # account for — the two are one keystroke apart in the code and only a
        # figure can tell them apart.
        self.assertEqual(adv.balance, Decimal("34940"))
        self.assertEqual(
            Expense.objects.filter(category=Expense.Category.BANK_CHARGE,
                                   advance__isnull=True).count(), 2,
            "the issue charge and the top-up charge are the church's own cost "
            "and must not be linked to the advance")

        # 5. He accounts for the rest, to the shilling.
        self._account_for(self.office, adv, date=LAST_RECEIPTS,
                          desc="Accommodation and per diem", amount="34940",
                          category="ALLOWANCE")
        adv.refresh_from_db()
        self.assertEqual(adv.balance, Decimal("0"))
        self.assertEqual(adv.status, StaffAdvance.Status.SETTLED,
                         "an advance accounted for in full should read as settled")
        self.assertEqual(tp.outstanding_advances_total(LAST_RECEIPTS), Decimal("0"),
                         "nothing is owed once every shilling is accounted for")

        # 6. The treasurer closes it.
        self.submit(self.office, "advance_close",
                    {"note": "Fully accounted; file closed."}, args=[adv.pk])
        adv.refresh_from_db()
        self.assertEqual(adv.status, StaffAdvance.Status.CLOSED)
        self.assertEqual(adv.settled_on, CLOSING_DAY)

        # 7. THE MONEY. 55,000 advanced and accounted for, plus 250 of sending
        #    charges the church itself bore. Nothing else may have moved.
        self.assert_fund_balance(self.local_fund, Decimal("244750"),
                                 as_of=CLOSING_DAY)
        self.assert_fund_balance(self.trust_fund, Decimal("64000"),
                                 as_of=CLOSING_DAY)
        self.assert_books_balance("after a bank advance was issued and retired")
        self.assert_trial_balance_balances()

        # 8. And the same total, read the three ways the app offers it.
        self.assert_agree(
            "what the pastor still has to account for, at the close",
            model_balance=money(adv.balance),
            advance_page_statement=money(self._advance_page_still_to_account(
                self.office, adv)),
            amount_less_what_was_accounted=money(
                adv.amount - adv.accounted_total),
        )

    def test_the_advance_charge_is_the_churchs_cost_not_the_holders(self):
        """The sending charge is booked against the fund but never against the
        person. Two figures, one keystroke apart, that must not be the same."""
        adv = self._issue(self.office, bank_charge="150")
        self.submit(self.office, "advance_topup", {
            "date": TOPPED_UP.isoformat(), "amount": "5000", "charge": "100"},
            args=[adv.pk])
        adv.refresh_from_db()

        # what the holder owes: the cash he was given, nothing else
        self.assertEqual(adv.balance, Decimal("45000"))
        # what the fund paid: only the two charges, because nothing has been
        # spent yet
        self.assert_fund_balance(self.local_fund, Decimal("299750"),
                                 as_of=CLOSING_DAY)
        self.assert_agree(
            "the sending charges, read off the fund and off the advance",
            fund_movement_since_the_gift=money(
                Decimal("300000") - Decimal("299750")),
            issue_charge_plus_topup_charge=money(
                adv.bank_charge + AdvanceTopUp.objects.get(advance=adv).charge),
        )
        self.assert_books_balance("after two sending charges")

    # =====================================================================
    # 2. THE FIGURE AS AT A DATE — the July worksheet re-read in September
    # =====================================================================

    def test_closing_in_august_does_not_rewrite_the_july_outstanding_figure(self):
        # 1. An advance goes out in July and is only partly accounted for.
        adv = self._issue(self.office, amount="25000")
        self._account_for(self.office, adv, date=FIRST_RECEIPTS,
                          desc="Fuel for the outreach van", amount="9000",
                          category="TRANSPORT")

        # 2. At the month end the treasurer reads the outstanding figure. 16,000
        #    is out with the holder, and the three totals agree about it.
        self.assert_agree(
            "outstanding advances at the month end",
            combined=money(tp.outstanding_advances_total(MONTH_END)),
            bank_plus_petty=money(tp.outstanding_bank_advances_total(MONTH_END)
                                  + tp.outstanding_petty_advances_total(MONTH_END)),
            expected=money("16000"))

        # 3. Weeks later the holder settles up out of his own pocket and the
        #    treasurer closes the file. `AdvanceClose` stamps today.
        self.submit(self.office, "advance_close",
                    {"note": "Balance recovered in cash."}, args=[adv.pk])
        adv.refresh_from_db()
        self.assertEqual(adv.status, StaffAdvance.Status.CLOSED)
        self.assertGreater(adv.settled_on, MONTH_END,
                           "this test is only meaningful if the closure is AFTER "
                           "the reporting date")

        # 4. THE ASSERTION. Somebody re-opens the month-end figure in September.
        #    The money was demonstrably out on that date; the closure happened
        #    afterwards and cannot reach back through it.
        self.assert_agree(
            "the month-end figure, re-read after the advance was closed",
            combined=money(tp.outstanding_advances_total(MONTH_END)),
            bank_plus_petty=money(tp.outstanding_bank_advances_total(MONTH_END)
                                  + tp.outstanding_petty_advances_total(MONTH_END)),
            what_it_said_before_the_closure=money("16000"))

        # 5. …and the fix must not keep a retired receivable alive for ever.
        self.assertEqual(tp.outstanding_advances_total(CLOSING_DAY), Decimal("0"))
        self.assert_books_balance("after a partly-accounted advance was closed")

    def test_the_july_reconciliation_keeps_its_advance_line_when_reopened(self):
        """The defect that made this rule matter, walked as a human hits it.

        The worksheet re-syncs its auto-managed lines on every ordinary page
        load and DELETES one whose recomputed value is zero. So a worksheet
        that balanced in July loses its advance line the first time anybody
        opens it after the advance is closed — and is then out by exactly that
        amount, with nothing on the page to say what changed.
        """
        # 1. An advance is out over the month end, partly accounted for.
        adv = self._issue(self.office, amount="25000")
        self._account_for(self.office, adv, date=FIRST_RECEIPTS,
                          desc="Fuel for the outreach van", amount="9000",
                          category="TRANSPORT")

        # 2. The treasurer prepares the month-end bank reconciliation.
        self.submit(self.office, "reconciliation_new", {
            "statement_date": MONTH_END.isoformat(),
            "bank_balance": "250000", "book_balance": "250000",
            "notes": "Month-end worksheet"})
        from statements.models import BankReconciliation
        rec = BankReconciliation.objects.get(statement_date=MONTH_END)

        # 3. Opening it lists the advance among the reconciling items, without
        #    the treasurer typing it — that is what "auto-managed" means.
        self.visit(self.office, "reconciliation_detail", args=[rec.pk])
        line = rec.items.filter(description__icontains="Staff advances issued").first()
        self.assertIsNotNone(
            line, "the worksheet did not pick up the outstanding advance at all")
        self.assertEqual(line.amount, Decimal("16000"))
        balanced_at = rec.items.count()

        # 4. The advance is closed, after the worksheet's date.
        self.submit(self.office, "advance_close", {"note": "Recovered."},
                    args=[adv.pk])

        # 5. Somebody opens the July worksheet again in September. The page load
        #    itself re-syncs. The line must survive it, unchanged.
        self.visit(self.office, "reconciliation_detail", args=[rec.pk])
        rec.refresh_from_db()
        still_there = rec.items.filter(
            description__icontains="Staff advances issued").first()
        self.assertIsNotNone(
            still_there,
            "re-opening the month-end worksheet DELETED its staff-advance line "
            "because the advance has since been closed. The worksheet balanced "
            "in July and is now out by 16,000 with nothing on it to say why.")
        self.assertEqual(still_there.amount, Decimal("16000"))
        self.assertEqual(rec.items.count(), balanced_at,
                         "re-opening the worksheet changed how many lines it has")

    # =====================================================================
    # 3. THE PETTY-CASH-FUNDED ADVANCE — real cash out of a real box
    # =====================================================================

    def _fund_the_tin(self, amount="60000"):
        """Put cash in the box the way the treasurer does: the top-up form on
        the petty-cash page."""
        self.submit(self.office, "petty_cash_topup", {
            "date": FLOAT_FUNDED.isoformat(), "amount": amount,
            "note": "Float established"})

    def _assert_the_box_holds(self, expected, as_of, stage):
        """Three answers to 'what is in the tin', at one date. The float service
        (what the card on the dashboard reads), the register page's running
        balance (what a treasurer checking the book reads), and the hand count
        (what is physically there, tracked by this test as the story goes)."""
        self.assert_agree(
            f"the petty-cash float {stage}",
            float_service=money(self._petty_float(as_of)),
            petty_cash_register_page=money(self._register_closing(
                self.office, PERIOD_START, as_of)),
            counted_by_hand=money(expected))

    def test_a_petty_cash_advance_moves_the_float_and_only_when_cash_moves(self):
        # 1. The treasurer establishes a 60,000 float.
        self._fund_the_tin("60000")
        self._assert_the_box_holds("60000", FLOAT_FUNDED, "once the float is set up")

        # 2. She gives 18,000 out of the tin to a deacon for the harvest
        #    programme. The cash physically leaves the box.
        adv = self._issue(self.office, staff_name="Deacon Wanjiru",
                          amount="18000", from_petty_cash="1",
                          purpose="Harvest programme — market purchases",
                          bank_charge="0", reference="PC-01")
        self.assertTrue(adv.from_petty_cash)
        self.assertEqual(adv.method, "CASH",
                         "money out of the petty tin is cash, by definition")
        self._assert_the_box_holds("42000", ISSUED_ON, "after 18,000 left the tin")
        self.assertEqual(tp.outstanding_petty_advances_total(ISSUED_ON),
                         Decimal("18000"))

        # 3. The deacon hands in receipts. THIS MUST NOT MOVE THE FLOAT. No cash
        #    came back to the box — a receipt reclassifies money that already
        #    left. Booking it as a petty disbursement would take the same
        #    18,000 out of the tin twice.
        self._account_for(self.office, adv, date=FIRST_RECEIPTS,
                          desc="Maize and beans for the harvest table",
                          amount="7500", category="MATERIALS")
        self._assert_the_box_holds("42000", FIRST_RECEIPTS,
                                   "after a receipt was filed (paperwork, not cash)")
        settling = Expense.objects.get(description__startswith="Maize and beans")
        self.assertFalse(
            settling.paid_from_petty_cash,
            "a receipt settling an advance must not also be booked as a petty "
            "disbursement — the tin would lose the same money twice")

        # 4. She tops the advance up by 6,000, again out of the tin. Cash moves,
        #    so the float moves.
        self.submit(self.office, "advance_topup", {
            "date": TOPPED_UP.isoformat(), "amount": "6000", "charge": "0",
            "note": "more stalls than expected"}, args=[adv.pk])
        adv.refresh_from_db()
        self.assertEqual(adv.amount, Decimal("24000"))
        self._assert_the_box_holds("36000", TOPPED_UP,
                                   "after a 6,000 top-up out of the tin")

        # 5. More receipts. Still no cash movement.
        self._account_for(self.office, adv, date=LAST_RECEIPTS,
                          desc="Transport for the produce", amount="10000",
                          category="TRANSPORT")
        adv.refresh_from_db()
        self.assertEqual(adv.balance, Decimal("6500"))
        self._assert_the_box_holds("36000", LAST_RECEIPTS,
                                   "after the second batch of receipts")
        self.assertEqual(tp.outstanding_petty_advances_total(MONTH_END),
                         Decimal("6500"),
                         "what is still out of the tin and unaccounted for")

        # 6. The deacon brings the unspent 6,500 back and the file is closed.
        #    NOW the notes are physically in the box again.
        self.submit(self.office, "advance_close", {
            "returned_to_petty": "6500",
            "note": "Balance returned in cash to the tin."}, args=[adv.pk])
        adv.refresh_from_db()
        self.assertEqual(adv.status, StaffAdvance.Status.CLOSED)
        self.assertEqual(adv.returned_to_petty, Decimal("6500"))
        self._assert_the_box_holds("42500", CLOSING_DAY,
                                   "after the unspent cash came back")

        # 7. The advance is fully accounted for: 17,500 spent, 6,500 returned.
        self.assert_agree(
            "everything the deacon was given, accounted for",
            advanced=money(adv.amount),
            receipts_plus_cash_returned=money(adv.accounted_total),
            expected=money("24000"))
        self.assertEqual(tp.outstanding_petty_advances_total(CLOSING_DAY),
                         Decimal("0"))

        # 8. THE MONEY. The fund carries the 17,500 actually spent — and NOT the
        #    6,500 that came back, which never was an expense. The tin is a cash
        #    location, so it does not touch a fund balance at all.
        self.assert_fund_balance(self.local_fund, Decimal("282500"),
                                 as_of=CLOSING_DAY)
        self.assert_books_balance("after a petty-cash advance was retired")
        self.assert_trial_balance_balances()

    def test_the_tin_cannot_hand_out_more_than_it_holds(self):
        """A float of 5,000 cannot fund an 18,000 advance. The refusal has to be
        a refusal — not a silent write that leaves the tin holding minus 13,000.
        """
        self._fund_the_tin("5000")
        self.submit(self.office, "advance_new", {
            "staff_name": "Deacon Wanjiru", "department": self.local_fund.id,
            "amount": "18000", "date_issued": ISSUED_ON.isoformat(),
            "purpose": "Harvest programme", "method": "CASH",
            "from_petty_cash": "1", "bank_charge": "0", "reference": ""})
        self.assertFalse(
            StaffAdvance.objects.filter(staff_name="Deacon Wanjiru").exists(),
            "the app issued an advance the petty tin could not cover")
        self._assert_the_box_holds("5000", ISSUED_ON, "after the refused advance")
        self.assert_books_balance("after a refused petty advance")

    def test_the_tin_is_checked_against_the_sending_charge_too(self):
        """DEFECT — the issue form can overdraw the petty tin by the charge.

        `AdvanceCreate.post` guards the float with `if amount > avail`, and then
        `_sync_advance_charge` books the sending charge as a further petty
        disbursement (`paid_from_petty_cash = adv.from_petty_cash`). The charge
        is never in the guard, so an advance for exactly the float leaves the
        tin holding MINUS the charge.

        The same rule one screen along gets it right: `AdvanceTopUpView` tests
        `amount + charge > avail`. Both controls sit on the same form with
        nothing hiding either, so this is one rule written twice and only once
        correctly — the exact shape this suite exists to catch.

        Walked: float 5,000 → issue 5,000 from petty with a 200 M-Pesa charge →
        `petty_balance_asof` returns **-200.00**, and the petty-cash register
        agrees with it, so the two figures that are supposed to cross-check each
        other confirm a tin holding negative cash. Expected: the same refusal
        the top-up form gives, and a float of 5,000 untouched.
        """
        self._fund_the_tin("5000")
        self.submit(self.office, "advance_new", {
            "staff_name": "Deacon Wanjiru", "department": self.local_fund.id,
            "amount": "5000", "date_issued": ISSUED_ON.isoformat(),
            "purpose": "Harvest programme", "method": "MPESA",
            "from_petty_cash": "1", "bank_charge": "200", "reference": ""})
        self.assertGreaterEqual(
            self._petty_float(ISSUED_ON), Decimal("0"),
            "the petty tin is holding a NEGATIVE balance. No box can. The "
            "advance was allowed out for the whole float and the sending "
            "charge was then taken out of the same empty tin.")

    def test_the_petty_advance_is_still_out_of_the_tin_at_the_month_end(self):
        """The as-at-date rule again, on the cash side. The tin was 18,000 down
        on the month-end date; closing the file in September does not put the
        notes back in retrospectively."""
        self._fund_the_tin("60000")
        adv = self._issue(self.office, staff_name="Deacon Wanjiru",
                          amount="18000", from_petty_cash="1",
                          purpose="Harvest programme", bank_charge="0")
        self._account_for(self.office, adv, date=FIRST_RECEIPTS,
                          desc="Maize and beans", amount="7500",
                          category="MATERIALS")
        month_end_float = self._petty_float(MONTH_END)
        self.assertEqual(month_end_float, Decimal("42000"))
        self.assertEqual(tp.outstanding_petty_advances_total(MONTH_END),
                         Decimal("10500"))

        self.submit(self.office, "advance_close", {
            "returned_to_petty": "10500", "note": "Returned."}, args=[adv.pk])

        # Re-read the month-end figures. The cash was out then; the return
        # happened on the closing date and belongs on the closing date.
        self.assert_agree(
            "the tin at the month end, re-read after the advance was closed",
            float_now_read_for_the_month_end=money(self._petty_float(MONTH_END)),
            what_it_said_before_the_closure=money(month_end_float))
        self.assertEqual(
            tp.outstanding_petty_advances_total(MONTH_END), Decimal("10500"),
            "the cash was demonstrably out of the tin on the month-end date")
        # …and the return lands on the day it happened.
        self.assertEqual(self._petty_float(CLOSING_DAY), Decimal("52500"))
        self.assert_books_balance("after a petty advance was returned and closed")

    # =====================================================================
    # 4. WHERE THE WORKFLOW ENDS, AND WHO MAY WALK IT
    # =====================================================================

    def test_every_page_this_workflow_touches_opens_at_every_stage(self):
        """Failure #125 was a page that rendered only on an EMPTY record; #126 a
        screen reachable from nowhere. An advance passes through four states and
        the page has to open in all of them, not just the one it was built on.
        """
        self._fund_the_tin("60000")
        self.visit(self.office, "advance_list")
        self.visit(self.office, "advance_new")

        adv = self._issue(self.office, staff_name="Deacon Wanjiru",
                          amount="18000", from_petty_cash="1",
                          purpose="Harvest programme", bank_charge="0")
        self.visit(self.office, "advance_detail", args=[adv.pk])   # ISSUED
        self.visit(self.office, "advance_edit", args=[adv.pk])

        self._account_for(self.office, adv, date=FIRST_RECEIPTS,
                          desc="Maize and beans", amount="7500",
                          category="MATERIALS")
        self.visit(self.office, "advance_detail", args=[adv.pk])   # PARTLY

        self._account_for(self.office, adv, date=LAST_RECEIPTS,
                          desc="Transport", amount="10500", category="TRANSPORT")
        self.visit(self.office, "advance_detail", args=[adv.pk])   # SETTLED

        self.submit(self.office, "advance_close", {"note": "Done."}, args=[adv.pk])
        self.visit(self.office, "advance_detail", args=[adv.pk])   # CLOSED
        self.visit(self.office, "advance_list")
        self.visit(self.office, "petty_cash")
        self.assert_books_balance("after walking every advance page")

    def test_a_holder_cannot_account_for_more_than_he_was_given(self):
        """The holder hands in 45,000 of receipts against a 40,000 advance.

        Something has to give, and the only safe answer is a refusal: accepting
        it would make the fund carry 5,000 of spending nobody advanced and turn
        the receivable negative. The refusal must also be REAL — the workflow
        this suite exists to catch is the one where the app says no on screen
        and writes the row anyway.
        """
        adv = self._issue(self.office)
        self._account_for(self.office, adv, date=FIRST_RECEIPTS,
                          desc="Venue hire", amount="30000")
        fund_before = Decimal("270000")   # 300,000 gift less the 30,000 spent
        self.assert_fund_balance(self.local_fund, fund_before, as_of=CLOSING_DAY)

        # 15,000 more, against a 10,000 balance.
        self._account_for(self.office, adv, date=LAST_RECEIPTS,
                          desc="Overstated per diem claim", amount="15000",
                          category="ALLOWANCE")
        adv.refresh_from_db()
        self.assertFalse(
            Expense.objects.filter(description="Overstated per diem claim").exists(),
            "the app accounted for 15,000 against a 10,000 balance")
        self.assertEqual(adv.settled_total, Decimal("30000"))
        self.assertEqual(adv.balance, Decimal("10000"))
        self.assert_fund_balance(self.local_fund, fund_before, as_of=CLOSING_DAY)

        # The remaining 10,000 goes through, so the refusal was about the excess
        # and not about the second receipt.
        self._account_for(self.office, adv, date=LAST_RECEIPTS,
                          desc="Per diem, as advanced", amount="10000",
                          category="ALLOWANCE")
        adv.refresh_from_db()
        self.assertEqual(adv.balance, Decimal("0"))
        self.assert_fund_balance(self.local_fund, Decimal("260000"),
                                 as_of=CLOSING_DAY)
        self.assert_books_balance("after an over-claim was refused")

    def test_the_advance_list_totals_agree_with_the_figures_they_split(self):
        """The advances page shows one total and then splits it in two, and it
        works the split out by SUBTRACTION (`all - bank`) rather than by asking
        for the petty figure. Two routes to one number is the shape of nearly
        every defect in this codebase's history, so the split has to be checked
        against the figure it is standing in for — with both kinds of advance
        open at once, which is the only state in which the two can disagree.
        """
        self._fund_the_tin("60000")
        bank_adv = self._issue(self.office)                       # 40,000, bank
        petty_adv = self._issue(self.office, staff_name="Deacon Wanjiru",
                                amount="18000", from_petty_cash="1",
                                purpose="Harvest programme", bank_charge="0")
        self._account_for(self.office, bank_adv, date=FIRST_RECEIPTS,
                          desc="Venue hire", amount="12000")
        self._account_for(self.office, petty_adv, date=FIRST_RECEIPTS,
                          desc="Maize and beans", amount="7500",
                          category="MATERIALS")

        page = self.visit(self.office, "advance_list")
        ctx = page.context
        self.assert_agree(
            "the advances page total, and the receivable service behind it",
            page_total=money(ctx["total_outstanding"]),
            outstanding_advances_total=money(
                tp.outstanding_advances_total(CLOSING_DAY)),
            expected=money("38500"))
        self.assert_agree(
            "the bank half of that total",
            page_bank=money(ctx["bank_outstanding"]),
            outstanding_bank_advances_total=money(
                tp.outstanding_bank_advances_total(CLOSING_DAY)),
            expected=money("28000"))
        self.assert_agree(
            "the petty half — worked out on the page by subtracting the bank "
            "half, and worked out by the service from the tin's own movements",
            page_petty_by_subtraction=money(ctx["petty_outstanding"]),
            outstanding_petty_advances_total=money(
                tp.outstanding_petty_advances_total(CLOSING_DAY)),
            expected=money("10500"))
        self.assert_books_balance("with a bank and a petty advance both open")

    def test_an_auditor_reads_the_advance_and_cannot_issue_or_close_one(self):
        """Segregation walked rather than asserted on a mixin. A hidden button
        is not a permission (#137d) — the POST itself has to be refused."""
        adv = self._issue(self.office)
        self._account_for(self.office, adv, date=FIRST_RECEIPTS,
                          desc="Venue hire", amount="12000")

        reading_room = self.acting_as(self.auditor)
        self.visit(reading_room, "advance_list")
        self.visit(reading_room, "advance_detail", args=[adv.pk])

        # issue one
        reading_room.post(reverse("advance_new"), {
            "staff_name": "Auditor's own advance", "department": self.local_fund.id,
            "amount": "9000", "date_issued": ISSUED_ON.isoformat(),
            "purpose": "no", "method": "BANK", "bank_charge": "0"})
        self.assertFalse(
            StaffAdvance.objects.filter(staff_name="Auditor's own advance").exists(),
            "a read-only auditor issued a staff advance")

        # account for one
        reading_room.post(reverse("advance_add_expense", args=[adv.pk]), {
            "date": LAST_RECEIPTS.isoformat(), "description": "Auditor's receipt",
            "amount": "1000", "category": "OTHER", "charge": "0"})
        adv.refresh_from_db()
        self.assertEqual(adv.settled_total, Decimal("12000"),
                         "a read-only auditor accounted for an advance")

        # close one
        reading_room.post(reverse("advance_close", args=[adv.pk]), {"note": "closed"})
        adv.refresh_from_db()
        self.assertNotEqual(adv.status, StaffAdvance.Status.CLOSED,
                            "a read-only auditor closed a staff advance")
        self.assert_books_balance("after an auditor was turned away three times")
