"""A mobile-banking sweep can batch several genuinely-distinct payments under
ONE bank Core Ref and ONE Channel REF, distinguished only by the unique 10-char
M-Pesa receipt inside each narration.

Real example from a production statement — three payments of 10, 11 and 9, all
carrying Core Ref S90288428260130 and Channel REF SFI40DCBA1EA1F6DABA9, telling
apart only by receipts UATKR5A7M8 / UATKR5A7N9 / UATKR5AIDQ:

  558357:MBANKING~UATKR5A7M8 SDAKAHAWAW 25471****36~E CHANNELS~ENOV3   (10.00)
  558357:MBANKING~UATKR5A7N9 SDAKAHAWAW 25471****36~E CHANNELS~ENOV3   (11.00)
  558357:MBANKING~UATKR5AIDQ SDAKAHAWAW 25471****36~E CHANNELS~ENOV3   (9.00)

Keying dedup on the shared Core Ref (or Channel REF) collapsed these three real
payments into one and silently dropped two — money the register denied ever
arrived. The unique narration receipt must win.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from giving.models import Transaction
from statements.models import BankAccount, StatementImport
from statements.services.register import _is_mpesa_receipt, dedup_key


THREE_ROWS = [
    ("558357:MBANKING~UATKR5A7M8 SDAKAHAWAW 25471****36~E CHANNELS~ENOV3",
     "UATKR5A7M8", Decimal("10.00")),
    ("558357:MBANKING~UATKR5A7N9 SDAKAHAWAW 25471****36~E CHANNELS~ENOV3",
     "UATKR5A7N9", Decimal("11.00")),
    ("558357:MBANKING~UATKR5AIDQ SDAKAHAWAW 25471****36~E CHANNELS~ENOV3",
     "UATKR5AIDQ", Decimal("9.00")),
]
SHARED_CORE = "S90288428260130"
SHARED_CHANNEL = "SFI40DCBA1EA1F6DABA9"


class DedupKeyTests(TestCase):
    def _row(self, receipt, credit):
        return {"date": dt.date(2026, 1, 30), "credit": credit, "debit": None,
                "core_ref": SHARED_CORE, "mpesa_ref": SHARED_CHANNEL,
                "receipt": receipt, "raw_narration": "x"}

    def test_shared_core_ref_but_unique_receipts_get_distinct_keys(self):
        keys = {dedup_key(self._row(rc, amt)) for _, rc, amt in THREE_ROWS}
        self.assertEqual(len(keys), 3, "the three real payments must not collapse")
        # and each key is the unique receipt, not the shared bank ref
        self.assertIn("UATKR5A7M8", keys)
        self.assertNotIn(SHARED_CORE, keys)
        self.assertNotIn(SHARED_CHANNEL, keys)

    def test_no_narration_receipt_folds_in_amount_and_narration(self):
        """A cheque / bank-charge line with no M-Pesa receipt keys on the core
        ref PLUS amount and narration, so distinct charges under one reference
        stay distinct while a true re-import (same everything) still collapses."""
        row = {"date": dt.date(2026, 1, 30), "credit": None,
               "debit": Decimal("250.00"), "core_ref": "CB00123", "mpesa_ref": "",
               "receipt": "", "raw_narration": "MONTHLY CHARGE"}
        key = dedup_key(row)
        self.assertTrue(key.startswith("CB00123"))
        self.assertTrue(key.endswith("|D"))          # debit direction preserved
        self.assertIn("250", key)                     # amount folded in
        # a second charge under the same ref but a different amount differs
        row2 = dict(row, debit=Decimal("300.00"), raw_narration="OTHER CHARGE")
        self.assertNotEqual(dedup_key(row2), key)

    def test_mpesa_receipt_detector(self):
        self.assertTrue(_is_mpesa_receipt("UATKR5A7M8"))    # 10 char, letters+digits
        self.assertFalse(_is_mpesa_receipt(SHARED_CHANNEL))  # 20 chars
        self.assertFalse(_is_mpesa_receipt("ABCDEFGHIJ"))    # no digit
        self.assertFalse(_is_mpesa_receipt("1234567890"))    # no letter
        self.assertFalse(_is_mpesa_receipt("SHORT"))


class BankChargeBatchTests(TestCase):
    """A journal batching several DISTINCT charges under one reference — stamp
    duty, excise and a cheque-book fee all under Core Ref CB0170485260413 /
    Channel REF CB0170485_13042026, with NO M-Pesa receipt. Keying on the bare
    reference collapsed the three into one; the amount + narration disambiguate."""

    CHARGES = [
        ("STAMP DUTY STAMP DUTY", Decimal("250.00")),
        ("EXCISE EXCISE", Decimal("300.00")),
        ("160738: 1 100 LEAF CHEQUE BK FOR RANGE 401 TO 500", Decimal("1500.00")),
    ]
    CORE = "CB0170485260413"
    CHANNEL = "CB0170485_13042026"

    def _row(self, narr, debit):
        return {"date": dt.date(2026, 4, 13), "credit": None, "debit": debit,
                "core_ref": self.CORE, "mpesa_ref": self.CHANNEL,
                "receipt": "", "raw_narration": narr}

    def test_distinct_charges_under_one_reference_get_distinct_keys(self):
        keys = {dedup_key(self._row(n, d)) for n, d in self.CHARGES}
        self.assertEqual(len(keys), 3, "three charges collapsed to fewer keys")

    def test_same_charge_keys_stably_for_reimport(self):
        row = self._row(self.CHARGES[0][0], self.CHARGES[0][1])
        self.assertEqual(dedup_key(row), dedup_key(row))


class ImporterBankChargeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("t_bc", password="x")
        self.account = BankAccount.get_default()

    def test_all_three_charges_import_and_reimport_is_idempotent(self):
        from statements.services.ingest import ingest_event
        charges = [("STAMP DUTY", Decimal("250")),
                   ("EXCISE", Decimal("300")),
                   ("CHEQUE BOOK", Decimal("1500"))]

        def do_import():
            out = []
            for narr, amt in charges:
                txn, outcome = ingest_event(
                    date=dt.date(2026, 4, 13), amount=amt,
                    direction=Transaction.Direction.DEBIT,
                    reference=narr, phone="", name="", raw_narration=narr,
                    core_ref="CB0170485260413", bank_receipt=None,
                    mpesa_ref="CB0170485_13042026", bank_account=self.account)
                out.append(outcome)
            return out

        first = do_import()
        self.assertEqual(first, ["created", "created", "created"])
        before = Transaction.objects.count()
        second = do_import()
        self.assertEqual(second, ["duplicate", "duplicate", "duplicate"])
        self.assertEqual(Transaction.objects.count(), before)


class LedgerImportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("t_dedup", password="x")
        self.account = BankAccount.get_default()

    def _import_rows(self):
        """Drive the importer's dedup directly by creating transactions the way
        run_import would, exercising the shared-core_ref collision path."""
        from statements.services.ingest import ingest_event

        created = []
        for narr, receipt, amount in THREE_ROWS:
            txn, outcome = ingest_event(
                date=dt.date(2026, 1, 30), amount=amount,
                direction=Transaction.Direction.CREDIT,
                reference="", phone="", name="", raw_narration=narr,
                core_ref=SHARED_CORE, bank_receipt=receipt,
                mpesa_ref=SHARED_CHANNEL, bank_account=self.account)
            created.append((txn, outcome))
        return created

    def test_all_three_payments_are_created(self):
        created = self._import_rows()
        outcomes = [o for _, o in created]
        self.assertEqual(outcomes, ["created", "created", "created"],
                         "two real payments were dropped as false duplicates")
        # each receipt present exactly once, amounts preserved
        for _, receipt, amount in THREE_ROWS:
            tx = Transaction.objects.filter(bank_receipt=receipt).first()
            self.assertIsNotNone(tx, f"{receipt} was lost")
            self.assertEqual(tx.amount, amount)
        # the shared core_ref only stored bare once; the rest suffixed
        core_refs = set(Transaction.objects.exclude(core_ref__isnull=True)
                        .values_list("core_ref", flat=True))
        self.assertIn(SHARED_CORE, core_refs)
        self.assertTrue(any(c.startswith(f"{SHARED_CORE}-S") for c in core_refs))

    def test_reimport_is_still_idempotent(self):
        self._import_rows()
        before = Transaction.objects.count()
        second = self._import_rows()
        self.assertEqual([o for _, o in second],
                         ["duplicate", "duplicate", "duplicate"])
        self.assertEqual(Transaction.objects.count(), before)


class FalsePositiveReceiptTests(TestCase):
    """A paybill narration's typed reference ("expenses12", "Development200")
    can accidentally be 10 characters with both a letter and a digit — shaped
    exactly like a genuine M-Pesa receipt. Two DIFFERENT payments that happen to
    share the same typed reference (the same payer, paying twice for the same
    fund) must not collide: their GENUINE identifiers — different Channel REF,
    different Core Ref, different date, different amount — are what make them
    distinct, and the false "receipt" must not override that.

    Real example: JOSEPH NGWATO paid 200 on 11 Jul (Channel REF UGBGTBHRZS) and
    250 on 13 Jul (Channel REF UGDGTBSPCN), both referencing "expenses12". The
    second payment was silently dropped as a "duplicate" of the first.
    """
    NARR_1 = ("UGBGTBHRZS~441211# expenses12~254795422548~MPESAC2B_400222~"
             "JOSEPH NGWATO  ")
    NARR_2 = ("UGDGTBSPCN~441211# expenses12~254795422548~MPESAC2B_400222~"
             "JOSEPH NGWATO  ")

    def test_parser_does_not_extract_the_reference_as_a_receipt(self):
        from statements.services.parser import parse_narration
        r1 = parse_narration(self.NARR_1)
        r2 = parse_narration(self.NARR_2)
        # the reference text itself must never be returned as the receipt
        self.assertNotEqual(r1["receipt"].upper(), "EXPENSES12")
        self.assertNotEqual(r2["receipt"].upper(), "EXPENSES12")
        # and the two genuinely different payments get different receipts
        self.assertNotEqual(r1["receipt"], r2["receipt"])
        self.assertEqual(r1["receipt"], "UGBGTBHRZS")
        self.assertEqual(r2["receipt"], "UGDGTBSPCN")
        # the reference itself is still extracted correctly
        self.assertEqual(r1["reference"].strip(), "expenses12")

    def test_genuine_receipt_inside_the_reference_free_segment_still_works(self):
        """The masking must be narrow: it must not break the OTHER narration
        shape, where the whole second segment (no '#') legitimately contains
        the real receipt (the SFI40 M-Pesa sweep case)."""
        from statements.services.parser import parse_narration
        r = parse_narration(
            "558357:MBANKING~UATKR5A7M8 SDAKAHAWAW 25471****36~E CHANNELS~ENOV3")
        self.assertEqual(r["receipt"], "UATKR5A7M8")

    def test_tithe_reference_still_extracts_the_real_receipt(self):
        from statements.services.parser import parse_narration
        r = parse_narration(
            "UER2Q5NF2W~441211#tithe~254790301470~MPESAC2B_400222~KEVIN OGEGA")
        self.assertEqual(r["receipt"], "UER2Q5NF2W")
        self.assertEqual(r["reference"], "tithe")

    def test_two_distinct_payments_both_import(self):
        from statements.services.ingest import ingest_event
        from statements.services.parser import parse_narration
        r1 = parse_narration(self.NARR_1)
        r2 = parse_narration(self.NARR_2)
        account = BankAccount.objects.create(name="M", is_default=True, active=True)

        t1, o1 = ingest_event(
            date=dt.date(2026, 7, 11), amount=Decimal("200"),
            direction=Transaction.Direction.CREDIT, reference=r1["reference"],
            phone=r1["phone"], name=r1["name"], raw_narration=self.NARR_1,
            core_ref="CB0749194260711", bank_receipt=r1["receipt"],
            mpesa_ref="UGBGTBHRZS", bank_account=account)
        t2, o2 = ingest_event(
            date=dt.date(2026, 7, 13), amount=Decimal("250"),
            direction=Transaction.Direction.CREDIT, reference=r2["reference"],
            phone=r2["phone"], name=r2["name"], raw_narration=self.NARR_2,
            core_ref="CB1118627260713", bank_receipt=r2["receipt"],
            mpesa_ref="UGDGTBSPCN", bank_account=account)

        self.assertEqual(o1, "created")
        self.assertEqual(o2, "created",
                         "the second, genuinely different payment was dropped "
                         "as a false duplicate of the first")
        self.assertEqual(Transaction.objects.count(), 2)
