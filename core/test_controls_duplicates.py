"""Controls duplicates load on demand and use corrected offering logic (#6)."""
import datetime as dt
from decimal import Decimal

from django.test import TestCase, Client
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.contrib.auth.models import User, Group

from departments.models import Department
from giving.models import Transaction
from members.models import Member
from core.views import _duplicate_offerings


class ControlsDuplicatesTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("ct", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(u)
        self.d = Department.objects.create(name="Tithe", fund_type="TRUST", category="TRUST")

    def _credit(self, channel, amount, name, day, ref="", core=None):
        return Transaction.objects.create(
            date=dt.date(2026, 6, day), channel=channel, direction="CREDIT",
            amount=Decimal(str(amount)), department=self.d, payer_name=name,
            reference=ref, allocation_status="MANUAL", confirmed=True, core_ref=core)

    def test_controls_page_does_not_run_scans(self):
        self._credit("BANK", 1000, "A B", 3, core="X1")
        with CaptureQueriesContext(connection) as ctx:
            r = self.c.get("/controls/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Run check", r.content.decode())
        self.assertLess(len(ctx.captured_queries), 60)

    def test_bank_plus_envelope_same_month_flagged(self):
        self._credit("BANK", 1000, "John Doe", 3, core="BK1")
        self._credit("ENVELOPE", 1000, "John Doe", 10)
        dups = _duplicate_offerings()
        self.assertTrue(any("bank + envelope" in d["by"] for d in dups))

    def test_shared_paybill_reference_not_flagged(self):
        # two distinct bank gifts sharing reference 'tithe' but unique receipts
        self._credit("BANK", 500, "Mary W", 4, ref="tithe", core="BK2")
        self._credit("BANK", 500, "Mary W", 5, ref="tithe", core="BK3")
        dups = _duplicate_offerings()
        self.assertFalse(any((d.get("payer") or "").upper() == "MARY W".upper()
                             and "bank + envelope" in d["by"] for d in dups))

    def test_on_demand_endpoints(self):
        self.assertEqual(self.c.get("/controls/check/expenses/").status_code, 200)
        self.assertEqual(self.c.get("/controls/check/offerings/").status_code, 200)
