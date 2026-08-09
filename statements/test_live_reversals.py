"""A bank entry made in error and undone is a NON-EVENT — on the live feed too.

The statement importer has recognised this for a long time: it pairs the bank's
mistaken entry with the bank's own reversal of it inside one file and posts
neither, because a church's books were otherwise showing a gift it never
received and its income was overstated by the amount of the bank's own mistake
(see `benevolent/test_round7.py::ReversalTests`).

The real-time CBS feed did none of it. `statements/services/ingest.py` opens by
promising the live feed and the file import "can never drift apart", and on
reversals they had never agreed at all: the feed sees one event per request, so
the pair it would have to recognise arrives in two separate calls minutes or
hours apart, and nothing looked back. The mistaken credit was allocated to a
fund and counted as income, and the reversing debit was posted beside it as an
unrelated payment out.

So the pairing runs backwards here: when an event arrives, look for the entry it
undoes among the ones this account has recently taken in, and mark both. What
counts as a pair is still the importer's `_reversal_row_pairs` and nothing else —
opposite direction, equal amount, close in time, and a narration that SAYS so.
"""
import json
from decimal import Decimal

from django.db.models import Sum
from django.test import Client, TestCase

from core.models import SiteConfig
from departments.models import Department
from giving.models import AllocationRule, Transaction
from members.models import Member
from statements.models import BankAccount


class LiveFeedReversalTests(TestCase):

    def setUp(self):
        cfg = SiteConfig.get()
        cfg.bank_feed_enabled = True
        cfg.bank_feed_auth_mode = SiteConfig.BankFeedAuth.TOKEN
        cfg.bank_feed_token = "test-secret"
        cfg.require_import_confirmation = False
        cfg.save()
        self.tithe = Department.objects.create(name="Tithe", fund_type="TRUST")
        AllocationRule.objects.create(reference="tithe", department=self.tithe,
                                      source="SEED", match_type="EXACT")
        self.account = BankAccount.objects.create(
            name="Main", account_number="01134248358600", is_default=True)
        self.client = Client()

    def _post(self, **over):
        payload = {
            "AcctNo": "01134248358600", "Currency": "KES",
            "BookedBalance": "1000", "ClearedBalance": "1000",
            "PostingDate": "2026-08-01", "ValueDate": "2026-08-01",
            "TransactionDate": "2026-08-01",
        }
        payload.update(over)
        response = self.client.post(
            "/api/bank/cbs-events/", data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer test-secret")
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def _real_gift(self):
        self._post(TransactionId="CB_GIFT_1", EventType="CREDIT", Amount="1000",
                   PaymentRef="01082026_1",
                   Narration="UGIFT11111~441211#tithe~254700~MPESAC2B~REAL GIVER")

    def _mistaken_credit(self):
        self._post(TransactionId="CB_ERR_1", EventType="CREDIT", Amount="5000",
                   PaymentRef="02082026_2",
                   Narration="UERR222222~441211#tithe~254701~MPESAC2B~BANK ERROR")

    def _the_bank_takes_it_back(self):
        self._post(TransactionId="CB_REV_1", EventType="DEBIT", Amount="5000",
                   PaymentRef="03082026_3", TransactionDate="2026-08-03",
                   PostingDate="2026-08-03", ValueDate="2026-08-03",
                   Narration="REVERSAL OF WRONG CREDIT POSTED IN ERROR")

    # -- the bank credits by mistake and takes it back -----------------------

    def test_the_mistaken_credit_stops_being_income_when_the_reversal_arrives(self):
        """THE bug. Two calls, hours apart, and the feed treated them as two
        real movements: a 5,000 gift the church never received, plus a payment
        out. Its income was overstated by the bank's own mistake."""
        self._real_gift()
        self._mistaken_credit()
        self._the_bank_takes_it_back()

        income = (Transaction.objects.confirmed_credits()
                  .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        self.assertEqual(
            income, Decimal("1000"),
            "income should be the ONE real gift — not 6,000, which counts the "
            "5,000 the bank credited by mistake and took straight back")

    def test_both_halves_are_marked_so_every_report_excludes_them(self):
        """`TransactionQuerySet.active` is what every report reads, and it
        excludes a reversed original and its contra. Marking is what puts the
        pair inside that definition instead of beside it."""
        self._mistaken_credit()
        self._the_bank_takes_it_back()
        original = Transaction.objects.get(core_ref="CB_ERR_1")
        reversal = Transaction.objects.get(core_ref="CB_REV_1")
        self.assertTrue(original.is_reversed)
        self.assertIsNotNone(original.reversed_at)
        self.assertTrue(reversal.is_reversed)
        self.assertFalse(Transaction.objects.active()
                         .filter(core_ref__in=["CB_ERR_1", "CB_REV_1"]).exists())

    def test_both_halves_are_still_recorded(self):
        """The money did leave and come back, and the feed log must show the
        bank's own entries were handled — nothing is deleted or refused."""
        self._mistaken_credit()
        self._the_bank_takes_it_back()
        self.assertEqual(
            Transaction.objects.filter(core_ref__in=["CB_ERR_1", "CB_REV_1"]).count(), 2)

    # -- the bank debits in error and refunds it -----------------------------

    def test_a_wrong_debit_refunded_is_paired_the_other_way_round_too(self):
        """The pair is symmetric: whichever half arrives second is the one that
        finds the other."""
        self._post(TransactionId="CB_WD_1", EventType="DEBIT", Amount="2500",
                   PaymentRef="04082026_4",
                   Narration="LEDGER FEE CHARGED IN ERROR")
        self._post(TransactionId="CB_WD_2", EventType="CREDIT", Amount="2500",
                   PaymentRef="05082026_5", TransactionDate="2026-08-04",
                   PostingDate="2026-08-04", ValueDate="2026-08-04",
                   Narration="REVERSAL OF CHARGE DEBITED IN ERROR")
        self.assertTrue(Transaction.objects.get(core_ref="CB_WD_1").is_reversed)
        self.assertTrue(Transaction.objects.get(core_ref="CB_WD_2").is_reversed)

    # -- what the pair must do to the cash balance ---------------------------

    def test_a_reversed_pair_moves_the_cash_balance_by_nothing_either_way_round(self):
        """The whole point: a non-event must net to zero.

        This is the assertion that catches how the halves are FLAGGED, which no
        amount of checking booleans will. `signed_cash_case` signs an
        `is_reversal` row negative whatever direction it carries — correct for a
        mistaken credit clawed back by a debit, and exactly wrong for a mistaken
        DEBIT refunded by a credit, where the refund would sign negative as well
        and the pair would read as minus twice the amount. Marking both halves
        `is_reversed` leaves each one to sign by its own direction, so both
        shapes come to nothing.
        """
        # the bank credits in error, then takes it back
        self._mistaken_credit()
        self._the_bank_takes_it_back()
        self.assertEqual(
            Transaction.objects.filter(core_ref__in=["CB_ERR_1", "CB_REV_1"])
            .signed_cash_total(), Decimal("0"))

        # the bank debits in error, then refunds it
        self._post(TransactionId="CB_WD_1", EventType="DEBIT", Amount="2500",
                   PaymentRef="04082026_4",
                   Narration="LEDGER FEE CHARGED IN ERROR")
        self._post(TransactionId="CB_WD_2", EventType="CREDIT", Amount="2500",
                   PaymentRef="05082026_5", TransactionDate="2026-08-04",
                   PostingDate="2026-08-04", ValueDate="2026-08-04",
                   Narration="REVERSAL OF CHARGE DEBITED IN ERROR")
        self.assertEqual(
            Transaction.objects.filter(core_ref__in=["CB_WD_1", "CB_WD_2"])
            .signed_cash_total(), Decimal("0"))

    def test_the_refund_credit_is_not_allocated_to_a_fund_or_given_a_member(self):
        """A bank correcting itself is not a donor. Allocating the refund to a
        fund would put the church's own money back in as income under some
        fund's name, and matching it would create a member out of the bank's
        narration."""
        before = Member.objects.count()
        self._post(TransactionId="CB_WD_3", EventType="DEBIT", Amount="2500",
                   PaymentRef="06082026_6",
                   Narration="LEDGER FEE CHARGED IN ERROR")
        self._post(TransactionId="CB_WD_4", EventType="CREDIT", Amount="2500",
                   PaymentRef="07082026_7", TransactionDate="2026-08-04",
                   PostingDate="2026-08-04", ValueDate="2026-08-04",
                   Narration="REVERSAL~441211#tithe~254702~MPESAC2B~WRONG DEBIT")
        refund = Transaction.objects.get(core_ref="CB_WD_4")
        self.assertTrue(refund.is_reversed)
        self.assertIsNone(refund.department_id)
        self.assertIsNone(refund.member_id)
        self.assertEqual(Member.objects.count(), before)

    # -- the safety rail -----------------------------------------------------

    def test_a_gift_and_an_unrelated_payment_that_cancel_out_are_NOT_paired(self):
        """The same rail the importer has: a church that receives 5,000 on
        Monday and pays a 5,000 supplier on Tuesday has two perfectly real
        movements. Erasing both because they cancel out is far worse than
        leaving a genuine reversal unrecognised, so a keyword is REQUIRED —
        and a false positive here suppresses real income."""
        self._post(TransactionId="CB_OK_1", EventType="CREDIT", Amount="5000",
                   PaymentRef="08082026_8",
                   Narration="UGIFT99999~441211#tithe~254703~MPESAC2B~A GIVER")
        self._post(TransactionId="CB_OK_2", EventType="DEBIT", Amount="5000",
                   PaymentRef="09082026_9", TransactionDate="2026-08-02",
                   PostingDate="2026-08-02", ValueDate="2026-08-02",
                   Narration="CHQ 000777 A SUPPLIER")
        self.assertFalse(Transaction.objects.get(core_ref="CB_OK_1").is_reversed)
        self.assertFalse(Transaction.objects.get(core_ref="CB_OK_2").is_reversed)
        income = (Transaction.objects.confirmed_credits()
                  .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        self.assertEqual(income, Decimal("5000"))

    def test_a_reversal_does_not_reach_back_past_the_pairing_window(self):
        """Close in time is part of the rule, and the rule is the importer's:
        a charge from two months ago is not what today's reversal is undoing."""
        self._post(TransactionId="CB_OLD_1", EventType="CREDIT", Amount="750",
                   PaymentRef="01062026_1", TransactionDate="2026-06-01",
                   PostingDate="2026-06-01", ValueDate="2026-06-01",
                   Narration="UOLD1111111~441211#tithe~254704~MPESAC2B~A GIVER")
        self._post(TransactionId="CB_OLD_2", EventType="DEBIT", Amount="750",
                   PaymentRef="01082026_2",
                   Narration="REVERSAL OF WRONG CREDIT POSTED IN ERROR")
        self.assertFalse(Transaction.objects.get(core_ref="CB_OLD_1").is_reversed)
        self.assertFalse(Transaction.objects.get(core_ref="CB_OLD_2").is_reversed)

    def test_a_reversal_only_pairs_with_a_matching_amount(self):
        """Equal amounts are what makes two entries the same entry undone."""
        self._post(TransactionId="CB_AMT_1", EventType="CREDIT", Amount="5000",
                   PaymentRef="10082026_1",
                   Narration="UAMT1111111~441211#tithe~254705~MPESAC2B~A GIVER")
        self._post(TransactionId="CB_AMT_2", EventType="DEBIT", Amount="4000",
                   PaymentRef="10082026_2", TransactionDate="2026-08-02",
                   PostingDate="2026-08-02", ValueDate="2026-08-02",
                   Narration="REVERSAL OF WRONG CREDIT POSTED IN ERROR")
        self.assertFalse(Transaction.objects.get(core_ref="CB_AMT_1").is_reversed)
        self.assertFalse(Transaction.objects.get(core_ref="CB_AMT_2").is_reversed)

    def test_one_reversal_undoes_one_entry_and_not_every_entry_like_it(self):
        """Two identical mistaken credits and a single reversal: the reversal
        cannot cancel both, or the second real credit disappears with it."""
        self._post(TransactionId="CB_TWO_1", EventType="CREDIT", Amount="3000",
                   PaymentRef="11082026_1",
                   Narration="UTWO1111111~441211#tithe~254706~MPESAC2B~GIVER ONE")
        self._post(TransactionId="CB_TWO_2", EventType="CREDIT", Amount="3000",
                   PaymentRef="11082026_2",
                   Narration="UTWO2222222~441211#tithe~254707~MPESAC2B~GIVER TWO")
        self._post(TransactionId="CB_TWO_3", EventType="DEBIT", Amount="3000",
                   PaymentRef="11082026_3", TransactionDate="2026-08-02",
                   PostingDate="2026-08-02", ValueDate="2026-08-02",
                   Narration="REVERSAL OF WRONG CREDIT POSTED IN ERROR")
        reversed_count = Transaction.objects.filter(
            core_ref__in=["CB_TWO_1", "CB_TWO_2"], is_reversed=True).count()
        self.assertEqual(reversed_count, 1)
        income = (Transaction.objects.confirmed_credits()
                  .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        self.assertEqual(income, Decimal("3000"), "one real gift survives")
