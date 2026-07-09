"""Loan module — accounting integrity. Verifies the exact journal shapes the
requirement specifies, that the books stay balanced, that a full rebuild
regenerates loan postings with no special path, and that a loan is invisible
to income while fully visible to cash."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from cashbook.models import Expense
from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from ledger.models import JournalEntry, JournalLine
from ledger.services import posting
from loans.models import Lender, Loan
from loans.services import loans as svc


def _lines(source_type, source_id):
    je = JournalEntry.objects.filter(source_type=source_type,
                                     source_id=source_id).first()
    if not je:
        return {}
    out = {}
    for ln in je.lines.select_related("account"):
        key = ln.account.system_key
        d, c = out.get(key, (Decimal(0), Decimal(0)))
        out[key] = (d + ln.debit, c + ln.credit)
    return out


class LoanPostingTests(TestCase):
    def setUp(self):
        posting.ensure_chart()
        self.user = User.objects.create_user("lp_tr", password="x")
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(name="Development", fund_type="LOCAL")
        lender = Lender.objects.create(name="ACME SACCO")
        self.loan = Loan.objects.create(lender=lender, fund=self.fund,
                                        loan_date=dt.date(2026, 1, 5))

    def _balanced(self):
        rows, totals = posting.trial_balance()
        self.assertEqual(totals["debit"], totals["credit"], "trial balance out")

    def test_receipt_posts_dr_cash_cr_loans_payable(self):
        lt = svc.record_receipt(self.loan, date=dt.date(2026, 1, 5),
                                amount=Decimal("100000"), user=self.user)
        lines = _lines("transaction", lt.receipt_transaction_id)
        self.assertEqual(lines["CASH"], (Decimal("100000"), Decimal(0)))
        self.assertEqual(lines["LOANS_PAYABLE"], (Decimal(0), Decimal("100000")))
        self.assertNotIn("INC_DEVELOPMENT", lines)     # never income
        self._balanced()

    def test_repayment_posts_dr_loans_payable_cr_cash(self):
        svc.record_receipt(self.loan, date=dt.date(2026, 1, 5),
                           amount=Decimal("100000"), user=self.user)
        lt = svc.record_repayment(self.loan, date=dt.date(2026, 3, 1),
                                  amount=Decimal("40000"), user=self.user)
        lines = _lines("expense", lt.expense_id)
        self.assertEqual(lines["LOANS_PAYABLE"], (Decimal("40000"), Decimal(0)))
        self.assertEqual(lines["CASH"], (Decimal(0), Decimal("40000")))
        self._balanced()

    def test_interest_posts_dr_interest_expense_cr_cash(self):
        svc.record_receipt(self.loan, date=dt.date(2026, 1, 5),
                           amount=Decimal("100000"), user=self.user)
        lt = svc.record_interest(self.loan, date=dt.date(2026, 2, 1),
                                 amount=Decimal("1500"), user=self.user)
        lines = _lines("expense", lt.expense_id)
        self.assertEqual(lines["EXP_LOAN_INTEREST"], (Decimal("1500"), Decimal(0)))
        self.assertEqual(lines["CASH"], (Decimal(0), Decimal("1500")))
        self._balanced()

    def test_conversion_nets_dr_loans_payable_cr_income_zero_cash(self):
        svc.record_receipt(self.loan, date=dt.date(2026, 1, 5),
                           amount=Decimal("50000"), user=self.user)
        lt = svc.convert_to_donation(self.loan, date=dt.date(2026, 6, 1),
                                     user=self.user)
        inc = _lines("transaction", lt.income_transaction_id)
        exp = _lines("expense", lt.expense_id)
        # combined: cash nets to zero; liability debited; income credited
        cash_net = (inc.get("CASH", (0, 0))[0] - exp.get("CASH", (0, 0))[1])
        self.assertEqual(cash_net, Decimal(0))
        self.assertEqual(exp["LOANS_PAYABLE"][0], Decimal("50000"))
        self.assertEqual(inc["INC_DEVELOPMENT"][1], Decimal("50000"))
        self._balanced()

    def test_rebuild_regenerates_loan_postings(self):
        """Loans need no loan-specific rebuild step — both halves are standard
        source documents the existing rebuild already iterates."""
        lt = svc.record_receipt(self.loan, date=dt.date(2026, 1, 5),
                                amount=Decimal("100000"), user=self.user)
        svc.record_repayment(self.loan, date=dt.date(2026, 3, 1),
                             amount=Decimal("40000"), user=self.user)
        posting.rebuild()
        lines = _lines("transaction", lt.receipt_transaction_id)
        self.assertEqual(lines["LOANS_PAYABLE"], (Decimal(0), Decimal("100000")))
        self._balanced()
        # net liability on the books = outstanding on the loan
        agg = JournalLine.objects.filter(account__system_key="LOANS_PAYABLE")
        net = (sum((l.credit for l in agg), Decimal(0))
               - sum((l.debit for l in agg), Decimal(0)))
        self.assertEqual(net, Decimal("60000"))
        self.assertEqual(net, self.loan.outstanding_principal)

    def test_fund_ledger_balance_includes_loan_cash(self):
        svc.record_receipt(self.loan, date=dt.date(2026, 1, 5),
                           amount=Decimal("100000"), user=self.user)
        self.assertEqual(posting.fund_balance_from_ledger(self.fund),
                         Decimal("100000"))
        svc.record_repayment(self.loan, date=dt.date(2026, 3, 1),
                             amount=Decimal("40000"), user=self.user)
        self.assertEqual(posting.fund_balance_from_ledger(self.fund),
                         Decimal("60000"))

    def test_fund_variance_drilldown_does_not_flag_loan_receipts(self):
        svc.record_receipt(self.loan, date=dt.date(2026, 1, 5),
                           amount=Decimal("100000"), user=self.user)
        issues = posting.fund_variance_detail(self.fund)
        self.assertEqual([i for i in issues if i["kind"] == "transaction"], [])


class LoanEngineVisibilityTests(TestCase):
    """The engine side: loan money is cash to the fund but never income,
    repayments reduce cash but never operating expenditure."""

    def setUp(self):
        posting.ensure_chart()
        self.user = User.objects.create_user("lv_tr", password="x")
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(name="Development", fund_type="LOCAL")
        lender = Lender.objects.create(name="ACME SACCO")
        self.loan = Loan.objects.create(lender=lender, fund=self.fund,
                                        loan_date=dt.date(2026, 1, 5))
        svc.record_receipt(self.loan, date=dt.date(2026, 1, 5),
                           amount=Decimal("100000"), user=self.user)
        # an ordinary contribution alongside, to prove separation
        Transaction.objects.create(date=dt.date(2026, 1, 6), channel="BANK",
            direction="CREDIT", amount=Decimal("2000"), department=self.fund,
            allocation_status="AUTO", confirmed=True, core_ref="ORD1")

    def test_fund_cash_includes_loan_income_excludes_it(self):
        from reports.services.balances import receipts_by_department
        # cash to the fund: both rows
        cash = receipts_by_department()
        self.assertEqual(cash[self.fund.id], Decimal("102000"))
        # income: only the contribution
        income = (Transaction.objects.confirmed_credits()
                  .filter(excluded_from_income=False, department=self.fund)
                  .aggregate(t=__import__("django").db.models.Sum("amount"))["t"])
        self.assertEqual(income, Decimal("2000"))

    def test_repayment_out_of_ie_but_in_fund_expenses(self):
        from reports.services.balances import expenses_by_department
        svc.record_repayment(self.loan, date=dt.date(2026, 2, 1),
                             amount=Decimal("30000"), user=self.user)
        # the fund's own ledger sees the outflow (its balance drops) …
        full = expenses_by_department()
        self.assertEqual(full[self.fund.id], Decimal("30000"))
        # … but the I&E / operating view does not (liability settlement)
        op = expenses_by_department(include_remittance=False)
        self.assertEqual(op.get(self.fund.id, Decimal(0)), Decimal(0))

    def test_interest_is_operating_expenditure(self):
        from reports.services.balances import expenses_by_department
        svc.record_interest(self.loan, date=dt.date(2026, 2, 1),
                            amount=Decimal("900"), user=self.user)
        op = expenses_by_department(include_remittance=False)
        self.assertEqual(op[self.fund.id], Decimal("900"))

    def test_conversion_becomes_income_without_cash_movement(self):
        from reports.services.balances import (expenses_by_department,
                                               receipts_by_department)
        cash_before = receipts_by_department()[self.fund.id] \
            - expenses_by_department().get(self.fund.id, Decimal(0))
        svc.convert_to_donation(self.loan, date=dt.date(2026, 6, 1),
                                user=self.user)
        cash_after = receipts_by_department()[self.fund.id] \
            - expenses_by_department().get(self.fund.id, Decimal(0))
        self.assertEqual(cash_before, cash_after)          # no cash moved
        income = (Transaction.objects.confirmed_credits()
                  .filter(excluded_from_income=False, department=self.fund)
                  .aggregate(t=__import__("django").db.models.Sum("amount"))["t"])
        self.assertEqual(income, Decimal("102000"))        # 2,000 + 100,000 gift
