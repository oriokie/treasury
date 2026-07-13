"""Bulk actions (Reverse selected / Send to review) now show a dynamic,
informative confirmation before executing - the exact count and total
amount of what's about to happen, not a vague "are you sure?" - protecting
against a mistaken Select All followed by a click. Verified end-to-end with
a real browser (Playwright): selecting entries and clicking either bulk
button shows a dialog like "You are about to reverse: 3 transactions,
Total: KES 4,500.00", and dismissing it correctly blocks the submission.
This test file checks the template ships the expected structure (data
attributes for the JS to compute the total, and the confirmation function
itself) since Django's test client doesn't execute JavaScript."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import Transaction


def _tr():
    u = User.objects.create_user("tr_bulkconfirmui", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class BulkConfirmationUITests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)
        self.d = Department.objects.create(name="BulkConfirmFund", fund_type="LOCAL",
            category="MINISTRY")

    def test_checkbox_carries_amount_and_direction_data_attributes(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 10), amount=Decimal("1234.50"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="bulkconfirmtest")
        # explicit range: this test is about the bulk-confirm checkbox's data
        # attributes, not the list view's bare-visit current-month default
        b = self.c.get("/transactions/?date_from=2026-06-01&date_to=2026-06-30").content.decode()
        self.assertIn(f'value="{t.id}"', b)
        self.assertIn('data-amount="1234.50"', b)
        self.assertIn('data-direction="CREDIT"', b)

    def test_dynamic_confirmation_function_present_not_static_onclick(self):
        b = self.c.get("/transactions/").content.decode()
        self.assertIn("confirmBulk", b)
        self.assertIn("You are about to ", b)
        # the old static onclick confirm() text must be gone from the buttons
        self.assertNotIn('id="txb-reverse" class="btn btn-sm btn-danger" disabled onclick=', b)

    def test_buttons_no_longer_have_inline_onclick_confirm(self):
        b = self.c.get("/transactions/").content.decode()
        import re
        reverse_btn = re.search(r'<button[^>]*id="txb-reverse"[^>]*>', b)
        self.assertIsNotNone(reverse_btn)
        self.assertNotIn("onclick", reverse_btn.group(0))

    def test_bulk_actions_still_function_correctly_end_to_end(self):
        """The safety-dialog change must not have broken the underlying
        bulk actions themselves."""
        t1 = Transaction.objects.create(date=dt.date(2026, 6, 11), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="bulkstillworks1")
        t2 = Transaction.objects.create(date=dt.date(2026, 6, 12), amount=Decimal("300"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="bulkstillworks2")
        r = self.c.post("/transactions/bulk-reverse/", {
            "action": "reverse", "ids": [str(t1.id), str(t2.id)]})
        self.assertEqual(r.status_code, 302)
        t1.refresh_from_db(); t2.refresh_from_db()
        self.assertTrue(t1.is_reversed)
        self.assertTrue(t2.is_reversed)
