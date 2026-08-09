"""An expense, from the claim on someone's desk to the fund it comes out of.

The path a real payment takes: a treasurer records it, it waits because the
church requires approval, somebody approves it, and only then does it reduce the
fund and appear in the ledger. Each of those steps has unit tests. What none of
them can show is that the money is right at the END, or that a claim which has
NOT been approved is correctly absent from everything — which is the half of the
rule that goes wrong quietly, because an expense missing from a report looks
exactly like an expense that was never entered.
"""
import datetime as dt
from decimal import Decimal

from django.db.models import Sum
from django.urls import reverse

from cashbook.models import Expense
from core.models import SiteConfig

from .base import PERIOD_END, TODAY, BusinessWorkflowTest


class ExpenseFromClaimToFundBalance(BusinessWorkflowTest):

    def setUp(self):
        super().setUp()
        cfg = SiteConfig.get()
        cfg.require_expense_approval = True     # the default, and the strict path
        cfg.save()
        self.office = self.acting_as(self.treasurer)
        # A second treasurer, because approving one's own claim is the thing
        # segregation of duties exists to stop and a church with two people
        # should be modelled with two.
        self.checker = self.make_user("wf_treasurer_2", "Treasurer")
        self.second_treasurer_client = self.acting_as(self.checker)

        # The fund has to have something in it before anything can come out, and
        # it gets there the way it really does — a receipt, not a fixture write.
        from giving.models import Transaction
        Transaction.objects.create(
            date=dt.date(2026, 7, 1), amount=Decimal("50000"), direction="CREDIT",
            channel="BANK", confirmed=True, allocation_status="MANUAL",
            department=self.local_fund, payer_name="OPENING GIFT")

    def _record_expense(self, amount="12000", **over):
        data = {
            "date": TODAY.isoformat(), "department": self.local_fund.id,
            "description": "Roofing sheets", "amount": amount,
            "category": "OTHER", "method": Expense.Method.BANK,
            "payee": "Mwangi Hardware", "voucher_no": "V-1001",
        }
        data.update(over)
        return self.submit(self.office, "expense_create", data)

    def test_an_expense_reaches_the_fund_only_after_it_is_approved(self):
        # 1. the treasurer records the claim
        self._record_expense()
        expense = Expense.objects.get(description="Roofing sheets")
        self.assertEqual(
            expense.status, Expense.Status.PENDING,
            "the church requires approval, so a new claim must wait")

        # 2. while it waits, it has NOT touched the money. This is the assertion
        #    that a per-step test does not make: the fund still holds everything.
        self.assert_fund_balance(self.local_fund, Decimal("50000"))

        # 3. it is approved — through the page that does it, by a SECOND
        #    treasurer, which is how a church that has one runs it
        self.submit(self.second_treasurer_client, "expense_approve",
                    {"action": "approve"}, args=[expense.pk])
        expense.refresh_from_db()
        self.assertEqual(expense.status, Expense.Status.APPROVED)

        # 4. NOW the fund is 12,000 lighter, and the books still balance
        self.assert_fund_balance(self.local_fund, Decimal("38000"))
        self.assert_books_balance("after approving an expense")
        self.assert_trial_balance_balances()

    def test_the_expense_reaches_every_report_that_should_show_it(self):
        """The figure has to be the same wherever it is read.

        Three places compute "what did we spend" by different routes, and the
        recurring fault in this application is precisely a total assembled one
        way asserted equal to the same total assembled another (#10, #134, and
        the whole v3.10 net-assets series). One expense, three readings.
        """
        self._record_expense(amount="12000")
        expense = Expense.objects.get(description="Roofing sheets")
        self.submit(self.second_treasurer_client, "expense_approve",
                    {"action": "approve"}, args=[expense.pk])

        from core import metrics
        from reports.services import balances

        rows = balances.department_summary(None, PERIOD_END)
        summary_spend = sum(
            (r.get("expenses") or Decimal(0)) for r in rows
            if getattr(r.get("department", None), "id", None) == self.local_fund.id)

        self.assert_agree(
            "one approved expense, read three ways",
            fund_summary=summary_spend,
            operating_expense_metric=metrics.metrics.operating_expense(
                dt.date(2026, 7, 1), PERIOD_END),
            expense_rows=Expense.objects.filter(
                status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
                date__lte=PERIOD_END).aggregate(t=Sum("amount"))["t"] or Decimal(0),
        )

    def test_the_expense_page_the_treasurer_lands_on_actually_opens(self):
        """Where the workflow ends. A claim that is entered and approved and
        then cannot be looked at has not been dealt with — and this is the
        specific failure this application shipped five times."""
        self._record_expense()
        expense = Expense.objects.get(description="Roofing sheets")
        self.visit(self.office, "expense_list")
        self.visit(self.office, "expense_detail", args=[expense.pk])

    def test_an_auditor_can_read_the_expense_and_cannot_approve_it(self):
        """Segregation, walked rather than asserted on a mixin: the read-only
        role must be able to see the claim and must not be able to pass it."""
        self._record_expense()
        expense = Expense.objects.get(description="Roofing sheets")

        reading_room = self.acting_as(self.auditor)
        self.visit(reading_room, "expense_list")

        reading_room.post(reverse("expense_approve", args=[expense.pk]),
                          {"action": "approve"})
        expense.refresh_from_db()
        self.assertEqual(
            expense.status, Expense.Status.PENDING,
            "a read-only auditor approved an expense")
        self.assert_fund_balance(self.local_fund, Decimal("50000"))
