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

    def test_no_narration_receipt_falls_back_to_core_ref(self):
        """A cheque / bank-charge line with no M-Pesa receipt still keys on the
        core ref, exactly as before — the fix only changes the receipt case."""
        row = {"date": dt.date(2026, 1, 30), "credit": None,
               "debit": Decimal("250.00"), "core_ref": "CB00123", "mpesa_ref": "",
               "receipt": "", "raw_narration": "MONTHLY CHARGE"}
        self.assertEqual(dedup_key(row), "CB00123|D")

    def test_mpesa_receipt_detector(self):
        self.assertTrue(_is_mpesa_receipt("UATKR5A7M8"))    # 10 char, letters+digits
        self.assertFalse(_is_mpesa_receipt(SHARED_CHANNEL))  # 20 chars
        self.assertFalse(_is_mpesa_receipt("ABCDEFGHIJ"))    # no digit
        self.assertFalse(_is_mpesa_receipt("1234567890"))    # no letter
        self.assertFalse(_is_mpesa_receipt("SHORT"))


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
