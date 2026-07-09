"""Loan module — models & services: lender resolution/merge, loan numbering,
computed outstanding balances (single source of truth), status derivation,
interest accrual, and the validation rules (over-repayment, retired-loan
edits, duplicate lenders)."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import TestCase

from cashbook.models import Expense
from core.roles import TREASURER
from departments.models import Department
from ledger.services import posting
from loans.models import Lender, Loan, LoanTransaction
from loans.services import loans as svc


def _user(name="lm_tr", role=TREASURER):
    u = User.objects.create_user(name, password="x")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


class LenderResolutionTests(TestCase):
    def test_phone_is_the_trusted_signal(self):
        a = Lender.objects.create(name="RUTH MOMANYI", phone="0790301470")
        self.assertEqual(a.phone, "254790301470")            # normalised on save
        m, how = svc.match_or_create_lender("R MOMANYI", "254790301470")
        self.assertEqual(m.pk, a.pk)
        self.assertEqual(how, "matched_phone")

    def test_name_match_only_when_unambiguous(self):
        Lender.objects.create(name="RUTH MOMANYI")
        m, how = svc.match_or_create_lender("MOMANYI RUTH", None)   # order-insensitive
        self.assertEqual(how, "matched_name")
        Lender.objects.create(name="JOHN OKOTH", phone="0711000001")
        Lender.objects.create(name="OKOTH JOHN", phone="0711000002")
        m, how = svc.match_or_create_lender("JOHN OKOTH", None)     # ambiguous now
        self.assertEqual(how, "created")

    def test_national_id_beats_everything(self):
        a = Lender.objects.create(name="ACME SACCO", national_id="99887766")
        m, how = svc.match_or_create_lender("ACME", "0711999999", national_id="99887766")
        self.assertEqual(m.pk, a.pk)
        self.assertEqual(how, "matched_id")

    def test_never_creates_a_member(self):
        from members.models import Member
        before = Member.objects.count()
        svc.match_or_create_lender("VISITING WELLWISHER", "0722123456")
        self.assertEqual(Member.objects.count(), before)

    def test_merge_repoints_loans_and_retires_duplicate(self):
        fund = Department.objects.create(name="Development", fund_type="LOCAL")
        keep = Lender.objects.create(name="MARY A", phone="0722000111")
        dup = Lender.objects.create(name="MARY AKINYI", email="m@x.com")
        Loan.objects.create(lender=dup, fund=fund, loan_date=dt.date.today())
        svc.merge_lenders(keep, dup)
        dup.refresh_from_db()
        self.assertEqual(dup.merged_into_id, keep.pk)
        self.assertEqual(keep.loans.count(), 1)
        self.assertEqual(keep.email, "m@x.com")              # detail absorbed
        # merged duplicates never match again
        m, how = svc.match_or_create_lender("MARY AKINYI", None)
        self.assertNotEqual(m.pk, dup.pk)


class LoanMathTests(TestCase):
    def setUp(self):
        posting.ensure_chart()
        self.user = _user()
        self.fund = Department.objects.create(name="Development", fund_type="LOCAL")
        self.lender = Lender.objects.create(name="ELDER KIP", phone="0710000001")
        self.loan = Loan.objects.create(lender=self.lender, fund=self.fund,
                                        loan_date=dt.date(2026, 1, 10))

    def test_loan_number_permanent_and_unique(self):
        self.assertTrue(self.loan.number.startswith("LN-2026-"))
        other = Loan.objects.create(lender=self.lender, fund=self.fund,
                                    loan_date=dt.date(2026, 2, 1))
        self.assertNotEqual(self.loan.number, other.number)

    def test_outstanding_computed_from_transactions(self):
        svc.record_receipt(self.loan, date=dt.date(2026, 1, 10),
                           amount=Decimal("100000"), user=self.user)
        svc.record_receipt(self.loan, date=dt.date(2026, 2, 10),
                           amount=Decimal("50000"), user=self.user)
        svc.record_repayment(self.loan, date=dt.date(2026, 3, 1),
                             amount=Decimal("60000"), user=self.user)
        self.assertEqual(self.loan.received_total, Decimal("150000"))
        self.assertEqual(self.loan.outstanding_principal, Decimal("90000"))
        self.assertEqual(self.loan.status, Loan.Status.ACTIVE)

    def test_repayment_cannot_exceed_outstanding(self):
        svc.record_receipt(self.loan, date=dt.date(2026, 1, 10),
                           amount=Decimal("10000"), user=self.user)
        with self.assertRaises(ValidationError):
            svc.record_repayment(self.loan, date=dt.date(2026, 2, 1),
                                 amount=Decimal("10001"), user=self.user)

    def test_full_repayment_completes_and_locks_the_loan(self):
        svc.record_receipt(self.loan, date=dt.date(2026, 1, 10),
                           amount=Decimal("10000"), user=self.user)
        svc.record_repayment(self.loan, date=dt.date(2026, 6, 1),
                             amount=Decimal("10000"), user=self.user)
        self.assertEqual(self.loan.status, Loan.Status.COMPLETED)
        with self.assertRaises(ValidationError):        # no transacting on retired loans
            svc.record_receipt(self.loan, date=dt.date.today(),
                               amount=Decimal("1"), user=self.user)

    def test_rejected_voucher_flows_back_into_the_balance(self):
        """The documents are authoritative: rejecting the settlement voucher
        restores the outstanding balance with no loan-side edit."""
        svc.record_receipt(self.loan, date=dt.date(2026, 1, 10),
                           amount=Decimal("10000"), user=self.user)
        lt = svc.record_repayment(self.loan, date=dt.date(2026, 2, 1),
                                  amount=Decimal("4000"), user=self.user)
        self.assertEqual(self.loan.outstanding_principal, Decimal("6000"))
        lt.expense.status = Expense.Status.REJECTED
        lt.expense.save()
        self.assertFalse(lt.effective)
        self.assertEqual(self.loan.outstanding_principal, Decimal("10000"))

    def test_interest_never_touches_principal(self):
        svc.record_receipt(self.loan, date=dt.date(2026, 1, 10),
                           amount=Decimal("10000"), user=self.user)
        svc.record_interest(self.loan, date=dt.date(2026, 2, 1),
                            amount=Decimal("500"), user=self.user)
        self.assertEqual(self.loan.outstanding_principal, Decimal("10000"))
        self.assertEqual(self.loan.interest_paid, Decimal("500"))

    def test_simple_interest_accrual_is_indicative(self):
        self.loan.interest_rate = Decimal("10")
        self.loan.interest_method = Loan.InterestMethod.SIMPLE
        self.loan.save()
        svc.record_receipt(self.loan, date=dt.date(2026, 1, 10),
                           amount=Decimal("36500"), user=self.user)
        # 10% p.a. on 36,500 = 10/day: 30 days later ≈ 300
        accrued = self.loan.accrued_interest(as_of=dt.date(2026, 2, 9))
        self.assertEqual(accrued, Decimal("300.00"))

    def test_conversion_creates_income_and_zeroes_the_loan(self):
        svc.record_receipt(self.loan, date=dt.date(2026, 1, 10),
                           amount=Decimal("20000"), user=self.user)
        lt = svc.convert_to_donation(self.loan, date=dt.date(2026, 5, 1),
                                     user=self.user)
        self.assertEqual(self.loan.outstanding_principal, Decimal(0))
        self.assertEqual(self.loan.status, Loan.Status.CONVERTED)
        # the income half is a real, dated contribution credit
        t = lt.income_transaction
        self.assertFalse(t.excluded_from_income)
        self.assertEqual(t.amount, Decimal("20000"))
        self.assertEqual(t.date, dt.date(2026, 5, 1))
        # the settlement half nets the cash out again
        self.assertEqual(lt.expense.amount, Decimal("20000"))
        self.assertEqual(lt.expense.category, Expense.Category.LOAN_REPAYMENT)

    def test_partial_writeoff(self):
        svc.record_receipt(self.loan, date=dt.date(2026, 1, 10),
                           amount=Decimal("20000"), user=self.user)
        svc.write_off(self.loan, date=dt.date(2026, 5, 1),
                      amount=Decimal("5000"), user=self.user)
        self.assertEqual(self.loan.outstanding_principal, Decimal("15000"))
        self.assertEqual(self.loan.status, Loan.Status.ACTIVE)

    def test_retire_cannot_exceed_outstanding(self):
        svc.record_receipt(self.loan, date=dt.date(2026, 1, 10),
                           amount=Decimal("5000"), user=self.user)
        with self.assertRaises(ValidationError):
            svc.convert_to_donation(self.loan, date=dt.date.today(),
                                    amount=Decimal("6000"), user=self.user)

    def test_conversion_attributes_the_linked_member(self):
        from members.models import Member
        member = Member.objects.create(name="ELDER KIP")
        self.lender.member = member
        self.lender.save()
        svc.record_receipt(self.loan, date=dt.date(2026, 1, 10),
                           amount=Decimal("7000"), user=self.user)
        lt = svc.convert_to_donation(self.loan, date=dt.date(2026, 6, 1),
                                     user=self.user)
        self.assertEqual(lt.income_transaction.member_id, member.pk)
        # write-off never attributes a contribution
        loan2 = Loan.objects.create(lender=self.lender, fund=self.fund,
                                    loan_date=dt.date(2026, 1, 1))
        svc.record_receipt(loan2, date=dt.date(2026, 1, 10),
                           amount=Decimal("1000"), user=self.user)
        lt2 = svc.write_off(loan2, date=dt.date(2026, 6, 1), user=self.user)
        self.assertIsNone(lt2.income_transaction.member_id)
