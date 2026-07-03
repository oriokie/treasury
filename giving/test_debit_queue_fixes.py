"""Debit queue fixes: blank department no longer crashes the remittance flow,
remittance resolution links to a recent open batch, and an 'already accounted
for' option resolves a debit without touching any expense/fund balance."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import Transaction
from cashbook.models import Expense, RemittanceBatch


def _tr():
    u = User.objects.create_user("tr_dqf", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class RemittanceBlankDeptTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)
        self.deb = Transaction.objects.create(date=dt.date(2026, 6, 10),
            amount=Decimal("5000"), direction="DEBIT", channel="BANK",
            allocation_status="REVIEW", core_ref="DQF001",
            raw_narration="Test debit", confirmed=True)

    def test_blank_department_does_not_crash(self):
        r = self.c.post(f"/debits/{self.deb.id}/resolve/",
            {"kind": "remittance", "department": ""})
        self.assertIn(r.status_code, (200, 302))
        self.deb.refresh_from_db()
        self.assertEqual(self.deb.allocation_status, "REVIEW")

    def test_missing_department_field_does_not_crash(self):
        r = self.c.post(f"/debits/{self.deb.id}/resolve/", {"kind": "remittance"})
        self.assertIn(r.status_code, (200, 302))


class RemittanceBatchLinkTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.trust = Department.objects.create(name="TrustDQF", fund_type="TRUST",
            category="OFFERING")
        self.c = Client(); self.c.force_login(self.tr)
        self.deb = Transaction.objects.create(date=dt.date(2026, 6, 10),
            amount=Decimal("5000"), direction="DEBIT", channel="BANK",
            allocation_status="REVIEW", core_ref="DQF002",
            raw_narration="Test debit", confirmed=True)

    def test_links_to_recent_open_batch(self):
        batch = RemittanceBatch.objects.create(status="DRAFT", created_by=self.tr)
        self.c.post(f"/debits/{self.deb.id}/resolve/",
            {"kind": "remittance", "department": self.trust.id})
        exp = Expense.objects.filter(bank_transaction=self.deb).first()
        self.assertIsNotNone(exp)
        self.assertEqual(exp.remittance_batch_id, batch.id)

    def test_no_batch_still_resolves(self):
        self.c.post(f"/debits/{self.deb.id}/resolve/",
            {"kind": "remittance", "department": self.trust.id})
        self.deb.refresh_from_db()
        self.assertEqual(self.deb.allocation_status, "MANUAL")
        exp = Expense.objects.filter(bank_transaction=self.deb).first()
        self.assertIsNone(exp.remittance_batch_id)

    def test_ignores_remitted_batch(self):
        RemittanceBatch.objects.create(status="REMITTED", created_by=self.tr)
        self.c.post(f"/debits/{self.deb.id}/resolve/",
            {"kind": "remittance", "department": self.trust.id})
        exp = Expense.objects.filter(bank_transaction=self.deb).first()
        self.assertIsNone(exp.remittance_batch_id)


class AlreadyAccountedForTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)
        self.deb = Transaction.objects.create(date=dt.date(2026, 6, 11),
            amount=Decimal("1200"), direction="DEBIT", channel="BANK",
            allocation_status="REVIEW", core_ref="DQF003",
            raw_narration="Some payment", confirmed=True)

    def test_resolves_without_expense_or_fund(self):
        before = Expense.objects.count()
        r = self.c.post(f"/debits/{self.deb.id}/resolve/",
            {"kind": "already_accounted", "description": "Entered manually last week"})
        self.assertIn(r.status_code, (200, 302))
        self.deb.refresh_from_db()
        self.assertEqual(self.deb.allocation_status, "MANUAL")
        self.assertEqual(Expense.objects.count(), before)
        self.assertIsNone(self.deb.department_id)

    def test_requires_a_reason(self):
        r = self.c.post(f"/debits/{self.deb.id}/resolve/",
            {"kind": "already_accounted", "description": ""})
        self.deb.refresh_from_db()
        self.assertEqual(self.deb.allocation_status, "REVIEW")

    def test_drops_off_queue(self):
        self.c.post(f"/debits/{self.deb.id}/resolve/",
            {"kind": "already_accounted", "description": "Duplicate of #99"})
        b = self.c.get("/debits/").content.decode()
        self.assertNotIn("DQF003", b)

    def test_ui_option_present(self):
        b = self.c.get("/debits/").content.decode()
        self.assertIn("already_accounted", b)
        self.assertIn("Already accounted for", b)
