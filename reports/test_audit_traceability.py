"""The audit log can be searched by the reference somebody actually holds.

An audit line reading "Transaction changed by tabitha" is only useful if the
transaction can then be found. The detail column held `str(instance)` — for a
bank receipt, a payer's name and little else — so an auditor holding an M-Pesa
code, a voucher number or an expense number had nothing to match on, and the
search box searched only that same rendered string.

The log now carries each record's id, the reference it is known by, and its
amount; searching finds a record by any of them, the CSV export carries them, and
where the record has a page of its own the reference links to it.

The other thing these tests hold is the cost. The view already had a note
explaining that calling `str()` on a reconstructed historical instance issued a
query per row, and it was careful to avoid it. Reading a reference off the
historical row's own columns keeps that promise; reading it off `h.instance`
would quietly undo it, and on a church with years of history that is the
difference between a page and a timeout.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from cashbook.models import Expense
from core import roles
from departments.models import Department
from giving.models import Transaction
from members.models import Member


class AuditTraceabilityTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user("tess-audit", password="office-pass-1")
        self.user.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.fund = Department.objects.create(
            name="Local Church Budget", slug="lcb-audit",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.transaction = Transaction.objects.create(
            date=dt.date.today(), channel="BANK", direction="CREDIT",
            amount=Decimal("2500"), department=self.fund,
            allocation_status="AUTO", confirmed=True,
            core_ref="AUDITCORE1", bank_receipt="UER2Q5NF2W",
            payer_name="KEVIN OGEGA")
        self.expense = Expense.objects.create(
            date=dt.date.today(), department=self.fund,
            description="Cement for repairs", amount=Decimal("900"),
            category=Expense.Category.MATERIALS, status=Expense.Status.PAID,
            recorded_by=self.user, voucher_no="VCH-77")
        self.client = Client()
        self.client.force_login(self.user)

    def _page(self, **params):
        response = self.client.get(reverse("report_audit"), params)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    # -- what the log shows --------------------------------------------------

    def test_the_bank_receipt_is_shown(self):
        self.assertIn("UER2Q5NF2W", self._page())

    def test_the_voucher_number_is_shown(self):
        self.assertIn("VCH-77", self._page())

    def test_the_record_id_is_shown(self):
        self.assertIn(f"#{self.expense.pk}", self._page())

    def test_the_amount_is_shown(self):
        self.assertIn("2,500", self._page())

    # -- and can be searched for ---------------------------------------------

    def test_a_member_can_be_traced_by_the_mpesa_receipt_they_quote(self):
        """The reason this page gets opened: somebody rings about a payment."""
        body = self._page(q="UER2Q5NF2W")
        self.assertIn("UER2Q5NF2W", body)
        self.assertNotIn("VCH-77", body)

    def test_a_payment_can_be_traced_by_the_banks_own_reference(self):
        self.assertIn("AUDITCORE1", self._page(q="AUDITCORE1"))

    def test_an_expense_can_be_traced_by_its_voucher(self):
        body = self._page(q="VCH-77")
        self.assertIn("VCH-77", body)
        self.assertNotIn("UER2Q5NF2W", body)

    def test_an_expense_can_be_traced_by_its_number(self):
        self.assertIn(f"#{self.expense.pk}", self._page(q=str(self.expense.pk)))

    def test_a_search_that_matches_nothing_says_so(self):
        self.assertIn("No matching history", self._page(q="NOSUCHREFERENCE"))

    # -- and followed back to the record --------------------------------------

    def test_an_expense_links_to_its_own_page(self):
        self.assertIn(reverse("expense_detail", args=[self.expense.pk]),
                      self._page(q="VCH-77"))

    def test_a_member_links_to_their_own_page(self):
        member = Member.objects.create(name="Ruth Momanyi", phone="254790301470")
        self.assertIn(reverse("member_detail", args=[member.pk]),
                      self._page(q="254790301470"))

    # -- the export carries them ---------------------------------------------

    def test_the_csv_has_columns_for_the_reference_and_the_id(self):
        raw = self.client.get(reverse("report_audit"),
                              {"export": "csv"}).content.decode()
        header = raw.splitlines()[0]
        for column in ("Record ID", "Reference", "Amount"):
            self.assertIn(column, header)

    def test_the_csv_carries_the_receipt_itself(self):
        raw = self.client.get(reverse("report_audit"),
                              {"export": "csv"}).content.decode()
        self.assertIn("UER2Q5NF2W", raw)

    # -- without costing a query per row --------------------------------------

    def test_the_page_does_not_query_per_history_row(self):
        """The promise the view's own note makes, kept.

        Twenty more transactions, each with their own history, must not mean
        twenty more queries. Reading the reference off the historical row's
        columns is what keeps this true; reading it off `h.instance` would not.
        """
        self._page()
        with CaptureQueriesContext(connection) as ctx:
            self._page()
        before = len(ctx.captured_queries)
        for i in range(20):
            Transaction.objects.create(
                date=dt.date.today(), channel="BANK", direction="CREDIT",
                amount=Decimal("100"), department=self.fund,
                allocation_status="AUTO", confirmed=True,
                core_ref=f"BULK{i}", bank_receipt=f"UERBULK{i}")
        with CaptureQueriesContext(connection) as ctx:
            self._page()
        after = len(ctx.captured_queries)
        self.assertLessEqual(
            after, before + 2,
            f"The audit log cost {before} queries and now costs {after} after "
            "twenty more records — it is querying per history row again.")

    def test_a_record_with_no_reference_does_not_break_the_row(self):
        """Most models have no such thing, and must render anyway."""
        Transaction.objects.create(
            date=dt.date.today(), channel="CASH", direction="CREDIT",
            amount=Decimal("50"), department=self.fund,
            allocation_status="MANUAL", confirmed=True, core_ref="NOREF1")
        self.assertIn("Audit log", self._page())

    def test_existing_filters_still_work_alongside_the_search(self):
        body = self._page(model="Expense", q="VCH-77")
        self.assertIn("VCH-77", body)
        self.assertNotIn("UER2Q5NF2W", body)
