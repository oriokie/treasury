"""Duplicate-offering detection: split halves not flagged (#13), proximity-based
bank+envelope matching removes false positives (#14)."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase
from giving.models import Transaction
from core.views import _duplicate_offerings


def _mk(ch, amt, day, name, **kw):
    return Transaction.objects.create(date=dt.date(2026, 6, day), channel=ch,
        direction="CREDIT", amount=Decimal(amt), payer_name=name,
        allocation_status="MANUAL", confirmed=True, **kw)


class DuplicateLogicTests(TestCase):
    def _flagged(self, name):
        return [x for x in _duplicate_offerings() if name in (x["payer"] or "")]

    def test_true_bank_envelope_duplicate_flagged(self):
        _mk("BANK", "1000", 1, "ALICE", core_ref="BK1")
        _mk("ENVELOPE", "1000", 2, "ALICE")
        self.assertEqual(len(self._flagged("ALICE")), 1)

    def test_same_amount_far_apart_not_flagged(self):
        # two genuinely separate gifts of the same amount, 19 days apart (#14)
        _mk("BANK", "500", 1, "BOB", core_ref="BK2")
        _mk("ENVELOPE", "500", 20, "BOB")
        self.assertEqual(len(self._flagged("BOB")), 0)

    def test_split_half_not_flagged(self):
        # bank 1000 split into 500+500; one half receipted as a 500 envelope (#13)
        _mk("BANK", "500", 5, "CAROL", core_ref="BK3")
        _mk("BANK", "500", 5, "CAROL", core_ref="BK3-S1")
        _mk("ENVELOPE", "500", 5, "CAROL")
        self.assertEqual(len(self._flagged("CAROL")), 0)

    def test_split_fully_double_counted_flagged(self):
        # whole split (1000) ALSO receipted as a 1000 envelope -> genuine double
        _mk("BANK", "500", 7, "DAVE", core_ref="BK4")
        _mk("BANK", "500", 7, "DAVE", core_ref="BK4-S1")
        _mk("ENVELOPE", "1000", 7, "DAVE")
        hit = self._flagged("DAVE")
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["amount"], Decimal("1000"))

    def test_shared_paybill_reference_not_flagged(self):
        # many givers, same reference, unique bank receipts, no envelope
        _mk("BANK", "200", 3, "ERIN", core_ref="BK5", reference="tithe")
        _mk("BANK", "200", 4, "FRANK", core_ref="BK6", reference="tithe")
        self.assertEqual(self._flagged("ERIN"), [])
        self.assertEqual(self._flagged("FRANK"), [])
