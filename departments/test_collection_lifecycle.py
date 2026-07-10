"""Collection accounts + consolidation (#1) and account lifecycle (#2)."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import (Department, DepartmentStatusLog,
                                expense_departments, income_departments)
from giving.models import Transaction
from cashbook.models import FundTransfer
from reports.services.balances import fund_balance


class CollectionAndLifecycleTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("cl", password="x", is_superuser=True)
        self.u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(self.u)
        self.parent = Department.objects.create(name="Camp Meeting", fund_type="LOCAL",
            category="MINISTRY")
        self.g1 = Department.objects.create(name="Camp Group 1", fund_type="LOCAL",
            category="MINISTRY", parent=self.parent, collection_only=True)
        self.g2 = Department.objects.create(name="Camp Group 2", fund_type="LOCAL",
            category="MINISTRY", parent=self.parent, collection_only=True)

    def _give(self, dept, amount):
        Transaction.objects.create(date=dt.date(2026, 6, 1), channel="CASH",
            direction="CREDIT", amount=Decimal(amount), department=dept,
            allocation_status="MANUAL", confirmed=True)

    # --- #1 collection accounts ---
    def test_collection_excluded_from_expenses(self):
        self.assertFalse(Department.objects.get(pk=self.g1.pk).show_in_expenses)
        ids = {d.id for d in expense_departments()}
        self.assertNotIn(self.g1.id, ids)
        self.assertNotIn(self.g2.id, ids)

    def test_collection_can_receive_income(self):
        self._give(self.g1, "4000")
        self.assertEqual(fund_balance(self.g1), Decimal("4000"))
        self.assertIn(self.g1.id, {d.id for d in income_departments()})

    def test_consolidation(self):
        self._give(self.g1, "4000"); self._give(self.g2, "6000")
        self.c.post(f"/departments/{self.parent.id}/consolidate/")
        self.assertEqual(fund_balance(self.g1), Decimal("0"))
        self.assertEqual(fund_balance(self.g2), Decimal("0"))
        self.assertEqual(fund_balance(self.parent), Decimal("10000"))
        self.assertEqual(FundTransfer.objects.filter(destination=self.parent).count(), 2)
        # history preserved
        self.assertEqual(Transaction.objects.filter(department=self.g1).count(), 1)

    # --- #2 lifecycle ---
    def test_close_requires_zero_balance(self):
        self._give(self.g1, "500")
        self.c.post(f"/departments/{self.g1.id}/close/")
        self.g1.refresh_from_db()
        self.assertEqual(self.g1.status, "ACTIVE")          # blocked
        # zero it then close
        FundTransfer.objects.create(date=dt.date(2026, 6, 2), source=self.g1,
            destination=self.parent, amount=Decimal("500"), recorded_by=self.u)
        self.c.post(f"/departments/{self.g1.id}/close/")
        self.g1.refresh_from_db()
        self.assertEqual(self.g1.status, "CLOSED")
        self.assertFalse(self.g1.active)

    def test_status_change_logged(self):
        self.c.post(f"/departments/{self.g1.id}/close/")
        self.assertTrue(DepartmentStatusLog.objects.filter(
            department=self.g1, to_status="CLOSED").exists())

    def test_closed_excluded_from_income(self):
        self.c.post(f"/departments/{self.g1.id}/close/")
        self.g1.refresh_from_db()
        self.assertNotIn(self.g1.id, {d.id for d in income_departments()})

    def test_archive_and_reopen(self):
        self.c.post(f"/departments/{self.g1.id}/close/")
        self.c.post(f"/departments/{self.g1.id}/archive/")
        self.g1.refresh_from_db(); self.assertEqual(self.g1.status, "ARCHIVED")
        self.c.post(f"/departments/{self.g1.id}/reopen/")
        self.g1.refresh_from_db()
        self.assertEqual(self.g1.status, "ACTIVE"); self.assertTrue(self.g1.active)

    def test_historical_page(self):
        self.c.post(f"/departments/{self.g1.id}/close/")
        b = self.c.get("/departments/historical/").content.decode()
        self.assertIn("Camp Group 1", b)
