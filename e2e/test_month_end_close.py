"""Closing a month, and the pack the board is handed afterwards.

The capstone of this suite. Every other file walks one process; this one builds a
month with a bit of everything in it — receipts, an expense, a transfer between
funds — and then does what a treasurer does at the end of it: reconcile, check
the close list, lock, and produce the report the board reads.

It exists for the joins, not the steps. A month can be assembled from perfectly
correct individual entries and still produce a board pack whose headline figure
disagrees with the statement three pages later, because the two were computed by
different routes — which is the fault this application has recorded more often
than any other (#10, #134, and the whole v3.10 net-assets series). So the
assertions here are almost all of the form "these two must be the same number",
and the last one is that a locked month actually refuses a late entry, since a
lock that does not lock is worse than no lock at all: it is a lock people trust.
"""
import datetime as dt
from decimal import Decimal

from django.urls import reverse

from cashbook.models import Expense
from core.models import SiteConfig
from giving.models import Transaction

from .base import PERIOD_END, PERIOD_START, BusinessWorkflowTest

MONTH_YEAR, MONTH_NO = 2026, 7


class AMonthIsAssembledReconciledAndClosed(BusinessWorkflowTest):

    def setUp(self):
        super().setUp()
        cfg = SiteConfig.get()
        cfg.require_expense_approval = True
        cfg.save()
        self.office = self.acting_as(self.treasurer)
        self.checker = self.make_user("wf_close_checker", "Treasurer")
        self.second_office = self.acting_as(self.checker)

    # -- building the month ---------------------------------------------------

    def _a_month_of_giving(self):
        """Receipts into both funds, dated inside the month.

        Written straight to Transaction rather than through the envelope grid on
        purpose: the giving path has its own file in this suite
        (test_sabbath_giving_cycle) and re-walking it here would test it twice
        and this file's subject not at all. What this file needs is a month with
        money in it.
        """
        Transaction.objects.create(
            date=dt.date(2026, 7, 4), amount=Decimal("60000"), direction="CREDIT",
            channel="BANK", confirmed=True, allocation_status="MANUAL",
            department=self.trust_fund, payer_name="TITHE — JULY")
        Transaction.objects.create(
            date=dt.date(2026, 7, 11), amount=Decimal("40000"), direction="CREDIT",
            channel="CASH", confirmed=True, allocation_status="MANUAL",
            department=self.local_fund, payer_name="BUILDING — JULY")

    def _an_approved_expense(self, amount="15000"):
        self.submit(self.office, "expense_create", {
            "date": dt.date(2026, 7, 18).isoformat(),
            "department": self.local_fund.id,
            "description": "Cement for the roof", "amount": amount,
            "category": "OTHER", "method": Expense.Method.BANK,
            "payee": "Mwangi Hardware", "voucher_no": "V-JULY-1"})
        expense = Expense.objects.get(description="Cement for the roof")
        self.submit(self.second_office, "expense_approve",
                    {"action": "approve"}, args=[expense.pk])
        expense.refresh_from_db()
        self.assertEqual(expense.status, Expense.Status.APPROVED)
        return expense

    def _full_month(self):
        self._a_month_of_giving()
        self._an_approved_expense()

    # -- the workflow ---------------------------------------------------------

    def test_the_month_closes_and_every_figure_agrees(self):
        # 1. the month happens
        self._full_month()

        # 2. the books are sound before anyone reports on them. A month that
        #    does not balance cannot be closed, and finding that out from the
        #    board pack is finding out too late.
        self.assert_books_balance("at the end of the month")
        self.assert_trial_balance_balances(PERIOD_START, PERIOD_END)

        # 3. the funds hold what the month put in them
        self.assert_fund_balance(self.trust_fund, Decimal("60000"), PERIOD_END)
        self.assert_fund_balance(self.local_fund, Decimal("25000"), PERIOD_END)

        # 4. the treasurer opens the controls page and reads the close list
        controls = self.visit(
            self.office, "controls",
            query=f"?year={MONTH_YEAR}&checklist_month={MONTH_NO}")
        checklist = controls.context.get("checklist")
        self.assertIsNotNone(
            checklist, "the close checklist did not render for an unlocked month")

        # 5. the month is locked, from the page that locks it
        self.submit(self.office, "controls",
                    {"action": "lock", "year": MONTH_YEAR, "month": MONTH_NO,
                     "note": "July closed after review"})
        from core.models import period_locked
        self.assertIsNotNone(
            period_locked(PERIOD_END),
            "the month did not lock, so nothing after this proves anything")

        # 6. and the lock LOCKS. A late expense dated into the closed month must
        #    be refused — this is the assertion that matters, because a lock
        #    people trust and which does not hold is worse than none.
        #
        #    Asserted against the SAME post succeeding while unlocked, a few
        #    lines below. On its own, "the form refused it" proves only that
        #    something was wrong with the request — a typo in a field name would
        #    satisfy it just as well as a working lock, and would go on
        #    satisfying it after somebody removed the lock entirely.
        late_claim = {
            "date": dt.date(2026, 7, 30).isoformat(),
            "department": self.local_fund.id,
            "description": "Late claim into a closed month", "amount": "5000",
            "category": "OTHER", "method": Expense.Method.BANK,
            "payee": "Someone", "voucher_no": "V-JULY-LATE"}

        before = Expense.objects.count()
        self.submit(self.office, "expense_create", late_claim,
                    allow_form_errors=True)
        self.assertEqual(
            Expense.objects.count(), before,
            "an expense was accepted into a LOCKED month — the close is "
            "decorative, and every figure the board was given can still move")

        # 7. the control test: unlock, send the identical claim, and watch it
        #    land. Now step 6 is known to have been the lock refusing it.
        admin = self.make_user("wf_close_unlocker", "Treasurer", is_superuser=True)
        self.submit(self.acting_as(admin), "controls",
                    {"action": "unlock", "year": MONTH_YEAR, "month": MONTH_NO})
        self.submit(self.office, "expense_create", late_claim)
        self.assertEqual(
            Expense.objects.count(), before + 1,
            "the identical claim was refused with the month UNLOCKED too, so "
            "step 6 proved nothing about the lock")

    def test_the_board_pack_agrees_with_the_statements_behind_it(self):
        """The join this file exists for.

        The pack's headline figures and the standalone statements for the same
        dates are computed by different routes. They are supposed to be the same
        numbers. Historically they have not been.
        """
        self._full_month()

        pack = self.visit(self.office, "report_board",
                          query=f"?start={PERIOD_START}&end={PERIOD_END}")

        from core import metrics
        from reports.services import balances

        rows = balances.department_summary(PERIOD_START, PERIOD_END)
        summary_receipts = sum((r.get("receipts") or Decimal(0)) for r in rows)
        summary_expenses = sum((r.get("expenses") or Decimal(0)) for r in rows)

        self.assert_agree(
            "total receipts for July, read two ways",
            fund_summary=summary_receipts,
            total_income_metric=metrics.metrics.total_income(PERIOD_START, PERIOD_END),
        )
        self.assert_agree(
            "total expenditure for July, read two ways",
            fund_summary=summary_expenses,
            operating_expense_metric=metrics.metrics.operating_expense(
                PERIOD_START, PERIOD_END),
        )
        # and the pack is not an empty shell. A report whose every table reads
        # "Nothing to report for this period" returns a perfectly healthy 200 —
        # that exact fault shipped in v3.45.0 and had to be fixed in v3.46.0.
        body = pack.content.decode()
        self.assertNotIn("Nothing to report for this period", body,
                         "the board pack rendered its empty-section text for a "
                         "month that plainly has content in it")

    def test_the_pack_still_reads_as_at_the_period_end_after_later_activity(self):
        """A closed month must not move because August happened.

        The whole value of a board pack is that the figures it carries are the
        figures for the period it names. Entering August's giving must leave
        July's pack exactly where it was.
        """
        self._full_month()

        from reports.services import balances
        july_before = balances.department_summary(PERIOD_START, PERIOD_END)
        july_total_before = sum((r.get("closing") or Decimal(0)) for r in july_before)

        Transaction.objects.create(
            date=dt.date(2026, 8, 6), amount=Decimal("99000"), direction="CREDIT",
            channel="BANK", confirmed=True, allocation_status="MANUAL",
            department=self.local_fund, payer_name="AUGUST GIFT")

        july_after = balances.department_summary(PERIOD_START, PERIOD_END)
        july_total_after = sum((r.get("closing") or Decimal(0)) for r in july_after)

        self.assert_agree(
            "July's closing position, before and after August was entered",
            before_august=july_total_before,
            after_august=july_total_after,
        )

    def test_the_pages_a_month_end_actually_passes_through_all_open(self):
        """Five screens, in the order a treasurer opens them. Each has to load
        with real data behind it — this application's recurring failure is a
        page that renders on an empty database and falls over on a populated
        one (#125)."""
        self._full_month()
        self.visit(self.office, "controls",
                   query=f"?year={MONTH_YEAR}&checklist_month={MONTH_NO}")
        self.visit(self.office, "report_board",
                   query=f"?start={PERIOD_START}&end={PERIOD_END}")
        self.visit(self.office, "trial_balance",
                   query=f"?start={PERIOD_START}&end={PERIOD_END}")
        self.visit(self.office, "expense_list")
        self.visit(self.office, "dashboard")

    def test_only_an_administrator_can_reopen_a_closed_month(self):
        """Closing is a control. If anyone who can close can also quietly
        reopen, it records nothing."""
        self._full_month()
        self.submit(self.office, "controls",
                    {"action": "lock", "year": MONTH_YEAR, "month": MONTH_NO})

        from core.models import period_locked
        self.assertIsNotNone(period_locked(PERIOD_END))

        # the treasurer is not a superuser, and must not be able to unlock
        self.submit(self.office, "controls",
                    {"action": "unlock", "year": MONTH_YEAR, "month": MONTH_NO})
        self.assertIsNotNone(
            period_locked(PERIOD_END),
            "a non-administrator reopened a closed accounting month")

        admin = self.make_user("wf_close_admin", "Treasurer", is_superuser=True)
        self.submit(self.acting_as(admin), "controls",
                    {"action": "unlock", "year": MONTH_YEAR, "month": MONTH_NO})
        self.assertIsNone(period_locked(PERIOD_END),
                          "an administrator could not reopen the month")
