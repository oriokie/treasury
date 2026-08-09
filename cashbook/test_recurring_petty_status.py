"""A scheduled expense paid out of the petty cash tin is PAID, exactly like
every other petty-cash expense — the schedule does not get its own answer.

`services.expenses.new_expense_status` is where the rule lives, and its first
clause is the one that keeps being forgotten: money already out of the tin is
paid, not pending, because recording it as awaiting approval leaves the float
disagreeing with the drawer. The form, the spreadsheet import and the batch
screen read that rule. `services.recurring` did not: `generate_schedule` and
`pay_early` each carried their own copy of the second half of it —
"APPROVED if auto else PENDING" — and so could produce only those two states,
though both faithfully copy `paid_from_petty_cash` from the schedule onto the
expense they create.

The result, in the DEFAULT configuration (`require_expense_approval` is on out
of the box), was a petty-cash schedule generating PENDING rows for cash that
had already left the box. `petty_balance_asof` counts only APPROVED and PAID
disbursements, so the float went on reporting money the tin no longer held, by
the full amount of every generated instalment, until somebody approved a
payment that had already been made.

Nothing here covered `paid_from_petty_cash` through either path before, which
is how the two copies of the rule survived the v3.18.0 consolidation that
created `new_expense_status` in the first place.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from core.models import SiteConfig
from departments.models import Department

from .models import Expense, PettyCashTopUp, RecurringExpense
from .services import recurring as rec
from .services.treasury_position import petty_balance_asof

JAN = dt.date(2026, 1, 1)
JAN_END = dt.date(2026, 1, 31)


class ASchedulePaidFromPettyCashRecordsPaidExpensesTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("sched_owner", password="x")
        self.caller = User.objects.create_user("sched_caller", password="x")
        self.fund = Department.objects.create(
            name="PettySchedFund", fund_type="LOCAL", category="MINISTRY")
        # 10,000 put in the tin on the 1st; the schedule takes 2,000 of it.
        PettyCashTopUp.objects.create(date=JAN, amount=Decimal("10000"),
                                      note="float", recorded_by=self.owner)
        # The default configuration: approval required, no dual-approval
        # threshold. This is what a church runs on until someone changes it,
        # and it is the configuration the defect needed.
        cfg = SiteConfig.get()
        self.assertTrue(cfg.require_expense_approval)

    def _schedule(self, *, petty, amount="2000", day=1):
        return RecurringExpense.objects.create(
            description="Tea and milk", department=self.fund, category="OTHER",
            amount=Decimal(amount), frequency="MONTHLY", day_of_month=day,
            start_date=JAN, paid_from_petty_cash=petty,
            created_by=self.owner)

    def test_a_petty_cash_schedule_generates_a_paid_row_not_a_pending_one(self):
        sched = self._schedule(petty=True)
        rec.generate_schedule(sched, upto=JAN_END, user=self.caller)
        expense = Expense.objects.get(recurring=sched)
        self.assertTrue(expense.paid_from_petty_cash)
        self.assertEqual(
            expense.status, Expense.Status.PAID,
            "The cash is out of the tin; the row cannot be waiting for "
            "someone to approve a payment that has already happened.")

    def test_the_generated_row_leaves_the_float_agreeing_with_the_drawer(self):
        """The whole reason the status matters: `petty_balance_asof` counts
        APPROVED and PAID disbursements only, so a PENDING one is money the
        float claims to still have."""
        sched = self._schedule(petty=True)
        rec.generate_schedule(sched, upto=JAN_END, user=self.caller)
        self.assertEqual(petty_balance_asof(JAN_END), Decimal("8000"))

    def test_the_paid_row_is_stamped_with_the_day_the_cash_left(self):
        """Status and paid_date are written as a pair everywhere else — an
        expense that is PAID with no payment date is a row no report can age."""
        sched = self._schedule(petty=True)
        rec.generate_schedule(sched, upto=JAN_END, user=self.caller)
        expense = Expense.objects.get(recurring=sched)
        self.assertEqual(expense.paid_date, JAN)
        self.assertEqual(expense.date, JAN)
        # the schedule's owner is the approver of record, never whoever (or
        # whatever cron job) happened to run the generation
        self.assertEqual(expense.approved_by, self.owner)
        self.assertEqual(expense.recorded_by, self.caller)

    def test_an_instalment_paid_early_from_petty_cash_is_paid_as_well(self):
        """`pay_early` had the identical copy of the formula and so the
        identical hole in it."""
        sched = self._schedule(petty=True, day=15)
        expense = rec.pay_early(sched, dt.date(2026, 2, 15),
                                on=dt.date(2026, 1, 20), user=self.caller)
        self.assertEqual(expense.status, Expense.Status.PAID)
        # `on`, not the due date: paid_date is when the money actually moved
        self.assertEqual(expense.paid_date, dt.date(2026, 1, 20))
        self.assertEqual(expense.approved_by, self.owner)
        self.assertEqual(petty_balance_asof(dt.date(2026, 1, 20)),
                         Decimal("8000"))

    def test_an_ordinary_schedule_still_waits_for_approval(self):
        """The petty-cash clause must not become a general amnesty: a bank or
        cash payment under the default configuration is still PENDING with
        nobody recorded as having approved it."""
        sched = self._schedule(petty=False)
        rec.generate_schedule(sched, upto=JAN_END, user=self.caller)
        expense = Expense.objects.get(recurring=sched)
        self.assertEqual(expense.status, Expense.Status.PENDING)
        self.assertIsNone(expense.approved_by)
        self.assertIsNone(expense.paid_date)

    def test_an_ordinary_schedule_auto_approves_where_the_church_allows_it(self):
        """With approval switched off the generated row is APPROVED — not
        PAID, because nothing says the money has actually gone yet."""
        cfg = SiteConfig.get()
        cfg.require_expense_approval = False
        cfg.save()
        sched = self._schedule(petty=False)
        rec.generate_schedule(sched, upto=JAN_END, user=self.caller)
        expense = Expense.objects.get(recurring=sched)
        self.assertEqual(expense.status, Expense.Status.APPROVED)
        self.assertEqual(expense.approved_by, self.owner)
        self.assertIsNone(expense.paid_date)

    def test_the_dual_approval_threshold_still_holds_a_big_payment_back(self):
        """Both paths compute `auto` with the high-value rule and hand it to
        `new_expense_status`; routing the status through one definition must
        not lose the threshold on the way. `pay_early` was never covered for
        this at all."""
        cfg = SiteConfig.get()
        cfg.require_expense_approval = False
        cfg.dual_approval_threshold = Decimal("10000")
        cfg.save()
        sched = self._schedule(petty=False, amount="15000", day=15)
        rec.generate_schedule(sched, upto=dt.date(2026, 1, 20), user=self.caller)
        self.assertEqual(Expense.objects.get(recurring=sched).status,
                         Expense.Status.PENDING)
        early = rec.pay_early(sched, dt.date(2026, 2, 15),
                              on=dt.date(2026, 1, 25), user=self.caller)
        self.assertEqual(early.status, Expense.Status.PENDING)
        self.assertIsNone(early.approved_by)

    def test_petty_cash_outranks_the_threshold_and_this_is_deliberate(self):
        """The one case where the two rules disagree, pinned so nobody has to
        guess which way it was meant to go.

        `new_expense_status` tests petty cash BEFORE the high-value rule, so a
        petty-cash payment at or above `dual_approval_threshold` generates PAID
        rather than PENDING. It reads like the threshold leaking, and it is not:
        approval decides whether money MAY leave, and this money has already
        left the tin. Recording it as pending would not un-spend it — it would
        only make `petty_balance_asof` skip it, so the float on screen would
        disagree with the cash in the drawer by the amount of the largest
        payment, which is the discrepancy the whole rule exists to prevent.

        The control this leaves is real but it is a different one: the size of
        the float itself, and the top-up that replenishes it. A church that
        wants big payments approved should not be making them out of petty cash.
        """
        cfg = SiteConfig.get()
        cfg.require_expense_approval = True      # approval ON, the strict setting
        cfg.dual_approval_threshold = Decimal("10000")
        cfg.save()
        sched = self._schedule(petty=True, amount="15000", day=15)
        rec.generate_schedule(sched, upto=dt.date(2026, 1, 20), user=self.caller)
        self.assertEqual(
            Expense.objects.get(recurring=sched).status, Expense.Status.PAID,
            "a petty-cash payment above the threshold must still record as "
            "PAID — the cash is already gone, and holding the ROW back only "
            "makes the float overstate the drawer")
