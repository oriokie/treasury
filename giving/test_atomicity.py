"""Database review: two multi-write financial operations (splitting a
contribution across several funds, and settling a trust remittance batch)
had no transaction.atomic() wrapper. A failure partway through — a database
constraint violation, a ledger-posting error, a server restart — would leave
the books in an inconsistent, partially-written state: money silently
vanishing from an incompletely-split contribution, or a remittance batch
marked paid with its settling transaction never actually resolved. Both are
now wrapped so a failure rolls back completely, never partially."""
import datetime as dt
from decimal import Decimal
from django.db import IntegrityError
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import Transaction
from cashbook.models import Expense, RemittanceBatch


def _tr():
    u = User.objects.create_user("tr_atomic", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class SplitIntoAtomicityTests(TestCase):
    def setUp(self):
        self.d1 = Department.objects.create(name="AtomicSplit1", fund_type="LOCAL",
            category="MINISTRY")
        self.d2 = Department.objects.create(name="AtomicSplit2", fund_type="LOCAL",
            category="MINISTRY")

    def test_failure_partway_rolls_back_completely(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 1), amount=Decimal("1000"),
            direction="CREDIT", confirmed=True, channel="BANK",
            allocation_status="MANUAL", department=self.d1, core_ref="ATOMICSPLIT")
        # pre-create a row that collides with the first sibling's generated core_ref
        Transaction.objects.create(date=dt.date(2026, 6, 1), amount=Decimal("1"),
            direction="CREDIT", confirmed=True, channel="BANK",
            allocation_status="MANUAL", department=self.d2, core_ref="ATOMICSPLIT-S1")

        with self.assertRaises(IntegrityError):
            t.split_into([(self.d1, Decimal("600"), None), (self.d2, Decimal("400"), None)])

        t.refresh_from_db()
        self.assertEqual(t.amount, Decimal("1000"))     # not left at 600
        self.assertEqual(t.department_id, self.d1.id)    # not left reassigned

    def test_successful_split_still_works(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 1), amount=Decimal("1000"),
            direction="CREDIT", confirmed=True, channel="BANK",
            allocation_status="MANUAL", department=self.d1)
        out = t.split_into([(self.d1, Decimal("600"), None), (self.d2, Decimal("400"), None)])
        self.assertEqual(len(out), 2)
        self.assertEqual(sum((x.amount for x in out), Decimal(0)), Decimal("1000"))


class RemittanceBatchSettlementAtomicityTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.trust = Department.objects.create(name="AtomicTrustFund", fund_type="TRUST",
            category="OFFERING")
        self.c = Client(); self.c.force_login(self.tr)

    def test_successful_settlement_still_works(self):
        batch = RemittanceBatch.create_batch(created_by=self.tr, status="APPROVED")
        Expense.objects.create(date=dt.date(2026, 6, 28), department=self.trust,
            description="remit", amount=Decimal("30000"), category="REMITTANCE",
            status="PENDING", recorded_by=self.tr, remittance_batch=batch)
        batch.recompute_total(); batch.save(update_fields=["total_amount"])
        txn = Transaction.objects.create(date=dt.date(2026, 7, 1), amount=Decimal("30000"),
            direction="DEBIT", channel="BANK", allocation_status="REVIEW",
            core_ref="ATOMICRB1", confirmed=True)
        r = self.c.post(f"/debits/{txn.id}/resolve/", {"kind": "remittance_batch",
            "batch": str(batch.id)})
        batch.refresh_from_db(); txn.refresh_from_db()
        self.assertEqual(batch.status, "REMITTED")
        self.assertEqual(txn.allocation_status, "MANUAL")
        self.assertTrue(Expense.objects.filter(remittance_batch=batch, status="PAID").exists())

    def test_mismatched_amount_does_not_partially_settle(self):
        batch = RemittanceBatch.create_batch(created_by=self.tr, status="APPROVED")
        exp = Expense.objects.create(date=dt.date(2026, 6, 28), department=self.trust,
            description="remit", amount=Decimal("30000"), category="REMITTANCE",
            status="PENDING", recorded_by=self.tr, remittance_batch=batch)
        batch.recompute_total(); batch.save(update_fields=["total_amount"])
        txn = Transaction.objects.create(date=dt.date(2026, 7, 1), amount=Decimal("999"),
            direction="DEBIT", channel="BANK", allocation_status="REVIEW",
            core_ref="ATOMICRB2", confirmed=True)
        self.c.post(f"/debits/{txn.id}/resolve/", {"kind": "remittance_batch",
            "batch": str(batch.id)})
        batch.refresh_from_db(); txn.refresh_from_db(); exp.refresh_from_db()
        # amount mismatch is rejected before any writes happen at all
        self.assertEqual(batch.status, "APPROVED")
        self.assertEqual(exp.status, "PENDING")
        self.assertEqual(txn.allocation_status, "REVIEW")
