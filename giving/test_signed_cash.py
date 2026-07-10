"""Tests for Option A (recommendation #47): the canonical signed-cash
definition and its consumers.

The pair: a manually-entered BANK envelope posts an ENVELOPE transaction (the
income/fund row), and the matching bank-statement credit is marked via
``mark_manual_receipt`` — becoming a memo (excluded from income, detached from
its fund). The memo's cash lives on the envelope row, so in every cash
aggregation the memo must contribute ZERO. Covered here:

* ``Transaction.is_bank_memo`` / ``signed_cash_amount`` /
  ``signed_cash_case`` / queryset ``signed_cash_total`` agree with each other;
* the transactions page running balance no longer double-counts the pair
  (reproduction of the production report), across page boundaries too;
* ``processed_via_envelope`` bank rows (no second posting) still count;
* the CSV/XLSX export's Amount column sums to reality;
* the Cash Book counts confirmed, non-reversed, non-memo receipts only;
* the memo row is still visible on the page, badged.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction


def _treasurer(username):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
    return u


class _PairSeed(TestCase):
    """One manually-receipted pair + one ordinary credit + one debit."""

    def setUp(self):
        self.tr = _treasurer("oa_tr")
        self.dept = Department.objects.create(name="Tithe", fund_type="TRUST")
        # the envelope half — the income/fund row (the money)
        self.env = Transaction.objects.create(
            date=dt.date(2026, 6, 6), channel="ENVELOPE", direction="CREDIT",
            amount=Decimal("15000"), department=self.dept,
            allocation_status="MANUAL", confirmed=True,
            payer_name="MARY", reference="envelope B12")
        # the bank half — imported from the statement, then manually receipted
        self.bank = Transaction.objects.create(
            date=dt.date(2026, 6, 8), channel="BANK", direction="CREDIT",
            amount=Decimal("15000"), department=self.dept,
            allocation_status="AUTO", confirmed=True, payer_name="MARY")
        self.bank.mark_manual_receipt(True)
        self.bank.refresh_from_db()
        # an ordinary credit and a bank debit for signing coverage
        self.plain = Transaction.objects.create(
            date=dt.date(2026, 6, 10), channel="CASH", direction="CREDIT",
            amount=Decimal("4000"), department=self.dept,
            allocation_status="AUTO", confirmed=True)
        self.debit = Transaction.objects.create(
            date=dt.date(2026, 6, 12), channel="BANK", direction="DEBIT",
            amount=Decimal("1000"), allocation_status="MANUAL", confirmed=True)


class SignedCashDefinitionTests(_PairSeed):
    def test_memo_predicate(self):
        self.assertTrue(self.bank.is_bank_memo)
        self.assertFalse(self.env.is_bank_memo)
        self.assertFalse(self.plain.is_bank_memo)
        # marking is reversible — unmarking restores the cash effect
        self.bank.mark_manual_receipt(False)
        self.bank.refresh_from_db()
        self.assertFalse(self.bank.is_bank_memo)

    def test_signed_amounts(self):
        self.assertEqual(self.bank.signed_cash_amount, Decimal(0))
        self.assertEqual(self.env.signed_cash_amount, Decimal("15000"))
        self.assertEqual(self.plain.signed_cash_amount, Decimal("4000"))
        self.assertEqual(self.debit.signed_cash_amount, Decimal("-1000"))

    def test_case_expression_agrees_with_property(self):
        total = Transaction.objects.filter(
            id__in=[self.env.id, self.bank.id, self.plain.id,
                    self.debit.id]).signed_cash_total()
        by_property = sum((t.signed_cash_amount for t in
                           [self.env, self.bank, self.plain, self.debit]),
                          Decimal(0))
        self.assertEqual(total, by_property)          # 15000+0+4000-1000
        self.assertEqual(total, Decimal("18000"))

    def test_reversal_still_nets_to_zero(self):
        orig = Transaction.objects.create(
            date=dt.date(2026, 6, 14), channel="CASH", direction="CREDIT",
            amount=Decimal("500"), department=self.dept,
            allocation_status="AUTO", confirmed=True, is_reversed=True)
        rev = Transaction.objects.create(
            date=dt.date(2026, 6, 14), channel="CASH", direction="CREDIT",
            amount=Decimal("500"), department=self.dept,
            allocation_status="AUTO", confirmed=True, is_reversal=True)
        self.assertEqual(Transaction.objects.filter(
            id__in=[orig.id, rev.id]).signed_cash_total(), Decimal(0))

    def test_processed_via_envelope_is_not_a_memo(self):
        # the system pull attaches an envelope to the bank row — no second
        # posting — so the bank row is still the money and must count
        t = Transaction.objects.create(
            date=dt.date(2026, 6, 15), channel="BANK", direction="CREDIT",
            amount=Decimal("700"), department=self.dept,
            allocation_status="AUTO", confirmed=True,
            processed_via_envelope=True)
        self.assertFalse(t.is_bank_memo)
        self.assertEqual(t.signed_cash_amount, Decimal("700"))


class RunningBalanceTests(_PairSeed):
    def _page(self, extra=""):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("transaction_list") + f"?{extra}")
        self.assertEqual(r.status_code, 200)
        return r

    def test_running_balance_counts_pair_once(self):
        r = self._page()
        balances = r.context["running_balances"]
        # final (newest) balance: 15000 (env) + 0 (memo) + 4000 − 1000 = 18000
        self.assertEqual(balances[self.debit.id], Decimal("18000"))
        # the memo row leaves the balance unchanged from the row before it
        self.assertEqual(balances[self.bank.id], balances[self.env.id])

    def test_memo_zero_effect_survives_pagination_boundary(self):
        # push the pair off the current page so the memo lands in the
        # "everything before this page" aggregate — the SQL Case path
        for i in range(60):
            Transaction.objects.create(
                date=dt.date(2026, 6, 20), channel="CASH", direction="CREDIT",
                amount=Decimal("10"), department=self.dept,
                allocation_status="AUTO", confirmed=True)
        r = self._page("page=1")
        balances = r.context["running_balances"]
        newest = max(balances.values())
        # 18000 + 60×10 = 18600 — the memo contributed nothing via either path
        self.assertEqual(newest, Decimal("18600"))

    def test_memo_row_still_visible_and_badged(self):
        html = self._page().content.decode()
        self.assertIn("MEMO", html)
        self.assertIn("no cash effect", html)


class ExportTests(_PairSeed):
    def test_export_amount_column_sums_to_reality(self):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("transaction_list") + "?export=csv")
        self.assertEqual(r.status_code, 200)
        import csv, io
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        header, data = rows[0], rows[1:]
        amt = header.index("Amount")
        status = header.index("Receipt status")
        total = sum(Decimal(row[amt]) for row in data if row[amt])
        self.assertEqual(total, Decimal("18000"))
        memo_rows = [row for row in data if row[status].startswith("Memo")]
        self.assertEqual(len(memo_rows), 1)
        self.assertEqual(Decimal(memo_rows[0][amt]), Decimal(0))


class CashBookTests(_PairSeed):
    def test_cash_book_counts_pair_once(self):
        # add rows the cash book must ignore: unconfirmed, and a reversed pair
        Transaction.objects.create(
            date=dt.date(2026, 6, 9), channel="CASH", direction="CREDIT",
            amount=Decimal("999"), department=self.dept,
            allocation_status="AUTO", confirmed=False)
        Transaction.objects.create(
            date=dt.date(2026, 6, 9), channel="CASH", direction="CREDIT",
            amount=Decimal("300"), department=self.dept,
            allocation_status="AUTO", confirmed=True, is_reversed=True)
        Transaction.objects.create(
            date=dt.date(2026, 6, 9), channel="CASH", direction="CREDIT",
            amount=Decimal("300"), department=self.dept,
            allocation_status="AUTO", confirmed=True, is_reversal=True)
        self.client.force_login(self.tr)
        r = self.client.get(reverse("report_cashbook")
                            + "?start=2026-06-01&end=2026-06-30")
        self.assertEqual(r.status_code, 200)
        credits = sum((en["credit"] or Decimal(0)) for en in r.context["entries"])
        # envelope 15000 + plain 4000 — memo, unconfirmed and reversed excluded
        self.assertEqual(credits, Decimal("19000"))
