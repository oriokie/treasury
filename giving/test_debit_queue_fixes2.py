"""Trust remittance settling a multi-fund remittance batch (item 3)."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import Transaction
from cashbook.models import Expense, RemittanceBatch


def _tr():
    u = User.objects.create_user("tr_dqf2", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class RemittanceBatchMatchingTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.t1 = Department.objects.create(name="TrustA", fund_type="TRUST", category="OFFERING")
        self.t2 = Department.objects.create(name="TrustB", fund_type="TRUST", category="OFFERING")
        self.c = Client(); self.c.force_login(self.tr)

    def _make_batch(self, status="APPROVED"):
        batch = RemittanceBatch.create_batch(created_by=self.tr, status=status)
        Expense.objects.create(date=dt.date(2026, 6, 28), department=self.t1,
            description="remit t1", amount=Decimal("30000"), category="REMITTANCE",
            status="PENDING", recorded_by=self.tr, remittance_batch=batch)
        Expense.objects.create(date=dt.date(2026, 6, 28), department=self.t2,
            description="remit t2", amount=Decimal("20000"), category="REMITTANCE",
            status="PENDING", recorded_by=self.tr, remittance_batch=batch)
        batch.recompute_total(); batch.save(update_fields=["total_amount"])
        return batch

    def test_batch_settles_all_fund_lines(self):
        batch = self._make_batch()
        deb = Transaction.objects.create(date=dt.date(2026, 7, 1), amount=Decimal("50000"),
            direction="DEBIT", channel="BANK", allocation_status="REVIEW",
            core_ref="RBT001", confirmed=True)
        self.c.post(f"/debits/{deb.id}/resolve/",
            {"kind": "remittance_batch", "batch": str(batch.id)})
        batch.refresh_from_db()
        self.assertEqual(batch.status, "REMITTED")
        for exp in batch.expenses.all():
            self.assertEqual(exp.status, "PAID")
            self.assertEqual(exp.paid_date, dt.date(2026, 7, 1))
        # each fund keeps its own department — not forced onto one
        depts = set(batch.expenses.values_list("department_id", flat=True))
        self.assertEqual(depts, {self.t1.id, self.t2.id})

    def test_amount_mismatch_rejected(self):
        batch = self._make_batch()
        deb = Transaction.objects.create(date=dt.date(2026, 7, 1), amount=Decimal("40000"),
            direction="DEBIT", channel="BANK", allocation_status="REVIEW",
            core_ref="RBT002", confirmed=True)
        self.c.post(f"/debits/{deb.id}/resolve/",
            {"kind": "remittance_batch", "batch": str(batch.id)})
        deb.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(deb.allocation_status, "REVIEW")
        self.assertNotEqual(batch.status, "REMITTED")

    def test_already_remitted_batch_rejected(self):
        batch = self._make_batch(status="REMITTED")
        deb = Transaction.objects.create(date=dt.date(2026, 7, 1), amount=Decimal("50000"),
            direction="DEBIT", channel="BANK", allocation_status="REVIEW",
            core_ref="RBT003", confirmed=True)
        self.c.post(f"/debits/{deb.id}/resolve/",
            {"kind": "remittance_batch", "batch": str(batch.id)})
        deb.refresh_from_db()
        self.assertEqual(deb.allocation_status, "REVIEW")

    def test_no_batch_chosen_shows_error(self):
        deb = Transaction.objects.create(date=dt.date(2026, 7, 1), amount=Decimal("50000"),
            direction="DEBIT", channel="BANK", allocation_status="REVIEW",
            core_ref="RBT004", confirmed=True)
        r = self.c.post(f"/debits/{deb.id}/resolve/", {"kind": "remittance_batch", "batch": ""})
        self.assertIn(r.status_code, (200, 302))
        deb.refresh_from_db()
        self.assertEqual(deb.allocation_status, "REVIEW")

    def test_ui_shows_batch_option(self):
        self._make_batch()
        Transaction.objects.create(date=dt.date(2026, 7, 1), amount=Decimal("50000"),
            direction="DEBIT", channel="BANK", allocation_status="REVIEW",
            core_ref="RBT005", confirmed=True)
        b = self.c.get("/debits/").content.decode()
        self.assertIn("remittance_batch", b)
        self.assertIn("settle a batch", b)
