"""Round 8 — the debit bug, at its true root.

A real bank statement (Transaction_Summary_14Jul2026) exports **no debit column
at all**:

    Posting Date | Value Date | Core Ref | Channel REF | Narration |
    Credit Amount | Running Balance

A cheque payment appears as **Credit Amount = 0.00**, with the **running balance
dropping** by the amount paid. Every one of those rows was being thrown away by
the parser's "nothing moved on this row" guard. On one month's statement that
silently discarded eight cheques worth **3,061,850**.

And because no debit ever reached the ledger, the debit review queue stayed
permanently empty — so `clear_for_bank_debit` and `suggest_instrument_for_debit`,
which have been built, tested and wired into that queue all along, had nothing
whatever to act on.

One bug. Three symptoms: debits not importing, the register's balance not
reconciling, and cheques never clearing.
"""
import datetime as dt
import io
from decimal import Decimal

import openpyxl
from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction

from cashbook.models import PaymentInstrument
from cashbook.services import payments as pay_svc
from statements.models import BankAccount, StatementImport
from statements.services import importer as imp_svc
from statements.services import parser as parser_svc
from statements.services import register as reg_svc
from statements.models_register import StatementLine


def _real_bank_xlsx(rows):
    """A file in the EXACT shape the real bank exports: a credit column, a running
    balance, and **no debit column**. `rows` are (date, narration, credit, balance).
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([])
    ws.append([None, "Transaction Summary"])
    ws.append([])
    ws.append([None, "Report generated on JUL 14 2026"])
    ws.append([None, None, None, None, None, None, None, "Total Search Results: 3"])
    ws.append([None, "Posting Date", "Value Date", "Core Ref", "Channel REF",
               "Narration", "Credit Amount", "Running Balance"])
    for i, (date, narr, credit, balance) in enumerate(rows):
        ws.append([None, date, date, f"CB{i:013d}", f"CHAN{i:04d}",
                   narr, credit, balance])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class NoDebitColumnParsingTests(TestCase):
    """The bank does not tell us the debit. It tells us the balance, and expects
    us to notice it went down."""

    def test_a_debit_is_derived_from_the_falling_running_balance(self):
        data = _real_bank_xlsx([
            ("01-07-2026", "UG1A39TLDA~441211#tithe", "300.00", "4,234,950.03"),
            # a cheque: credit column says 0.00, but the balance drops 15,500
            ("01-07-2026", "HENRY CHQ No.000412", "0.00", "4,219,450.03"),
            ("02-07-2026", "UG1FBABNXX~441211#offering", "6,000.00", "4,225,450.03"),
        ])
        rows = parser_svc.read_rows(data, "s.xlsx")

        self.assertEqual(len(rows), 3, "the debit row was silently discarded")
        debits = [r for r in rows if r.get("debit")]
        self.assertEqual(len(debits), 1)
        self.assertEqual(debits[0]["debit"], Decimal("15500.00"))
        self.assertIn("000412", debits[0]["raw_narration"])

    def test_the_credit_column_is_still_believed_where_it_says_something(self):
        """Deliberately conservative: this only fills in a movement the file does
        not otherwise state. A row whose credit column carries a real figure is
        taken exactly as given."""
        data = _real_bank_xlsx([
            ("01-07-2026", "UG1A39TLDA~441211#a", "300.00", "1,000.00"),
            ("01-07-2026", "UG1FBABNXX~441211#b", "700.00", "1,700.00"),
        ])
        rows = parser_svc.read_rows(data, "s.xlsx")
        self.assertEqual([r["credit"] for r in rows],
                         [Decimal("300.00"), Decimal("700.00")])
        self.assertFalse(any(r.get("debit") for r in rows))

    def test_a_file_WITH_a_proper_debit_column_is_untouched(self):
        """The fix must not change how a well-behaved statement is read."""
        import csv
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Date", "Narration", "Credit", "Debit", "Balance"])
        w.writerow(["2026-07-01", "A GIFT", "1000", "", "1000"])
        w.writerow(["2026-07-02", "A CHEQUE", "", "250", "750"])
        rows = parser_svc.read_rows(buf.getvalue().encode(), "s.csv")
        self.assertEqual(rows[0]["credit"], Decimal("1000"))
        self.assertEqual(rows[1]["debit"], Decimal("250"))

    def test_the_whole_statement_reconciles_to_the_banks_own_closing_balance(self):
        """The reported symptom: "the statement's own balance column doesn't
        reconcile, which usually means a row is missing". It did — every debit."""
        data = _real_bank_xlsx([
            ("01-07-2026", "GIFT ONE", "300.00", "10,300.00"),
            ("01-07-2026", "CHQ No.000412", "0.00", "9,300.00"),
            ("02-07-2026", "GIFT TWO", "700.00", "10,000.00"),
            ("03-07-2026", "CHQ No.000413", "0.00", "5,000.00"),
        ])
        rows = parser_svc.read_rows(data, "s.xlsx")
        opening = (rows[0]["balance"] - (rows[0].get("credit") or 0)
                   + (rows[0].get("debit") or 0))
        computed = opening
        for r in rows:
            computed += (r.get("credit") or 0) - (r.get("debit") or 0)
        self.assertEqual(computed, rows[-1]["balance"],
                         "our arithmetic does not reach the bank's own closing "
                         "balance — a row is missing, and it is a debit")


class DebitsReachTheLedgerTests(TestCase):

    def setUp(self):
        self.treasurer = User.objects.create_user("t_r8", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.account = BankAccount.objects.create(name="Main", account_number="1")
        self.data = _real_bank_xlsx([
            ("01-07-2026", "UG1A39TLDA~441211#tithe", "300.00", "10,300.00"),
            ("01-07-2026", "HENRY CHQ No.000412", "0.00", "-5,200.00"),
        ])

    def test_the_importer_posts_the_debit(self):
        si = StatementImport.objects.create(
            uploaded_by=self.treasurer, filename="s.xlsx",
            bank_account=self.account)
        imp_svc.run_import(si, path_or_bytes=self.data, filename="s.xlsx")
        debits = Transaction.objects.filter(statement_import=si, direction="DEBIT")
        self.assertEqual(debits.count(), 1, "the debit never reached the ledger")
        self.assertEqual(debits.first().amount, Decimal("15500.00"))

    def test_the_register_holds_the_debit_and_reconciles(self):
        reg_svc.import_file(self.account, path_or_bytes=self.data,
                            filename="s.xlsx", user=self.treasurer)
        self.assertEqual(
            StatementLine.objects.filter(debit__isnull=False).count(), 1)
        self.assertEqual(reg_svc.balance_drift(self.account), [],
                         "the register's running balance does not agree with the "
                         "bank's own column — a line is missing")


class ChequeAutoClearingTests(TestCase):
    """Reported: "automatching CHQs pending clearing".

    The machinery for this was already built, tested, and wired into the debit
    review queue. It had simply never run, because the queue was permanently
    empty — no debit ever survived the parser.
    """

    def setUp(self):
        self.treasurer = User.objects.create_user("t_ch", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.account = BankAccount.objects.create(name="Main", account_number="1")

    def _cheque(self, number, amount):
        return PaymentInstrument.objects.create(
            method="CHEQUE", instrument_number=number, payee="A Payee",
            amount=Decimal(amount), source_kind="EXPENSE", status="ISSUED",
            date_issued=dt.date(2026, 6, 28))

    def _import(self, rows):
        si = StatementImport.objects.create(
            uploaded_by=self.treasurer, filename="s.xlsx",
            bank_account=self.account)
        imp_svc.run_import(si, path_or_bytes=_real_bank_xlsx(rows),
                           filename="s.xlsx")
        return si

    def test_a_cheque_the_bank_has_debited_is_cleared_automatically(self):
        chq = self._cheque("000412", "15500")
        self._import([
            ("01-07-2026", "A GIFT", "300.00", "10,300.00"),
            ("01-07-2026", "HENRY CHQ No.000412", "0.00", "-5,200.00"),
        ])
        chq.refresh_from_db()
        self.assertEqual(chq.status, "CLEARED")
        self.assertEqual(chq.date_cleared, dt.date(2026, 7, 1))
        self.assertIsNotNone(chq.bank_transaction_id,
                             "the cheque should be linked to the bank debit that "
                             "cleared it — that link IS the reconciliation trail")

    def test_the_cheque_number_matches_with_or_without_leading_zeros(self):
        """A bank prints "CHQ No.000412"; a cheque book may be recorded as "412"."""
        chq = self._cheque("412", "15500")
        self._import([
            ("01-07-2026", "A GIFT", "300.00", "10,300.00"),
            ("01-07-2026", "CHQ No.000412", "0.00", "-5,200.00"),
        ])
        chq.refresh_from_db()
        self.assertEqual(chq.status, "CLEARED")

    def test_a_number_match_with_the_WRONG_AMOUNT_is_NOT_auto_cleared(self):
        """A cheque that was altered, partly paid, or misread. That wants
        somebody's eyes on it, not a silent tick."""
        chq = self._cheque("000412", "9999")     # we wrote it for 9,999
        self._import([
            ("01-07-2026", "A GIFT", "300.00", "10,300.00"),
            ("01-07-2026", "CHQ No.000412", "0.00", "-5,200.00"),   # bank took 15,500
        ])
        chq.refresh_from_db()
        self.assertEqual(chq.status, "ISSUED",
                         "a cheque whose number matches but whose AMOUNT does not "
                         "must not be cleared silently")

    def test_a_cheque_the_bank_has_not_debited_stays_outstanding(self):
        chq = self._cheque("000999", "5000")
        self._import([("01-07-2026", "A GIFT", "300.00", "10,300.00")])
        chq.refresh_from_db()
        self.assertEqual(chq.status, "ISSUED")

    def test_an_amount_only_match_is_never_auto_applied(self):
        """Two cheques for the same amount are perfectly ordinary, and guessing
        between them would clear the wrong one. It stays a SUGGESTION, offered in
        the debit queue, exactly as it always was."""
        a = self._cheque("000501", "15500")
        b = self._cheque("000502", "15500")
        self._import([
            ("01-07-2026", "A GIFT", "300.00", "10,300.00"),
            ("01-07-2026", "UNMARKED PAYMENT", "0.00", "-5,200.00"),
        ])
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.status, "ISSUED")
        self.assertEqual(b.status, "ISSUED")

    def test_clearing_is_idempotent_across_a_re_import(self):
        """Re-importing an overlapping period must not try to clear a cheque
        twice, nor relink it to a different debit."""
        chq = self._cheque("000412", "15500")
        rows = [("01-07-2026", "A GIFT", "300.00", "10,300.00"),
                ("01-07-2026", "CHQ No.000412", "0.00", "-5,200.00")]
        self._import(rows)
        first_txn = PaymentInstrument.objects.get(pk=chq.pk).bank_transaction_id
        self._import(rows)          # the statement, again
        chq.refresh_from_db()
        self.assertEqual(chq.status, "CLEARED")
        self.assertEqual(chq.bank_transaction_id, first_txn)

    def test_the_register_matches_the_debit_against_the_cheque_we_wrote(self):
        from statements.models_register import RegisterException
        self._cheque("000412", "15500")
        rows = [("01-07-2026", "A GIFT", "300.00", "10,300.00"),
                ("01-07-2026", "CHQ No.000412", "0.00", "-5,200.00")]
        self._import(rows)
        reg_svc.import_file(self.account, path_or_bytes=_real_bank_xlsx(rows),
                            filename="s.xlsx", user=self.treasurer)
        reg_svc.recheck(self.account)
        self.assertFalse(
            RegisterException.objects.filter(
                kind=RegisterException.Kind.MISSING_IN_LEDGER,
                status=RegisterException.Status.OPEN).exists(),
            "a cheque the church wrote, and the bank debited, and the ledger "
            "recorded, was still reported as a discrepancy")
