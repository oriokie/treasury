"""Taking bank-register exceptions to the books — the four dispositions.

The point of these tests is the ACCOUNTING, not the plumbing: a banking deposit
must not recognise income or touch a fund (the money was booked when the cash was
counted); an "already booked" close must create nothing; a bank charge posts an
expense; only a genuine new movement adds money, and it lands in the review queue.
Bulk actions must skip items the disposition does not fit and say why.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from statements.models import BankAccount
from statements.models_register import RegisterException, StatementLine
from statements.services import exceptions_intake as ei
from reports.services import balances

TODAY = dt.date(2026, 4, 13)


class DispositionFixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("t_ex", password="x")
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.account = BankAccount.objects.create(
            name="Main", is_default=True, active=True)
        self.fund = Department.objects.create(
            name="Charges Fund", slug="charges-fund",
            fund_type=Department.FundType.LOCAL)

    def _line(self, *, credit=None, debit=None, narr="X", ref="R1"):
        return StatementLine.objects.create(
            account=self.account, date=TODAY, credit=credit, debit=debit,
            core_ref="CR1", mpesa_ref="", receipt="", reference=ref,
            raw_narration=narr, dedup_key=f"K{StatementLine.objects.count()}")

    def _exc(self, *, credit=None, debit=None, narr="X"):
        line = self._line(credit=credit, debit=debit, narr=narr)
        amount = (credit or Decimal(0)) - (debit or Decimal(0))
        return RegisterException.objects.create(
            account=self.account,
            kind=RegisterException.Kind.MISSING_IN_LEDGER,
            line=line, date=TODAY, amount=amount, detail=narr)


class ApplicabilityTests(DispositionFixture):
    def test_credit_offers_banking_not_bank_charge(self):
        exc = self._exc(credit=Decimal("5000"))
        d = ei.applicable_dispositions(exc)
        self.assertIn(ei.BANKING, d)
        self.assertIn(ei.NEW_MOVEMENT, d)
        self.assertIn(ei.ALREADY_BOOKED, d)
        self.assertNotIn(ei.BANK_CHARGE, d)

    def test_debit_offers_bank_charge_not_banking(self):
        exc = self._exc(debit=Decimal("250"))
        d = ei.applicable_dispositions(exc)
        self.assertIn(ei.BANK_CHARGE, d)
        self.assertNotIn(ei.BANKING, d)

    def test_missing_in_bank_offers_nothing(self):
        txn = Transaction.objects.create(
            date=TODAY, channel="BANK", direction="CREDIT", amount=Decimal("10"),
            allocation_status="MANUAL", confirmed=True)
        exc = RegisterException.objects.create(
            account=self.account, kind=RegisterException.Kind.MISSING_IN_BANK,
            transaction=txn, date=TODAY, amount=Decimal("10"))
        self.assertEqual(ei.applicable_dispositions(exc), [])


class BankingDispositionTests(DispositionFixture):
    def test_banking_does_not_recognise_income_or_touch_a_fund(self):
        income_before = balances.income_by_channel().get("BANK", Decimal(0)) \
            if False else None
        fund_before = balances.fund_balance(self.fund)
        exc = self._exc(credit=Decimal("8000"), narr="SABBATH CASH DEPOSIT")

        result = ei.take_to_books(
            exc, disposition=ei.BANKING, user=self.user, account=self.account)

        exc.refresh_from_db()
        self.assertEqual(exc.status, RegisterException.Status.RESOLVED)
        txn = Transaction.objects.get(pk=result["transaction_id"])
        # the banking entry: real bank credit, but NOT income and NO fund
        self.assertTrue(txn.is_banking)
        self.assertTrue(txn.excluded_from_income)
        self.assertIsNone(txn.department_id)
        # fund balance unchanged — the cash was booked when it was counted
        self.assertEqual(balances.fund_balance(self.fund), fund_before)
        # not counted as pending suspense either
        self.assertEqual(balances.pending_receipts_total(), Decimal(0))

    def test_banking_counts_toward_bank_position(self):
        exc = self._exc(credit=Decimal("8000"), narr="DEPOSIT")
        before = balances.bank_position()["system_balance"]
        ei.take_to_books(exc, disposition=ei.BANKING, user=self.user,
                         account=self.account)
        # the money really is at the bank, so the bank position rises by 8000
        after = balances.bank_position()["system_balance"]
        self.assertEqual(after - before, Decimal("8000"))


class NewMovementTests(DispositionFixture):
    def test_new_credit_goes_to_review_queue(self):
        exc = self._exc(credit=Decimal("1200"), narr="UNKNOWN GIVER")
        result = ei.take_to_books(
            exc, disposition=ei.NEW_MOVEMENT, user=self.user, account=self.account)
        txn = Transaction.objects.get(pk=result["transaction_id"])
        self.assertEqual(txn.allocation_status, Transaction.Status.REVIEW)
        self.assertEqual(txn.direction, "CREDIT")
        self.assertIsNone(txn.department_id)
        self.assertFalse(txn.is_banking)

    def test_new_debit_goes_to_review_queue(self):
        exc = self._exc(debit=Decimal("700"), narr="UNKNOWN WITHDRAWAL")
        result = ei.take_to_books(
            exc, disposition=ei.NEW_MOVEMENT, user=self.user, account=self.account)
        txn = Transaction.objects.get(pk=result["transaction_id"])
        self.assertEqual(txn.direction, "DEBIT")
        self.assertEqual(txn.allocation_status, Transaction.Status.REVIEW)


class AlreadyBookedTests(DispositionFixture):
    def test_already_booked_creates_no_transaction(self):
        exc = self._exc(debit=Decimal("5000"), narr="PAID SEVERAL EXPENSES")
        before = Transaction.objects.count()
        result = ei.take_to_books(
            exc, disposition=ei.ALREADY_BOOKED, user=self.user,
            account=self.account, note="One withdrawal covering April expenses")
        self.assertEqual(Transaction.objects.count(), before)
        exc.refresh_from_db()
        self.assertEqual(exc.status, RegisterException.Status.RESOLVED)
        self.assertIn("April", exc.resolution)


class BankChargeTests(DispositionFixture):
    def test_bank_charge_posts_an_expense(self):
        from cashbook.models import Expense
        exc = self._exc(debit=Decimal("250"), narr="STAMP DUTY")
        result = ei.take_to_books(
            exc, disposition=ei.BANK_CHARGE, user=self.user, account=self.account,
            department=self.fund)
        expense = Expense.objects.get(pk=result["expense_id"])
        self.assertEqual(expense.category, Expense.Category.BANK_CHARGE)
        self.assertEqual(expense.amount, Decimal("250"))
        self.assertEqual(expense.status, Expense.Status.PAID)

    def test_bank_charge_requires_a_fund(self):
        exc = self._exc(debit=Decimal("250"), narr="EXCISE")
        with self.assertRaises(Exception):
            ei.take_to_books(exc, disposition=ei.BANK_CHARGE, user=self.user,
                             account=self.account, department=None)


class NotApplicableTests(DispositionFixture):
    def test_banking_on_a_debit_is_rejected(self):
        exc = self._exc(debit=Decimal("250"))
        with self.assertRaises(ei.DispositionNotApplicable):
            ei.take_to_books(exc, disposition=ei.BANKING, user=self.user,
                             account=self.account)

    def test_bank_charge_on_a_credit_is_rejected(self):
        exc = self._exc(credit=Decimal("250"))
        with self.assertRaises(ei.DispositionNotApplicable):
            ei.take_to_books(exc, disposition=ei.BANK_CHARGE, user=self.user,
                             account=self.account, department=self.fund)


class BulkTests(DispositionFixture):
    def test_bulk_banking_skips_debits_and_reports_them(self):
        c1 = self._exc(credit=Decimal("1000"), narr="DEP1")
        c2 = self._exc(credit=Decimal("2000"), narr="DEP2")
        d1 = self._exc(debit=Decimal("250"), narr="CHARGE")   # should be skipped

        outcome = ei.bulk_take_to_books(
            [c1, c2, d1], disposition=ei.BANKING, user=self.user,
            account=self.account)

        self.assertEqual(len(outcome["done"]), 2)
        self.assertEqual(len(outcome["skipped"]), 1)
        skipped_exc, reason = outcome["skipped"][0]
        self.assertEqual(skipped_exc.pk, d1.pk)
        self.assertIn("debit", reason.lower())
        # the two credits banked; the debit untouched
        c1.refresh_from_db(); d1.refresh_from_db()
        self.assertEqual(c1.status, RegisterException.Status.RESOLVED)
        self.assertEqual(d1.status, RegisterException.Status.OPEN)

    def test_bulk_new_movement_applies_to_all(self):
        exs = [self._exc(credit=Decimal("100"), narr=f"G{i}") for i in range(3)]
        outcome = ei.bulk_take_to_books(
            exs, disposition=ei.NEW_MOVEMENT, user=self.user, account=self.account)
        self.assertEqual(len(outcome["done"]), 3)
        self.assertEqual(len(outcome["skipped"]), 0)
        self.assertEqual(
            Transaction.objects.filter(allocation_status="REVIEW").count(), 3)


class ExceptionsViewTests(DispositionFixture):
    def test_page_renders_with_dispositions(self):
        self._exc(credit=Decimal("1000"), narr="DEP")
        self._exc(debit=Decimal("250"), narr="CHARGE")
        self.client.force_login(self.user)
        resp = self.client.get(f"/bank-register/exceptions/?account={self.account.pk}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Take selected to the books")

    def test_bulk_post_banking_skips_debit(self):
        self._exc(credit=Decimal("1000"), narr="DEP1")
        self._exc(credit=Decimal("2000"), narr="DEP2")
        self._exc(debit=Decimal("250"), narr="CHARGE")
        ids = list(RegisterException.objects.values_list("pk", flat=True))
        self.client.force_login(self.user)
        resp = self.client.post(
            f"/bank-register/exceptions/?account={self.account.pk}",
            {"bulk_take": "1", "account": self.account.pk,
             "bulk_disposition": ei.BANKING, "selected": [str(i) for i in ids]},
            follow=True)
        self.assertEqual(resp.status_code, 200)
        # two credits banked, one debit skipped
        self.assertEqual(
            Transaction.objects.filter(is_banking=True).count(), 2)
        self.assertEqual(
            RegisterException.objects.filter(status="OPEN").count(), 1)

    def test_single_take_to_books_banking(self):
        exc = self._exc(credit=Decimal("5000"), narr="SABBATH DEPOSIT")
        self.client.force_login(self.user)
        resp = self.client.post(
            f"/bank-register/exceptions/?account={self.account.pk}",
            {"take_to_books": str(exc.pk), "account": self.account.pk,
             "disposition": ei.BANKING}, follow=True)
        self.assertEqual(resp.status_code, 200)
        exc.refresh_from_db()
        self.assertEqual(exc.status, RegisterException.Status.RESOLVED)
        self.assertEqual(Transaction.objects.filter(is_banking=True).count(), 1)
