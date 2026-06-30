"""Reconciliation redesign + petty cash reconciling item (#1,#5), petty cash
export+selector (#3), live feed dashboard tile (#4)."""
import json
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from statements.models import BankReconciliation, ReconciliationItem, BankEvent
from cashbook.models import PettyCashTopUp


class ReconPettyCashTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("rp", password="x", is_superuser=True)
        self.u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(self.u)
        self.rec = BankReconciliation.objects.create(
            statement_date=dt.date(2026, 6, 30), bank_balance=Decimal("100000"),
            created_by=self.u)

    def test_page_has_kpis_and_groups(self):
        b = self.c.get(f"/reconciliations/{self.rec.id}/").content.decode()
        self.assertIn("recon-kpis", b)
        self.assertIn("Adjusted bank balance", b)

    def test_petty_cash_added_as_item(self):
        PettyCashTopUp.objects.create(date=dt.date(2026, 6, 1),
            amount=Decimal("5000"), recorded_by=self.u)
        before = self.rec.adjusted_balance
        self.c.get(f"/reconciliations/{self.rec.id}/")   # auto-syncs managed items
        it = ReconciliationItem.objects.filter(
            reconciliation=self.rec, description__icontains="petty cash").first()
        self.assertIsNotNone(it)
        self.assertEqual(it.effect, "ADD")
        self.assertEqual(it.amount, Decimal("5000"))
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.adjusted_balance - before, Decimal("5000"))

    def test_petty_cash_idempotent(self):
        PettyCashTopUp.objects.create(date=dt.date(2026, 6, 1),
            amount=Decimal("5000"), recorded_by=self.u)
        self.c.get(f"/reconciliations/{self.rec.id}/")
        self.c.get(f"/reconciliations/{self.rec.id}/")
        self.assertEqual(ReconciliationItem.objects.filter(
            reconciliation=self.rec, description__icontains="petty cash").count(), 1)


class PettyCashRegisterTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("pcr", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(u)

    def test_export_and_selector(self):
        self.assertIn("spreadsheet",
            self.c.get("/petty-cash/?export=xlsx")["Content-Type"])
        self.assertIn("text/csv", self.c.get("/petty-cash/?export=csv")["Content-Type"])
        b = self.c.get("/petty-cash/").content.decode()
        self.assertIn('name="start"', b)
        self.assertIn("export=xlsx", b)


class FeedTileTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("ft", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(u)

    def test_tile_appears_with_feed(self):
        BankEvent.objects.create(
            payload=json.dumps({"ClearedBalance": "250000.50"}),
            received_at=dt.datetime(2026, 6, 27, 9, 0), acct_no="1", currency="KES")
        b = self.c.get("/").content.decode()
        self.assertIn("Bank balance (live feed)", b)
        self.assertIn("250,000.50", b)

    def test_no_tile_without_feed(self):
        b = self.c.get("/").content.decode()
        self.assertNotIn("Bank balance (live feed)", b)
