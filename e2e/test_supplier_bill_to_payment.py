"""A supplier's invoice, from the day it arrives to the day it is fully paid.

The process a church office actually runs. The builder delivers, the invoice
comes in on terms, it sits on the balance sheet as money owed, it is paid in
instalments as the offerings come in, and when the last shilling goes the debt
disappears — from the statement, and from the supplier's account.

Every step of that has unit tests. What none of them can show is that the
figure is right at the SEAMS:

* the liability on a 31 July statement must net the invoice by the payments
  made ON OR BEFORE 31 July, and by no others — a cheque written in August
  cannot retrospectively reduce July's debt, and that is the single easiest
  arithmetic in this file to get wrong;
* the supplier's own account and the balance sheet must reach the same figure
  by different routes (one sums `balance_asof` per invoice in Python, the other
  is a single annotated SQL query) — this codebase's most frequent defect is
  exactly two such routes drifting apart;
* paying more than is owed must be refused rather than quietly capped;
* a payable that is already discharged must not be payable a second time. That
  one is not hypothetical: a legacy row carrying a `settled` flag with no
  payment rows behind it was, until recently, settleable again — posting real
  money to the ledger twice for a debt already paid.

Both halves of the liability note are walked, because payables and accruals
share one implementation and a rule fixed on one side has, in this codebase,
been left unfixed on the other.
"""
import datetime as dt
from decimal import Decimal

from django.urls import reverse

from cashbook.models import Accrual, Expense, Payable
from vendors.models import Vendor

from .base import BusinessWorkflowTest, WorkflowError

# The invoice arrives in July and is finished off in August, deliberately: the
# whole point of the as-at rule is that the two months disagree.
FUND_OPENED = dt.date(2026, 7, 1)
INVOICE_DATE = dt.date(2026, 7, 10)
PART_PAID_ON = dt.date(2026, 7, 20)
JULY_END = dt.date(2026, 7, 31)
FINAL_PAID_ON = dt.date(2026, 8, 5)
AUGUST_CUTOFF = dt.date(2026, 8, 6)

CENT = Decimal("0.01")


def shillings(value):
    """A money figure at one fixed scale.

    Every figure handed to `assert_agree` goes through here. The harness
    compares `str(Decimal(v))`, so 60000 and 60000.00 — the same money, read
    off a model field and off an aggregate — are reported as disagreeing. That
    is a false alarm about the very thing this suite exists to detect, so the
    scale is settled before the comparison rather than argued about inside it.
    """
    return Decimal(value or 0).quantize(CENT)


OPENING_GIFT = Decimal("500000")
INVOICE = Decimal("100000")
FIRST_INSTALMENT = Decimal("40000")
BALANCE_AFTER_PART = INVOICE - FIRST_INSTALMENT          # 60,000
OTHER_SUPPLIER_BILL = Decimal("25000")


class SupplierBillWorkflow(BusinessWorkflowTest):
    """Shared ground: a church with money in the bank, a supplier register that
    already has somebody in it, and an unpaid bill from a second supplier.

    The register is populated on purpose. Failure #125 in this project's history
    was a page that worked only on an empty record; a liability figure asserted
    against a database holding exactly one payable proves nothing about the
    per-row netting that keeps one supplier's payment from cancelling another's
    debt.
    """

    def setUp(self):
        super().setUp()
        self.office = self.acting_as(self.treasurer)

        # Money in the fund before anything is spent from it — through a real
        # receipt, because a fund balance conjured by a fixture would not be in
        # the ledger and the closing assertions would be measuring nothing.
        from giving.models import Transaction
        Transaction.objects.create(
            date=FUND_OPENED, amount=OPENING_GIFT, direction="CREDIT",
            channel="BANK", confirmed=True, allocation_status="MANUAL",
            department=self.local_fund, payer_name="HARVEST THANKSGIVING")

        # A supplier the church already deals with, and an open bill from them.
        # Background, but real background: it goes in through the same screens.
        self.other_supplier = self._create_supplier(
            "Kilimo Feeds Ltd", terms=Vendor.Terms.NET14, code="KF")
        self._raise_payable(
            supplier=self.other_supplier, amount=OTHER_SUPPLIER_BILL,
            description="Grounds fertiliser", date=dt.date(2026, 7, 5))

    # -- private helpers (this file's own; the harness has no supplier verbs) --

    def assert_agree(self, description, **figures):
        """`BusinessWorkflowTest.assert_agree`, at one money scale.

        The harness compares `str(Decimal(v))`, which makes 60000 (a model
        field) and 60000.00 (a SQL aggregate) look like a disagreement. Every
        figure in this file is a two-decimal money column, so the scale is
        fixed here and the comparison is left to say what it means. A real
        difference of a cent or more still fails.
        """
        return super().assert_agree(
            description, **{name: shillings(value)
                            for name, value in figures.items()})

    def _create_supplier(self, name, terms=Vendor.Terms.NET30, code="",
                         credit_limit=""):
        """Register a supplier the way the office does — the register's own form."""
        self.submit(self.office, "vendor_create", {
            "action": "save", "name": name, "code": code,
            "status": Vendor.Status.ACTIVE, "payment_terms": terms,
            "credit_limit": credit_limit,
            "phone": "0722000111", "email": "accounts@example.co.ke",
            "notes": "Opened by the treasurer.",
        })
        vendor = Vendor.objects.filter(name=name).first()
        if vendor is None:
            raise WorkflowError(
                f"The supplier form accepted a POST but no “{name}” exists in "
                f"the register — the first step of the workflow did nothing.")
        return vendor

    def _raise_payable(self, *, supplier, amount, description,
                       date=INVOICE_DATE, due_date="", fund=None):
        """Enter the invoice on the payables screen."""
        self.submit(self.office, "payable_create", {
            "date": date.isoformat(),
            "supplier": supplier.pk if supplier else "",
            "vendor": "",                     # filled from the supplier
            "description": description,
            "amount": str(amount),
            "department": (fund or self.local_fund).pk,
            "category": Expense.Category.MAINTENANCE,
            "due_date": due_date,
        })
        payable = Payable.objects.filter(description=description).first()
        if payable is None:
            raise WorkflowError(
                f"“{description}” was submitted to payable_create and no "
                f"payable exists. The bill was never raised.")
        return payable

    def _pay(self, payable, amount=None, on=PART_PAID_ON, reference="",
             expect_refusal=False):
        """Pay some or all of a payable through the settle button."""
        data = {"paid_on": on.isoformat(), "method": Expense.Method.BANK,
                "reference": reference}
        if amount is not None:
            data["amount"] = str(amount)
        response = self.submit(self.office, "payable_settle", data,
                               args=[payable.pk])
        errors = self._error_messages(response)
        if expect_refusal and not errors:
            raise WorkflowError(
                "The payment was accepted. It should have been refused.")
        if errors and not expect_refusal:
            raise WorkflowError(
                "Paying the supplier was refused: " + "; ".join(errors))
        payable.refresh_from_db()
        return response

    @staticmethod
    def _error_messages(response):
        """The error flashes on a rendered response.

        Needed because the settle and create views do not re-render a bound
        form on rejection — they flash a message and redirect — so the
        harness's form-error trap cannot see them, and a refused payment would
        otherwise read as a successful one.
        """
        context = getattr(response, "context", None)
        if not context:
            return []
        try:
            flashes = context.get("messages") or []
        except Exception:
            return []
        return [str(m) for m in flashes if "error" in (m.level_tag or "")]

    def _balance_sheet(self, as_of):
        """The Statement of Financial Position as a treasurer opens it."""
        return self.visit(self.office, "report_financial_position",
                          query=f"?as_of={as_of.isoformat()}")

    def _sofp_figure(self, as_of, key):
        response = self._balance_sheet(as_of)
        value = response.context.get(key)
        if value is None:
            raise WorkflowError(
                f"The Statement of Financial Position as at {as_of} carries no "
                f"“{key}” line at all.")
        return Decimal(value)


class SupplierBillFromArrivalToFullyPaid(SupplierBillWorkflow):

    def test_the_bill_arrives_is_part_paid_then_cleared_and_leaves_the_books(self):
        # 1. the builder is new to the church: the treasurer opens an account for
        #    them in the supplier register, on 30-day terms
        supplier = self._create_supplier("Mwangi Hardware Ltd",
                                         terms=Vendor.Terms.NET30, code="MH")
        self.assertEqual(supplier.payment_terms, Vendor.Terms.NET30)

        # 2. the invoice arrives and is entered against that supplier. The due
        #    date is left blank — the terms on the account are supposed to work
        #    it out, which is the only reason to record terms at all.
        bill = self._raise_payable(supplier=supplier, amount=INVOICE,
                                   description="Roofing sheets and timber")
        self.assertEqual(bill.supplier_id, supplier.pk)
        self.assertEqual(
            bill.due_date, dt.date(2026, 8, 9),
            "30-day terms on the supplier account did not set the due date")
        self.assertEqual(
            bill.vendor, "Mwangi Hardware Ltd",
            "the name on the invoice should default to the supplier's")

        # 3. before a shilling moves it is a LIABILITY on the statement, and the
        #    fund still holds everything it held.
        self.assert_agree(
            "the unpaid invoice as at 31 July",
            balance_sheet=self._sofp_figure(JULY_END, "payables"),
            invoice_plus_the_other_supplier=INVOICE + OTHER_SUPPLIER_BILL,
        )
        self.assert_fund_balance(self.local_fund, OPENING_GIFT, as_of=JULY_END)

        # 4. part payment: 40,000 on 20 July, the rest to follow
        self._pay(bill, FIRST_INSTALMENT, on=PART_PAID_ON, reference="FT26072001")
        self.assertFalse(bill.settled, "a part-paid bill is not settled")
        self.assertEqual(bill.balance, BALANCE_AFTER_PART)

        # 5. the liability drops by EXACTLY what was paid, on the day it was
        #    paid — read off the statement, not off the model
        self.assert_agree(
            "the liability after one instalment, as at 31 July",
            balance_sheet=self._sofp_figure(JULY_END, "payables"),
            invoice_less_instalment=BALANCE_AFTER_PART + OTHER_SUPPLIER_BILL,
        )
        # and the money really left the fund on the 20th
        self.assert_fund_balance(self.local_fund,
                                 OPENING_GIFT - FIRST_INSTALMENT, as_of=JULY_END)

        # 6. the supplier's account must reach the same figure by its own route
        from vendors.services import accounts as account_svc
        self.assert_agree(
            "what is owed to this one supplier at 31 July",
            supplier_account=account_svc.outstanding(supplier, JULY_END),
            the_bill_itself=bill.balance_asof(JULY_END),
            statement_less_the_other_supplier=(
                self._sofp_figure(JULY_END, "payables") - OTHER_SUPPLIER_BILL),
        )

        # 7. the balance is cleared in August: no amount means "the rest of it",
        #    which is what the Pay button does when the box is left empty
        self._pay(bill, None, on=FINAL_PAID_ON, reference="FT26080501")
        self.assertTrue(bill.settled, "the final instalment did not close the bill")
        self.assertEqual(bill.settled_on, FINAL_PAID_ON,
                         "settled_on must be the date the LAST instalment cleared it")
        self.assertEqual(bill.balance, Decimal("0"))

        # 8. it has left the liability entirely — and only from August. July's
        #    statement is untouched by a payment made after it.
        self.assert_agree(
            "the same 31 July statement, re-read after August's payment",
            balance_sheet=self._sofp_figure(JULY_END, "payables"),
            unchanged=BALANCE_AFTER_PART + OTHER_SUPPLIER_BILL,
        )
        self.assert_agree(
            "the liability once the bill is cleared, as at 6 August",
            balance_sheet=self._sofp_figure(AUGUST_CUTOFF, "payables"),
            only_the_other_supplier=OTHER_SUPPLIER_BILL,
        )

        # 9. the money: 100,000 has left the fund in two payments, the ledger is
        #    whole, and the statement still ties.
        self.assert_fund_balance(self.local_fund, OPENING_GIFT - INVOICE,
                                 as_of=AUGUST_CUTOFF)
        self.assert_agree(
            "the two instalments against the invoice",
            payments_recorded=sum(
                (e.amount for e in bill.payments.all()), Decimal("0")),
            invoice_total=INVOICE,
        )
        self.assert_books_balance("after settling a supplier bill in two payments")
        self.assert_trial_balance_balances()
        self.assertTrue(
            self._balance_sheet(AUGUST_CUTOFF).context["balanced"],
            "the Statement of Financial Position does not balance after the "
            "bill was settled")

    def test_a_payment_made_in_august_does_not_reduce_the_july_statement(self):
        """The as-at rule, on its own, because it is the seam that breaks.

        A treasurer closing July prints the statement in mid-August. Anything
        the office has paid since the 1st must not appear to have been paid in
        July — the July liability is what was owed ON 31 July.
        """
        supplier = self._create_supplier("Bahati Electrical", code="BE")
        bill = self._raise_payable(supplier=supplier, amount=INVOICE,
                                   description="Rewiring the hall")

        self._pay(bill, FIRST_INSTALMENT, on=FINAL_PAID_ON)   # 5 August

        july = self._sofp_figure(JULY_END, "payables")
        august = self._sofp_figure(AUGUST_CUTOFF, "payables")

        self.assert_agree(
            "July owes the whole invoice; the August payment is not July's",
            july_statement=july,
            invoice_untouched=INVOICE + OTHER_SUPPLIER_BILL,
        )
        self.assert_agree(
            "August owes the invoice less the August payment",
            august_statement=august,
            invoice_less_payment=(INVOICE - FIRST_INSTALMENT
                                  + OTHER_SUPPLIER_BILL),
        )
        # The same figure, computed the other way: per-row `balance_asof` in
        # Python versus the annotated query the statement uses.
        from cashbook.services.treasury_position import open_payables_total
        self.assert_agree(
            "31 July payables, two implementations",
            annotated_query=open_payables_total(JULY_END),
            per_row_in_python=sum((p.balance_asof(JULY_END)
                                   for p in Payable.objects.all()), Decimal("0")),
            statement_page=july,
        )
        self.assert_books_balance("after an August payment on a July invoice")

    def test_paying_more_than_is_owed_is_refused(self):
        """Not capped, not written off — refused, with nothing posted.

        An overpayment is either a typo or a credit the supplier now holds, and
        the church has to decide which. Silently capping it would post the
        smaller figure to the ledger while the bank shows the larger.
        """
        supplier = self._create_supplier("Nyeri Timber Yard", code="NT")
        bill = self._raise_payable(supplier=supplier, amount=INVOICE,
                                   description="Ceiling boards")
        self._pay(bill, FIRST_INSTALMENT, on=PART_PAID_ON)

        payments_before = bill.payments.count()
        expenses_before = Expense.objects.count()

        # 70,000 against a 60,000 balance
        self._pay(bill, Decimal("70000"), on=PART_PAID_ON, expect_refusal=True)

        self.assertEqual(bill.payments.count(), payments_before,
                         "a refused overpayment still created a payment")
        self.assertEqual(Expense.objects.count(), expenses_before,
                         "a refused overpayment still posted an expense")
        self.assertEqual(bill.balance, BALANCE_AFTER_PART)
        self.assert_fund_balance(self.local_fund,
                                 OPENING_GIFT - FIRST_INSTALMENT, as_of=JULY_END)
        self.assert_books_balance("after an overpayment was refused")

    def test_a_bill_already_settled_cannot_be_settled_again(self):
        """Pay it in full, then press Pay again. Nothing may move."""
        supplier = self._create_supplier("Karatina Cement", code="KC")
        bill = self._raise_payable(supplier=supplier, amount=INVOICE,
                                   description="Cement, 60 bags")
        self._pay(bill, None, on=PART_PAID_ON)
        self.assertTrue(bill.settled)

        expenses_before = Expense.objects.count()
        self._pay(bill, None, on=FINAL_PAID_ON, expect_refusal=True)

        self.assertEqual(
            Expense.objects.count(), expenses_before,
            "a settled payable was paid a second time — real money posted "
            "twice for one invoice")
        self.assert_fund_balance(self.local_fund, OPENING_GIFT - INVOICE,
                                 as_of=AUGUST_CUTOFF)
        self.assert_books_balance("after a second settlement was refused")

    def test_a_legacy_row_flagged_settled_with_no_payments_cannot_be_paid(self):
        """The CRITICAL audit finding, walked.

        Before instalments existed a settlement set the flag and did not always
        record the expense link, so the database still holds rows that say
        "settled" with nothing behind them. `balance_asof` treats such a row as
        discharged — the flag is the only evidence there is — and `settle()`
        must therefore refuse it. When the read path and the write path
        disagreed about that row, pressing Pay posted the whole invoice again.

        The legacy row is written with a queryset UPDATE rather than through a
        screen ON PURPOSE: no current screen can produce this shape, because
        the bug that produced it is fixed. It is pre-existing data, not a step
        of the workflow.
        """
        supplier = self._create_supplier("Old Ledger Traders", code="OLT")
        bill = self._raise_payable(supplier=supplier, amount=INVOICE,
                                   description="Pews, refurbished (2019)")
        Payable.objects.filter(pk=bill.pk).update(
            settled=True, settled_on=dt.date(2026, 7, 12))
        bill.refresh_from_db()
        self.assertEqual(bill.payments.count(), 0)

        expenses_before = Expense.objects.count()
        fund_before = self._sofp_figure(JULY_END, "payables")

        self._pay(bill, None, on=PART_PAID_ON, expect_refusal=True)

        self.assertEqual(
            Expense.objects.count(), expenses_before,
            "a payable flagged settled with no payments was paid AGAIN — "
            "100,000 of real money posted to the ledger for a debt the "
            "church had already discharged")
        self.assert_agree(
            "the statement is unmoved by the refused second payment",
            before=fund_before,
            after=self._sofp_figure(JULY_END, "payables"),
        )
        self.assert_books_balance("after refusing to pay a legacy settled row")

    def test_the_supplier_account_shows_the_whole_history(self):
        """Where the workflow ends: the treasurer opens the supplier's account.

        A bill entered, paid twice and then invisible on the account it belongs
        to is the shape of failure this suite exists for — the money is right
        and the person cannot see it.
        """
        supplier = self._create_supplier("Meru Glassworks", code="MG")
        bill = self._raise_payable(supplier=supplier, amount=INVOICE,
                                   description="Window panes")
        self._pay(bill, FIRST_INSTALMENT, on=PART_PAID_ON, reference="FT001")
        self._pay(bill, None, on=FINAL_PAID_ON, reference="FT002")

        page = self.visit(self.office, "vendor_detail", args=[supplier.pk])
        summary = page.context["summary"]
        rows = page.context["transactions"]

        kinds = [r["kind"] for r in rows]
        self.assertEqual(
            kinds.count("Invoice"), 1,
            f"the invoice is missing from the supplier's account: {kinds}")
        self.assertEqual(
            kinds.count("Payment"), 2,
            f"both instalments should appear on the account: {kinds}")

        self.assert_agree(
            "the supplier owes nothing and was paid the invoice in full",
            still_owed=summary["outstanding"],
            nothing=Decimal("0"),
        )
        self.assert_agree(
            "total spend with this supplier",
            supplier_account=summary["total_spend"],
            invoice=INVOICE,
            instalments_added_up=-sum((r["amount"] for r in rows
                                       if r["kind"] == "Payment"), Decimal("0")),
        )
        self.assertEqual(summary["payment_count"], 2)

        # the ageing table must not still be carrying a paid bill
        self.assert_agree(
            "nothing outstanding, nothing ageing",
            ageing_total=page.context["ageing"]["total"],
            nothing=Decimal("0"),
        )

    def test_every_page_the_workflow_touches_opens(self):
        """The register, the bill, the statement and the account.

        Reversing a URL in a unit test is not the step a user takes; this walks
        the doors in the order the office does.
        """
        supplier = self._create_supplier("Embu Plumbing", code="EP")
        bill = self._raise_payable(supplier=supplier, amount=INVOICE,
                                   description="Sanitary fittings")
        self._pay(bill, FIRST_INSTALMENT, on=PART_PAID_ON)

        self.visit(self.office, "vendor_list")
        self.visit(self.office, "vendor_detail", args=[supplier.pk])
        self.visit(self.office, "accruals")
        self.visit(self.office, "payable_edit", args=[bill.pk])
        self._balance_sheet(JULY_END)

        # the bill must be findable by searching the register for the supplier
        found = self.visit(self.office, "vendor_list", query="?q=Embu")
        names = [row["vendor"].name for row in found.context["rows"]]
        self.assertIn("Embu Plumbing", names,
                      "a supplier just created cannot be found in the register")

        # and the payables screen must show the bill as part paid, with what is
        # still owed on it
        page = self.visit(self.office, "accruals")
        body = page.content.decode()
        self.assertIn("Sanitary fittings", body,
                      "the bill does not appear on the payables screen")
        self.assertIn("60,000", body,
                      "the payables screen does not show what is still owed")

    def test_an_auditor_can_read_the_supplier_account_and_cannot_pay_a_bill(self):
        """Segregation, walked: the read-only role sees the debt and cannot
        discharge it."""
        supplier = self._create_supplier("Thika Roofing", code="TR")
        bill = self._raise_payable(supplier=supplier, amount=INVOICE,
                                   description="Gutters")

        reading_room = self.acting_as(self.auditor)
        self.visit(reading_room, "vendor_detail", args=[supplier.pk])
        self.visit(reading_room, "accruals")

        reading_room.post(reverse("payable_settle", args=[bill.pk]),
                          {"amount": str(INVOICE)})
        bill.refresh_from_db()
        self.assertFalse(bill.settled, "a read-only auditor paid a supplier")
        self.assertEqual(bill.payments.count(), 0)
        self.assert_fund_balance(self.local_fund, OPENING_GIFT, as_of=JULY_END)


class AccrualFromEstimateToPaid(SupplierBillWorkflow):
    """The other half of the liability note.

    An accrual is the same obligation with no invoice behind it — the power
    bill the church knows is coming. It shares `SettleableObligation` and
    `services.payables.settle` with the payable, which is exactly why it is
    walked separately: a rule fixed on one side of a shared implementation has
    in this codebase been left unfixed on the other.
    """

    ESTIMATE = Decimal("18000")
    PART = Decimal("7000")

    def _accrue(self, description, amount, date=INVOICE_DATE):
        self.submit(self.office, "accrual_create", {
            "date": date.isoformat(), "description": description,
            "amount": str(amount), "department": self.local_fund.pk,
            "category": Expense.Category.UTILITIES,
        })
        accrual = Accrual.objects.filter(description=description).first()
        if accrual is None:
            raise WorkflowError(
                f"“{description}” was submitted to accrual_create and no "
                f"accrual exists.")
        return accrual

    def _settle_accrual(self, accrual, amount=None, on=PART_PAID_ON,
                        expect_refusal=False):
        data = {"paid_on": on.isoformat(), "method": Expense.Method.BANK}
        if amount is not None:
            data["amount"] = str(amount)
        response = self.submit(self.office, "accrual_settle", data,
                               args=[accrual.pk])
        errors = self._error_messages(response)
        if expect_refusal and not errors:
            raise WorkflowError("The accrual payment should have been refused.")
        if errors and not expect_refusal:
            raise WorkflowError("Settling the accrual was refused: "
                                + "; ".join(errors))
        accrual.refresh_from_db()
        return response

    def test_an_accrual_is_raised_part_paid_and_cleared(self):
        # 1. the treasurer accrues July's electricity before the bill arrives
        accrual = self._accrue("Electricity, July estimate", self.ESTIMATE)
        self.assert_agree(
            "the accrual on the 31 July statement",
            balance_sheet=self._sofp_figure(JULY_END, "accruals"),
            the_estimate=self.ESTIMATE,
        )

        # 2. part of it is paid on account in July
        self._settle_accrual(accrual, self.PART, on=PART_PAID_ON)
        self.assertFalse(accrual.settled)
        self.assert_agree(
            "the accrual after a payment on account",
            balance_sheet=self._sofp_figure(JULY_END, "accruals"),
            estimate_less_payment=self.ESTIMATE - self.PART,
        )

        # 3. the rest goes in August — and must not touch July
        self._settle_accrual(accrual, None, on=FINAL_PAID_ON)
        self.assertTrue(accrual.settled)
        self.assertEqual(accrual.settled_on, FINAL_PAID_ON)
        self.assert_agree(
            "July is unchanged by August's payment",
            july_statement=self._sofp_figure(JULY_END, "accruals"),
            estimate_less_july_payment=self.ESTIMATE - self.PART,
        )
        self.assert_agree(
            "the accrual is gone by 6 August",
            august_statement=self._sofp_figure(AUGUST_CUTOFF, "accruals"),
            nothing=Decimal("0"),
        )

        # 4. the money left the fund, once, in two pieces
        self.assert_fund_balance(self.local_fund, OPENING_GIFT - self.ESTIMATE,
                                 as_of=AUGUST_CUTOFF)
        self.assert_books_balance("after settling an accrual in two payments")
        self.assert_trial_balance_balances()

    def test_an_accrual_cannot_be_overpaid_or_paid_twice(self):
        accrual = self._accrue("Water, July estimate", self.ESTIMATE)
        self._settle_accrual(accrual, self.PART, on=PART_PAID_ON)

        expenses_before = Expense.objects.count()
        self._settle_accrual(accrual, self.ESTIMATE, on=PART_PAID_ON,
                             expect_refusal=True)
        self.assertEqual(Expense.objects.count(), expenses_before,
                         "an over-payment on an accrual was posted anyway")

        self._settle_accrual(accrual, None, on=PART_PAID_ON)
        self.assertTrue(accrual.settled)

        expenses_before = Expense.objects.count()
        self._settle_accrual(accrual, None, on=FINAL_PAID_ON,
                             expect_refusal=True)
        self.assertEqual(Expense.objects.count(), expenses_before,
                         "a settled accrual was paid a second time")
        self.assert_fund_balance(self.local_fund, OPENING_GIFT - self.ESTIMATE,
                                 as_of=AUGUST_CUTOFF)
        self.assert_books_balance("after refusing to overpay an accrual")

    def test_a_legacy_accrual_flagged_settled_with_no_payments_cannot_be_paid(self):
        """The same CRITICAL finding, on the accrual half of the shared code.

        As above, the legacy shape is written directly because no current
        screen can produce it.
        """
        accrual = self._accrue("Power, 2019 estimate", self.ESTIMATE)
        Accrual.objects.filter(pk=accrual.pk).update(
            settled=True, settled_on=dt.date(2026, 7, 12))
        accrual.refresh_from_db()

        expenses_before = Expense.objects.count()
        self._settle_accrual(accrual, None, on=PART_PAID_ON,
                             expect_refusal=True)
        self.assertEqual(
            Expense.objects.count(), expenses_before,
            "an accrual flagged settled with no payments was paid again")
        self.assert_books_balance("after refusing to pay a legacy accrual")


class DefectsFoundWalkingTheSupplierBill(SupplierBillWorkflow):
    """Three things the workflow did that it should not have.

    All three are now FIXED, and these are their regression tests. They were
    written first, while the defects were live, and each carried an
    `expectedFailure` marker so the suite stayed green and the finding was not
    lost. The marker came off when the fix landed — which is the whole point of
    writing the test before the fix: it was authored by someone describing the
    bug, not by someone defending a patch, so it cannot have been quietly
    shaped to fit whatever the fix happened to do.
    """

    def setUp(self):
        super().setUp()
        # Stated rather than assumed: the first defect below only appears in a
        # church that requires expenses to be approved — which is the default
        # and the strict path, but the test should say which world it is in.
        from core.models import SiteConfig
        cfg = SiteConfig.get()
        cfg.require_expense_approval = True
        cfg.require_different_approver = False   # one treasurer, as the church has
        cfg.save()

    # ---------------------------------------------------------------- CRITICAL
    def test_a_bill_paid_in_full_from_pay_in_detail_can_be_paid_all_over_again(self):
        """DEFECT: a 100,000 invoice can be paid TWICE — 200,000 out of the
        fund — without a single warning.

        The payables screen offers two Pay routes on the same row. The quick
        button goes through `services.payables.settle`, which refuses to pay a
        discharged bill (this suite's other test proves it). "Pay in detail"
        goes to `expense_create?settle=payable:N`, which does not.

        The mechanism, walked below:

        1. "Pay in detail" creates the expense and immediately calls
           `refresh_settlement`. The church requires approval, so the expense
           is PENDING at that moment, and a PENDING payment does not count
           towards a settlement. The payable is therefore left `settled=False`.
        2. A treasurer approves the payment. `ExpenseApprove`
           (cashbook/views.py:505) never refreshes the obligation, so the flag
           is still `False` on a bill that is now paid in full.
        3. `ExpenseCreate._settle_target` selects on `settled=False`, so the
           same bill is offered for settlement again, and takes a second
           payment of the full amount.

        This is the CRITICAL "settled row paid twice" finding, alive on the
        other route: the flag and the payments disagree, and the write path
        trusts the flag.
        """
        supplier = self._create_supplier("Twice Paid Ltd", code="TP")
        bill = self._raise_payable(supplier=supplier, amount=INVOICE,
                                   description="Pews, one invoice")

        self._pay_in_detail(bill, INVOICE, "First cheque", approve=True)
        bill.refresh_from_db()
        self._pay_in_detail(bill, INVOICE, "Second cheque", approve=True)
        bill.refresh_from_db()

        self.assert_agree(
            "one invoice, one invoice's worth of money",
            invoice=INVOICE,
            actually_paid=bill.paid_total,
        )
        self.assert_fund_balance(self.local_fund, OPENING_GIFT - INVOICE,
                                 as_of=AUGUST_CUTOFF)

    # -------------------------------------------------------------------- 500
    def test_registering_a_supplier_that_already_exists_says_so_instead_of_crashing(self):
        """DEFECT: the duplicate-supplier guard 500s on the create screen.

        `Vendor.clean()` raises a ValidationError naming the existing record —
        "Use that record, or change this name to tell them apart" — which is
        exactly the right message. `VendorSaveView.post` catches it and then
        does `redirect("vendor_detail", pk=vendor.pk)` (vendors/views.py:199)
        with `vendor` still None, because nothing was created. The treasurer
        gets a server error instead of the sentence written for them.

        The same crash answers a blank supplier name, which is the likelier way
        to meet it: press Save on an empty form.
        """
        self._create_supplier("Mwangi Hardware Ltd", code="MH")

        response = self.office.post(reverse("vendor_create"), {
            "action": "save", "name": "Mwangi Hardware Ltd",
            "status": Vendor.Status.ACTIVE,
            "payment_terms": Vendor.Terms.NET30}, follow=True)

        self.assertLess(response.status_code, 500,
                        "registering a duplicate supplier returned a server error")
        self.assertEqual(
            Vendor.objects.filter(name="Mwangi Hardware Ltd").count(), 1,
            "the duplicate was created anyway")

    # -------------------------------------------------------------------- minor
    def test_the_payment_reference_is_shown_against_the_instalment(self):
        """DEFECT: the bank reference recorded against an instalment is never
        displayed.

        `PayableSettle` takes a `reference` and stores it on the expense's
        `voucher_no` — the view says so in a comment, because `Expense` has no
        `reference` field. The payables screen then renders
        `{% if pay.reference %}{{ pay.reference }}{% endif %}`
        (templates/cashbook/accruals.html), which resolves to nothing on every
        payment ever made. The instalment list shows a date and an amount and
        no way to tie either to the bank statement — which is the one thing the
        list is for.
        """
        supplier = self._create_supplier("Naivasha Supplies", code="NS")
        bill = self._raise_payable(supplier=supplier, amount=INVOICE,
                                   description="Chairs")
        self._pay(bill, FIRST_INSTALMENT, on=PART_PAID_ON, reference="FT26072099")

        self.assertEqual(bill.payments.first().voucher_no, "FT26072099",
                         "the reference was not recorded at all")
        self.assertIn(
            "FT26072099", self.visit(self.office, "accruals").content.decode(),
            "the payment reference does not appear on the payables screen")

    # -- helper used only by the defect walk ----------------------------------

    def _pay_in_detail(self, payable, amount, label, approve=False):
        """The other Pay route on the payables row: the full expense form.

        Not `submit()`, because this POST is expected to be accepted when it
        should not be — the defect IS the acceptance.
        """
        self.office.post(
            reverse("expense_create") + f"?settle=payable:{payable.pk}", {
                "settle": f"payable:{payable.pk}",
                "date": PART_PAID_ON.isoformat(),
                "department": self.local_fund.pk,
                "description": label, "amount": str(amount),
                "category": Expense.Category.MAINTENANCE,
                "expenditure_type": "RECURRENT",
                "method": Expense.Method.BANK,
                "payee": payable.vendor,
            }, follow=True)
        expense = Expense.objects.filter(description=label).first()
        if expense is None:
            raise WorkflowError(
                f"“{label}” was never recorded — the Pay-in-detail form "
                f"rejected it, so this walk cannot continue.")
        if approve:
            self.submit(self.office, "expense_approve", {"action": "approve"},
                        args=[expense.pk])
        return expense
