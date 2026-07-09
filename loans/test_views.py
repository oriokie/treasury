"""Loan module — views & permissions: every page renders for the roles that
may see it and is refused to those that may not; the money actions work
end-to-end through the forms; the review-queue hand-off records a queued bank
credit as a loan receipt; lender matching links/creates/merges correctly."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import ASSISTANT, AUDITOR, LEADER, TREASURER
from departments.models import Department
from giving.models import Transaction
from ledger.services import posting
from loans.models import Lender, Loan, LoanTransaction
from loans.services import loans as svc


def _user(name, role):
    u = User.objects.create_user(name, password="x")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


class LoanViewTests(TestCase):
    def setUp(self):
        posting.ensure_chart()
        self.treasurer = _user("lw_tr", TREASURER)
        self.assistant = _user("lw_as", ASSISTANT)
        self.auditor = _user("lw_au", AUDITOR)
        self.leader = _user("lw_le", LEADER)
        self.fund = Department.objects.create(name="Development", fund_type="LOCAL")
        self.lender = Lender.objects.create(name="ACME SACCO", phone="0722000111")
        self.loan = Loan.objects.create(lender=self.lender, fund=self.fund,
                                        loan_date=dt.date(2026, 1, 5),
                                        created_by=self.treasurer)
        svc.record_receipt(self.loan, date=dt.date(2026, 1, 5),
                           amount=Decimal("100000"), user=self.treasurer)

    # ---- access ----
    def test_read_pages_render_for_staff_and_auditor(self):
        pages = [reverse("loan_dashboard"), reverse("loan_register"),
                 reverse("loan_detail", args=[self.loan.pk]),
                 reverse("lender_list")]
        for user in (self.treasurer, self.assistant, self.auditor):
            self.client.force_login(user)
            for url in pages:
                self.assertEqual(self.client.get(url).status_code, 200,
                                 f"{user} {url}")

    def test_leader_is_refused(self):
        self.client.force_login(self.leader)
        r = self.client.get(reverse("loan_dashboard"))
        self.assertEqual(r.status_code, 302)

    def test_auditor_cannot_record_money(self):
        self.client.force_login(self.auditor)
        r = self.client.post(reverse("loan_repay", args=[self.loan.pk]),
                             {"date": "2026-02-01", "amount": "1000"})
        self.assertEqual(r.status_code, 302)          # bounced, not processed
        self.assertEqual(self.loan.outstanding_principal, Decimal("100000"))

    def test_convert_is_treasurer_only(self):
        self.client.force_login(self.assistant)
        r = self.client.post(reverse("loan_convert", args=[self.loan.pk]),
                             {"date": "2026-02-01", "amount": "100000"})
        self.assertEqual(self.loan.outstanding_principal, Decimal("100000"))
        self.client.force_login(self.treasurer)
        self.client.post(reverse("loan_convert", args=[self.loan.pk]),
                         {"date": "2026-02-01", "amount": "100000"})
        self.assertEqual(self.loan.outstanding_principal, Decimal(0))

    # ---- money actions through the forms ----
    def test_repay_through_form_with_validation(self):
        self.client.force_login(self.treasurer)
        url = reverse("loan_repay", args=[self.loan.pk])
        # over-repayment refused with the entry intact
        r = self.client.post(url, {"date": "2026-02-01", "amount": "100001",
                                   "method": "BANK"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "exceeds the outstanding principal")
        # a good repayment lands
        r = self.client.post(url, {"date": "2026-02-01", "amount": "40000",
                                   "method": "BANK"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.loan.outstanding_principal, Decimal("60000"))

    def test_receipt_and_interest_through_forms(self):
        self.client.force_login(self.treasurer)
        self.client.post(reverse("loan_receipt", args=[self.loan.pk]),
                         {"date": "2026-02-01", "amount": "20000", "channel": "CASH"})
        self.assertEqual(self.loan.received_total, Decimal("120000"))
        self.client.post(reverse("loan_interest", args=[self.loan.pk]),
                         {"date": "2026-02-15", "amount": "800", "method": "MPESA"})
        self.assertEqual(self.loan.interest_paid, Decimal("800"))

    def test_retired_loan_blocks_edit_and_delete_with_transactions(self):
        self.client.force_login(self.treasurer)
        svc.record_repayment(self.loan, date=dt.date(2026, 3, 1),
                             amount=Decimal("100000"), user=self.treasurer)
        self.assertEqual(self.loan.status, Loan.Status.COMPLETED)
        r = self.client.get(reverse("loan_edit", args=[self.loan.pk]))
        self.assertEqual(r.status_code, 302)          # bounced to detail
        r = self.client.post(reverse("loan_delete", args=[self.loan.pk]))
        self.assertTrue(Loan.objects.filter(pk=self.loan.pk).exists())

    def test_register_exports(self):
        self.client.force_login(self.auditor)
        r = self.client.get(reverse("loan_register") + "?export=csv")
        self.assertEqual(r["Content-Type"], "text/csv")
        self.assertIn(self.loan.number, r.content.decode())
        r = self.client.get(reverse("loan_detail", args=[self.loan.pk])
                            + "?export=xlsx")
        self.assertIn("spreadsheetml", r["Content-Type"])


class FromTransactionTests(TestCase):
    """The review-queue hand-off: a queued bank credit becomes a loan receipt
    while staying on the bank ledger for reconciliation."""

    def setUp(self):
        posting.ensure_chart()
        self.treasurer = _user("ft_tr", TREASURER)
        self.client.force_login(self.treasurer)
        self.fund = Department.objects.create(name="Development", fund_type="LOCAL")
        self.txn = Transaction.objects.create(
            date=dt.date(2026, 5, 2), channel="BANK", direction="CREDIT",
            amount=Decimal("20000"), department=None,
            allocation_status=Transaction.Status.REVIEW,
            payer_name="RUTH MOMANYI", payer_phone="254733000222",
            core_ref="UERQ1", reference="member loan")

    def test_creates_lender_loan_and_repoints_the_credit(self):
        url = reverse("loan_from_transaction", args=[self.txn.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        r = self.client.post(url, {"fund": self.fund.pk,
                                   "lender_name": "RUTH MOMANYI",
                                   "lender_phone": "254733000222"})
        self.assertEqual(r.status_code, 302)
        self.txn.refresh_from_db()
        self.assertTrue(self.txn.excluded_from_income)
        self.assertEqual(self.txn.department_id, self.fund.pk)
        self.assertIsNone(self.txn.member_id)
        loan = Loan.objects.get()
        self.assertEqual(loan.outstanding_principal, Decimal("20000"))
        self.assertEqual(loan.lender.name, "RUTH MOMANYI")
        # off the review queue and posted as a liability
        self.assertEqual(self.txn.allocation_status, Transaction.Status.MANUAL)
        from ledger.models import JournalLine
        payable = JournalLine.objects.filter(
            entry__source_type="transaction", entry__source_id=self.txn.pk,
            account__system_key="LOANS_PAYABLE")
        self.assertEqual(sum(l.credit for l in payable), Decimal("20000"))

    def test_cannot_record_twice(self):
        url = reverse("loan_from_transaction", args=[self.txn.pk])
        self.client.post(url, {"fund": self.fund.pk})
        self.client.post(url, {"fund": self.fund.pk})
        self.assertEqual(LoanTransaction.objects.count(), 1)


class LenderMatchingViewTests(TestCase):
    def setUp(self):
        self.treasurer = _user("lm_tr2", TREASURER)
        self.client.force_login(self.treasurer)
        self.fund = Department.objects.create(name="Development", fund_type="LOCAL")
        self.lender = Lender.objects.create(name="RUTH MOMANYI",
                                            phone="0733000222")
        Loan.objects.create(lender=self.lender, fund=self.fund,
                            loan_date=dt.date.today())

    def test_link_existing_member(self):
        from members.models import Member
        m = Member.objects.create(name="RUTH MOMANYI", phone="0733000222")
        r = self.client.get(reverse("lender_matching"))
        self.assertContains(r, "RUTH MOMANYI")
        self.client.post(reverse("lender_matching"),
                         {"action": "link", "lender_id": self.lender.pk,
                          "member_id": m.pk})
        self.lender.refresh_from_db()
        self.assertEqual(self.lender.member_id, m.pk)

    def test_create_member_prefilled(self):
        from members.models import Member
        self.client.post(reverse("lender_matching"),
                         {"action": "create_member", "lender_id": self.lender.pk})
        self.lender.refresh_from_db()
        m = Member.objects.get(pk=self.lender.member_id)
        self.assertEqual(m.name, "RUTH MOMANYI")
        self.assertEqual(m.phone, "254733000222")

    def test_merge_duplicates(self):
        dup = Lender.objects.create(name="MOMANYI RUTH")
        Loan.objects.create(lender=dup, fund=self.fund, loan_date=dt.date.today())
        self.client.post(reverse("lender_matching"),
                         {"action": "merge", "lender_id": self.lender.pk,
                          "absorb_id": dup.pk})
        dup.refresh_from_db()
        self.assertEqual(dup.merged_into_id, self.lender.pk)
        self.assertEqual(self.lender.loans.count(), 2)

    def test_duplicate_phone_blocked_on_the_form(self):
        r = self.client.post(reverse("lender_new"),
                             {"name": "R MOMANYI JR", "phone": "0733000222",
                              "status": "ACTIVE"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "already exists")
        self.assertEqual(Lender.objects.count(), 1)
