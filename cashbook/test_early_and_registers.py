"""Remittance approval, the petty cash register, and paying a schedule early.

Three faults found together, each one a case of a figure or an action being
right in one place and wrong in another.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.urls import reverse

from core import roles
from departments.models import Department

from .models import (Expense, ExpenseRefund, PettyCashTopUp, RecurringExpense,
                     RemittanceBatch)
from .services import recurring as rec


def _treasurer(name="tess"):
    user = User.objects.create_user(name, password="office-pass-1")
    user.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
    return user


class ApprovingABatchDoesNotRebuildTheWholeLedgerTests(TestCase):
    """Approving a batch reposts the batch, not the church's entire history.

    `repost_to_ledger` accepted the expenses that had changed and ignored them,
    calling `posting.rebuild()` — which deletes every non-manual journal entry
    in the database and re-posts every transaction, expense, refund, transfer,
    asset acquisition, disposal and depreciation run ever recorded. On the
    seeded demo that was 3,349 queries; on a real register with years behind it,
    minutes, twice per batch. To the treasurer the approve button had hung.

    The cost also grew every year the church kept using the system, so the
    assertion here is a ceiling that a full rebuild cannot possibly meet.
    """

    def setUp(self):
        self.user = _treasurer()
        self.trust = Department.objects.create(
            name="Trust Fund", slug="trust-remit", is_trust=True,
            fund_type=Department.FundType.TRUST,
            category=Department.Category.TRUST)
        self.batch = RemittanceBatch.objects.create(
            batch_number="RB-TEST-1", date=dt.date.today(),
            created_by=self.user, status=RemittanceBatch.Status.DRAFT)
        for i in range(3):
            Expense.objects.create(
                date=dt.date.today(), department=self.trust,
                description=f"Remittance line {i}", amount=Decimal("1000"),
                category=Expense.Category.REMITTANCE,
                status=Expense.Status.PENDING, recorded_by=self.user,
                remittance_batch=self.batch)
        # The ledger only accepts postings once the chart exists; without it
        # `repost_to_ledger` correctly does nothing and this suite would be
        # comparing two empty ledgers.
        from ledger.services import posting
        posting.ensure_chart()
        self.client = Client()
        self.client.force_login(self.user)

    def test_approving_a_batch_approves_its_expenses(self):
        self.client.post(reverse("remittance_batch_approve", args=[self.batch.pk]))
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, RemittanceBatch.Status.APPROVED)
        self.assertEqual(
            self.batch.expenses.filter(status=Expense.Status.APPROVED).count(), 3)

    def test_approving_a_batch_does_not_repost_the_entire_ledger(self):
        with CaptureQueriesContext(connection) as ctx:
            self.client.post(reverse("remittance_batch_approve", args=[self.batch.pk]))
        self.assertLess(
            len(ctx.captured_queries), 400,
            f"Approving a three-line batch took {len(ctx.captured_queries)} "
            "queries. That is a whole-ledger rebuild, whose cost grows with "
            "every transaction the church has ever recorded — which is what "
            "made this button appear to hang.")

    def test_the_ledger_matches_what_a_full_rebuild_would_produce(self):
        """Speed must not change the books.

        Reposting only the affected expenses is a different route to the same
        ledger, so the guard that matters is that it arrives at the same place.
        """
        from ledger.models import JournalLine
        from ledger.services import posting

        def snapshot():
            return sorted(
                JournalLine.objects.exclude(entry__source_type="manual")
                .values_list("entry__source_type", "entry__source_id",
                             "account__system_key", "debit", "credit",
                             "entry__date", "department_id"))

        self.client.post(reverse("remittance_batch_approve", args=[self.batch.pk]))
        targeted = snapshot()
        posting.rebuild()
        self.assertEqual(
            targeted, snapshot(),
            "The targeted repost left a different ledger from a full rebuild, "
            "so the reconciliation between register and ledger is now wrong.")

    def test_a_batch_that_is_not_draft_is_refused(self):
        self.batch.status = RemittanceBatch.Status.APPROVED
        self.batch.save(update_fields=["status"])
        self.client.post(reverse("remittance_batch_approve", args=[self.batch.pk]))
        self.assertEqual(
            self.batch.expenses.filter(status=Expense.Status.PENDING).count(), 3,
            "A batch outside DRAFT was approved a second time.")


class PettyCashRegisterAgreesWithTheFloatCardTests(TestCase):
    """The register's closing balance and the "float on hand" card are one number.

    They were computed two different ways. `petty_balance_asof`, which the card
    reads, counts refunds handed back into the tin; the register's movement list
    did not list them at all. Since the register's *opening* balance comes from
    that same helper, a refund before the period was counted and a refund inside
    it silently vanished — so the two figures differed by exactly the refunds
    falling in the period, and the register did not show cash that had
    physically gone back into the box.
    """

    def setUp(self):
        self.user = _treasurer("tess-petty")
        self.fund = Department.objects.create(
            name="Local Church Budget", slug="lcb-petty",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.today = dt.date.today()
        # Twenty days back, not the 1st: the register is asked for
        # `start`..`today`, and the expense below sits at start+2 with a refund
        # at start+5, so anchoring on the 1st pushed both past today whenever
        # the suite ran early in a month — the refund row could not appear
        # because the refund had not happened yet.
        self.start = self.today - dt.timedelta(days=20)
        PettyCashTopUp.objects.create(date=self.start, amount=Decimal("5000"),
                                      recorded_by=self.user)
        self.expense = Expense.objects.create(
            date=self.start + dt.timedelta(days=2), department=self.fund,
            description="Petty purchase", amount=Decimal("2000"),
            category=Expense.Category.MATERIALS, status=Expense.Status.PAID,
            recorded_by=self.user, paid_from_petty_cash=True)
        self.client = Client()
        self.client.force_login(self.user)

    def _context(self):
        response = self.client.get(
            f"/petty-cash/?start={self.start}&end={self.today}")
        self.assertEqual(response.status_code, 200)
        return response.context

    def test_closing_balance_matches_the_float_card_without_refunds(self):
        ctx = self._context()
        self.assertEqual(ctx["closing"], ctx["balance_now"])

    def test_closing_balance_matches_the_float_card_with_a_refund(self):
        ExpenseRefund.objects.create(
            expense=self.expense, date=self.start + dt.timedelta(days=5),
            amount=Decimal("300"), to_petty_cash=True, recorded_by=self.user)
        ctx = self._context()
        self.assertEqual(
            ctx["closing"], ctx["balance_now"],
            "The register's closing balance and the float card disagree by the "
            "refunds paid back into the tin during the period.")

    def test_the_refund_appears_as_a_row_in_the_register(self):
        """Agreement alone is not enough — the movement has to be visible.

        Whoever counts the cash has to be able to trace the balance, and a
        refund is real money going back into the box.
        """
        ExpenseRefund.objects.create(
            expense=self.expense, date=self.start + dt.timedelta(days=5),
            amount=Decimal("300"), to_petty_cash=True, recorded_by=self.user)
        rows = [m for m in self._context()["movements"] if m["in"] == Decimal("300")]
        self.assertTrue(
            rows, "The refund is counted in the balance but never shown, so the "
                  "register does not add up on its face.")

    def test_a_refund_not_returned_to_petty_cash_is_left_alone(self):
        """Only cash that went back into the tin belongs in the tin's register."""
        ExpenseRefund.objects.create(
            expense=self.expense, date=self.start + dt.timedelta(days=5),
            amount=Decimal("300"), to_petty_cash=False, recorded_by=self.user)
        ctx = self._context()
        self.assertEqual(ctx["closing"], ctx["balance_now"])
        self.assertFalse([m for m in ctx["movements"] if m["in"] == Decimal("300")],
                         "A refund banked rather than returned to the tin was "
                         "added to the petty cash float.")


class PayingAScheduledExpenseEarlyTests(TestCase):
    """A schedule can be settled ahead of its due date without paying twice.

    There was no way to pay a scheduled instalment early, so it was recorded by
    hand — and generation deduplicated on the expense *date*, which a hand-made
    row dated earlier does not match. The schedule then raised the same charge
    again on the due date and the fund was debited twice with nothing to flag
    it. The missing idea was *which instalment a payment settles*, as distinct
    from *when the money left*.
    """

    def setUp(self):
        self.user = _treasurer("tess-rec")
        self.fund = Department.objects.create(
            name="Pastoral", slug="pastoral-rec",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.today = dt.date.today()
        self.due = self.today + dt.timedelta(days=20)
        self.sched = RecurringExpense.objects.create(
            description="Pastor stipend", department=self.fund,
            amount=Decimal("5000"),
            frequency=RecurringExpense.Frequency.MONTHLY,
            day_of_month=self.due.day, start_date=self.due,
            created_by=self.user)
        self.client = Client()
        self.client.force_login(self.user)

    def test_paying_early_dates_the_expense_when_the_money_left(self):
        """Funds are kept on a cash basis, so the date is the payment date."""
        expense = rec.pay_early(self.sched, self.due, on=self.today, user=self.user)
        self.assertEqual(expense.date, self.today)
        self.assertEqual(expense.recurring_due_date, self.due)

    def test_the_schedule_does_not_charge_the_period_again(self):
        rec.pay_early(self.sched, self.due, on=self.today, user=self.user)
        rec.generate_schedule(self.sched, self.due + dt.timedelta(days=1), self.user)
        rows = Expense.objects.filter(recurring=self.sched)
        self.assertEqual(
            rows.count(), 1,
            "The instalment was recorded twice: paid early, then generated "
            "again on its due date.")
        self.assertEqual(sum(r.amount for r in rows), Decimal("5000"))

    def test_an_instalment_already_paid_is_not_offered_again(self):
        self.assertIn(self.due, rec.upcoming_instalments(self.sched, 3))
        rec.pay_early(self.sched, self.due, on=self.today, user=self.user)
        self.assertNotIn(self.due, rec.upcoming_instalments(self.sched, 3))

    def test_paying_the_same_instalment_twice_is_refused(self):
        rec.pay_early(self.sched, self.due, on=self.today, user=self.user)
        with self.assertRaises(ValueError):
            rec.pay_early(self.sched, self.due, on=self.today, user=self.user)

    def test_a_date_that_is_not_a_due_date_is_refused(self):
        with self.assertRaises(ValueError):
            rec.pay_early(self.sched, self.due + dt.timedelta(days=3),
                          on=self.today, user=self.user)

    def test_an_instalment_after_the_schedule_ends_is_refused(self):
        self.sched.end_date = self.due - dt.timedelta(days=1)
        self.sched.save(update_fields=["end_date"])
        with self.assertRaises(ValueError):
            rec.pay_early(self.sched, self.due, on=self.today, user=self.user)

    def test_normal_generation_still_records_the_instalment_it_settles(self):
        """The dedup key has to be populated by the ordinary path too."""
        rec.generate_schedule(self.sched, self.due + dt.timedelta(days=1), self.user)
        expense = Expense.objects.get(recurring=self.sched)
        self.assertEqual(expense.recurring_due_date, expense.date)

    def test_generation_is_still_idempotent(self):
        rec.generate_schedule(self.sched, self.due + dt.timedelta(days=1), self.user)
        rec.generate_schedule(self.sched, self.due + dt.timedelta(days=1), self.user)
        self.assertEqual(Expense.objects.filter(recurring=self.sched).count(), 1)

    def test_the_page_offers_the_next_instalments(self):
        body = self.client.get(reverse("recurring_list")).content.decode()
        self.assertIn("Pay early", body)
        self.assertIn(self.due.isoformat(), body)

    def test_the_view_records_the_payment(self):
        self.client.post(reverse("recurring_pay_early", args=[self.sched.pk]),
                         {"due_date": self.due.isoformat()})
        expense = Expense.objects.get(recurring=self.sched)
        self.assertEqual(expense.recurring_due_date, self.due)
        self.assertEqual(expense.date, dt.date.today())

    def test_the_view_rejects_a_bad_date_without_creating_anything(self):
        self.client.post(reverse("recurring_pay_early", args=[self.sched.pk]),
                         {"due_date": "not-a-date"})
        self.assertEqual(Expense.objects.filter(recurring=self.sched).count(), 0)
