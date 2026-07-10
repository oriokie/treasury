"""Fix: once a contribution has been receipted (envelope or manual), the
Split action must disappear from the Transactions page - splitting the
ledger entry afterward would create a mismatch with what the issued
receipt says, letting someone alter an already-allocated receipt without
realising it. Fixed both the button's visibility AND added a server-side
guard on TransactionSplitView itself (GET and POST), since hiding a button
in the template is not sufficient defense against a direct request to the
URL. Also blocked for a reversed/reversal entry, an existing gap fixed
alongside this."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import Transaction


def _tr():
    u = User.objects.create_user("tr_splitvis", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class SplitButtonVisibilityTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)
        self.d = Department.objects.create(name="SplitVisFund", fund_type="LOCAL",
            category="MINISTRY")

    def test_split_link_shown_for_unreceipted_allocated_entry(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 10), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="splitvisshown")
        b = self.c.get("/transactions/").content.decode()
        self.assertIn(f'/transactions/{t.id}/split/', b)

    def test_split_link_hidden_for_manually_receipted_entry(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 11), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="splitvishidden1", manual_receipt=True)
        b = self.c.get("/transactions/").content.decode()
        self.assertNotIn(f'/transactions/{t.id}/split/', b)

    def test_split_link_hidden_for_envelope_receipted_entry(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 12), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="splitvishidden2", processed_via_envelope=True)
        b = self.c.get("/transactions/").content.decode()
        self.assertNotIn(f'/transactions/{t.id}/split/', b)


class SplitServerSideGuardTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)
        self.d = Department.objects.create(name="SplitGuardFund2", fund_type="LOCAL",
            category="MINISTRY")

    def test_get_blocked_for_manually_receipted_entry(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 13), amount=Decimal("400"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="guardget1", manual_receipt=True)
        r = self.c.get(f"/transactions/{t.id}/split/", follow=True)
        self.assertIn("already been receipted", r.content.decode())

    def test_post_blocked_for_manually_receipted_entry(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 14), amount=Decimal("400"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="guardpost1", manual_receipt=True)
        self.c.post(f"/transactions/{t.id}/split/", {
            "department": [str(self.d.id)], "amount": ["200"]})
        t.refresh_from_db()
        self.assertEqual(t.amount, Decimal("400"))
        self.assertFalse(Transaction.objects.filter(reference="guardpost1").exclude(pk=t.pk).exists())

    def test_post_blocked_for_envelope_receipted_entry(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 15), amount=Decimal("350"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="guardpost2", processed_via_envelope=True)
        self.c.post(f"/transactions/{t.id}/split/", {
            "department": [str(self.d.id)], "amount": ["175"]})
        t.refresh_from_db()
        self.assertEqual(t.amount, Decimal("350"))

    def test_post_blocked_for_reversed_entry(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 16), amount=Decimal("300"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="guardpost3")
        t.reverse(self.tr, reason="test")
        r = self.c.post(f"/transactions/{t.id}/split/", {
            "department": [str(self.d.id)], "amount": ["150"]}, follow=True)
        self.assertIn("reversed", r.content.decode())
        t.refresh_from_db()
        self.assertEqual(t.amount, Decimal("300"))

    def test_unreceipted_entry_can_still_be_split_normally(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 17), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="guardnormal")
        d2 = Department.objects.create(name="SplitGuardFund3", fund_type="LOCAL",
            category="MINISTRY")
        r = self.c.post(f"/transactions/{t.id}/split/", {
            "department": [str(self.d.id), str(d2.id)], "amount": ["300", "200"]},
            follow=True)
        self.assertIn("Split into", r.content.decode())
