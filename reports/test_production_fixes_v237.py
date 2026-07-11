"""Tests for the production-review fixes (v2.37):

1. Fund ledger includes loan/financing receipts and expense refunds, so its
   closing balance ties to the canonical fund balance (previously loans were
   invisible and the ledger could not reconcile).
2. The expense form's available-balance endpoint reads the canonical
   fund_balance_parts — reversed/reversal credits are no longer counted,
   remittance spend is no longer excluded, refunds are included — so the form
   always agrees with the departments page and every report.
3. Numbered fund families accept /regex/ prefixes for misspellings; invalid
   patterns and capturing groups are handled safely.
4. The Statement of Financial Position reclassifies the petty-cash float out
   of Cash & bank onto its own line (mirroring staff advances); totals and the
   balance-sheet tie are unchanged.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from cashbook.models import (Expense, ExpenseRefund, FundTransfer,
                             PettyCashTopUp)
from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction


def _treasurer(username):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
    return u


class FundLedgerLoansAndRefundsTests(TestCase):
    """Item 1 — loans and refunds must appear so the ledger reconciles."""

    def setUp(self):
        self.u = User.objects.create_user("fl_u", password="x", is_superuser=True)
        self.tr = _treasurer("fl_tr")
        self.dept = Department.objects.create(name="Development",
                                              fund_type="LOCAL")
        # ordinary income credit
        Transaction.objects.create(
            date=dt.date(2026, 3, 5), channel="CASH", direction="CREDIT",
            amount=Decimal("10000"), department=self.dept,
            allocation_status="AUTO", confirmed=True)
        # loan receipt: cash into the fund, but NOT income
        Transaction.objects.create(
            date=dt.date(2026, 3, 10), channel="BANK", direction="CREDIT",
            amount=Decimal("50000"), department=self.dept,
            allocation_status="MANUAL", confirmed=True,
            excluded_from_income=True, payer_name="DEV LOAN")
        # an expense and a partial refund of it
        exp = Expense.objects.create(
            date=dt.date(2026, 3, 15), department=self.dept,
            description="Materials", amount=Decimal("4000"),
            category="MATERIALS", status="PAID", recorded_by=self.u)
        ExpenseRefund.objects.create(expense=exp, date=dt.date(2026, 3, 20),
                                     amount=Decimal("500"), recorded_by=self.u)

    def _ledger_ctx(self):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("report_fund", args=[self.dept.id])
                            + "?start=2026-03-01&end=2026-03-31")
        self.assertEqual(r.status_code, 200)
        return r.context

    def test_loan_receipt_appears_labelled_as_financing(self):
        ctx = self._ledger_ctx()
        financing = [e for e in ctx["entries"] if e["src"] == "Financing"]
        self.assertEqual(len(financing), 1)
        self.assertEqual(financing[0]["credit"], Decimal("50000"))
        self.assertIn("financing", financing[0]["desc"].lower())
        self.assertIn("not income", financing[0]["desc"].lower())

    def test_refund_appears_as_contra_credit(self):
        ctx = self._ledger_ctx()
        refunds = [e for e in ctx["entries"] if e["src"] == "Refund"]
        self.assertEqual(len(refunds), 1)
        self.assertEqual(refunds[0]["credit"], Decimal("500"))

    def test_ledger_closing_ties_to_canonical_fund_balance(self):
        from reports.services.balances import fund_balance
        ctx = self._ledger_ctx()
        # 0 + 10000 + 50000 − 4000 + 500 = 56500
        self.assertEqual(ctx["closing"], Decimal("56500"))
        self.assertEqual(ctx["closing"],
                         fund_balance(self.dept, dt.date(2026, 3, 31)))

    def test_loan_still_excluded_from_income(self):
        # the ledger shows the cash, income reports must not count it
        from core.metrics import metrics
        self.assertEqual(metrics.total_income(dt.date(2026, 3, 1),
                                              dt.date(2026, 3, 31)),
                         Decimal("10000"))


class ExpenseAvailableBalanceTests(TestCase):
    """Item 2 — the form's available balance must equal the canonical fund
    balance; reversals, remittances and refunds must be treated correctly."""

    def setUp(self):
        self.u = User.objects.create_user("ab_u", password="x", is_superuser=True)
        self.tr = _treasurer("ab_tr")
        self.dept = Department.objects.create(
            name="LCB", fund_type="LOCAL", opening_balance=Decimal("1000"))
        self.other = Department.objects.create(name="Youth", fund_type="LOCAL")
        # a good credit
        Transaction.objects.create(
            date=dt.date(2026, 4, 1), channel="CASH", direction="CREDIT",
            amount=Decimal("5000"), department=self.dept,
            allocation_status="AUTO", confirmed=True)
        # a REVERSED credit + its reversal row — Edwin's production suspicion:
        # neither may count as a receipt
        Transaction.objects.create(
            date=dt.date(2026, 4, 2), channel="CASH", direction="CREDIT",
            amount=Decimal("2000"), department=self.dept,
            allocation_status="AUTO", confirmed=True, is_reversed=True)
        Transaction.objects.create(
            date=dt.date(2026, 4, 2), channel="CASH", direction="CREDIT",
            amount=Decimal("2000"), department=self.dept,
            allocation_status="AUTO", confirmed=True, is_reversal=True)
        # a remittance-category expense — real cash out, must reduce available
        Expense.objects.create(
            date=dt.date(2026, 4, 5), department=self.dept,
            description="Remit", amount=Decimal("700"),
            category="REMITTANCE", status="PAID", recorded_by=self.u)
        # an ordinary expense with a refund
        exp = Expense.objects.create(
            date=dt.date(2026, 4, 6), department=self.dept,
            description="Supplies", amount=Decimal("300"),
            category="MATERIALS", status="PAID", recorded_by=self.u)
        ExpenseRefund.objects.create(expense=exp, date=dt.date(2026, 4, 8),
                                     amount=Decimal("100"), recorded_by=self.u)
        # a transfer out
        FundTransfer.objects.create(
            date=dt.date(2026, 4, 9), source=self.dept, destination=self.other,
            amount=Decimal("400"), recorded_by=self.u)

    def _endpoint(self):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("department_balance")
                            + f"?id={self.dept.id}")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_reversed_credits_not_counted(self):
        d = self._endpoint()
        # only the 5000 good credit — not the reversed 2000 nor its reversal row
        self.assertEqual(Decimal(str(d["receipts"])), Decimal("5000"))

    def test_balance_equals_canonical_fund_balance(self):
        from reports.services.balances import fund_balance
        d = self._endpoint()
        # 1000 + 5000 − (700 + 300) + 100 − 400 = 4700
        self.assertEqual(Decimal(str(d["balance"])), Decimal("4700"))
        self.assertEqual(Decimal(str(d["balance"])),
                         fund_balance(self.dept))

    def test_balance_equals_department_summary_closing(self):
        from reports.services.balances import department_summary
        d = self._endpoint()
        row = next(r for r in department_summary(None, None)
                   if r["department"].id == self.dept.id)
        self.assertEqual(Decimal(str(d["balance"])), row["closing"])

    def test_remittance_reduces_available(self):
        d = self._endpoint()
        self.assertEqual(Decimal(str(d["spent"])), Decimal("1000"))  # 700+300

    def test_breakdown_fields_present(self):
        d = self._endpoint()
        for k in ("opening", "receipts", "spent", "refunded",
                  "transfers_in", "transfers_out", "balance"):
            self.assertIn(k, d, k)


class RegexFundFamilyTests(TestCase):
    """Item 3 — /regex/ prefixes in numbered fund families."""

    def setUp(self):
        from core.models import SiteConfig
        self.cfg = SiteConfig.get()
        for n in (1, 7, 30):
            Department.objects.create(name=f"CAMP_{n}", fund_type="LOCAL")

    def _set(self, value):
        self.cfg.numbered_fund_families = value
        self.cfg.save()

    def _allocate(self, ref):
        from giving.services.allocation import allocate
        return allocate(ref)

    def test_plain_prefixes_still_work(self):
        self._set("expense, exp, expe = CAMP_{n}")
        dept, status = self._allocate("EXPENSE7")
        self.assertEqual(getattr(dept, "name", None), "CAMP_7")
        self.assertEqual(status, "AUTO")

    def test_regex_prefix_catches_misspellings(self):
        self._set("/expen[sc]es?/, exp = CAMP_{n}")
        for ref in ("EXPENCE7", "EXPENSES7", "expense 30"):
            dept, status = self._allocate(ref)
            self.assertEqual(getattr(dept, "name", None),
                             "CAMP_7" if "7" in ref else "CAMP_30", ref)
            self.assertEqual(status, "AUTO", ref)

    def test_regex_with_capturing_group_is_made_safe(self):
        # a user pattern with its own group must not shift the number capture
        self._set("/exp(ense|ence)/ = CAMP_{n}")
        dept, status = self._allocate("EXPENCE1")
        self.assertEqual(getattr(dept, "name", None), "CAMP_1")

    def test_invalid_regex_is_skipped_not_fatal(self):
        self._set("/exp[ense/ = CAMP_{n}\nexpense = CAMP_{n}")
        dept, status = self._allocate("EXPENSE7")   # plain line still works
        self.assertEqual(getattr(dept, "name", None), "CAMP_7")
        # the broken pattern alone never matches and never crashes
        self._set("/exp[ense/ = CAMP_{n}")
        dept, status = self._allocate("EXPENSE7")
        self.assertEqual(dept, "UNALLOCATED")

    def test_nonexistent_fund_falls_through(self):
        self._set("/expen[sc]e/ = CAMP_{n}")
        dept, status = self._allocate("EXPENSE99")   # no CAMP_99 fund
        self.assertEqual(dept, "UNALLOCATED")


class SofpPettyCashTests(TestCase):
    """Item 4 — petty cash on its own SOFP line; the statement still ties."""

    def setUp(self):
        self.u = User.objects.create_user("pc_u", password="x", is_superuser=True)
        self.tr = _treasurer("pc_tr")
        self.dept = Department.objects.create(name="LCB", fund_type="LOCAL")
        Transaction.objects.create(
            date=dt.date(2026, 5, 1), channel="CASH", direction="CREDIT",
            amount=Decimal("20000"), department=self.dept,
            allocation_status="AUTO", confirmed=True)
        PettyCashTopUp.objects.create(date=dt.date(2026, 5, 2),
                                      amount=Decimal("3000"),
                                      recorded_by=self.u)

    def test_petty_line_shown_and_statement_ties(self):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("report_financial_position")
                            + "?as_of=2026-05-31")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["petty"], Decimal("3000"))
        # reclassification only: cash_on_hand + petty + advances == total cash
        self.assertEqual(r.context["cash_on_hand"] + r.context["petty"]
                         + r.context["advances"], r.context["cash"])
        self.assertTrue(r.context["balanced"])
        self.assertContains(r, "Petty cash float")

    def test_engine_summary_shows_same_split(self):
        from core.reporting import ReportContext
        from reports.financial_statements import FinancialPositionSummarySection
        ctx = ReportContext.for_period(dt.date(2026, 1, 1),
                                       dt.date(2026, 5, 31))
        data = FinancialPositionSummarySection().render(ctx, {})
        labels = [row.cells["label"] for row in data.rows]
        self.assertIn("Petty cash float", labels)
        self.assertIn("Staff advances (receivable)", labels)
        by = {row.cells["label"]: row.cells["value"] for row in data.rows}
        self.assertEqual(by["Petty cash float"], Decimal("3000"))
        # the three cash lines sum to the fund cash inside total assets
        cash_lines = (by["Bank (funds on hand)"]
                      + by["Petty cash float"]
                      + by["Staff advances (receivable)"])
        self.assertEqual(cash_lines + by["Receipts pending allocation"],
                         by["Total assets"])
