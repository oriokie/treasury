"""Functional review finding: Expense.amount (and every other money-movement
model in cashbook) had no positivity validation, unlike Transaction.amount
which already enforced MinValueValidator(0.01). A negative "expense" would
post as an unreviewed credit to cash while still being categorised as an
expense — bypassing income recognition entirely. Now every genuine money-
movement amount field is consistently protected."""
import datetime as dt
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.contrib.auth.models import User
from departments.models import Department
from cashbook.models import (Expense, ExpenseRefund, FundTransfer, RecurringExpense,
    PettyCashTopUp, Payable, Accrual, Prepayment, StaffAdvance, AdvanceTopUp,
    ChequeRegister, PaymentInstrument)


class ExpenseAmountValidationTests(TestCase):
    def setUp(self):
        self.tr = User.objects.create_user("tr_amtval", password="x")
        self.d = Department.objects.create(name="AmtValFund", fund_type="LOCAL",
            category="MINISTRY")

    def _expense(self, amount):
        return Expense(date=dt.date(2026, 6, 1), department=self.d,
            description="test", amount=amount, category="OTHER",
            recorded_by=self.tr)

    def test_negative_amount_rejected(self):
        with self.assertRaises(ValidationError):
            self._expense(Decimal("-500")).full_clean()

    def test_zero_amount_rejected(self):
        with self.assertRaises(ValidationError):
            self._expense(Decimal("0")).full_clean()

    def test_positive_amount_accepted(self):
        self._expense(Decimal("500")).full_clean()   # must not raise

    def test_form_rejects_negative(self):
        from cashbook.forms import ExpenseForm
        form = ExpenseForm(data={"date": "2026-06-01", "department": str(self.d.id),
            "description": "t", "amount": "-100", "category": "OTHER", "method": "CASH"})
        self.assertFalse(form.is_valid())
        self.assertIn("amount", form.errors)


class OtherMoneyMovementAmountValidationTests(TestCase):
    """Every other genuine money-movement amount field in cashbook gets the
    same protection, for consistency (Transaction.amount already had it)."""
    def setUp(self):
        self.tr = User.objects.create_user("tr_amtval2", password="x")
        self.d = Department.objects.create(name="AmtValFund2", fund_type="LOCAL",
            category="MINISTRY")
        self.exp = Expense.objects.create(date=dt.date(2026, 6, 1), department=self.d,
            description="base", amount=Decimal("100"), category="OTHER",
            recorded_by=self.tr)

    def test_expense_refund_rejects_negative(self):
        with self.assertRaises(ValidationError):
            ExpenseRefund(expense=self.exp, date=dt.date(2026, 6, 2),
                amount=Decimal("-50"), recorded_by=self.tr).full_clean()

    def test_fund_transfer_rejects_negative(self):
        d2 = Department.objects.create(name="AmtValFund3", fund_type="LOCAL",
            category="MINISTRY")
        with self.assertRaises(ValidationError):
            FundTransfer(date=dt.date(2026, 6, 1), source=self.d,
                destination=d2, amount=Decimal("-100")).full_clean()

    def test_recurring_expense_rejects_negative(self):
        with self.assertRaises(ValidationError):
            RecurringExpense(department=self.d, description="rent",
                amount=Decimal("-500"), frequency="MONTHLY",
                start_date=dt.date(2026, 1, 1)).full_clean()

    def test_petty_topup_rejects_negative(self):
        with self.assertRaises(ValidationError):
            PettyCashTopUp(date=dt.date(2026, 6, 1), amount=Decimal("-1000"),
                recorded_by=self.tr).full_clean()

    def test_payable_rejects_negative(self):
        with self.assertRaises(ValidationError):
            Payable(description="supplier", amount=Decimal("-100"),
                department=self.d).full_clean()

    def test_accrual_rejects_negative(self):
        with self.assertRaises(ValidationError):
            Accrual(description="accrued cost", amount=Decimal("-100"),
                department=self.d).full_clean()

    def test_prepayment_rejects_negative(self):
        with self.assertRaises(ValidationError):
            Prepayment(description="prepaid", amount=Decimal("-100"),
                department=self.d).full_clean()

    def test_staff_advance_rejects_negative(self):
        with self.assertRaises(ValidationError):
            StaffAdvance(staff_name="X", department=self.d, amount=Decimal("-1000"),
                date_issued=dt.date(2026, 6, 1), purpose="x", method="CASH",
                issued_by=self.tr).full_clean()

    def test_advance_topup_rejects_negative(self):
        adv = StaffAdvance.objects.create(staff_name="Y", department=self.d,
            amount=Decimal("1000"), date_issued=dt.date(2026, 6, 1), purpose="x",
            method="CASH", issued_by=self.tr)
        with self.assertRaises(ValidationError):
            AdvanceTopUp(advance=adv, date=dt.date(2026, 6, 2),
                amount=Decimal("-500")).full_clean()

    def test_cheque_register_rejects_negative(self):
        with self.assertRaises(ValidationError):
            ChequeRegister(cheque_number="001", amount=Decimal("-1000"),
                date_issued=dt.date(2026, 6, 1)).full_clean()

    def test_payment_instrument_rejects_negative(self):
        with self.assertRaises(ValidationError):
            PaymentInstrument(amount=Decimal("-1000")).full_clean()
