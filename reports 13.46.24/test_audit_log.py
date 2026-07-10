"""Audit log filters, search, pagination and CSV download (#2)."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from cashbook.models import Expense
from departments.models import Department


class AuditLogTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("au", password="x", is_superuser=True)
        self.u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(self.u)
        d = Department.objects.create(name="LCB", fund_type="LOCAL", category="OFFERING")
        for i in range(3):
            Expense.objects.create(date=dt.date(2026, 6, 1), department=d,
                description=f"Item {i}", amount=Decimal("100"), category="OTHER",
                status="PAID", recorded_by=self.u)

    def test_page_has_filters_and_download(self):
        b = self.c.get("/reports/audit/").content.decode()
        for name in ('name="model"', 'name="user"', 'name="type"', 'name="q"',
                     'name="start"', 'name="end"', 'export=csv'):
            self.assertIn(name, b)

    def test_filter_by_model(self):
        r = self.c.get("/reports/audit/?model=Expense")
        self.assertEqual(r.status_code, 200)

    def test_csv_export(self):
        r = self.c.get("/reports/audit/?export=csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("audit_log", r.get("Content-Disposition", ""))

    def test_search_no_match(self):
        r = self.c.get("/reports/audit/?q=zzz_nomatch_zzz")
        self.assertEqual(r.status_code, 200)
