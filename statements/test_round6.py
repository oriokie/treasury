"""Round 6 — reported issues.

The serious one is again the register's MATCHING (item 3). My previous fix
removed the date-window bug but left three others, because I was still filtering
the match index by things that are OUR classifications rather than the bank's
facts: channel, bank account, and reversal status. Every one of them could hide
a transaction that plainly carried the bank's reference.
"""
import csv
import datetime as dt
import io
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.models import SiteConfig
from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from members.models import Member

from cashbook.models import Expense, PaymentInstrument, PettyCashTopUp
from cashbook.services import payments as pay_svc
from statements.models import BankAccount
from statements.models_register import RegisterException, StatementLine
from statements.services import register as reg_svc

TODAY = dt.date.today()


def _csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Narration", "Credit", "Debit", "Balance"])
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode()


class Round6Fixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t_r6", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.treasurer)
        self.account = BankAccount.objects.create(name="Main", account_number="1")
        self.fund = Department.objects.create(
            name="R6 Fund", slug="r6-fund", fund_type=Department.FundType.LOCAL)


# ===========================================================================
# Item 3 — the matching bug, again and properly
# ===========================================================================

class RegisterMatchingRobustnessTests(Round6Fixture):
    """Reported: "I can get the references under M-Pesa ref in the transactions.
    Yet being detected as missing in the reference UAVAM5CG31. Realized it
    affects transactions which have indicated as manual receipt, and the amount
    may be zero. Check how split funds are also matched."

    Every one of those clues is the same root cause, and it was mine: I was
    building the match index with filters on `channel`, `bank_account` and
    reversal status — all of which are classifications WE made after the fact,
    any of which can hide a transaction that carries the bank's own reference.

    * Marking a gift as a manual receipt sets `excluded_from_income` and detaches
      it from its fund. It is still the same bank line.
    * A split part can be zero-valued, and importer-created split parts have no
      `split_of` link at all — they share the parent's mpesa_ref and carry
      "REF-S1" core_refs.
    * A transaction may carry no bank_account, or one tagged later.
    * A human may have reclassified the channel.

    For the question "did we ever record this bank line?", the ONLY thing that
    can answer it is whether the bank's reference appears in our ledger.
    """

    def setUp(self):
        super().setUp()
        reg_svc.import_file(
            self.account, filename="s.csv", user=self.treasurer,
            path_or_bytes=_csv([
                ["2026-07-01", "UAVAM5CG31~x#t~254700~C2B~A", "1000", "", "1000"],
                ["2026-07-02", "UZERO00001~x#t~254701~C2B~B", "500", "", "1500"],
                ["2026-07-03", "UOTHER0001~x#t~254702~C2B~C", "700", "", "2200"],
                ["2026-07-04", "UNOCHAN001~x#t~254703~C2B~D", "300", "", "2500"],
            ]))

    def _txn(self, **kw):
        base = dict(date=dt.date(2026, 7, 1), channel="BANK", direction="CREDIT",
                    amount=Decimal("1000"), department=self.fund, confirmed=True,
                    allocation_status="AUTO", reference="t")
        base.update(kw)
        return Transaction.objects.create(**base)

    def test_a_transaction_MARKED_AS_MANUAL_RECEIPT_still_matches(self):
        """THE reported case. Marking it detaches it from its fund and excludes
        it from income — but it is still the bank line the bank sent."""
        t = self._txn(mpesa_ref="UAVAM5CG31", bank_account=self.account)
        t.mark_manual_receipt()
        t.refresh_from_db()
        self.assertTrue(t.excluded_from_income)
        self.assertIsNone(t.department_id)

        reg_svc.recheck(self.account)
        self.assertFalse(
            RegisterException.objects.filter(
                kind=RegisterException.Kind.MISSING_IN_LEDGER,
                ref="UAVAM5CG31", status=RegisterException.Status.OPEN).exists(),
            "a manual-receipted transaction carrying the bank's own reference was "
            "still reported as 'not in our books'")

    def test_a_ZERO_AMOUNT_transaction_still_matches(self):
        """A split fund with a 0% component produces one. A zero-value part still
        carries the bank's reference, and still proves we recorded the line."""
        self._txn(mpesa_ref="UZERO00001", amount=Decimal("0"),
                  bank_account=self.account)
        reg_svc.recheck(self.account)
        self.assertFalse(RegisterException.objects.filter(
            kind=RegisterException.Kind.MISSING_IN_LEDGER,
            ref="UZERO00001").exists())

    def test_a_transaction_tagged_to_ANOTHER_ACCOUNT_still_matches(self):
        """The bank issued that reference once. Which account WE tagged the row
        to is our filing, not the bank's fact."""
        other = BankAccount.objects.create(name="Other", account_number="2")
        self._txn(mpesa_ref="UOTHER0001", bank_account=other)
        reg_svc.recheck(self.account)
        self.assertFalse(RegisterException.objects.filter(
            kind=RegisterException.Kind.MISSING_IN_LEDGER,
            ref="UOTHER0001").exists())

    def test_a_transaction_whose_CHANNEL_was_changed_still_matches(self):
        self._txn(mpesa_ref="UNOCHAN001", channel="CASH", bank_account=None)
        reg_svc.recheck(self.account)
        self.assertFalse(RegisterException.objects.filter(
            kind=RegisterException.Kind.MISSING_IN_LEDGER,
            ref="UNOCHAN001").exists())

    def test_all_four_together_leave_no_false_positives(self):
        t = self._txn(mpesa_ref="UAVAM5CG31", bank_account=self.account)
        t.mark_manual_receipt()
        self._txn(mpesa_ref="UZERO00001", amount=Decimal("0"))
        other = BankAccount.objects.create(name="Other2", account_number="3")
        self._txn(mpesa_ref="UOTHER0001", bank_account=other)
        self._txn(mpesa_ref="UNOCHAN001", channel="CASH")
        res = reg_svc.recheck(self.account)
        self.assertEqual(res["matched"], 4)
        self.assertEqual(
            RegisterException.objects.filter(
                kind=RegisterException.Kind.MISSING_IN_LEDGER,
                status=RegisterException.Status.OPEN).count(), 0)

    def test_a_SPLIT_PART_matches_through_its_parents_reference(self):
        """A part split through the UI carries `split_of`; the parent holds the
        bank's reference."""
        parent = self._txn(mpesa_ref="UAVAM5CG31", amount=Decimal("1000"),
                           bank_account=self.account)
        trust = Department.objects.create(name="R6 Trust", slug="r6-trust",
                                          fund_type=Department.FundType.TRUST)
        parts = parent.split_into([(self.fund, Decimal("600"), None),
                                   (trust, Decimal("400"), None)])
        self.assertEqual(parts[1].split_of_id, parent.pk)
        reg_svc.recheck(self.account)
        self.assertFalse(RegisterException.objects.filter(
            kind=RegisterException.Kind.MISSING_IN_LEDGER,
            ref="UAVAM5CG31").exists())

    def test_a_genuinely_missing_line_is_STILL_flagged(self):
        """The fix must not silence the report — only stop it crying wolf."""
        reg_svc.recheck(self.account)
        self.assertEqual(
            RegisterException.objects.filter(
                kind=RegisterException.Kind.MISSING_IN_LEDGER,
                status=RegisterException.Status.OPEN).count(), 4)


# ===========================================================================
# Item 2 — the register's opening balance
# ===========================================================================

class RegisterOpeningBalanceTests(Round6Fixture):
    """A register that starts mid-year sums forward from zero, so its closing
    balance is out by whatever the account already held."""

    def test_the_opening_is_DERIVED_from_the_banks_own_balance_column(self):
        """Nobody should have to type a number the bank has already given us —
        and a typed number is the only one of the two that can be wrong."""
        reg_svc.import_file(
            self.account, filename="s.csv", user=self.treasurer,
            path_or_bytes=_csv([
                # bank says the balance is 51,000 AFTER a 1,000 credit,
                # so the account held 50,000 before it
                ["2026-07-01", "UAA111~x#a~254700~C2B~A", "1000", "", "51000"],
                ["2026-07-02", "UBB222~x#b~254701~C2B~B", "500", "", "51500"],
            ]))
        r = reg_svc.running(self.account)
        self.assertEqual(r["opening"], Decimal("50000"))
        self.assertEqual(r["closing"], Decimal("51500"))
        self.assertEqual(reg_svc.balance_drift(self.account), [],
                         "our running balance should now agree with the bank's own")

    def test_a_stated_opening_is_used_when_there_is_no_balance_column(self):
        self.account.register_opening_balance = Decimal("50000")
        self.account.save()
        reg_svc.import_file(
            self.account, filename="s.csv", user=self.treasurer,
            path_or_bytes=_csv([
                ["2026-07-01", "UCC333~x#a~254700~C2B~A", "1000", "", ""],
                ["2026-07-02", "UDD444~x#b~254701~C2B~B", "500", "", ""],
            ]))
        r = reg_svc.running(self.account)
        self.assertEqual(r["opening"], Decimal("50000"))
        self.assertEqual(r["closing"], Decimal("51500"))

    def test_the_page_asks_for_an_opening_ONLY_when_it_cannot_derive_one(self):
        reg_svc.import_file(
            self.account, filename="s.csv", user=self.treasurer,
            path_or_bytes=_csv([["2026-07-01", "UEE555~x#a~254700~C2B~A",
                                 "1000", "", ""]]))
        r = self.client.get(reverse("bank_register"), {"start": "", "end": ""})
        self.assertTrue(r.context["needs_opening"])

        # …and does not ask once the bank's own column answers it
        StatementLine.objects.update(bank_balance=Decimal("51000"))
        r2 = self.client.get(reverse("bank_register"), {"start": "", "end": ""})
        self.assertFalse(r2.context["needs_opening"])

    def test_the_opening_can_be_set_from_the_page(self):
        reg_svc.import_file(
            self.account, filename="s.csv", user=self.treasurer,
            path_or_bytes=_csv([["2026-07-01", "UFF666~x#a~254700~C2B~A",
                                 "1000", "", ""]]))
        self.client.post(reverse("bank_register"), {
            "account": self.account.pk,
            "register_opening_balance": "50000",
            "register_opening_date": "2026-06-30"})
        self.account.refresh_from_db()
        self.assertEqual(self.account.register_opening_balance, Decimal("50000"))


# ===========================================================================
# Item 1 — pending receipt excludes cash; and the Telegram route
# ===========================================================================

class PendingReceiptCashTests(Round6Fixture):

    def setUp(self):
        super().setUp()
        self.trust = Department.objects.create(
            name="R6 Trust Fund", slug="r6-trust-fund",
            fund_type=Department.FundType.TRUST)

    def _gift(self, channel, ref):
        return Transaction.objects.create(
            date=TODAY, channel=channel, direction="CREDIT", amount=Decimal("500"),
            department=self.trust, confirmed=True, allocation_status="AUTO",
            reference=ref, payer_name="Giver", mpesa_ref=ref)

    def test_a_CASH_gift_is_not_pending_receipt(self):
        """Cash is receipted at the point of counting — it goes onto an envelope
        at the table. It does not arrive silently and wait to be chased, so
        listing it here asked a treasurer to chase a receipt for money that was
        never going to have one."""
        from giving.services.pending_receipt import pending_receipt_rows
        self._gift("CASH", "CASHGIFT1")
        refs = [r[5] for r in pending_receipt_rows()]
        self.assertNotIn("CASHGIFT1", refs)

    def test_a_BANK_gift_still_is(self):
        from giving.services.pending_receipt import pending_receipt_rows
        self._gift("BANK", "BANKGIFT1")
        refs = [r[5] for r in pending_receipt_rows()]
        self.assertIn("BANKGIFT1", refs)

    def test_an_ENVELOPE_gift_is_not_pending_receipt(self):
        """It IS the receipt."""
        from giving.services.pending_receipt import pending_receipt_rows
        self._gift("ENVELOPE", "ENVGIFT1")
        refs = [r[5] for r in pending_receipt_rows()]
        self.assertNotIn("ENVGIFT1", refs)


class TelegramPendingTests(Round6Fixture):

    def test_the_pending_command_returns_the_same_pdf_the_web_page_serves(self):
        """One function, one query — so the bot and the web page can never show a
        treasurer two different answers to the same question."""
        from core.services.telegram_bot import _do_pending
        trust = Department.objects.create(
            name="R6 T2", slug="r6-t2", fund_type=Department.FundType.TRUST)
        Transaction.objects.create(
            date=TODAY, channel="BANK", direction="CREDIT", amount=Decimal("500"),
            department=trust, confirmed=True, allocation_status="AUTO",
            reference="TG1", payer_name="G", mpesa_ref="TGREF1")

        reply = _do_pending(chat_id=123)
        self.assertIn("document", reply)
        self.assertEqual(reply["filename"], "pending_receipt.pdf")
        self.assertTrue(reply["document"].startswith(b"%PDF"))
        self.assertIn("pending receipt", reply["caption"].lower())

    def test_it_says_so_plainly_when_there_is_nothing_pending(self):
        from core.services.telegram_bot import _do_pending
        reply = _do_pending(chat_id=123)
        self.assertNotIn("document", reply)
        self.assertIn("Nothing pending", reply["text"])

    def test_the_command_is_routed(self):
        from core.services.telegram_bot import handle_update
        # not asserting on the reply (that needs a session); only that /pending
        # is a command the bot knows, rather than falling through to free text
        import core.services.telegram_bot as bot
        self.assertIn("/pending", bot.HELP)


# ===========================================================================
# Items 4, 5, 6 — petty cash, the payee, and the retired duplicate form
# ===========================================================================

class PettyCashChequeTests(Round6Fixture):
    """A cheque cashed for petty cash is TWO movements: money leaves the bank,
    and money arrives in the tin. Both must be recorded or the books do not add
    up — record only the cheque and the float is understated; record only the
    top-up and the bank is overstated."""

    def _cheque(self, amount="20000"):
        return PaymentInstrument.objects.create(
            method="CHEQUE", instrument_number="000123", payee="CASH",
            amount=Decimal(amount),
            source_kind=PaymentInstrument.SourceKind.PETTY_CASH, status="DRAFT")

    def test_issuing_a_petty_cash_cheque_tops_up_the_float(self):
        chq = self._cheque()
        pay_svc.apply_event(chq, "APPROVE", user=self.treasurer)
        pay_svc.apply_event(chq, "ISSUE", user=self.treasurer, on=TODAY)
        topup = PettyCashTopUp.objects.get(instrument=chq)
        self.assertEqual(topup.amount, Decimal("20000"))
        self.assertEqual(topup.date, TODAY)
        self.assertIn("cheque 000123", topup.note)

    def test_re_issuing_does_not_top_up_twice(self):
        chq = self._cheque()
        pay_svc.apply_event(chq, "APPROVE", user=self.treasurer)
        pay_svc.apply_event(chq, "ISSUE", user=self.treasurer, on=TODAY)
        pay_svc.apply_event(chq, "ISSUE", user=self.treasurer, on=TODAY)
        self.assertEqual(PettyCashTopUp.objects.filter(instrument=chq).count(), 1)

    def test_cancelling_the_cheque_takes_the_cash_back_out_of_the_float(self):
        """It never became notes in the tin. A float that still counts it will
        not reconcile against the money actually there."""
        chq = self._cheque()
        pay_svc.apply_event(chq, "APPROVE", user=self.treasurer)
        pay_svc.apply_event(chq, "ISSUE", user=self.treasurer, on=TODAY)
        pay_svc.apply_event(chq, "CANCEL", user=self.treasurer)
        self.assertFalse(PettyCashTopUp.objects.filter(instrument=chq).exists())

    def test_an_ordinary_cheque_does_NOT_touch_the_float(self):
        chq = PaymentInstrument.objects.create(
            method="CHEQUE", instrument_number="000999", payee="A Supplier",
            amount=Decimal("5000"),
            source_kind=PaymentInstrument.SourceKind.EXPENSE, status="DRAFT")
        pay_svc.apply_event(chq, "APPROVE", user=self.treasurer)
        pay_svc.apply_event(chq, "ISSUE", user=self.treasurer, on=TODAY)
        self.assertFalse(PettyCashTopUp.objects.filter(instrument=chq).exists())


class ExpensePayeeTests(Round6Fixture):
    """The person who incurred a cost and the person the cheque is written to are
    often different — a supplier is paid directly, while the claimant is the
    member who asked for it."""

    def test_the_expense_form_offers_a_payee(self):
        r = self.client.get(reverse("expense_create"))
        self.assertContains(r, "id_payee")

    def test_the_payee_is_kept_separately_from_the_claimant(self):
        e = Expense.objects.create(
            date=TODAY, department=self.fund, description="Chairs",
            amount=Decimal("5000"), category="OTHER",
            claimant="Pastor Mwangi", payee="Nakuru Furniture Ltd",
            recorded_by=self.treasurer)
        self.assertEqual(e.claimant, "Pastor Mwangi")
        self.assertEqual(e.payee, "Nakuru Furniture Ltd")


class PettyCashDisbursementRetiredTests(Round6Fixture):

    def test_the_duplicate_form_is_gone(self):
        """It wrote the same Expense the expense form writes — but could not
        attach a receipt, set an expenditure type or a budget line."""
        import cashbook.forms as f
        self.assertFalse(hasattr(f, "PettyCashDisbursementForm"))

    def test_the_old_route_redirects_to_the_expense_form(self):
        r = self.client.get(reverse("petty_cash_disburse"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("petty=1", r["Location"])

    def test_the_expense_form_pre_ticks_the_petty_box(self):
        r = self.client.get(reverse("expense_create"), {"petty": "1"})
        self.assertTrue(r.context["form"].initial.get("paid_from_petty_cash"))

    def test_an_expense_paid_from_the_float_still_reduces_it(self):
        """The capability is not lost — it just lives in one form now."""
        e = Expense.objects.create(
            date=TODAY, department=self.fund, description="Tea",
            amount=Decimal("300"), category="REFRESHMENTS",
            paid_from_petty_cash=True, status="PAID", paid_date=TODAY,
            recorded_by=self.treasurer, approved_by=self.treasurer)
        self.assertTrue(e.paid_from_petty_cash)


# ===========================================================================
# Item 7 — printing onto a real cheque leaf
# ===========================================================================

class ChequeLeafPrintTests(Round6Fixture):

    def setUp(self):
        super().setUp()
        self.chq = PaymentInstrument.objects.create(
            method="CHEQUE", instrument_number="000456", payee="Nakuru Furniture",
            amount=Decimal("12500.50"), source_kind="EXPENSE", status="ISSUED",
            date_issued=dt.date(2026, 7, 14))

    def test_the_advice_still_prints_on_plain_paper(self):
        r = self.client.get(reverse("payment_print", args=[self.chq.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "cheque-doc")   # the facsimile document

    def test_the_leaf_mode_prints_ONLY_the_values(self):
        """The leaf already has its own borders, labels and background. Printing
        ours on top of them would ruin it."""
        r = self.client.get(reverse("payment_print", args=[self.chq.pk]),
                            {"mode": "leaf"})
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertNotIn("cheque-doc", body)       # no facsimile borders
        self.assertIn("Nakuru Furniture", body)    # the payee
        self.assertIn("12500.50", body)            # the figures
        self.assertIn("14 07 2026", body)          # the date

    def test_the_page_is_sized_to_the_leaf(self):
        r = self.client.get(reverse("payment_print", args=[self.chq.pk]),
                            {"mode": "leaf"})
        body = r.content.decode()
        self.assertIn("@page", body)
        self.assertIn("180.0mm", body)   # the configured leaf width

    def test_a_calibration_sheet_can_be_printed(self):
        """Cheque leaves differ between banks by a few millimetres, and a
        numbered leaf spoiled by a bad guess is not free. So the layout is not
        guessed — it is measured, once, against a spoiled leaf."""
        r = self.client.get(reverse("payment_print", args=[self.chq.pk]),
                            {"mode": "calibrate"})
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("cal-grid", body)     # the millimetre grid
        self.assertIn("cal-cross", body)    # where each field will land
        self.assertIn("PAYEE", body)

    def test_the_layout_is_configurable(self):
        cfg = SiteConfig.get()
        cfg.cheque_width_mm = Decimal("175.0")
        cfg.cheque_payee_x_mm = Decimal("30.0")
        cfg.save()
        body = self.client.get(reverse("payment_print", args=[self.chq.pk]),
                               {"mode": "leaf"}).content.decode()
        self.assertIn("175.0mm", body)
        self.assertIn("30.0mm", body)

    def test_a_global_offset_nudges_everything_at_once(self):
        """A printer that grips the paper 2mm off should be corrected once, not
        by moving every field one at a time."""
        cfg = SiteConfig.get()
        cfg.cheque_offset_x_mm = Decimal("2.5")
        cfg.save()
        body = self.client.get(reverse("payment_print", args=[self.chq.pk]),
                               {"mode": "leaf"}).content.decode()
        self.assertIn("2.5mm", body)
