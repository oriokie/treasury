"""Safety/code-quality fixes: debit period lock (#1), rejection audit + notify
(#2,#4), shared block_if_locked (#6)."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import Transaction
from cashbook.models import Expense
from core.models import Notification


class SafetyFixTests(TestCase):
    def setUp(self):
        self.t = User.objects.create_user("treas", password="x", is_superuser=True)
        self.t.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.sub = User.objects.create_user("clerk", password="x")
        self.sub.groups.add(Group.objects.get_or_create(name="Assistant")[0])
        self.c = Client(); self.c.force_login(self.t)
        self.fund = Department.objects.create(name="LCB", fund_type="LOCAL",
            category="OFFERING", show_in_expenses=True)

    def test_block_if_locked_single_source(self):
        # both modules import the same callable from core.utils
        from giving.views import _block_if_locked as g
        from cashbook.views import _block_if_locked as c
        from core.utils import block_if_locked as canonical
        self.assertIs(g, canonical)
        self.assertIs(c, canonical)

    def test_debit_resolve_blocked_in_locked_period(self):
        from core.models import PeriodLock
        PeriodLock.objects.create(year=2026, month=6, locked_by=self.t)
        t = Transaction.objects.create(date=dt.date(2026, 6, 1), channel="BANK",
            direction="DEBIT", amount=Decimal("1000"), allocation_status="REVIEW",
            confirmed=True, core_ref="D-LOCK")
        before = Expense.objects.count()
        self.c.post(f"/debits/{t.id}/resolve/",
                    {"kind": "bank_charge", "department": str(self.fund.id)})
        # no expense posted into the locked period
        self.assertEqual(Expense.objects.count(), before)
        # and an unlocked-period debit still resolves
        t2 = Transaction.objects.create(date=dt.date(2026, 7, 1), channel="BANK",
            direction="DEBIT", amount=Decimal("1000"), allocation_status="REVIEW",
            confirmed=True, core_ref="D-OK")
        self.c.post(f"/debits/{t2.id}/resolve/",
                    {"kind": "bank_charge", "department": str(self.fund.id)})
        self.assertEqual(Expense.objects.count(), before + 1)

    def test_reject_sets_rejected_by_not_approved_by(self):
        exp = Expense.objects.create(date=dt.date(2026, 6, 1), department=self.fund,
            description="Claim", amount=Decimal("500"), category="OTHER",
            status="PENDING", recorded_by=self.sub)
        self.c.post(f"/expenses/{exp.id}/approve/", {"action": "reject"})
        exp.refresh_from_db()
        self.assertEqual(exp.status, "REJECTED")
        self.assertEqual(exp.rejected_by_id, self.t.id)
        self.assertIsNone(exp.approved_by_id)

    def test_reject_notifies_submitter(self):
        exp = Expense.objects.create(date=dt.date(2026, 6, 1), department=self.fund,
            description="Travel claim", amount=Decimal("500"), category="OTHER",
            status="PENDING", recorded_by=self.sub)
        self.c.post(f"/expenses/{exp.id}/approve/",
                    {"action": "reject", "note": "missing receipt"})
        self.assertTrue(Notification.objects.filter(
            recipient=self.sub, kind="REJECTION").exists())
