"""A loan converted to a donation is not expenditure.

The lender lets the church keep the money. The debt is retired against the gift
as a contra pair of ordinary documents, and no cash moves. The Income &
Expenditure statement has always known this — its expense base excludes the
whole liability document class — but the Collections Summary, the dashboard and
the treasurer's report each re-derived "expenditure" inline as "everything
except a remittance", which let the contra through and reported spending the
church never did.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from cashbook.models import Expense
from core.reporting import ReportContext
from departments.models import Department
from reports.services import balances, monthly

START, END = dt.date(2026, 1, 1), dt.date(2026, 12, 31)


class _Converted(TestCase):
    """A 40,000 loan converted to a donation, plus one real 6,000 expense."""

    def setUp(self):
        from loans.models import Lender, Loan
        from loans.services import loans as loan_svc
        self.user = User.objects.create_user("conv", is_superuser=True)
        self.fund = Department.objects.create(name="Building", fund_type="LOCAL")
        lender = Lender.objects.create(name="A Benefactor")
        self.loan = Loan.objects.create(
            lender=lender, fund=self.fund, loan_date=dt.date(2026, 2, 1),
            principal_amount=Decimal("100000"), status="ACTIVE")
        loan_svc.record_receipt(self.loan, date=dt.date(2026, 2, 1),
                                amount=Decimal("100000"), user=self.user)
        loan_svc.convert_to_donation(self.loan, date=dt.date(2026, 5, 1),
                                     amount=Decimal("40000"), user=self.user)
        Expense.objects.create(
            date=dt.date(2026, 5, 10), department=self.fund,
            description="Repairs", amount=Decimal("6000"),
            category="MAINTENANCE", status="PAID", recorded_by=self.user)


class ContraDocumentTests(_Converted):
    def test_the_conversion_really_did_raise_a_contra_expense(self):
        """The thing being excluded exists — otherwise these tests would pass
        for the wrong reason."""
        contra = Expense.objects.filter(category="LOAN_REPAYMENT")
        self.assertEqual(contra.count(), 1)
        self.assertEqual(contra.first().amount, Decimal("40000"))
        self.assertEqual(contra.first().doc_class, Expense.DocClass.LIABILITY)


class CollectionsSummaryTests(_Converted):
    def test_expenditure_excludes_the_conversion(self):
        d = monthly.collections_summary_period(START, END)
        self.assertEqual(d["totals"]["expenditure"], Decimal("6000"))

    def test_the_year_form_agrees(self):
        d = monthly.collections_summary(2026)
        self.assertEqual(d["tot_expenditure"], Decimal("6000"))

    def test_the_detail_form_agrees(self):
        d = monthly.collections_detail(START, END)
        self.assertEqual(d["tot_expenditure"], Decimal("6000"))

    def test_it_now_matches_the_income_statement_basis(self):
        """The discrepancy itself: two pages reporting different spending for
        the same month."""
        summary = monthly.collections_summary_period(START, END)
        statement = (balances.operating_expense_total(START, END)
                     + balances.capital_expenditure_total(START, END))
        self.assertEqual(summary["totals"]["expenditure"], statement)


class BoardPackTests(_Converted):
    def _ctx(self):
        return ReportContext.for_period(START, END)

    def test_the_headline_expenditure_excludes_it(self):
        from reports.board_sections import BoardKpiComponent
        data = BoardKpiComponent().render(self._ctx(), {})
        v = {r.cells["label"]: r.cells["value"] for r in data.rows}
        self.assertEqual(v["Total expenditure"], Decimal("6000"))

    def test_the_headline_agrees_with_the_collections_table(self):
        from reports.board_sections import (BoardKpiComponent,
                                            CollectionsSummaryComponent)
        ctx = self._ctx()
        kpi = {r.cells["label"]: r.cells["value"]
               for r in BoardKpiComponent().render(ctx, {}).rows}
        table = CollectionsSummaryComponent().render(ctx, {})
        totals = table.total.cells
        self.assertEqual(kpi["Total receipts"], totals["collections"])
        self.assertEqual(kpi["Total trust funds"], totals["trust"])
        self.assertEqual(kpi["Total expenditure"], totals["expenditure"])

    def test_the_cash_flow_still_reconciles(self):
        """The contra leaving expenditure must not break the statement that
        depends on both legs cancelling."""
        from reports.financial_statements import CashFlowStatementSection
        ctx = self._ctx()
        data = CashFlowStatementSection().render(ctx, {})
        lines = {r.cells["line"]: r.cells["amount"] for r in data.rows}
        closing = sum((r["closing"] or Decimal(0)
                       for r in ctx.fund_summary()), Decimal(0))
        self.assertEqual(lines["Cash & bank at end of period"], closing)
        self.assertIn("Reconciles", data.note)


class HeadlineFiguresTests(TestCase):
    """What the overview shows, and that it is sourced from one place."""

    def setUp(self):
        from giving.models import Transaction
        self.user = User.objects.create_user("hdr", is_superuser=True)
        local = Department.objects.create(name="Building", fund_type="LOCAL")
        trust = Department.objects.create(name="Tithe", fund_type="TRUST")
        for amount, dept in (("40000", local), ("18000", trust)):
            Transaction.objects.create(
                date=dt.date(2026, 5, 4), channel="CASH", direction="CREDIT",
                amount=Decimal(amount), department=dept,
                allocation_status="AUTO", confirmed=True)
        Expense.objects.create(
            date=dt.date(2026, 5, 10), department=local, description="Repairs",
            amount=Decimal("6000"), category="MAINTENANCE", status="PAID",
            recorded_by=self.user)

    def test_the_four_figures_a_board_asked_for(self):
        from reports.board_sections import BoardKpiComponent
        data = BoardKpiComponent().render(
            ReportContext.for_period(START, END), {})
        labels = [r.cells["label"] for r in data.rows]
        self.assertEqual(labels, ["Total receipts", "Total trust funds",
                                  "Total expenditure", "Trust still to remit"])

    def test_the_values(self):
        from reports.board_sections import BoardKpiComponent
        data = BoardKpiComponent().render(
            ReportContext.for_period(START, END), {})
        v = {r.cells["label"]: r.cells["value"] for r in data.rows}
        self.assertEqual(v["Total receipts"], Decimal("58000"))
        self.assertEqual(v["Total trust funds"], Decimal("18000"))
        self.assertEqual(v["Total expenditure"], Decimal("6000"))

    def test_tithe_and_net_fund_balance_are_gone(self):
        from reports.board_sections import BoardKpiComponent
        data = BoardKpiComponent().render(
            ReportContext.for_period(START, END), {})
        labels = [r.cells["label"] for r in data.rows]
        self.assertNotIn("Tithe", labels)
        self.assertNotIn("Net fund balance", labels)
