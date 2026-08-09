"""Two rules the cashbook screens had each written more than once.

**A debt may be paid exactly once.** The `settled`/`settled_on` columns are a
cache of what the APPROVED-or-PAID payments say, so in a church that requires
approval the cache is stale from the moment a payment is keyed until the moment
it is approved — and until this was fixed, approval told nobody. A bill paid in
full through "Pay in detail" therefore kept `settled=False` for ever, the
payables screen offered it again, and 100,000 of pews cost 200,000. The rule
that stops it cannot live at data entry: a pending payment discharges nothing,
so two pending payments of the whole invoice both look affordable when they are
keyed and the arithmetic only breaks when the second is approved. It lives at
the status change, which is the moment a payment starts counting.

**A box of notes cannot hold negative cash.** The advance form guarded the
petty float against the cash handed over and not against the sending charge,
which `_sync_advance_charge` books as a further petty disbursement. The top-up
form one screen along had the rule right. Both now ask the same function.

The workflow suite (`e2e/`) proves these through HTTP end to end; what is
pinned here is the rule itself, at the level it is written.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.urls import reverse

from core.models import SiteConfig
from core.roles import TREASURER
from departments.models import Department

from .models import Accrual, Expense, Payable, PettyCashTopUp, StaffAdvance

INVOICE_ON = dt.date(2026, 7, 10)
PAID_ON = dt.date(2026, 7, 20)
INVOICE = Decimal("100000.00")


class _CashbookRuleTest(TestCase):
    """One treasurer, one fund, approval required — the church the defect was
    found in. `require_expense_approval` is stated rather than assumed because
    every rule below turns on the gap between keying a payment and approving
    it, and that gap only exists when approval is required."""

    def setUp(self):
        cfg = SiteConfig.get()
        cfg.require_expense_approval = True
        cfg.require_different_approver = False
        cfg.enforce_fund_balance = False
        cfg.dual_approval_threshold = 0
        cfg.save()
        self.treasurer = User.objects.create_user("tess", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client = Client()
        self.client.force_login(self.treasurer)
        self.fund = Department.objects.create(
            name="Building Fund", slug="rule-building",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY, show_in_expenses=True)

    # -- helpers ------------------------------------------------------------

    def _payable(self, amount=INVOICE, description="Pews, one invoice"):
        return Payable.objects.create(
            date=INVOICE_ON, vendor="Mwangi Hardware", description=description,
            amount=amount, department=self.fund, recorded_by=self.treasurer)

    def _pay_in_detail(self, obligation, amount, label, kind="payable"):
        """The route the defect came in by: the full expense form, reached from
        the payables row's "Pay in detail" link."""
        self.client.post(
            reverse("expense_create") + f"?settle={kind}:{obligation.pk}", {
                "settle": f"{kind}:{obligation.pk}",
                "date": PAID_ON.isoformat(), "department": self.fund.pk,
                "description": label, "amount": str(amount),
                "category": Expense.Category.MAINTENANCE,
                "expenditure_type": "RECURRENT",
                "method": Expense.Method.BANK}, follow=True)
        return Expense.objects.filter(description=label).first()

    def _approve(self, expense, action="approve"):
        return self.client.post(reverse("expense_approve", args=[expense.pk]),
                                {"action": action}, follow=True)


class ApprovingAPaymentRefreshesTheDebt(_CashbookRuleTest):
    """The cache and the payments must never be able to disagree."""

    def test_approving_the_final_instalment_marks_the_bill_settled(self):
        bill = self._payable()
        expense = self._pay_in_detail(bill, INVOICE, "First cheque")

        bill.refresh_from_db()
        self.assertEqual(expense.status, Expense.Status.PENDING)
        self.assertFalse(
            bill.settled,
            "A payment nobody has approved has not discharged anything.")

        self._approve(expense)

        bill.refresh_from_db()
        self.assertTrue(
            bill.settled,
            "The bill is paid in full and approved; the flag has to say so. "
            "While it did not, the payables screen offered the bill again.")
        self.assertEqual(bill.settled_on, PAID_ON,
                         "settled_on is the date the money left, not today.")
        self.assertEqual(bill.balance, Decimal("0"))

    def test_rejecting_an_approved_payment_puts_the_debt_back(self):
        bill = self._payable()
        expense = self._pay_in_detail(bill, INVOICE, "Cheque, wrong supplier")
        self._approve(expense)
        bill.refresh_from_db()
        self.assertTrue(bill.settled)

        self._approve(expense, action="reject")

        bill.refresh_from_db()
        self.assertFalse(bill.settled,
                         "A rejected payment paid nothing, so the bill is owed "
                         "again — and the cache has to follow it back.")
        self.assertIsNone(bill.settled_on)
        self.assertEqual(bill.balance, INVOICE)

    def test_an_accrual_is_refreshed_on_approval_too(self):
        """Payables and accruals share one implementation, and this project's
        history is a rule fixed on one side and left on the other."""
        accrual = Accrual.objects.create(
            date=INVOICE_ON, description="Power, July estimate",
            amount=Decimal("8000.00"), department=self.fund,
            recorded_by=self.treasurer)
        expense = self._pay_in_detail(accrual, Decimal("8000.00"),
                                      "KPLC July", kind="accrual")
        self._approve(expense)

        accrual.refresh_from_db()
        self.assertTrue(accrual.settled)
        self.assertEqual(accrual.settled_on, PAID_ON)


class ADischargedDebtCannotBePaidAgain(_CashbookRuleTest):
    """The CRITICAL finding: 200,000 out of the fund for a 100,000 bill."""

    def test_a_second_full_payment_cannot_be_approved(self):
        bill = self._payable()
        first = self._pay_in_detail(bill, INVOICE, "First cheque")
        self._approve(first)

        second = self._pay_in_detail(bill, INVOICE, "Second cheque")
        self.assertIsNotNone(
            second,
            "The payment is recorded — it is evidence of what was keyed, and "
            "PENDING it costs the fund nothing. What it must never do is be "
            "approved.")
        self._approve(second)

        second.refresh_from_db()
        bill.refresh_from_db()
        self.assertEqual(
            second.status, Expense.Status.PENDING,
            "A second payment of the whole invoice was approved. That is "
            "100,000 of real money leaving the fund for a debt already paid.")
        self.assertEqual(bill.paid_total, INVOICE)
        self.assertEqual(
            Expense.objects.filter(
                payable=bill,
                status__in=Payable.COUNTED_STATUSES).count(), 1)

    def test_the_last_instalment_that_exactly_clears_it_is_allowed(self):
        """The guard refuses more than is owed, not the payment that finishes
        the job — a guard that stops the final instalment is worse than none,
        because the treasurer then records it outside the payable."""
        bill = self._payable()
        self._approve(self._pay_in_detail(bill, Decimal("40000"), "Deposit"))
        last = self._pay_in_detail(bill, Decimal("60000"), "Balance")
        self._approve(last)

        last.refresh_from_db()
        bill.refresh_from_db()
        self.assertEqual(last.status, Expense.Status.APPROVED)
        self.assertTrue(bill.settled)
        self.assertEqual(bill.paid_total, INVOICE)

    def test_an_instalment_larger_than_the_balance_is_refused(self):
        bill = self._payable()
        self._approve(self._pay_in_detail(bill, Decimal("40000"), "Deposit"))
        too_much = self._pay_in_detail(bill, Decimal("60000.01"), "Overshoot")
        self._approve(too_much)

        too_much.refresh_from_db()
        self.assertEqual(too_much.status, Expense.Status.PENDING)
        self.assertEqual(bill.balance_asof(), Decimal("60000.00"))

    def test_marking_an_approved_payment_paid_is_not_mistaken_for_a_new_one(self):
        """APPROVED -> PAID adds nothing to the debt; the payment is already in
        the total. A guard that read it as a fresh charge would refuse to let
        any settled bill's payment be marked paid."""
        bill = self._payable()
        expense = self._pay_in_detail(bill, INVOICE, "Cheque 0041")
        self._approve(expense)

        self._approve(expense, action="pay")

        expense.refresh_from_db()
        bill.refresh_from_db()
        self.assertEqual(expense.status, Expense.Status.PAID)
        self.assertTrue(bill.settled)
        self.assertEqual(bill.paid_total, INVOICE)

    def test_a_bulk_approval_is_not_the_way_round_the_guard(self):
        """Fifty claims ticked at once must obey the same rule as one."""
        bill = self._payable()
        first = self._pay_in_detail(bill, INVOICE, "First cheque")
        self._approve(first)
        second = self._pay_in_detail(bill, INVOICE, "Second cheque")

        self.client.post(reverse("expense_bulk"),
                         {"action": "approve", "ids": [str(second.pk)]},
                         follow=True)

        second.refresh_from_db()
        self.assertEqual(second.status, Expense.Status.PENDING)
        self.assertEqual(bill.paid_total, INVOICE)

    def test_a_legacy_flag_only_settlement_is_believed(self):
        """A row carrying `settled` with no payment behind it is how every
        settlement made before instalments existed looks. `balance_asof` says
        it owes nothing, so this route must refuse it too — the read path and
        the write path answering differently about one row is the whole fault.

        Refused at ENTRY rather than at approval, unlike every other case here,
        and for a reason worth stating: `refresh_settlement` derives the flag
        from the payments, so merely linking an unapproved payment to this row
        rewrites `settled` to False and destroys the only evidence the debt was
        ever discharged. There would then be nothing left for the approval gate
        to refuse.
        """
        bill = self._payable()
        Payable.objects.filter(pk=bill.pk).update(
            settled=True, settled_on=dt.date(2026, 7, 12))
        bill.refresh_from_db()

        self.assertIsNone(self._pay_in_detail(bill, INVOICE, "Paid again"),
                          "a debt already discharged took a second payment")
        bill.refresh_from_db()
        self.assertTrue(bill.settled,
                        "the flag-only settlement was erased on the way past")
        self.assertEqual(bill.balance_asof(), Decimal("0"))

    def test_the_form_no_longer_offers_the_whole_invoice_on_a_paid_bill(self):
        """The pre-filled amount was the trap: `balance or amount` fell back to
        the invoice total the moment the balance reached nought, so a
        bookmarked settle link opened the full bill again, one Enter from
        paying it twice."""
        bill = self._payable()
        self._approve(self._pay_in_detail(bill, INVOICE, "Cheque 0041"))

        page = self.client.get(
            reverse("expense_create") + f"?settle=payable:{bill.pk}")
        self.assertNotEqual(
            page.context["form"].initial.get("amount"), INVOICE,
            "the paid invoice was offered again as the amount to pay")


class WhenApprovalIsNotRequired(_CashbookRuleTest):
    """A church that trusts its treasurer has no approval step, so there is no
    later gate — the entry form itself has to refuse."""

    def setUp(self):
        super().setUp()
        cfg = SiteConfig.get()
        cfg.require_expense_approval = False
        cfg.save()

    def test_the_second_payment_is_refused_at_entry(self):
        bill = self._payable()
        self.assertIsNotNone(self._pay_in_detail(bill, INVOICE, "First cheque"))

        self.assertIsNone(
            self._pay_in_detail(bill, INVOICE, "Second cheque"),
            "With no approval step the expense counts the instant it is "
            "written, so nothing after entry can stop it; entry must.")
        bill.refresh_from_db()
        self.assertEqual(bill.paid_total, INVOICE)

    def test_a_first_payment_in_full_still_goes_through(self):
        bill = self._payable()
        self.assertIsNotNone(self._pay_in_detail(bill, INVOICE, "One cheque"))
        bill.refresh_from_db()
        self.assertTrue(bill.settled)


class TheInstalmentListShowsItsBankReference(_CashbookRuleTest):
    """The payables screen's instalment list exists to tie a payment to a line
    on the bank statement. It rendered `pay.reference`; `Expense` has no such
    field, so Django resolved it to nothing on every payment ever made."""

    def test_the_reference_appears_against_the_instalment(self):
        from .services import payables as payable_svc
        bill = self._payable()
        payable_svc.settle(bill, amount=Decimal("40000"), user=self.treasurer,
                           on=PAID_ON, reference="FT26072099")

        self.assertEqual(bill.payments.first().voucher_no, "FT26072099",
                         "the reference is stored on voucher_no")
        body = self.client.get(reverse("accruals")).content.decode()
        self.assertIn("FT26072099", body,
                      "the payment reference does not appear on the payables "
                      "screen, so the list cannot be tied to a statement")


class ThePettyTinCannotGoNegative(_CashbookRuleTest):
    """One rule for both screens that take cash out of the box."""

    FLOAT = Decimal("5000")
    ISSUED_ON = dt.date(2026, 7, 15)

    def setUp(self):
        super().setUp()
        PettyCashTopUp.objects.create(
            date=dt.date(2026, 7, 1), amount=self.FLOAT,
            recorded_by=self.treasurer)

    def _float(self, on=None):
        from .services.treasury_position import petty_balance_asof
        return petty_balance_asof(on or self.ISSUED_ON)

    def _issue(self, amount, charge):
        self.client.post(reverse("advance_new"), {
            "staff_name": "Deacon Wanjiru", "department": self.fund.pk,
            "amount": str(amount), "date_issued": self.ISSUED_ON.isoformat(),
            "purpose": "Harvest programme", "method": "MPESA",
            "from_petty_cash": "1", "bank_charge": str(charge),
            "reference": ""}, follow=True)
        return StaffAdvance.objects.filter(staff_name="Deacon Wanjiru").first()

    def test_the_sending_charge_is_weighed_with_the_cash(self):
        self.assertIsNone(
            self._issue(self.FLOAT, Decimal("200")),
            "An advance for the whole float plus a 200 charge was issued. The "
            "charge is booked as a further petty disbursement, so the box was "
            "left holding minus 200.")
        self.assertEqual(self._float(), self.FLOAT,
                         "the refused advance still moved money")
        self.assertGreaterEqual(self._float(), Decimal("0"))

    def test_an_advance_that_fits_with_its_charge_is_issued(self):
        advance = self._issue(Decimal("4800"), Decimal("200"))
        self.assertIsNotNone(advance, "4,800 plus a 200 charge is exactly the "
                                      "5,000 float and must be allowed")
        self.assertEqual(self._float(), Decimal("0"))

    def test_a_top_up_that_would_overdraw_the_tin_is_refused(self):
        """The screen that already had the rule right must keep it — now from
        the shared function rather than from its own copy of the arithmetic."""
        advance = self._issue(Decimal("1000"), Decimal("0"))
        self.assertEqual(self._float(), Decimal("4000"))

        self.client.post(reverse("advance_topup", args=[advance.pk]), {
            "amount": "4000", "charge": "200",
            "date": self.ISSUED_ON.isoformat(), "note": ""}, follow=True)

        advance.refresh_from_db()
        self.assertEqual(advance.amount, Decimal("1000"),
                         "the top-up was issued out of a tin that could not "
                         "cover it once the sending charge was counted")
        self.assertEqual(self._float(), Decimal("4000"))
