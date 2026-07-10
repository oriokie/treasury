"""Consolidated the Transactions page's per-row actions (Edit, Delete, Split,
Receipt, Reverse, Send to review, Audit history) into a single dropdown menu
(triggered by a "⋮" button) instead of a cluttered row of separate links -
which also made room to add two actions that didn't exist as per-row
options before: Reverse (previously bulk-only) and Audit history (new,
surfacing django-simple-history data that was already being tracked but
never shown to users for a single transaction). Status pills (manual
receipt / receipted / reversed / reversal) remain visible outside the menu,
since they're informational, not actions."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import Transaction


def _tr():
    u = User.objects.create_user("tr_dropdown", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class ActionsDropdownTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)
        self.d = Department.objects.create(name="DropdownTestFund", fund_type="LOCAL",
            category="MINISTRY")

    def test_dropdown_present_with_all_expected_actions(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 10), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="dropdowntest1")
        b = self.c.get("/transactions/?q=dropdowntest1").content.decode()
        self.assertIn("tx-actions-dd", b)
        self.assertIn(f'/transactions/{t.id}/edit/', b)
        self.assertIn(f'/transactions/{t.id}/reverse/', b)
        self.assertIn(f'/transactions/{t.id}/history/', b)
        self.assertIn(f'/transactions/{t.id}/send-to-review/', b)

    def test_reverse_not_offered_for_already_reversed_entry(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 11), amount=Decimal("400"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="dropdowntest2")
        t.reverse(self.tr, reason="test")
        b = self.c.get("/transactions/?q=dropdowntest2").content.decode()
        self.assertNotIn(f'/transactions/{t.id}/reverse/', b)

    def test_send_to_review_not_offered_for_review_status_entry(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 12), amount=Decimal("200"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="REVIEW",
            department=None, reference="dropdowntest3")
        b = self.c.get("/transactions/?q=dropdowntest3").content.decode()
        self.assertNotIn(f'/transactions/{t.id}/send-to-review/', b)

    def test_status_pills_still_visible_outside_the_menu(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 13), amount=Decimal("300"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="dropdowntest4", manual_receipt=True)
        b = self.c.get("/transactions/?q=dropdowntest4").content.decode()
        self.assertIn("manual receipt", b)

    def test_per_row_reverse_action_works(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 14), amount=Decimal("600"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="dropdowntest5")
        self.c.post(f"/transactions/{t.id}/reverse/")
        t.refresh_from_db()
        self.assertTrue(t.is_reversed)


class TransactionHistoryViewTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)
        self.d = Department.objects.create(name="HistoryTestFund", fund_type="LOCAL",
            category="MINISTRY")

    def test_history_shows_creation(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 15), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="historytest1")
        r = self.c.get(f"/transactions/{t.id}/history/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Created", r.content.decode())

    def test_history_shows_a_field_change(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 16), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="historytest2")
        t.amount = Decimal("750")
        t.save()
        r = self.c.get(f"/transactions/{t.id}/history/")
        b = r.content.decode()
        self.assertIn("amount", b)
        self.assertIn("500", b)
        self.assertIn("750", b)

    def test_history_shows_who_made_the_change(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 17), amount=Decimal("100"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="historytest3")
        t._history_user = self.tr
        t.amount = Decimal("125")
        t.save()
        r = self.c.get(f"/transactions/{t.id}/history/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(self.tr.username, r.content.decode())

    def test_auditor_can_view_history_read_only(self):
        au = User.objects.create_user("au_history", password="x")
        au.groups.add(Group.objects.get_or_create(name="Auditor")[0])
        t = Transaction.objects.create(date=dt.date(2026, 6, 18), amount=Decimal("150"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="historytest4")
        c2 = Client(); c2.force_login(au)
        r = c2.get(f"/transactions/{t.id}/history/")
        self.assertEqual(r.status_code, 200)


class CrDrAccessibilityBadgeTests(TestCase):
    """Accessibility fix: debit/reversal amounts previously relied on red
    text color alone. Added a CR/DR text badge alongside the color, so the
    distinction doesn't depend on color perception."""
    def setUp(self):
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)
        self.d = Department.objects.create(name="CrDrTestFund", fund_type="LOCAL",
            category="MINISTRY")

    def test_credit_entry_shows_cr_badge(self):
        Transaction.objects.create(date=dt.date(2026, 6, 20), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="crbadgetest")
        b = self.c.get("/transactions/?q=crbadgetest").content.decode()
        self.assertIn(">CR</span>", b)

    def test_reversal_entry_shows_dr_badge(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 21), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="drbadgetest")
        t.reverse(self.tr, reason="test")
        b = self.c.get("/transactions/?q=drbadgetest").content.decode()
        self.assertIn(">DR</span>", b)
