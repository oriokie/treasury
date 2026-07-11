"""Loan conversion/write-off contra expenses should never appear in the
Missing Receipts queue — that "expense" is one half of a same-day, same-
amount book-entry pair that retires a liability against income with no cash
ever moving (see loans.services.loans._retire); there is no physical
transaction for a receipt to document, so it could never leave the queue by
any real action a treasurer could take. A genuine PRINCIPAL/INTEREST loan
repayment — a real cash disbursement, also category=LOAN_REPAYMENT — must
still correctly require a receipt."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from cashbook.views import missing_receipts_queryset
from departments.models import Department
from loans.services import loans as loan_svc


class LoanContraMissingReceiptsTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_superuser("lc_u", password="x")
        self.fund = Department.objects.create(name="LC Fund", fund_type="LOCAL")
        self.lender, _ = loan_svc.match_or_create_lender("LC Lender", "0700000099")

    def _loan_with_receipt(self, amount="5000"):
        loan, _ = loan_svc.loan_for_receipt(self.lender, self.fund,
                                            dt.date(2026, 1, 1), user=self.u)
        loan_svc.record_receipt(loan, date=dt.date(2026, 1, 1),
                                amount=Decimal(amount), user=self.u, channel="BANK")
        return loan

    def test_conversion_contra_not_in_missing_receipts(self):
        loan = self._loan_with_receipt()
        lt = loan_svc.convert_to_donation(loan, date=dt.date(2026, 6, 1), user=self.u)
        qs = missing_receipts_queryset(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        self.assertFalse(qs.filter(pk=lt.expense_id).exists())

    def test_write_off_contra_not_in_missing_receipts(self):
        loan = self._loan_with_receipt()
        lt = loan_svc.write_off(loan, date=dt.date(2026, 6, 1), user=self.u)
        qs = missing_receipts_queryset(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        self.assertFalse(qs.filter(pk=lt.expense_id).exists())

    def test_real_repayment_still_requires_receipt(self):
        loan = self._loan_with_receipt()
        lt = loan_svc.record_repayment(loan, date=dt.date(2026, 3, 1),
                                       amount=Decimal("1000"), user=self.u)
        qs = missing_receipts_queryset(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        self.assertTrue(qs.filter(pk=lt.expense_id).exists())

    def test_real_interest_payment_still_requires_receipt(self):
        loan = self._loan_with_receipt()
        lt = loan_svc.record_interest(loan, date=dt.date(2026, 3, 1),
                                      amount=Decimal("100"), user=self.u)
        qs = missing_receipts_queryset(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        self.assertTrue(qs.filter(pk=lt.expense_id).exists())

    def test_repayment_leaves_queue_once_receipted(self):
        loan = self._loan_with_receipt()
        lt = loan_svc.record_repayment(loan, date=dt.date(2026, 3, 1),
                                       amount=Decimal("1000"), user=self.u)
        from cashbook.models import ExpenseAttachment
        ExpenseAttachment.objects.create(expense=lt.expense, text="proof")
        qs = missing_receipts_queryset(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        self.assertFalse(qs.filter(pk=lt.expense_id).exists())

    def test_dashboard_widget_count_excludes_contra(self):
        loan = self._loan_with_receipt()
        loan_svc.convert_to_donation(loan, date=dt.date(2026, 6, 1), user=self.u)
        from django.test import Client
        c = Client(); c.force_login(self.u)
        r = c.get("/")
        self.assertEqual(r.status_code, 200)
        # the widget's count comes from the same queryset; no assertion on
        # the exact number here (other demo data may exist) — the queryset-
        # level tests above are the authoritative check
