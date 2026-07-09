"""Loan enhancement tests: the report catalogue, financial-statement
integration (balance sheet & cash flow), dev-group exclusion, petty-cash
receipt/repayment, and departmental-leader visibility with conditional menu.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from cashbook.models import Expense, PettyCashTopUp
from core.roles import ASSISTANT, AUDITOR, LEADER, TREASURER
from departments.models import Department, DepartmentLeadership
from giving.models import Transaction
from ledger.models import JournalLine
from ledger.services import posting
from loans.models import Lender, Loan, LoanTransaction
from loans.services import loans as svc, reporting


def _user(name, role):
    u = User.objects.create_user(name, password="x")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


class LoanReportViewTests(TestCase):
    def setUp(self):
        posting.ensure_chart()
        self.tr = _user("lr_tr", TREASURER)
        self.client.force_login(self.tr)
        self.fund = Department.objects.create(name="Development", fund_type="LOCAL")
        self.lender = Lender.objects.create(name="ACME SACCO", phone="0722000111")
        self.loan = Loan.objects.create(lender=self.lender, fund=self.fund,
                                        loan_date=dt.date(2026, 1, 5),
                                        maturity_date=dt.date(2026, 6, 30),
                                        principal_amount=Decimal("100000"),
                                        interest_rate=Decimal("10"),
                                        interest_method="SIMPLE")
        svc.record_receipt(self.loan, date=dt.date(2026, 1, 5),
                           amount=Decimal("100000"), user=self.tr)
        svc.record_repayment(self.loan, date=dt.date(2026, 3, 1),
                             amount=Decimal("30000"), user=self.tr)
        svc.record_interest(self.loan, date=dt.date(2026, 2, 1),
                            amount=Decimal("2000"), user=self.tr)

    def test_all_report_pages_render_and_export(self):
        names = ["loan_reports", "report_loan_liability", "report_loans_outstanding",
                 "report_loan_ageing", "report_loan_maturity", "report_loans_by_fund",
                 "report_loans_by_lender", "report_loan_repayments",
                 "report_loan_interest", "report_loans_converted", "report_financing"]
        for name in names:
            url = reverse(name)
            self.assertEqual(self.client.get(url).status_code, 200, name)
            if name != "loan_reports":
                self.assertEqual(self.client.get(url + "?export=csv").status_code, 200, name)
                self.assertEqual(self.client.get(url + "?export=xlsx").status_code, 200, name)

    def test_liability_schedule_figures(self):
        rows = reporting.liability_schedule(as_of=dt.date(2026, 3, 15))
        row = next(r for r in rows if r["loan"].pk == self.loan.pk)
        self.assertEqual(row["outstanding_principal"], Decimal("70000"))
        self.assertEqual(row["original_principal"], Decimal("100000"))

    def test_outstanding_liability_ties_to_ledger(self):
        posting.rebuild()
        agg = JournalLine.objects.filter(
            account__system_key="LOANS_PAYABLE").aggregate(
            d=__import__("django").db.models.Sum("debit"),
            c=__import__("django").db.models.Sum("credit"))
        net = (agg["c"] or Decimal(0)) - (agg["d"] or Decimal(0))
        self.assertEqual(reporting.outstanding_liability()["total"], net)

    def test_repayment_and_interest_histories(self):
        reps = reporting.repayment_history()
        self.assertEqual(sum(t.amount for t in reps), Decimal("30000"))
        ints = reporting.interest_history()
        self.assertEqual(sum(t.amount for t in ints), Decimal("2000"))

    def test_ageing_buckets_outstanding_only(self):
        data = reporting.ageing(as_of=dt.date(2026, 3, 15))
        self.assertEqual(data["total"], Decimal("70000"))

    def test_financing_activity(self):
        fin = reporting.financing_activity(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        self.assertEqual(fin["receipts"], Decimal("100000"))
        self.assertEqual(fin["repayments"], Decimal("30000"))
        self.assertEqual(fin["interest"], Decimal("2000"))


class FinancialStatementTests(TestCase):
    def setUp(self):
        posting.ensure_chart()
        self.tr = _user("fs_tr", TREASURER)
        self.client.force_login(self.tr)
        self.fund = Department.objects.create(name="Development", fund_type="LOCAL")
        self.lender = Lender.objects.create(name="ACME SACCO")
        # long-term loan (matures >12 months out)
        self.loan = Loan.objects.create(lender=self.lender, fund=self.fund,
                                        loan_date=dt.date.today(),
                                        maturity_date=dt.date.today() + dt.timedelta(days=800))
        svc.record_receipt(self.loan, date=dt.date.today(),
                           amount=Decimal("100000"), user=self.tr)
        svc.record_repayment(self.loan, date=dt.date.today(),
                             amount=Decimal("25000"), user=self.tr)
        posting.rebuild()

    def test_balance_sheet_shows_loans_payable_and_balances(self):
        from reports.views import FinancialPositionView
        from django.test import RequestFactory
        req = RequestFactory().get("/reports/financial-position/")
        req.user = self.tr
        resp = FinancialPositionView.as_view()(req)
        ctx = resp.context_data
        self.assertEqual(ctx["loans_payable"], Decimal("75000"))
        self.assertEqual(ctx["loans_long_term"], Decimal("75000"))   # >12 months
        self.assertEqual(ctx["loans_current"], Decimal(0))
        self.assertTrue(ctx["balanced"])

    def test_current_vs_long_term_split(self):
        # a second loan maturing soon -> current
        near = Loan.objects.create(lender=self.lender, fund=self.fund,
                                   loan_date=dt.date.today(),
                                   maturity_date=dt.date.today() + dt.timedelta(days=100))
        svc.record_receipt(near, date=dt.date.today(),
                           amount=Decimal("40000"), user=self.tr)
        liab = reporting.outstanding_liability()
        self.assertEqual(liab["current"], Decimal("40000"))
        self.assertEqual(liab["long_term"], Decimal("75000"))
        self.assertEqual(liab["total"], Decimal("115000"))

    def test_cash_flow_classifies_financing_and_reconciles(self):
        from reports.views import StatementOfCashFlowsView
        from django.test import RequestFactory
        req = RequestFactory().get(
            "/reports/cash-flows/?start=%s&end=%s"
            % ((dt.date.today() - dt.timedelta(days=1)).isoformat(),
               (dt.date.today() + dt.timedelta(days=1)).isoformat()))
        req.user = self.tr
        resp = StatementOfCashFlowsView.as_view()(req)
        ctx = resp.context_data
        self.assertEqual(ctx["loan_receipts"], Decimal("100000"))
        self.assertEqual(ctx["loan_repayments"], Decimal("25000"))
        self.assertEqual(ctx["net_financing"], Decimal("75000"))
        # loan cash must not inflate operating receipts (no double count)
        self.assertTrue(ctx["ties"])

    def test_ie_excludes_repayment_includes_interest(self):
        from reports.services.balances import expenses_by_department
        svc.record_interest(self.loan, date=dt.date.today(),
                            amount=Decimal("1000"), user=self.tr)
        op = expenses_by_department(include_remittance=False)
        # repayment excluded, interest included
        self.assertEqual(op.get(self.fund.id, Decimal(0)), Decimal("1000"))

    def test_cash_flow_reconciles_with_conversion(self):
        """A conversion recognises income with no cash movement; the cash flow
        must still reconcile (the non-cash income leg is removed from operating)."""
        from reports.views import StatementOfCashFlowsView
        from django.test import RequestFactory
        conv = Loan.objects.create(lender=self.lender, fund=self.fund,
                                   loan_date=dt.date.today())
        svc.record_receipt(conv, date=dt.date.today(),
                           amount=Decimal("15000"), user=self.tr)
        svc.convert_to_donation(conv, date=dt.date.today(), user=self.tr)
        posting.rebuild()
        req = RequestFactory().get(
            "/reports/cash-flows/?start=%s&end=%s"
            % ((dt.date.today() - dt.timedelta(days=1)).isoformat(),
               (dt.date.today() + dt.timedelta(days=1)).isoformat()))
        req.user = self.tr
        ctx = StatementOfCashFlowsView.as_view()(req).context_data
        self.assertTrue(ctx["ties"])


class DevGroupExclusionTests(TestCase):
    def setUp(self):
        posting.ensure_chart()
        self.tr = _user("dg_tr", TREASURER)
        self.client.force_login(self.tr)
        self.dev = Department.objects.create(name="Development", fund_type="LOCAL",
                                             category="DEVELOPMENT")
        self.lender = Lender.objects.create(name="DEV LENDER")
        self.loan = Loan.objects.create(lender=self.lender, fund=self.dev,
                                        loan_date=dt.date(2026, 1, 1))
        self.lt = svc.record_receipt(self.loan, date=dt.date(2026, 1, 1),
                                     amount=Decimal("50000"), user=self.tr)

    def test_loan_receipt_not_in_unassigned_queue(self):
        from reports.views import DevGroupUnassignedView
        from django.test import RequestFactory
        v = DevGroupUnassignedView()
        req = RequestFactory().get("/reports/dev-groups/unassigned/")
        req.user = self.tr
        v.request = req
        self.assertFalse(v._qs().filter(pk=self.lt.receipt_transaction.pk).exists())

    def test_unassigned_page_renders_without_loan(self):
        r = self.client.get(reverse("dev_unassigned"))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "DEV LENDER")


class PettyCashLoanTests(TestCase):
    def setUp(self):
        posting.ensure_chart()
        self.tr = _user("pc_tr", TREASURER)
        self.fund = Department.objects.create(name="Development", fund_type="LOCAL")
        self.lender = Lender.objects.create(name="PC LENDER")
        self.loan = Loan.objects.create(lender=self.lender, fund=self.fund,
                                        loan_date=dt.date.today())

    def _petty(self):
        from cashbook.views import _petty_balance_asof
        return _petty_balance_asof(dt.date.today())

    def test_receipt_into_petty_raises_float(self):
        before = self._petty()
        lt = svc.record_receipt(self.loan, date=dt.date.today(),
                                amount=Decimal("10000"), user=self.tr,
                                into_petty_cash=True)
        self.assertEqual(self._petty() - before, Decimal("10000"))
        self.assertIsNotNone(lt.petty_topup_id)
        self.assertTrue(lt.receipt_transaction.excluded_from_income)

    def test_repayment_from_petty_reduces_float(self):
        svc.record_receipt(self.loan, date=dt.date.today(),
                           amount=Decimal("10000"), user=self.tr,
                           into_petty_cash=True)
        before = self._petty()
        svc.record_repayment(self.loan, date=dt.date.today(),
                             amount=Decimal("4000"), user=self.tr,
                             paid_from_petty_cash=True)
        self.assertEqual(before - self._petty(), Decimal("4000"))
        self.assertEqual(self.loan.outstanding_principal, Decimal("6000"))

    def test_petty_loan_fund_balance_reconciles(self):
        svc.record_receipt(self.loan, date=dt.date.today(),
                           amount=Decimal("10000"), user=self.tr,
                           into_petty_cash=True)
        svc.record_repayment(self.loan, date=dt.date.today(),
                             amount=Decimal("4000"), user=self.tr,
                             paid_from_petty_cash=True)
        self.assertEqual(posting.fund_balance_from_ledger(self.fund),
                         Decimal("6000"))

    def test_petty_receipt_through_form(self):
        self.client.force_login(self.tr)
        self.client.post(reverse("loan_receipt", args=[self.loan.pk]),
                         {"date": dt.date.today().isoformat(), "amount": "8000",
                          "destination": "PETTY"})
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.received_total, Decimal("8000"))
        self.assertEqual(LoanTransaction.objects.get(
            loan=self.loan).petty_topup.amount, Decimal("8000"))


class LeaderLoanVisibilityTests(TestCase):
    def setUp(self):
        posting.ensure_chart()
        self.tr = _user("ll_tr", TREASURER)
        self.leader = _user("ll_ld", LEADER)
        self.other_leader = _user("ll_ld2", LEADER)
        self.dev = Department.objects.create(name="Development", fund_type="LOCAL")
        self.youth = Department.objects.create(name="Youth", fund_type="LOCAL")
        DepartmentLeadership.objects.create(user=self.leader, department=self.dev)
        DepartmentLeadership.objects.create(user=self.other_leader, department=self.youth)
        lender = Lender.objects.create(name="ACME")
        self.loan = Loan.objects.create(lender=lender, fund=self.dev,
                                        loan_date=dt.date.today(),
                                        principal_amount=Decimal("50000"))
        svc.record_receipt(self.loan, date=dt.date.today(),
                           amount=Decimal("50000"), user=self.tr)

    def test_leader_sees_own_fund_loans(self):
        self.client.force_login(self.leader)
        r = self.client.get(reverse("leader_loans"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, self.loan.number)

    def test_leader_cannot_see_other_fund_loans(self):
        self.client.force_login(self.other_leader)
        r = self.client.get(reverse("leader_loans"))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, self.loan.number)

    def test_leader_loan_detail_scoped(self):
        # own fund: visible
        self.client.force_login(self.leader)
        self.assertEqual(
            self.client.get(reverse("leader_loan_detail", args=[self.loan.pk])).status_code, 200)
        # other leader: bounced away, never shown
        self.client.force_login(self.other_leader)
        r = self.client.get(reverse("leader_loan_detail", args=[self.loan.pk]))
        self.assertEqual(r.status_code, 302)

    def test_conditional_menu_flag(self):
        self.assertTrue(svc.user_has_accessible_loans(self.leader))
        self.assertFalse(svc.user_has_accessible_loans(self.other_leader))

    def test_leader_export(self):
        self.client.force_login(self.leader)
        r = self.client.get(reverse("leader_loans") + "?export=csv")
        self.assertEqual(r["Content-Type"], "text/csv")
        self.assertIn(self.loan.number, r.content.decode())

    def test_leader_loan_view_read_only(self):
        # no edit/receipt/repay actions leak into the leader detail page
        self.client.force_login(self.leader)
        r = self.client.get(reverse("leader_loan_detail", args=[self.loan.pk]))
        self.assertNotContains(r, reverse("loan_repay", args=[self.loan.pk]))
        self.assertNotContains(r, reverse("loan_edit", args=[self.loan.pk]))
